"""Stump as a managed library reader.

Stump watches the same directory Tankarr writes to, but it only learns about
new or renamed files when a library scan runs. This client gives Tankarr the
same handle it has on Komga — scan after the download queue drains, on a
timer and on demand, and report how many tracked books the reader actually
indexes. Canonical Tankarr artwork is uploaded after catalogue matching —
through Stump's GraphQL API (password login, bearer token).
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from time import monotonic
from typing import Any

import httpx

from tankarr.config import Settings
from tankarr.http import async_client

SCAN_JOB_NAMES = {"library_scan", "LibraryScanJob"}
SERIES_SCAN_JOB_NAMES = {"series_scan", "SeriesScanJob"}
ACTIVE_JOB_STATUSES = {"RUNNING", "QUEUED", "PAUSED"}
MEDIA_PAGE_SIZE = 500
MIN_SCAN_RETRY_SECONDS = 60.0
SCAN_DUTY_CYCLE = 0.10


class StumpReconciliationError(RuntimeError):
    pass


class StumpLibraryClient:
    """Drop-in for :class:`tankarr.komga.KomgaClient` when the reader is Stump."""

    standalone = False
    label = "Stump"
    kind = "stump"
    # Stump indexes the library for reading only; Tankarr's own files and
    # ledger are what readiness is about, so a lagging index never blocks it.
    gates_readiness = False
    supports_catalogue_metadata = False
    supports_catalogue_artwork = True

    def __init__(self, settings: Settings):
        self.settings = settings
        self._operation_lock = asyncio.Lock()
        self._token: str | None = None
        self._token_acquired_at = 0.0
        self.last_probe: dict[str, Any] | None = None

    @property
    def configured(self) -> bool:
        return bool(
            self.settings.reader_url
            and self.settings.reader_username
            and self.settings.reader_password
        )

    # -- transport --------------------------------------------------------

    def _client(self) -> httpx.AsyncClient:
        base = (self.settings.reader_api_url or self.settings.reader_url or "").rstrip(
            "/"
        )
        read_timeout = max(
            30.0,
            min(float(self.settings.komga_reconcile_timeout_seconds), 120.0),
        )
        return async_client(
            base_url=base,
            timeout=httpx.Timeout(read_timeout, connect=10.0),
        )

    async def _login(self, client: httpx.AsyncClient) -> str:
        response = await client.post(
            "/api/v2/auth/login",
            params={"generate_token": "true", "create_session": "false"},
            json={
                "username": self.settings.reader_username,
                "password": self.settings.reader_password,
            },
        )
        response.raise_for_status()
        token = str((response.json() or {}).get("accessToken") or "")
        if not token:
            raise ValueError("Stump did not return an access token")
        self._token = token
        self._token_acquired_at = monotonic()
        return token

    async def _graphql(
        self,
        client: httpx.AsyncClient,
        query: str,
        variables: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        # Tokens are short-lived; refresh proactively and once more on a 401.
        if self._token is None or monotonic() - self._token_acquired_at > 600:
            await self._login(client)
        for attempt in range(2):
            response = await client.post(
                "/api/graphql",
                json={"query": query, "variables": variables or {}},
                headers={"Authorization": f"Bearer {self._token}"},
            )
            if response.status_code == 401 and attempt == 0:
                await self._login(client)
                continue
            response.raise_for_status()
            payload = response.json() or {}
            if payload.get("errors"):
                raise ValueError(
                    str(payload["errors"][0].get("message") or "GraphQL error")
                )
            return payload.get("data") or {}
        raise ValueError("Stump rejected the access token")  # pragma: no cover

    # -- queries ----------------------------------------------------------

    async def _resolve_library(self, client: httpx.AsyncClient) -> dict[str, Any]:
        data = await self._graphql(client, "{ libraries { nodes { id name path } } }")
        nodes = (data.get("libraries") or {}).get("nodes") or []
        wanted = self.settings.reader_library_path.rstrip("/")
        for node in nodes:
            if str(node.get("path") or "").rstrip("/") == wanted:
                return {"id": str(node["id"]), "name": node.get("name"), "root": wanted}
        if len(nodes) == 1:
            node = nodes[0]
            return {
                "id": str(node["id"]),
                "name": node.get("name"),
                "root": str(node.get("path") or wanted).rstrip("/"),
            }
        raise ValueError(
            f"Stump has no library at {wanted}; set Settings → Reader → Library path"
        )

    async def _list_media_paths(
        self, client: httpx.AsyncClient, root: str
    ) -> dict[str, dict[str, Any]]:
        query = (
            "query($page: Int!, $size: Int!) { media(pagination:{offset:{page:$page,"
            " pageSize:$size}}) { nodes { id path seriesId status deletedAt } } }"
        )
        prefix = root.rstrip("/") + "/"
        found: dict[str, dict[str, Any]] = {}
        page = 1
        while True:
            data = await self._graphql(
                client, query, {"page": page, "size": MEDIA_PAGE_SIZE}
            )
            nodes = (data.get("media") or {}).get("nodes") or []
            for node in nodes:
                path = str(node.get("path") or "")
                if path.startswith(prefix) and not node.get("deletedAt"):
                    found[path] = node
            if len(nodes) < MEDIA_PAGE_SIZE:
                return found
            page += 1

    async def catalogue_for_paths(
        self, relative_paths: Iterable[str]
    ) -> dict[str, Any]:
        """Resolve Tankarr-relative files to Stump media and series IDs."""

        if not self.configured:
            return {"configured": False, "books": {}, "series": {}}
        requested = {PurePosixPath(str(path)).as_posix() for path in relative_paths}
        async with self._operation_lock, self._client() as client:
            library = await self._resolve_library(client)
            root = PurePosixPath(str(library["root"]))
            media = await self._list_media_paths(client, str(root))

        books: dict[str, dict[str, Any]] = {}
        series: dict[str, dict[str, Any]] = {}
        for path, item in media.items():
            if str(item.get("status") or "").upper() != "READY":
                continue
            try:
                relative = PurePosixPath(path).relative_to(root).as_posix()
            except ValueError:
                continue
            if relative not in requested:
                continue
            media_id = str(item.get("id") or "")
            series_id = str(item.get("seriesId") or "")
            if not media_id or not series_id:
                continue
            books[relative] = {
                "id": media_id,
                "series_id": series_id,
                "url": path,
            }
            series[series_id] = {"id": series_id}
        return {
            "configured": True,
            "library_id": str(library["id"]),
            "books": books,
            "series": series,
        }

    async def apply_catalogue_metadata(
        self, updates: Iterable[dict[str, Any]]
    ) -> dict[str, Any]:
        """Upload Tankarr-selected artwork to already matched Stump entities."""

        items = list(updates)
        if not self.configured:
            return {
                "configured": False,
                "updated": 0,
                "artwork_uploaded": 0,
                "receipts": [],
            }
        receipts: list[dict[str, Any]] = []
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
                    artwork_path = item.get("artwork_path")
                    if artwork_path:
                        path = self._safe_artwork_path(str(artwork_path))
                        image = base64.b64encode(path.read_bytes()).decode("ascii")
                        if kind == "series":
                            mutation = (
                                "mutation($id: ID!, $image: String!) { "
                                "uploadSeriesThumbnailBase64(id: $id, image: $image) "
                                "{ id } }"
                            )
                        elif kind == "book":
                            mutation = (
                                "mutation($id: ID!, $image: String!) { "
                                "uploadMediaThumbnailBase64(id: $id, image: $image) "
                                "{ id } }"
                            )
                        else:
                            raise ValueError(f"Unsupported Stump target kind: {kind}")
                        await self._graphql(
                            client,
                            mutation,
                            {"id": target_id, "image": image},
                        )
                        artwork_uploaded += 1
                    receipt["ok"] = True
                except Exception as exc:  # noqa: BLE001 - preserve partial receipts
                    receipt["ok"] = False
                    receipt["error"] = f"{type(exc).__name__}: {exc}"[:1000]
                receipts.append(receipt)
        return {
            "configured": True,
            "updated": 0,
            "artwork_uploaded": artwork_uploaded,
            "receipts": receipts,
        }

    def _safe_artwork_path(self, raw_path: str) -> Path:
        path = self.settings.data_dir.joinpath(raw_path).resolve()
        root = self.settings.data_dir.resolve()
        if root not in path.parents or not path.is_file() or path.is_symlink():
            raise StumpReconciliationError(f"Unsafe metadata artwork path: {raw_path}")
        return path

    async def _recent_jobs(self, client: httpx.AsyncClient) -> list[dict[str, Any]]:
        data = await self._graphql(
            client,
            "{ jobs(pagination:{offset:{page:1,pageSize:50}}) { nodes { id name"
            " description status createdAt completedAt msElapsed } } }",
        )
        jobs = list((data.get("jobs") or {}).get("nodes") or [])
        return sorted(
            jobs,
            key=lambda job: str(job.get("createdAt") or ""),
            reverse=True,
        )

    async def _job(self, client: httpx.AsyncClient, job_id: str) -> dict[str, Any]:
        data = await self._graphql(
            client,
            "query($id: ID!) { jobById(id: $id) { id name status createdAt"
            " completedAt msElapsed } }",
            {"id": job_id},
        )
        return data.get("jobById") or {}

    async def _stats(self, client: httpx.AsyncClient) -> dict[str, Any]:
        data = await self._graphql(
            client, "{ librariesStats { bookCount seriesCount } }"
        )
        return data.get("librariesStats") or {}

    @staticmethod
    def _scan_jobs(jobs: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        return [job for job in jobs if str(job.get("name")) in SCAN_JOB_NAMES]

    @staticmethod
    def _series_scan_jobs(
        jobs: Iterable[dict[str, Any]], series_path: str
    ) -> list[dict[str, Any]]:
        return [
            job
            for job in jobs
            if str(job.get("name")) in SERIES_SCAN_JOB_NAMES
            and str(job.get("description") or "").rstrip("/") == series_path.rstrip("/")
        ]

    def _scan_retry_in(
        self,
        job: dict[str, Any] | None,
        *,
        include_periodic_interval: bool = True,
    ) -> float:
        """Keep reader scans below a small share of wall-clock time.

        Stump only exposes whole-library and whole-series scans. A scan that
        took one minute therefore earns roughly nine quiet minutes before an
        automatic retry. This adapts to the reader and library instead of
        assuming anything about the host hardware.
        """

        if not job or str(job.get("status") or "") in ACTIVE_JOB_STATUSES:
            return 0.0
        completed_raw = str(job.get("completedAt") or "")
        if not completed_raw:
            return 0.0
        try:
            completed = datetime.fromisoformat(completed_raw.replace("Z", "+00:00"))
            if completed.tzinfo is None:
                completed = completed.replace(tzinfo=UTC)
        except ValueError:
            return 0.0
        duration = max(float(job.get("msElapsed") or 0) / 1000.0, 1.0)
        intervals = [
            MIN_SCAN_RETRY_SECONDS,
            duration * ((1.0 / SCAN_DUTY_CYCLE) - 1.0),
        ]
        if include_periodic_interval:
            intervals.append(float(self.settings.komga_refresh_interval_minutes * 60))
        target_interval = max(intervals)
        age = max(0.0, (datetime.now(UTC) - completed.astimezone(UTC)).total_seconds())
        return max(0.0, target_interval - age)

    @staticmethod
    def _running_job(jobs: Iterable[dict[str, Any]]) -> dict[str, Any] | None:
        return next(
            (
                job
                for job in jobs
                if str(job.get("status") or "") in ACTIVE_JOB_STATUSES
            ),
            None,
        )

    @staticmethod
    def _target_paths(root: str, expected: Iterable[str]) -> set[str]:
        prefix = root.rstrip("/")
        return {f"{prefix}/{str(path).lstrip('/')}" for path in expected}

    async def _media_at_path(
        self, client: httpx.AsyncClient, path: str
    ) -> dict[str, Any] | None:
        data = await self._graphql(
            client,
            "query($path: String!) { media(filter:{path:{eq:$path}}, pagination:{"
            "offset:{page:1,pageSize:1}}) { nodes { id path status deletedAt } } }",
            {"path": path},
        )
        nodes = (data.get("media") or {}).get("nodes") or []
        return nodes[0] if nodes else None

    async def _series_at_path(
        self, client: httpx.AsyncClient, path: str
    ) -> dict[str, Any] | None:
        data = await self._graphql(
            client,
            "query($path: String!) { series(filter:{path:{eq:$path}}, pagination:{"
            "offset:{page:1,pageSize:1}}) { nodes { id path status } } }",
            {"path": path},
        )
        nodes = (data.get("series") or {}).get("nodes") or []
        return nodes[0] if nodes else None

    # -- scanning ---------------------------------------------------------

    async def _start_scan(self, client: httpx.AsyncClient, library_id: str) -> str:
        jobs = self._scan_jobs(await self._recent_jobs(client))
        running = next(
            (job for job in jobs if str(job.get("status")) in ACTIVE_JOB_STATUSES),
            None,
        )
        if running is not None:
            return str(running["id"])
        data = await self._graphql(
            client,
            "mutation($id: ID!) { scanLibrary(id: $id) }",
            {"id": library_id},
        )
        job_id = data.get("scanLibrary")
        if isinstance(job_id, dict):
            job_id = job_id.get("id")
        if isinstance(job_id, str) and job_id:
            return job_id
        # The mutation only answers `true`; the job list is the source of
        # truth, and the new job can take a moment to appear there.
        known = {str(job.get("id")) for job in jobs}
        for _attempt in range(10):
            jobs = self._scan_jobs(await self._recent_jobs(client))
            fresh = next(
                (
                    job
                    for job in jobs
                    if str(job.get("id")) not in known
                    or str(job.get("status")) in ACTIVE_JOB_STATUSES
                ),
                None,
            )
            if fresh is not None:
                return str(fresh["id"])
            await asyncio.sleep(0.5)
        return ""

    async def _start_series_scan(
        self, client: httpx.AsyncClient, series_id: str
    ) -> None:
        await self._graphql(
            client,
            "mutation($id: ID!) { scanSeries(id: $id) }",
            {"id": series_id},
        )

    async def _wait_for_job(
        self, client: httpx.AsyncClient, job_id: str, deadline: float
    ) -> dict[str, Any]:
        if not job_id:
            return {}
        poll = 2.0
        while True:
            job = await self._job(client, job_id)
            status = str(job.get("status") or "")
            if status and status not in ACTIVE_JOB_STATUSES:
                return job
            now = monotonic()
            if now >= deadline:
                # A full pass over thousands of books outlives any sensible
                # wait; the job keeps running in Stump and the next audit or
                # probe reports its outcome.
                return job
            await asyncio.sleep(min(poll, max(deadline - now, 0.0)))
            poll = min(poll * 1.5, 15.0)

    def _report(
        self,
        library: dict[str, Any],
        targets: set[str],
        active: dict[str, dict[str, Any]],
        *,
        triggered: bool,
        job: dict[str, Any] | None,
    ) -> dict[str, Any]:
        matched = targets & active.keys()
        missing = sorted(targets - active.keys())
        stale = sorted(path for path in active if path not in targets)
        return {
            "configured": True,
            "reader": self.kind,
            "reader_label": self.label,
            "triggered": triggered,
            "library_id": library["id"],
            "active_books": len(active),
            "expected_books": len(targets),
            "matched_expected_books": len(matched),
            "missing_books": len(missing),
            "missing_sample": missing[:5],
            "stale_books": len(stale),
            "stale_sample": stale[:5],
            "scan_job": self._job_summary(job),
        }

    @staticmethod
    def _job_summary(job: dict[str, Any] | None) -> dict[str, Any] | None:
        if not job:
            return None
        return {
            "id": job.get("id"),
            "name": job.get("name"),
            "description": job.get("description"),
            "status": job.get("status"),
            "created_at": job.get("createdAt"),
            "completed_at": job.get("completedAt"),
            "ms_elapsed": job.get("msElapsed"),
        }

    async def _scan(
        self,
        expected_relative_paths: Iterable[str],
        *,
        force: bool,
    ) -> dict[str, Any]:
        async with self._operation_lock:
            if not self.configured:
                return {"configured": False, "triggered": False}
            expected = tuple(dict.fromkeys(str(p) for p in expected_relative_paths))
            async with self._client() as client:
                library = await self._resolve_library(client)
                targets = self._target_paths(library["root"], expected)
                jobs = self._scan_jobs(await self._recent_jobs(client))
                job = self._running_job(jobs)
                triggered = False
                scan_deferred = False
                retry_in_seconds = 0
                if job is None:
                    latest = jobs[0] if jobs else None
                    retry_in = 0.0 if force else self._scan_retry_in(latest)
                    if retry_in > 0:
                        job = latest
                        scan_deferred = True
                        retry_in_seconds = max(1, round(retry_in))
                    else:
                        job_id = await self._start_scan(client, library["id"])
                        job = await self._job(client, job_id)
                        triggered = True

                if job and not scan_deferred:
                    deadline = (
                        monotonic() + self.settings.komga_reconcile_timeout_seconds
                    )
                    job = await self._wait_for_job(
                        client, str(job.get("id") or ""), deadline
                    )
                active = await self._list_media_paths(client, library["root"])
                report = self._report(
                    library, targets, active, triggered=triggered, job=job
                )
                status = str(job.get("status") or "")
                report["scan_running"] = status in ACTIVE_JOB_STATUSES
                report["scan_deferred"] = scan_deferred
                report["retry_in_seconds"] = retry_in_seconds
                if status in {"FAILED", "CANCELLED"}:
                    report["error"] = f"Stump scan ended with status {status}"
                    report["ready"] = False
                return report

    async def scan(self, expected_relative_paths: Iterable[str] = ()) -> dict[str, Any]:
        """Run a coalesced library scan and report tracked-book alignment."""

        return await self._scan(expected_relative_paths, force=False)

    async def force_scan(
        self, expected_relative_paths: Iterable[str] = ()
    ) -> dict[str, Any]:
        """Run a user-requested scan regardless of the automatic quiet period."""

        return await self._scan(expected_relative_paths, force=True)

    async def ensure_present(
        self, expected_relative_paths: Iterable[str]
    ) -> dict[str, Any]:
        """Audit tracked books and scan only when Stump is missing one."""

        async with self._operation_lock:
            if not self.configured:
                return {
                    "configured": False,
                    "ready": True,
                    "triggered": False,
                    "expected_books": 0,
                }
            expected = tuple(dict.fromkeys(str(p) for p in expected_relative_paths))
            async with self._client() as client:
                library = await self._resolve_library(client)
                targets = self._target_paths(library["root"], expected)
                active = await self._list_media_paths(client, library["root"])
                job: dict[str, Any] | None = None
                triggered = False
                scan_deferred = False
                retry_in_seconds = 0
                scan_scope: str | None = None
                series_id: str | None = None
                missing_targets = sorted(targets - active.keys())
                all_jobs = await self._recent_jobs(client)
                library_jobs = self._scan_jobs(all_jobs)
                if missing_targets:
                    job = self._running_job(library_jobs)
                    if job is not None:
                        scan_scope = "library"
                    if job is None:
                        missing_series = sorted(
                            {
                                str(PurePosixPath(path).parent)
                                for path in missing_targets
                            }
                        )
                        missing_series_set = set(missing_series)
                        running_series = self._running_job(
                            job
                            for job in all_jobs
                            if str(job.get("name")) in SERIES_SCAN_JOB_NAMES
                            and str(job.get("description") or "").rstrip("/")
                            in missing_series_set
                        )
                        if running_series is not None:
                            job = running_series
                            scan_scope = "series"

                    deferred_series: list[tuple[float, dict[str, Any], str]] = []
                    unknown_series = False
                    if job is None:
                        for series_path in missing_series:
                            series = await self._series_at_path(client, series_path)
                            if series is None:
                                unknown_series = True
                                continue
                            series_jobs = self._series_scan_jobs(all_jobs, series_path)
                            latest = series_jobs[0] if series_jobs else None
                            retry_in = self._scan_retry_in(
                                latest, include_periodic_interval=False
                            )
                            if retry_in <= 0:
                                series_id = str(series["id"])
                                await self._start_series_scan(client, series_id)
                                triggered = True
                                scan_scope = "series"
                                break
                            if latest is not None:
                                deferred_series.append(
                                    (retry_in, latest, str(series["id"]))
                                )

                    if job is None and not triggered and unknown_series:
                        latest = library_jobs[0] if library_jobs else None
                        retry_in = self._scan_retry_in(
                            latest, include_periodic_interval=False
                        )
                        if retry_in <= 0:
                            job_id = await self._start_scan(client, library["id"])
                            job = await self._job(client, job_id)
                            triggered = True
                            scan_scope = "library"
                        else:
                            job = latest
                            scan_deferred = True
                            retry_in_seconds = max(1, round(retry_in))
                            scan_scope = "library"

                    if (
                        job is None
                        and not triggered
                        and deferred_series
                        and not unknown_series
                    ):
                        retry_in, job, series_id = min(
                            deferred_series, key=lambda item: item[0]
                        )
                        scan_deferred = True
                        retry_in_seconds = max(1, round(retry_in))
                        scan_scope = "series"
                else:
                    job = library_jobs[0] if library_jobs else None
                report = self._report(
                    library, targets, active, triggered=triggered, job=job
                )
                report["scan_running"] = (triggered and scan_scope == "series") or (
                    str((job or {}).get("status") or "") in ACTIVE_JOB_STATUSES
                )
                report["scan_deferred"] = scan_deferred
                report["retry_in_seconds"] = retry_in_seconds
                if scan_scope is not None:
                    report["scan_scope"] = scan_scope
                if series_id is not None:
                    report["series_id"] = series_id
                # Files Stump cannot index are reported, never blocking: the
                # library itself is complete on disk.
                report["ready"] = True
                return report

    async def sync_imported_path(self, relative_path: str) -> dict[str, Any]:
        """Queue the narrowest Stump scan for one newly published book.

        A full library scan per chapter makes the downloader wait for Stump and
        can keep SQLite continuously busy. Existing series use ``scanSeries``;
        new series fall back to one non-blocking, duty-cycle-limited library
        scan. Repeated imports are coalesced while the corresponding job runs
        or remains inside its adaptive quiet period.
        """

        async with self._operation_lock:
            if not self.configured:
                return {"configured": False, "triggered": False, "sync_pending": True}
            relative = PurePosixPath(str(relative_path))
            if relative.is_absolute() or ".." in relative.parts or not relative.parts:
                raise ValueError(f"Unsafe Stump library path: {relative_path}")
            async with self._client() as client:
                library = await self._resolve_library(client)
                target = f"{library['root'].rstrip('/')}/{relative.as_posix()}"
                if await self._media_at_path(client, target) is not None:
                    return {
                        "configured": True,
                        "reader": self.kind,
                        "reader_label": self.label,
                        "triggered": False,
                        "sync_pending": False,
                        "path": target,
                    }

                series_path = str(PurePosixPath(target).parent)
                series = await self._series_at_path(client, series_path)
                jobs = await self._recent_jobs(client)
                library_jobs = self._scan_jobs(jobs)
                running_library = self._running_job(library_jobs)
                if running_library is not None:
                    return {
                        "configured": True,
                        "reader": self.kind,
                        "reader_label": self.label,
                        "triggered": False,
                        "sync_pending": True,
                        "scan_scope": "library",
                        "scan_running": True,
                        "scan_job": self._job_summary(running_library),
                        "path": target,
                    }

                if series is not None:
                    series_jobs = self._series_scan_jobs(jobs, series_path)
                    running = self._running_job(series_jobs)
                    latest = series_jobs[0] if series_jobs else None
                    retry_in = (
                        0.0
                        if running is not None
                        else self._scan_retry_in(
                            latest, include_periodic_interval=False
                        )
                    )
                    if running is None and retry_in <= 0:
                        await self._start_series_scan(client, str(series["id"]))
                        return {
                            "configured": True,
                            "reader": self.kind,
                            "reader_label": self.label,
                            "triggered": True,
                            "sync_pending": True,
                            "scan_scope": "series",
                            "series_id": str(series["id"]),
                            "path": target,
                        }
                    return {
                        "configured": True,
                        "reader": self.kind,
                        "reader_label": self.label,
                        "triggered": False,
                        "sync_pending": True,
                        "scan_scope": "series",
                        "scan_running": running is not None,
                        "scan_deferred": running is None,
                        "retry_in_seconds": max(0, round(retry_in)),
                        "scan_job": self._job_summary(running or latest),
                        "series_id": str(series["id"]),
                        "path": target,
                    }

                running = self._running_job(library_jobs)
                latest = library_jobs[0] if library_jobs else None
                retry_in = 0.0 if running is not None else self._scan_retry_in(latest)
                if running is None and retry_in <= 0:
                    job_id = await self._start_scan(client, library["id"])
                    job = await self._job(client, job_id)
                    return {
                        "configured": True,
                        "reader": self.kind,
                        "reader_label": self.label,
                        "triggered": True,
                        "sync_pending": True,
                        "scan_scope": "library",
                        "scan_running": True,
                        "scan_job": self._job_summary(job),
                        "path": target,
                    }
                return {
                    "configured": True,
                    "reader": self.kind,
                    "reader_label": self.label,
                    "triggered": False,
                    "sync_pending": True,
                    "scan_scope": "library",
                    "scan_running": running is not None,
                    "scan_deferred": running is None,
                    "retry_in_seconds": max(0, round(retry_in)),
                    "scan_job": self._job_summary(running or latest),
                    "path": target,
                }

    async def probe(self) -> dict[str, Any]:
        """Cheap status for System: counts, last scan and whether one runs."""

        if not self.configured:
            return {"configured": False}
        async with self._client() as client:
            library = await self._resolve_library(client)
            stats = await self._stats(client)
            jobs = self._scan_jobs(await self._recent_jobs(client))
        running = next(
            (job for job in jobs if str(job.get("status")) in ACTIVE_JOB_STATUSES),
            None,
        )
        finished = next(
            (job for job in jobs if str(job.get("status")) not in ACTIVE_JOB_STATUSES),
            None,
        )
        result = {
            "configured": True,
            "reader": self.kind,
            "reader_label": self.label,
            "library_id": library["id"],
            "library_name": library.get("name"),
            "book_count": stats.get("bookCount"),
            "series_count": stats.get("seriesCount"),
            "scan_running": running is not None,
            "running_job": self._job_summary(running),
            "last_scan": self._job_summary(finished),
            "probed_at": datetime.now(UTC).isoformat(),
        }
        self.last_probe = result
        return result

    async def purge_stale_trash(
        self,
        expected_relative_paths: Iterable[str],
        *,
        path_exists: Callable[[str], bool],
    ) -> dict[str, Any]:
        """Delete Stump records for files that no longer exist on disk.

        A rename (naming migration, series title change, volume
        reclassification) or a removal leaves Stump with a record at the old
        path; its scan does not always mark it missing. A record is debris when
        Tankarr does not track the path *and* the file is gone. The caller has
        proven the library is mounted; as a second guard, one expected file
        missing from disk aborts the purge, so an unmounted share can never
        wipe the index.
        """

        async with self._operation_lock:
            if not self.configured:
                return {"configured": False, "triggered": False, "purged": False}
            expected = {str(path) for path in expected_relative_paths}
            if await asyncio.to_thread(
                lambda: any(not path_exists(path) for path in expected)
            ):
                return {
                    "configured": True,
                    "purged": False,
                    "reason": "A tracked file is not on disk; the library may be unmounted",
                }
            async with self._client() as client:
                library = await self._resolve_library(client)
                prefix = library["root"].rstrip("/") + "/"
                query = (
                    "query($page: Int!, $size: Int!) { media(pagination:{offset:{"
                    "page:$page, pageSize:$size}}) { nodes { id path status } } }"
                )
                stale: list[dict[str, Any]] = []
                page = 1
                while True:
                    data = await self._graphql(
                        client, query, {"page": page, "size": MEDIA_PAGE_SIZE}
                    )
                    nodes = (data.get("media") or {}).get("nodes") or []
                    for node in nodes:
                        path = str(node.get("path") or "")
                        if not path.startswith(prefix):
                            continue
                        relative = path[len(prefix) :]
                        if relative in expected or await asyncio.to_thread(
                            path_exists, relative
                        ):
                            continue
                        stale.append(node)
                    if len(nodes) < MEDIA_PAGE_SIZE:
                        break
                    page += 1
                removed = 0
                cleaned: dict[str, Any] = {}
                if stale:
                    # Stump's deleteMedia is a soft delete which a later scan
                    # cannot currently recover when the same path reappears.
                    # READY debris first needs a scan so Stump marks it
                    # MISSING. Already-MISSING debris can be cleaned directly;
                    # rescanning the whole library here only delays cleanup and
                    # can leave old rows visible after an HTTP timeout.
                    needs_scan = any(
                        str(node.get("status") or "").upper() != "MISSING"
                        for node in stale
                    )
                    if needs_scan:
                        deadline = (
                            monotonic() + self.settings.komga_reconcile_timeout_seconds
                        )
                        job_id = await self._start_scan(client, library["id"])
                        job = await self._wait_for_job(client, job_id, deadline)
                        status = str(job.get("status") or "")
                        if status in ACTIVE_JOB_STATUSES:
                            return {
                                "configured": True,
                                "triggered": True,
                                "purged": False,
                                "removed_books": 0,
                                "stale_series": 0,
                                "renamed_books": 0,
                                "scan_running": True,
                                "scan_job": self._job_summary(job),
                                "reason": "Stump scan is still running; cleanup deferred",
                            }
                        if status in {"FAILED", "CANCELLED"}:
                            return {
                                "configured": True,
                                "triggered": True,
                                "purged": False,
                                "removed_books": 0,
                                "stale_series": 0,
                                "renamed_books": 0,
                                "scan_running": False,
                                "scan_job": self._job_summary(job),
                                "reason": f"Stump scan ended with status {status}",
                            }
                    data = await self._graphql(
                        client,
                        "mutation($id: ID!) { cleanLibrary(id: $id) {"
                        " deletedMediaCount deletedSeriesCount } }",
                        {"id": library["id"]},
                    )
                    cleaned = data.get("cleanLibrary") or {}
                    removed = int(cleaned.get("deletedMediaCount") or 0)
            return {
                "configured": True,
                "triggered": bool(stale),
                "purged": bool(removed),
                "removed_books": removed,
                "stale_series": cleaned.get("deletedSeriesCount", 0),
                "renamed_books": 0,
            }

    async def prepare_for_moves(self) -> dict[str, Any]:
        # Stump keeps no content hashes Tankarr depends on; renames are safe.
        return {"configured": self.configured, "ready": True, "triggered": False}

    async def reconcile_deleted(
        self,
        relative_paths: Iterable[str],
        *,
        safety_check: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        """Let Stump drop records for files Tankarr removed: a scan does it."""

        if safety_check is not None:
            safety_check()
        paths = tuple(relative_paths)
        if not paths or not self.configured:
            return {
                "configured": self.configured,
                "requested": bool(paths),
                "triggered": False,
                "purged": False,
                "paths": len(paths),
            }

        # A removal is not an ordinary periodic scan: its quiet period must not
        # turn a deferred scan into an acknowledged deletion. Keep the durable
        # caller's receipt until the reader actually drops the stale records.
        async def marked_missing(client, active, remaining):
            # Stump can mark a removed directory MISSING while its child media
            # remain READY. cleanLibrary deletes that missing series and its
            # catalogue children together (without touching library files).
            parents = {
                str(PurePosixPath(path).parent)
                for path in remaining
                if str(active[path].get("status") or "").upper() != "MISSING"
            }
            for parent in parents:
                series = await self._series_at_path(client, parent)
                if str((series or {}).get("status") or "").upper() != "MISSING":
                    return False
            return True

        async with self._operation_lock, self._client() as client:
            library = await self._resolve_library(client)
            targets = self._target_paths(library["root"], paths)
            active = await self._list_media_paths(client, library["root"])
            remaining = targets & active.keys()
            needs_scan = not await marked_missing(client, active, remaining)
        result = (
            await self.force_scan()
            if needs_scan
            else {"configured": True, "triggered": False}
        )
        if result.get("scan_running") or result.get("error"):
            return {
                **result,
                "requested": True,
                "purged": False,
                "sync_pending": True,
                "paths": len(paths),
            }
        async with self._operation_lock, self._client() as client:
            library = await self._resolve_library(client)
            targets = self._target_paths(library["root"], paths)
            active = await self._list_media_paths(client, library["root"])
            remaining = targets & active.keys()
            if remaining and await marked_missing(client, active, remaining):
                if safety_check is not None:
                    safety_check()
                # cleanLibrary removes missing catalogue records, not media
                # files; unlike soft deletion, later reimports remain visible.
                await self._graphql(
                    client,
                    "mutation($id: ID!) { cleanLibrary(id: $id) {"
                    " deletedMediaCount deletedSeriesCount } }",
                    {"id": library["id"]},
                )
                active = await self._list_media_paths(client, library["root"])
                remaining = targets & active.keys()
        return {
            **result,
            "requested": True,
            "purged": not remaining,
            "sync_pending": bool(remaining),
            "paths": len(paths),
        }
