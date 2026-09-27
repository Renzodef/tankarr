"""Reviewed series assembly and opt-in, bounded background assembly."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import secrets
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, HTTPException, Query

from tankarr.assemble import AssemblyConflict, BookAssembler
from tankarr.series_units import load_series_units
from tankarr.service import LibraryUnavailable, RecoveryBlocked, UnsafeLibraryPath


class BookAssemblyBatch:
    def __init__(
        self, assembler: BookAssembler, *, busy: Callable[[], bool] | None = None
    ):
        self.assembler = assembler
        self.database = assembler.database
        self.service = assembler.service
        self.busy = busy or (lambda: False)
        self._key = secrets.token_bytes(32)

    def _sign(self, plan: dict[str, Any]) -> str:
        return hmac.new(
            self._key,
            json.dumps(plan, sort_keys=True, separators=(",", ":")).encode(),
            hashlib.sha256,
        ).hexdigest()

    def _busy(self) -> bool:
        if self.busy():
            return True
        with self.database.connect() as connection:
            return bool(
                connection.execute(
                    "SELECT 1 FROM download_job WHERE status='importing' LIMIT 1"
                ).fetchone()
                or connection.execute(
                    "SELECT 1 FROM torrent_download WHERE status='importing' LIMIT 1"
                ).fetchone()
            )

    def preview(self, manga_id: str) -> dict[str, Any]:
        groups = load_series_units(self.database, manga_id)
        books, errors = [], []
        for group in groups["books"]:
            if not group["can_assemble"]:
                continue
            try:
                books.append(self.assembler.preview(manga_id, group["volume"]))
            except (AssemblyConflict, OSError, ValueError) as exc:
                errors.append({"volume": group["volume"], "message": str(exc)})
        if not books:
            raise AssemblyConflict(
                "No covered books have all their chapter files ready for assembly"
            )
        plan = {"manga_id": manga_id, "books": books, "errors": errors}
        return {**plan, "confirmation_snapshot": self._sign(plan)}

    async def assemble(
        self, manga_id: str, confirmation_snapshot: str | None
    ) -> dict[str, Any]:
        self.service.assert_mutations_allowed()
        plan = await asyncio.to_thread(self.preview, manga_id)
        if confirmation_snapshot is None or not hmac.compare_digest(
            confirmation_snapshot, plan["confirmation_snapshot"]
        ):
            raise AssemblyConflict(
                "Series or files changed. Preview all covered books again"
            )
        assembled, errors = [], list(plan["errors"])
        for book in plan["books"]:
            try:
                assembled.append(
                    await self.assembler.assemble(
                        manga_id, book["volume"], book["confirmation_snapshot"]
                    )
                )
            except (
                AssemblyConflict,
                OSError,
                ValueError,
                RecoveryBlocked,
                LibraryUnavailable,
                UnsafeLibraryPath,
            ) as exc:
                errors.append({"volume": book["volume"], "message": str(exc)})
        remaining = await asyncio.to_thread(load_series_units, self.database, manga_id)
        return {
            "assembled": assembled,
            "errors": errors,
            "remaining": [
                book["volume"] for book in remaining["books"] if book["can_assemble"]
            ],
        }

    async def run_automatic(self, *, limit: int = 4) -> dict[str, Any]:
        result: dict[str, Any] = {"assembled": 0, "errors": []}
        if self._busy() or self.service.settings.restored_safe_mode:
            return result
        self.service.assert_mutations_allowed()
        with self.database.connect() as connection:
            series = [
                str(row["id"])
                for row in connection.execute(
                    "SELECT id FROM manga WHERE assemble_books_automatically=1 ORDER BY id"
                )
            ]
        attempted = 0
        for manga_id in series:
            groups = await asyncio.to_thread(load_series_units, self.database, manga_id)
            for group in groups["books"]:
                if not group["can_assemble"]:
                    continue
                if attempted >= limit or self._busy():
                    return result
                if not self.database.get_manga(
                    manga_id, include_logical_counts=False
                ).get("assemble_books_automatically"):
                    break
                try:
                    attempted += 1
                    preview = await asyncio.to_thread(
                        self.assembler.preview, manga_id, group["volume"]
                    )
                    await self.assembler.assemble(
                        manga_id, group["volume"], preview["confirmation_snapshot"]
                    )
                    result["assembled"] += 1
                except (
                    AssemblyConflict,
                    OSError,
                    ValueError,
                    RecoveryBlocked,
                    LibraryUnavailable,
                    UnsafeLibraryPath,
                ) as exc:
                    result["errors"].append(
                        {
                            "manga_id": manga_id,
                            "volume": group["volume"],
                            "message": str(exc),
                        }
                    )
        return result


def register_assembly_batch_routes(app: FastAPI, batch: BookAssemblyBatch) -> None:
    @app.post("/api/manga/{manga_id}/assemble")
    async def assemble_books(
        manga_id: str,
        dry_run: bool = False,
        confirmation_snapshot: str | None = Query(
            default=None, min_length=64, max_length=64, pattern="^[0-9a-f]{64}$"
        ),
    ):
        try:
            if dry_run:
                return await asyncio.to_thread(batch.preview, manga_id)
            return await batch.assemble(manga_id, confirmation_snapshot)
        except KeyError as exc:
            raise HTTPException(404, "Series not found") from exc
        except AssemblyConflict as exc:
            raise HTTPException(409, exc.detail) from exc
        except (LibraryUnavailable, RecoveryBlocked) as exc:
            raise HTTPException(503, str(exc)) from exc
        except UnsafeLibraryPath as exc:
            raise HTTPException(409, str(exc)) from exc
