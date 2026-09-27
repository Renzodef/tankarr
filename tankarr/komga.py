from __future__ import annotations

import asyncio
import json
import mimetypes
import posixpath
from collections.abc import Callable, Iterable
from pathlib import Path, PurePosixPath
from time import monotonic
from typing import Any
from urllib.parse import unquote, urlparse

import httpx

from tankarr.config import Settings


class KomgaReconciliationError(RuntimeError):
    """Komga cannot be reconciled without making a destructive guess."""


class KomgaClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        # Every scan initiated by Tankarr shares this lease. Komga's trash
        # endpoint is library-wide, so a concurrent import/scan must never run
        # between the final catalogue proof and a guarded purge.
        self._operation_lock = asyncio.Lock()

    @property
    def configured(self) -> bool:
        if not self.settings.komga_url:
            return False
        if self.settings.komga_effective_auth_method == "api_key":
            return bool(self.settings.komga_api_key)
        return bool(self.settings.komga_username and self.settings.komga_password)

    def _headers(self) -> dict[str, str]:
        if (
            self.settings.komga_effective_auth_method == "api_key"
            and self.settings.komga_api_key
        ):
            return {"X-API-Key": self.settings.komga_api_key}
        return {}

    def _auth(self) -> httpx.BasicAuth | None:
        if self.settings.komga_effective_auth_method != "basic":
            return None
        if self.settings.komga_username and self.settings.komga_password:
            return httpx.BasicAuth(
                self.settings.komga_username, self.settings.komga_password
            )
        return None

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=(
                self.settings.komga_internal_url
                or self.settings.komga_url
                or "http://komga.invalid"
            ),
            headers=self._headers(),
            auth=self._auth(),
            timeout=20,
        )

    async def list_libraries(self) -> list[dict[str, Any]]:
        if not self.configured:
            return []
        async with self._client() as client:
            return await self._list_libraries(client)

    async def probe_catalogue(self) -> dict[str, Any]:
        """Verify the read-only catalogue used by the optional UI shortcut."""

        if not self.configured:
            return {"ok": False, "error": "Komga credentials are missing"}
        async with self._operation_lock, self._client() as client:
            library = await self._resolve_library(client)
            books, series = await asyncio.gather(
                self._list_books(client, str(library["id"]), deleted=False),
                self._list_series(client, str(library["id"]), deleted=False),
            )
        return {
            "ok": True,
            "auth_method": self.settings.komga_effective_auth_method,
            "library_id": str(library["id"]),
            "library_name": str(library.get("name") or "Komga"),
            "book_count": len(books),
            "series_count": len(series),
        }

    async def catalogue_for_paths(
        self, relative_paths: Iterable[str]
    ) -> dict[str, Any]:
        """Resolve Tankarr-relative files to Komga book and series identifiers."""

        if not self.configured:
            return {"configured": False, "books": {}, "series": {}}
        requested = {PurePosixPath(str(path)).as_posix() for path in relative_paths}
        async with self._operation_lock, self._client() as client:
            library = await self._resolve_library(client)
            root = PurePosixPath(str(library["root"]))
            books = await self._list_books(client, str(library["id"]), deleted=False)
            series_items = await self._list_series(
                client, str(library["id"]), deleted=False
            )
        matched: dict[str, dict[str, Any]] = {}
        for book in books:
            raw_url = unquote(urlparse(str(book.get("url") or "")).path)
            try:
                relative = PurePosixPath(raw_url).relative_to(root).as_posix()
            except ValueError:
                continue
            if relative in requested:
                matched[relative] = {
                    "id": str(book.get("id") or ""),
                    "series_id": str(book.get("seriesId") or ""),
                    "url": raw_url,
                }
        return {
            "configured": True,
            "library_id": str(library["id"]),
            "books": matched,
            "series": {
                str(item.get("id") or ""): item
                for item in series_items
                if item.get("id")
            },
        }

    async def apply_catalogue_metadata(
        self,
        updates: Iterable[dict[str, Any]],
    ) -> dict[str, Any]:
        """Apply pre-matched metadata and artwork with per-target receipts."""

        items = list(updates)
        if not self.configured:
            return {
                "configured": False,
                "updated": 0,
                "artwork_uploaded": 0,
                "receipts": [],
            }
        receipts: list[dict[str, Any]] = []
        updated = 0
        artwork_uploaded = 0
        async with self._operation_lock, self._client() as client:
            for item in items:
                kind = str(item["target_kind"])
                target_id = str(item["target_id"])
                receipt = {
                    "target_kind": kind,
                    "target_id": target_id,
                    "payload_sha256": item.get("payload_sha256"),
                    "artwork_sha256": item.get("artwork_sha256"),
                }
                try:
                    payload = item.get("payload")
                    if payload:
                        endpoint = "series" if kind == "series" else "books"
                        response = await client.patch(
                            f"/api/v1/{endpoint}/{target_id}/metadata", json=payload
                        )
                        response.raise_for_status()
                        updated += 1
                    artwork_path = item.get("artwork_path")
                    if artwork_path:
                        endpoint = "series" if kind == "series" else "books"
                        path = self._safe_artwork_path(str(artwork_path))
                        media_type = str(
                            item.get("artwork_media_type")
                            or mimetypes.guess_type(path.name)[0]
                            or "image/jpeg"
                        )
                        response = await client.post(
                            f"/api/v1/{endpoint}/{target_id}/thumbnails",
                            params={"selected": "true"},
                            files={"file": (path.name, path.read_bytes(), media_type)},
                        )
                        response.raise_for_status()
                        artwork_uploaded += 1
                    receipt["ok"] = True
                except Exception as exc:  # noqa: BLE001 - preserve partial receipts
                    receipt["ok"] = False
                    receipt["error"] = f"{type(exc).__name__}: {exc}"[:1000]
                receipts.append(receipt)
        return {
            "configured": True,
            "updated": updated,
            "artwork_uploaded": artwork_uploaded,
            "receipts": receipts,
        }

    def _safe_artwork_path(self, raw_path: str) -> Path:
        path = self.settings.data_dir.joinpath(raw_path).resolve()
        root = self.settings.data_dir.resolve()
        if root not in path.parents or not path.is_file() or path.is_symlink():
            raise KomgaReconciliationError(f"Unsafe metadata artwork path: {raw_path}")
        return path

    async def scan(self, expected_relative_paths: Iterable[str] = ()) -> dict[str, Any]:
        async with self._operation_lock:
            return await self._scan(expected_relative_paths)

    async def _scan(
        self, expected_relative_paths: Iterable[str] = ()
    ) -> dict[str, Any]:
        if not self.configured:
            return {"configured": False, "triggered": False}
        expected = tuple(dict.fromkeys(str(path) for path in expected_relative_paths))
        async with self._client() as client:
            library = await self._resolve_library(client)
            policy_updated = await self._enforce_safe_policy(client, library)
            deadline = monotonic() + self.settings.komga_reconcile_timeout_seconds
            targets = self._target_paths(str(library["root"]), expected)
            if targets:
                active_items = await self._scan_until_expected_present(
                    client, library["id"], targets, deadline
                )
            else:
                response = await client.post(f"/api/v1/libraries/{library['id']}/scan")
                response.raise_for_status()
                active_items = await self._list_books(
                    client, library["id"], deleted=False
                )
            active_paths = self._catalogue_paths(active_items)
        return {
            "configured": True,
            "triggered": True,
            "library_id": library["id"],
            "hash_files": True,
            "empty_trash_after_scan": False,
            "policy_updated": policy_updated,
            "active_books": len(active_items),
            "expected_books": len(targets),
            "matched_expected_books": len(targets & active_paths),
        }

    async def prepare_for_moves(self) -> dict[str, Any]:
        """Ensure Komga can identify a book after Tankarr renames its path."""

        async with self._operation_lock:
            return await self._prepare_for_moves()

    async def ensure_present(
        self, expected_relative_paths: Iterable[str]
    ) -> dict[str, Any]:
        """Audit tracked books and scan only when Komga is missing one."""

        async with self._operation_lock:
            if not self.configured:
                return {
                    "configured": False,
                    "ready": True,
                    "triggered": False,
                    "expected_books": 0,
                }
            expected = tuple(
                dict.fromkeys(str(path) for path in expected_relative_paths)
            )
            async with self._client() as client:
                library = await self._resolve_library(client)
                policy_updated = await self._enforce_safe_policy(client, library)
                deadline = monotonic() + self.settings.komga_reconcile_timeout_seconds
                targets = self._target_paths(str(library["root"]), expected)
                active_items = await self._list_books(
                    client, library["id"], deleted=False
                )
                active_by_path = self._catalogue_by_path(active_items)
                missing = targets - active_by_path.keys()
                unhashed = {
                    path
                    for path in targets & active_by_path.keys()
                    if not active_by_path[path].get("fileHash")
                }
                triggered = False
                if missing or unhashed:
                    active_items = await self._scan_until_expected_present(
                        client, library["id"], targets, deadline
                    )
                    triggered = True
            return {
                "configured": True,
                "ready": True,
                "triggered": triggered,
                "library_id": library["id"],
                "policy_updated": policy_updated,
                "active_books": len(active_items),
                "expected_books": len(targets),
                "matched_expected_books": len(targets),
            }

    async def _scan_until_expected_present(
        self,
        client: httpx.AsyncClient,
        library_id: str,
        targets: set[str],
        deadline: float,
    ) -> list[dict[str, Any]]:
        """Wait until every target is imported and hashed, scanning only when needed.

        A library scan only helps a file Komga has not imported yet. Hashing
        is Komga's own analysis pipeline, and every extra scan re-queues that
        pipeline for books still in progress, so resubmitting scans while
        books are merely unhashed never converges and burns CPU on both
        sides. Scans are therefore resubmitted only for missing files, with
        backoff, and the status poll slows down while Komga works.
        """

        next_scan_at = 0.0
        scan_interval = 15.0
        poll_interval = 2.0
        active_items: list[dict[str, Any]] = []
        while True:
            active_items = await self._list_books(client, library_id, deleted=False)
            active_by_path = self._catalogue_by_path(active_items)
            missing = targets - active_by_path.keys()
            unhashed = {
                path
                for path in targets & active_by_path.keys()
                if not active_by_path[path].get("fileHash")
            }
            if not missing and not unhashed:
                return active_items
            now = monotonic()
            if now >= deadline:
                details = []
                if missing:
                    details.append(f"{len(missing)} missing")
                if unhashed:
                    details.append(f"{len(unhashed)} not hashed")
                raise KomgaReconciliationError(
                    "Komga did not align expected books before the deadline ("
                    + ", ".join(details)
                    + ")"
                )
            if missing and now >= next_scan_at:
                response = await client.post(f"/api/v1/libraries/{library_id}/scan")
                response.raise_for_status()
                # Komga may coalesce this request with a traversal that started
                # before the file existed; a later resubmission guarantees a
                # fresh traversal, but each one costs Komga a full pass.
                next_scan_at = now + scan_interval
                scan_interval = min(scan_interval * 2, 120.0)
            await asyncio.sleep(min(poll_interval, max(deadline - now, 0.0)))
            poll_interval = min(poll_interval * 1.5, 15.0)

    async def _prepare_for_moves(self) -> dict[str, Any]:

        if not self.configured:
            return {"configured": False, "ready": True, "triggered": False}
        async with self._client() as client:
            library = await self._resolve_library(client)
            policy_updated = await self._enforce_safe_policy(client, library)
            books = await self._list_books(client, library["id"], deleted=False)
            missing_hashes = self._books_without_hash(books)
            scan_triggered = False
            if missing_hashes:
                response = await client.post(f"/api/v1/libraries/{library['id']}/scan")
                response.raise_for_status()
                scan_triggered = True
                deadline = monotonic() + self.settings.komga_reconcile_timeout_seconds
                while missing_hashes:
                    if monotonic() >= deadline:
                        raise KomgaReconciliationError(
                            "Komga did not finish hashing every active book before "
                            f"the move deadline ({len(missing_hashes)} still missing)"
                        )
                    await asyncio.sleep(0.5)
                    books = await self._list_books(client, library["id"], deleted=False)
                    missing_hashes = self._books_without_hash(books)

        return {
            "configured": True,
            "ready": True,
            "triggered": scan_triggered,
            "library_id": library["id"],
            "hash_files": True,
            "empty_trash_after_scan": False,
            "policy_updated": policy_updated,
            "active_books": len(books),
            "hashed_books": len(books) - len(missing_hashes),
        }

    async def purge_stale_trash(
        self,
        expected_relative_paths: Iterable[str],
        *,
        path_exists: Callable[[str], bool],
    ) -> dict[str, Any]:
        """Empty Komga's trash when every record in it is explained.

        Tankarr never lets Komga empty its own trash: a scan while the
        library storage is unmounted would otherwise wipe read progress for
        the whole library. Trash that Tankarr itself produced is a different matter.
        A renamed file (naming migration, volume reclassification, title
        change) leaves a soft-deleted record whose content still lives under
        another path, and a file Tankarr removed leaves a record whose path
        no longer exists on disk. Once the caller has proven the library is
        mounted and complete, those records are dead weight and are purged.

        A record is *explained* when its file hash matches an active book,
        or when its path is neither tracked by Tankarr nor present on disk.
        One unexplained record keeps the whole trash intact.
        """

        async with self._operation_lock:
            return await self._purge_stale_trash(
                expected_relative_paths, path_exists=path_exists
            )

    async def _purge_stale_trash(
        self,
        expected_relative_paths: Iterable[str],
        *,
        path_exists: Callable[[str], bool],
    ) -> dict[str, Any]:
        if not self.configured:
            return {"configured": False, "triggered": False, "purged": False}
        expected = {str(path) for path in expected_relative_paths}
        async with self._client() as client:
            library = await self._resolve_library(client)
            root = str(library["root"]).rstrip("/")
            deleted_books = await self._list_books(client, library["id"], deleted=True)
            deleted_series = await self._list_series(
                client, library["id"], deleted=True
            )
            if not deleted_books and not deleted_series:
                return {
                    "configured": True,
                    "triggered": False,
                    "purged": False,
                    "stale_books": 0,
                    "stale_series": 0,
                }
            active_books = await self._list_books(client, library["id"], deleted=False)
            active_series = self._catalogue_paths(
                await self._list_series(client, library["id"], deleted=False)
            )
            active_hashes = {
                str(book.get("fileHash"))
                for book in active_books
                if book.get("fileHash")
            }

            def relative(path: str) -> str | None:
                prefix = root + "/"
                return path[len(prefix) :] if path.startswith(prefix) else None

            unexplained: list[str] = []
            renamed = 0
            removed = 0
            for book in deleted_books:
                path = self._normal_catalogue_path(str(book.get("url") or ""))
                rel = relative(path or "")
                if rel is None:
                    unexplained.append(str(book.get("url") or book.get("id")))
                    continue
                if book.get("fileHash") and str(book["fileHash"]) in active_hashes:
                    renamed += 1
                    continue
                if rel not in expected and not path_exists(rel):
                    removed += 1
                    continue
                unexplained.append(rel)
            for series in deleted_series:
                path = self._normal_catalogue_path(str(series.get("url") or ""))
                rel = relative(path or "")
                if rel is None:
                    unexplained.append(str(series.get("url") or series.get("id")))
                    continue
                if path in active_series or not path_exists(rel):
                    continue
                unexplained.append(rel)
            if unexplained:
                return {
                    "configured": True,
                    "triggered": False,
                    "purged": False,
                    "stale_books": len(deleted_books),
                    "stale_series": len(deleted_series),
                    "unexplained": unexplained[:10],
                    "reason": (
                        f"{len(unexplained)} trashed Komga record(s) still exist on "
                        "disk or are tracked by Tankarr; trash kept intact"
                    ),
                }
            response = await client.post(
                f"/api/v1/libraries/{library['id']}/empty-trash"
            )
            response.raise_for_status()
        return {
            "configured": True,
            "triggered": True,
            "purged": True,
            "library_id": library["id"],
            "stale_books": len(deleted_books),
            "stale_series": len(deleted_series),
            "renamed_books": renamed,
            "removed_books": removed,
        }

    async def reconcile_deleted(
        self,
        relative_paths: Iterable[str],
        *,
        safety_check: Callable[[], None],
    ) -> dict[str, Any]:
        """Purge only unavailable records explained by a Tankarr deletion."""

        async with self._operation_lock:
            return await self._reconcile_deleted(
                relative_paths,
                safety_check=safety_check,
            )

    async def _reconcile_deleted(
        self,
        relative_paths: Iterable[str],
        *,
        safety_check: Callable[[], None],
    ) -> dict[str, Any]:

        paths = tuple(dict.fromkeys(str(path) for path in relative_paths))
        if not paths:
            return {
                "configured": self.configured,
                "triggered": False,
                "purged": False,
                "matched_books": 0,
            }
        if not self.configured:
            return {
                "configured": False,
                "triggered": False,
                "purged": False,
                "reason": "Komga is not configured",
            }

        async with self._client() as client:
            library = await self._resolve_library(client)
            policy_updated = await self._enforce_safe_policy(client, library)
            targets = self._target_paths(str(library["root"]), paths)
            target_series = {posixpath.dirname(path) for path in targets}

            # Capture identity before the asynchronous scan. A target that was
            # active must be observed in Komga's soft-delete catalogue before
            # its durable Tankarr receipt can be retired.
            initial_active_items = await self._list_books(
                client, library["id"], deleted=False
            )
            initial_active = self._catalogue_by_path(initial_active_items)
            initial_target_ids = {
                str(initial_active[path].get("id") or "")
                for path in targets & initial_active.keys()
            }
            initial_deleted_books = self._catalogue_paths(
                await self._list_books(client, library["id"], deleted=True)
            )
            initial_deleted_series = self._catalogue_paths(
                await self._list_series(client, library["id"], deleted=True)
            )
            unexpected_books = initial_deleted_books - targets
            unexpected_series = initial_deleted_series - target_series
            if unexpected_books or unexpected_series:
                raise KomgaReconciliationError(
                    self._unexpected_trash_message(unexpected_books, unexpected_series)
                )

            expected_deleted_series = {
                series_path
                for series_path in target_series
                if (
                    series_books := {
                        path
                        for path in initial_active
                        if posixpath.dirname(path) == series_path
                    }
                )
                and series_books <= targets
            }

            response = await client.post(f"/api/v1/libraries/{library['id']}/scan")
            response.raise_for_status()
            deadline = monotonic() + self.settings.komga_reconcile_timeout_seconds
            await self._wait_for_task_queue_idle(client, deadline)

            active_books = self._catalogue_paths(
                await self._list_books(client, library["id"], deleted=False)
            )
            deleted_book_items = await self._list_books(
                client, library["id"], deleted=True
            )
            deleted_books = self._catalogue_paths(deleted_book_items)
            deleted_book_ids = {
                str(item.get("id") or "") for item in deleted_book_items
            }
            deleted_series = self._catalogue_paths(
                await self._list_series(client, library["id"], deleted=True)
            )
            unexpected_books = deleted_books - targets
            unexpected_series = deleted_series - target_series
            if unexpected_books or unexpected_series:
                raise KomgaReconciliationError(
                    self._unexpected_trash_message(unexpected_books, unexpected_series)
                )
            missing_transitions = initial_target_ids - deleted_book_ids
            missing_series = expected_deleted_series - deleted_series
            if active_books & targets or missing_transitions or missing_series:
                raise KomgaReconciliationError(
                    "Komga completed its task queue without proving every "
                    "authorized active book/series transitioned to trash"
                )

            matched_books = len(deleted_books & targets)
            if not deleted_books and not deleted_series:
                return {
                    "configured": True,
                    "triggered": True,
                    "purged": False,
                    "library_id": library["id"],
                    "matched_books": 0,
                    "policy_updated": policy_updated,
                }

            # Komga's scan is asynchronous. Revalidate the library storage
            # immediately before the irreversible library-wide catalogue purge.
            safety_check()
            library = await self._resolve_library(client)
            deleted_books = self._catalogue_paths(
                await self._list_books(client, library["id"], deleted=True)
            )
            deleted_series = self._catalogue_paths(
                await self._list_series(client, library["id"], deleted=True)
            )
            unexpected_books = deleted_books - targets
            unexpected_series = deleted_series - target_series
            if unexpected_books or unexpected_series:
                raise KomgaReconciliationError(
                    self._unexpected_trash_message(unexpected_books, unexpected_series)
                )

            # A second stable snapshot catches a late external/manual scan.
            await asyncio.sleep(1)
            stable_deleted_books = self._catalogue_paths(
                await self._list_books(client, library["id"], deleted=True)
            )
            stable_deleted_series = self._catalogue_paths(
                await self._list_series(client, library["id"], deleted=True)
            )
            if (
                stable_deleted_books != deleted_books
                or stable_deleted_series != deleted_series
            ):
                raise KomgaReconciliationError(
                    "Komga trash changed during the guarded purge window; retrying "
                    "instead of emptying it"
                )

            response = await client.post(
                f"/api/v1/libraries/{library['id']}/empty-trash"
            )
            response.raise_for_status()
            deadline = monotonic() + self.settings.komga_reconcile_timeout_seconds
            while True:
                remaining_books = self._catalogue_paths(
                    await self._list_books(client, library["id"], deleted=True)
                )
                remaining_series = self._catalogue_paths(
                    await self._list_series(client, library["id"], deleted=True)
                )
                if not remaining_books and not remaining_series:
                    break
                if monotonic() >= deadline:
                    raise KomgaReconciliationError(
                        "Komga accepted empty-trash but unavailable records remain"
                    )
                await asyncio.sleep(0.5)

        return {
            "configured": True,
            "triggered": True,
            "purged": True,
            "library_id": library["id"],
            "matched_books": matched_books,
            "policy_updated": policy_updated,
        }

    async def _list_libraries(self, client: httpx.AsyncClient) -> list[dict[str, Any]]:
        response = await client.get("/api/v1/libraries")
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, dict):
            return list(payload.get("content", []))
        return list(payload)

    async def _resolve_library(self, client: httpx.AsyncClient) -> dict[str, Any]:
        libraries = await self._list_libraries(client)
        library_id = self.settings.komga_library_id
        if library_id:
            library = next(
                (item for item in libraries if item.get("id") == library_id), None
            )
            if library is None:
                raise KomgaReconciliationError(
                    f"Configured Komga library does not exist: {library_id}"
                )
        elif len(libraries) == 1:
            library = libraries[0]
        else:
            raise KomgaReconciliationError(
                "Set TANKARR_KOMGA_LIBRARY_ID when Komga has zero or multiple libraries"
            )
        if library.get("unavailable"):
            raise KomgaReconciliationError(
                f"Komga reports library root unavailable: {library.get('name')}"
            )
        root = str(library.get("root") or "")
        if not root.startswith("/"):
            raise KomgaReconciliationError(
                f"Komga library has an unsafe or missing root: {root!r}"
            )
        return library

    async def _enforce_safe_policy(
        self, client: httpx.AsyncClient, library: dict[str, Any]
    ) -> bool:
        updates: dict[str, bool | str] = {}
        if library.get("hashFiles") is not True:
            updates["hashFiles"] = True
        if library.get("emptyTrashAfterScan") is not False:
            updates["emptyTrashAfterScan"] = False
        # Tankarr is the sole scheduled writer of library scans. Disabling
        # Komga's own schedule removes a destructive race around global trash
        # cleanup; a user-triggered scan is still detected by stable snapshots.
        if library.get("scanInterval") != "DISABLED":
            updates["scanInterval"] = "DISABLED"
        if library.get("scanOnStartup") is not False:
            updates["scanOnStartup"] = False
        if not updates:
            return False
        response = await client.patch(
            f"/api/v1/libraries/{library['id']}", json=updates
        )
        response.raise_for_status()
        library.update(updates)
        return True

    async def _list_books(
        self, client: httpx.AsyncClient, library_id: str, *, deleted: bool
    ) -> list[dict[str, Any]]:
        return await self._search_catalogue(
            client, "books", library_id, deleted=deleted
        )

    async def _list_series(
        self, client: httpx.AsyncClient, library_id: str, *, deleted: bool
    ) -> list[dict[str, Any]]:
        return await self._search_catalogue(
            client, "series", library_id, deleted=deleted
        )

    @staticmethod
    async def _search_catalogue(
        client: httpx.AsyncClient,
        kind: str,
        library_id: str,
        *,
        deleted: bool,
    ) -> list[dict[str, Any]]:
        response = await client.post(
            f"/api/v1/{kind}/list",
            params={"unpaged": "true"},
            json={
                "condition": {
                    "allOf": [
                        {
                            "libraryId": {
                                "operator": "is",
                                "value": library_id,
                            }
                        },
                        {"deleted": {"operator": "isTrue" if deleted else "isFalse"}},
                    ]
                }
            },
        )
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, dict):
            return list(payload.get("content", []))
        return list(payload)

    @staticmethod
    def _books_without_hash(books: list[dict[str, Any]]) -> list[str]:
        return [
            str(book.get("id") or book.get("url"))
            for book in books
            if not book.get("fileHash")
        ]

    @classmethod
    def _catalogue_paths(cls, items: Iterable[dict[str, Any]]) -> set[str]:
        paths: set[str] = set()
        for item in items:
            raw = str(item.get("url") or "")
            path = cls._normal_catalogue_path(raw)
            if not path:
                identifier = str(item.get("id") or "unknown")
                raise KomgaReconciliationError(
                    "Komga returned a catalogue record without a safe absolute "
                    f"path: {identifier}"
                )
            paths.add(path)
        return paths

    @classmethod
    def _catalogue_by_path(
        cls, items: Iterable[dict[str, Any]]
    ) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for item in items:
            path = cls._normal_catalogue_path(str(item.get("url") or ""))
            if not path:
                identifier = str(item.get("id") or "unknown")
                raise KomgaReconciliationError(
                    "Komga returned a catalogue record without a safe absolute "
                    f"path: {identifier}"
                )
            if path in result:
                raise KomgaReconciliationError(
                    f"Komga returned duplicate catalogue paths: {path}"
                )
            result[path] = item
        return result

    async def _wait_for_task_queue_idle(
        self, client: httpx.AsyncClient, deadline: float
    ) -> None:
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise KomgaReconciliationError(
                "Komga task queue did not become idle before the deadline"
            )
        try:
            async with asyncio.timeout(remaining):
                async with client.stream("GET", "/sse/v1/events") as response:
                    response.raise_for_status()
                    event_name = ""
                    async for line in response.aiter_lines():
                        if line.startswith("event:"):
                            event_name = line.partition(":")[2].strip()
                            continue
                        if (
                            not line.startswith("data:")
                            or event_name != "TaskQueueStatus"
                        ):
                            continue
                        try:
                            payload = json.loads(line.partition(":")[2].strip())
                            count = int(payload["count"])
                        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                            continue
                        if count == 0:
                            return
        except TimeoutError as exc:
            raise KomgaReconciliationError(
                "Komga task queue did not become idle before the deadline"
            ) from exc
        raise KomgaReconciliationError(
            "Komga task event stream ended before an idle state was observed"
        )

    @staticmethod
    def _normal_catalogue_path(raw: str) -> str:
        parsed = urlparse(raw)
        path = unquote(parsed.path if parsed.scheme else raw)
        if not path.startswith("/"):
            return ""
        return posixpath.normpath(path)

    @staticmethod
    def _target_paths(root: str, relative_paths: Iterable[str]) -> set[str]:
        normalized_root = posixpath.normpath(root)
        targets: set[str] = set()
        for raw in relative_paths:
            relative = PurePosixPath(raw)
            if relative.is_absolute() or not relative.parts or ".." in relative.parts:
                raise KomgaReconciliationError(
                    f"Unsafe relative Komga reconciliation path: {raw}"
                )
            target = posixpath.normpath(
                posixpath.join(normalized_root, relative.as_posix())
            )
            if posixpath.commonpath((normalized_root, target)) != normalized_root:
                raise KomgaReconciliationError(
                    f"Komga reconciliation path escapes the library root: {raw}"
                )
            targets.add(target)
        return targets

    @staticmethod
    def _unexpected_trash_message(books: set[str], series: set[str]) -> str:
        details = [
            *(f"book {path}" for path in sorted(books)),
            *(f"series {path}" for path in sorted(series)),
        ]
        preview = ", ".join(details[:5])
        if len(details) > 5:
            preview += f", and {len(details) - 5} more"
        return (
            "Refusing to empty Komga trash because it contains unavailable "
            f"records outside the authorized Tankarr deletion: {preview}"
        )
