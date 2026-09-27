"""Reviewed audit actions sharing the ordinary durable recycling journal."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import sqlite3
import stat
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from tankarr.database import ACTIVE_TORRENT_STATUSES, ActiveDownloadJobsError, Database
from tankarr.series_audit import SeriesAudit, StaleAudit, _source
from tankarr.service import (
    LibraryUnavailable,
    RecoveryBlocked,
    TankarrService,
    UnsafeLibraryPath,
)

logger = logging.getLogger(__name__)
_HEX = re.compile(r"^[0-9a-f]{64}$")


def _encoded(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), default=str
    ).encode()


class RetireFilesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    chapter_ids: list[str] = Field(min_length=1, max_length=1000)
    revision: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class RejectSourceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: str = Field(min_length=1, max_length=200)
    provider_manga_id: str = Field(min_length=1, max_length=1000)
    revision: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class AuditActions:
    def __init__(self, audit: SeriesAudit):
        self.audit = audit
        self.database = audit.database
        self.service = audit.service
        self._key = secrets.token_bytes(32)

    def _database_state(self, manga_id: str) -> dict:
        return {
            "manga": self.database.get_manga(manga_id, include_logical_counts=False),
            "releases": self.database.list_all_chapters(manga_id),
            "sources": self.database.list_release_sources(manga_id, enabled_only=False),
        }

    def _ready(self, manga_id: str) -> None:
        self.service.assert_mutations_allowed()
        with self.database.connect() as connection:
            self.database._raise_for_active_jobs(
                connection, "job.manga_id=?", (manga_id,)
            )
            torrents = connection.execute(
                "SELECT id, status FROM torrent_download WHERE manga_id=? "
                f"AND status IN ({','.join('?' for _ in ACTIVE_TORRENT_STATUSES)})",
                (manga_id, *ACTIVE_TORRENT_STATUSES),
            ).fetchall()
            if torrents:
                raise StaleAudit(
                    "Wait for active series torrents to finish before retirement"
                )

    def _plan(self, manga_id, chapter_ids, source, revision=None) -> dict:
        with self.database.read_snapshot():
            reviewed = self.audit.preview(
                manga_id, chapter_ids, source, revision=revision
            )
            database_state = self._database_state(manga_id)
            self._ready(manga_id)
        source_ids = []
        if source is not None:
            for release in database_state["releases"]:
                mapping = _source(release, database_state["sources"])
                if mapping and all(
                    mapping[field] == reviewed["source"][field]
                    for field in ("provider", "provider_manga_id")
                ):
                    source_ids.append(str(release["id"]))
        signed = {
            "audit": reviewed["confirmation_snapshot"],
            "database": database_state,
            "source_ids": sorted(source_ids),
        }
        reviewed["confirmation_snapshot"] = hmac.new(
            self._key, _encoded(signed), hashlib.sha256
        ).hexdigest()
        reviewed["releases_to_remove"] = len(source_ids)
        return {
            "reviewed": reviewed,
            "audit_confirmation": signed["audit"],
            "database": database_state,
            "source_ids": source_ids,
        }

    def preview(
        self, manga_id, chapter_ids=None, source=None, *, revision=None
    ) -> dict:
        return self._plan(manga_id, chapter_ids, source, revision)["reviewed"]

    @staticmethod
    def _matches(info, fingerprint, *, renamed=False):
        fields = [
            ("st_dev", "device"),
            ("st_ino", "inode"),
            ("st_size", "size"),
            ("st_mtime_ns", "mtime_ns"),
        ]
        if not renamed:
            fields.append(("st_ctime_ns", "ctime_ns"))
        return stat.S_ISREG(info.st_mode) and all(
            getattr(info, attribute) == fingerprint[key] for attribute, key in fields
        )

    @classmethod
    def _hash_file(cls, path, fingerprint, *, renamed=False):
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as handle:
            before = os.fstat(handle.fileno())
            if not cls._matches(before, fingerprint, renamed=renamed):
                raise StaleAudit("A file changed before retirement; preview again")
            digest = hashlib.sha256()
            remaining = fingerprint["size"] + 1
            while remaining:
                chunk = handle.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                digest.update(chunk)
            after = os.fstat(handle.fileno())
            if (
                remaining == 0
                or not cls._matches(after, fingerprint, renamed=renamed)
                or before.st_ctime_ns != after.st_ctime_ns
            ):
                raise StaleAudit("A file changed while retirement was checked")
            return digest.hexdigest()

    def _commit_locked(self, manga_id, chapter_ids, source, revision, confirmation):
        plan = self._plan(manga_id, chapter_ids, source, revision)
        reviewed = plan["reviewed"]
        if (
            not isinstance(confirmation, str)
            or not _HEX.fullmatch(confirmation)
            or not hmac.compare_digest(confirmation, reviewed["confirmation_snapshot"])
        ):
            raise StaleAudit("Files or source changed; preview and confirm again")
        selected = set(reviewed["chapter_ids"])
        root = self.service._library_root()
        expected = {}
        for release in plan["database"]["releases"]:
            if str(release["id"]) not in selected:
                continue
            path, fingerprint, _key = self.audit._file(release, root)
            ledger = release.get("library_sha256")
            if ledger and (not isinstance(ledger, str) or not _HEX.fullmatch(ledger)):
                raise StaleAudit(
                    "The stored file checksum is invalid; verify the file first"
                )
            digest = (
                ledger if isinstance(ledger, str) and _HEX.fullmatch(ledger) else None
            )
            digest = digest or self._hash_file(path, fingerprint)
            if path in expected and expected[path][1] != digest:
                raise StaleAudit("Shared file ownership has conflicting checksums")
            expected[path] = (fingerprint, digest)
        paths = list(expected)
        with self.database.write_snapshot():
            self.audit.validate(
                manga_id,
                chapter_ids,
                source,
                confirmation_snapshot=plan["audit_confirmation"],
            )
            if self._database_state(manga_id) != plan["database"]:
                raise StaleAudit("Series changed before retirement; preview again")
            self._ready(manga_id)
            self.service._assert_library_paths_not_shared(manga_id, selected, paths)

        # Prepared must be durable before BEGIN IMMEDIATE: a failed DB commit
        # then leaves a known journal state that ordinary recovery can restore.
        quarantine = self.service._stage_library_files(paths, disposition="retain")
        try:
            with self.database.write_snapshot():
                if self._database_state(manga_id) != plan["database"]:
                    raise StaleAudit("Series changed during retirement; preview again")
                self._ready(manga_id)
                self.service._assert_library_paths_not_shared(manga_id, selected, paths)
                moved = dict(quarantine.moved)
                if set(moved) != set(paths) or quarantine.missing:
                    raise StaleAudit("A reviewed file disappeared during retirement")
                for original, (fingerprint, digest) in expected.items():
                    actual = self._hash_file(moved[original], fingerprint, renamed=True)
                    if not hmac.compare_digest(actual, digest):
                        raise StaleAudit(
                            "File bytes changed during retirement; files restored"
                        )
                reset = {"chapters_reset": 0, "jobs_deleted": 0}
                if selected:
                    reset = self.database.reset_chapter_files(
                        manga_id, sorted(selected), quarantine.operation_id
                    )
                removed = 0
                if source is not None:
                    removed = self.database.forget_undownloaded_releases(
                        manga_id, plan["source_ids"]
                    )
                    self.database.delete_release_source(
                        manga_id,
                        reviewed["source"]["provider"],
                        reviewed["source"]["provider_manga_id"],
                    )
                    self.database.add_release_source_rejection(
                        manga_id,
                        reviewed["source"]["provider"],
                        reviewed["source"]["provider_manga_id"],
                        "rejected in series file audit",
                    )
        except Exception as exc:
            self.service._handle_deletion_database_exception(quarantine, exc)
            raise
        files = self.service._commit_file_quarantine(quarantine)
        return {
            "manga_id": manga_id,
            **files,
            "files_retired": files.get("files_retired", 0),
            **reset,
            "releases_removed": removed,
            "source_removed": source is not None,
        }

    async def apply(
        self,
        manga_id,
        chapter_ids=None,
        source=None,
        *,
        revision=None,
        confirmation_snapshot=None,
    ) -> dict:
        cancelled = False
        async with self.service._mutation_lock:
            pending = asyncio.create_task(
                asyncio.to_thread(
                    self._commit_locked,
                    manga_id,
                    chapter_ids,
                    source,
                    revision,
                    confirmation_snapshot,
                )
            )
            while not pending.done():
                try:
                    await asyncio.shield(pending)
                except asyncio.CancelledError:
                    cancelled = True
            result = pending.result()
        result["komga_scan"] = await self.service._request_komga_reconciliation(
            bool(result["files_retired"])
            and not result["cleanup_errors"]
            and not result["quarantine_files_remaining"]
        )
        if cancelled:
            raise asyncio.CancelledError
        return result


def register_audit_routes(
    app: FastAPI, database: Database, service: TankarrService
) -> SeriesAudit:
    audit = SeriesAudit(database, service)
    actions = AuditActions(audit)

    def error(exc):
        if isinstance(exc, KeyError):
            return HTTPException(404, "Series or file not found")
        if isinstance(exc, (StaleAudit, ActiveDownloadJobsError, UnsafeLibraryPath)):
            return HTTPException(409, str(exc))
        if isinstance(exc, (LibraryUnavailable, RecoveryBlocked)):
            return HTTPException(503, str(exc))
        if isinstance(exc, ValueError):
            return HTTPException(400, str(exc))
        logger.exception("Series audit request failed")
        return HTTPException(500, "The audit action could not be completed")

    @app.get("/api/manga/{manga_id}/audit")
    async def read_audit(manga_id: str):
        try:
            return await asyncio.to_thread(audit.read, manga_id)
        except (
            KeyError,
            ValueError,
            OSError,
            RecoveryBlocked,
            LibraryUnavailable,
        ) as exc:
            raise error(exc) from exc

    @app.get("/api/manga/{manga_id}/audit/files/{release_id}/{edge}")
    async def audit_thumbnail(
        manga_id: str,
        release_id: str,
        edge: str,
        request: Request,
        revision: str = Query(pattern="^[0-9a-f]{64}$"),
    ):
        try:
            path, digest = await audit.thumbnail(manga_id, release_id, edge, revision)
            headers = {
                "Cache-Control": "private, max-age=86400",
                "ETag": f'"{digest}"',
                "X-Content-Type-Options": "nosniff",
            }
            if request.headers.get("if-none-match") == headers["ETag"]:
                return Response(status_code=304, headers=headers)
            return FileResponse(path, media_type="image/webp", headers=headers)
        except (
            KeyError,
            ValueError,
            OSError,
            RecoveryBlocked,
            LibraryUnavailable,
        ) as exc:
            raise error(exc) from exc

    async def change(manga_id, chapter_ids, source, revision, dry_run, confirmation):
        try:
            if dry_run:
                return await asyncio.to_thread(
                    actions.preview, manga_id, chapter_ids, source, revision=revision
                )
            return await actions.apply(
                manga_id,
                chapter_ids,
                source,
                revision=revision,
                confirmation_snapshot=confirmation,
            )
        except (KeyError, ValueError, OSError, RuntimeError, sqlite3.Error) as exc:
            raise error(exc) from exc

    @app.post("/api/manga/{manga_id}/audit/retire")
    async def retire(
        manga_id: str,
        body: RetireFilesRequest,
        dry_run: bool = False,
        confirmation_snapshot: str | None = Query(
            default=None, pattern="^[0-9a-f]{64}$"
        ),
    ):
        return await change(
            manga_id,
            body.chapter_ids,
            None,
            body.revision,
            dry_run,
            confirmation_snapshot,
        )

    @app.post("/api/manga/{manga_id}/audit/reject-source")
    async def reject_source(
        manga_id: str,
        body: RejectSourceRequest,
        dry_run: bool = False,
        confirmation_snapshot: str | None = Query(
            default=None, pattern="^[0-9a-f]{64}$"
        ),
    ):
        return await change(
            manga_id,
            None,
            {"provider": body.provider, "provider_manga_id": body.provider_manga_id},
            body.revision,
            dry_run,
            confirmation_snapshot,
        )

    return audit
