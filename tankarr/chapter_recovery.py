"""Reviewed chapter acquisition without changing a series' volume format."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import secrets
from dataclasses import asdict
from typing import Any

from fastapi import FastAPI, HTTPException, Query

from tankarr.database import ReleaseBlocked
from tankarr.series_units import load_series_units
from tankarr.service import LibraryUnavailable, RecoveryBlocked


class ChapterRecovery:
    def __init__(self, service, worker):
        self.service = service
        self.database = service.database
        self.worker = worker
        self._key = secrets.token_bytes(32)

    def preview(self, manga_id: str) -> dict[str, Any]:
        manga = self.database.get_manga(manga_id)
        groups = load_series_units(self.database, manga_id)
        if groups.get("form") != "volumes":
            raise ValueError("Use the normal chapter search for a chapter series")
        from tankarr.series_unit import is_volume_release

        pending_books = self.database.pending_book_volumes([manga_id]).get(manga_id)
        active_book = any(
            is_volume_release(row) and row.get("queue_status")
            for row in self.database.preferred_missing_releases(manga_id)
        )
        if pending_books or active_book:
            raise ValueError(
                "A whole-volume download is active. Wait for it before recovering chapters"
            )
        candidates = [
            row
            for row in self.database.chapter_recovery_candidates(manga_id)
            if not row.get("blocked") and self.service.can_download_release(row)
        ]
        plan = {
            "manga_id": manga_id,
            "language": manga["preferred_language"],
            "map_confidence": groups.get("map_confidence"),
            "chapters": [
                {
                    "id": row["id"],
                    "chapter": row["chapter"],
                    "source": row.get("source_name") or row.get("provider"),
                }
                for row in candidates
            ],
        }
        signed = {
            "plan": plan,
            "map": [asdict(entry) for entry in self.database.chapter_map(manga_id)],
            "sources": [
                {
                    key: row.get(key)
                    for key in (
                        "id",
                        "provider",
                        "source_url",
                        "language",
                        "volume",
                        "chapter",
                    )
                }
                for row in candidates
            ],
        }
        signature = hmac.new(
            self._key, json.dumps(signed, sort_keys=True).encode(), hashlib.sha256
        ).hexdigest()
        return {**plan, "confirmation_snapshot": signature}

    async def queue(self, manga_id: str, snapshot: str | None) -> dict[str, Any]:
        # Revalidate inside the same mutation lock used by ordinary grabs and
        # deletion. A second confirmation cannot duplicate the first batch.
        jobs = []
        try:
            async with self.service._mutation_lock:
                self.service.assert_mutations_allowed()
                plan = await asyncio.to_thread(self.preview, manga_id)
                if not snapshot or not hmac.compare_digest(
                    snapshot, plan["confirmation_snapshot"]
                ):
                    raise ValueError(
                        "Series or queue changed. Preview chapter recovery again"
                    )
                for row in plan["chapters"]:
                    job = self.database.create_job(
                        manga_id, row["id"], plan["language"], origin="manual"
                    )
                    if job["status"] == "queued":
                        jobs.append(job["id"])
        finally:
            # Jobs already persisted before a concurrent refusal still reach
            # the worker; a retry previews only the remaining candidates.
            for job_id in jobs:
                await self.worker.enqueue(job_id)
        return {"queued": len(jobs), "job_ids": jobs}


def register_chapter_recovery_routes(app: FastAPI, service, worker) -> ChapterRecovery:
    recovery = ChapterRecovery(service, worker)

    @app.post("/api/manga/{manga_id}/recover-chapters")
    async def recover_chapters(
        manga_id: str,
        dry_run: bool = False,
        confirmation_snapshot: str | None = Query(
            default=None, min_length=64, max_length=64, pattern="^[0-9a-f]{64}$"
        ),
    ):
        try:
            if dry_run:
                return await asyncio.to_thread(recovery.preview, manga_id)
            return await recovery.queue(manga_id, confirmation_snapshot)
        except KeyError as exc:
            raise HTTPException(404, "Series not found") from exc
        except (ValueError, ReleaseBlocked) as exc:
            raise HTTPException(409, str(exc)) from exc
        except (LibraryUnavailable, RecoveryBlocked) as exc:
            raise HTTPException(503, str(exc)) from exc

    return recovery
