"""Durable, idempotent translation jobs. Credentials never enter this ledger."""

from __future__ import annotations

import json
import uuid

from tankarr.database import Database, utc_now
from tankarr.translation_policy import slot_key


class TranslationStore:
    def __init__(self, database: Database):
        self.database = database

    def get(self, job_id: str) -> dict:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM translation_job WHERE id=?", (job_id,)
            ).fetchone()
        if row is None:
            raise KeyError(job_id)
        result = dict(row)
        result["source"] = json.loads(result.pop("source_json"))
        return result

    def list(
        self,
        manga_id: str | None = None,
        *,
        active: bool = False,
        enabled_only: bool = False,
    ) -> list[dict]:
        clauses, params = [], []
        if manga_id is not None:
            clauses.append("manga_id=?")
            params.append(manga_id)
        if active:
            clauses.append(
                "status IN ('queued', 'preparing', 'processing', 'importing', 'waiting_native')"
            )
        if enabled_only:
            clauses.append(
                "manga_id IN (SELECT id FROM manga WHERE translation_enabled=1)"
            )
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        # Apply priority before the result limit: old paused jobs or completed
        # history must not hide work that can progress now.
        order = (
            " ORDER BY CASE status "
            "WHEN 'processing' THEN 0 WHEN 'importing' THEN 0 "
            "WHEN 'preparing' THEN 0 WHEN 'queued' THEN 1 "
            "WHEN 'waiting_native' THEN 2 ELSE 3 END, "
            + ("created_at" if active else "updated_at DESC")
            + " LIMIT 200"
        )
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT id FROM translation_job" + where + order,
                params,
            ).fetchall()
        return [self.get(row["id"]) for row in rows]

    def create(self, manga_id: str, source: dict, target_language: str) -> dict:
        now = utc_now()
        key = slot_key(source)
        # Whitelist metadata: a provider response can contain arbitrary fields.
        metadata = {
            key: source.get(key)
            for key in (
                "id",
                "provider",
                "language",
                "volume",
                "chapter",
                "title",
                "source_url",
                "source_name",
                "numbering_status",
                "release_unit",
            )
        }
        metadata["target_edition_book_count"] = self.database.get_manga(manga_id).get(
            "edition_book_count"
        )
        with self.database.connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO translation_job "
                "(id,manga_id,slot_key,source_json,target_language,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    uuid.uuid4().hex,
                    manga_id,
                    key,
                    json.dumps(metadata),
                    target_language,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT id FROM translation_job WHERE manga_id=? AND slot_key=? AND target_language=?",
                (manga_id, key, target_language),
            ).fetchone()
        return self.get(row["id"])

    def slots(self, manga_id: str, target_language: str) -> set[str]:
        with self.database.connect() as connection:
            return {
                row[0]
                for row in connection.execute(
                    "SELECT slot_key FROM translation_job WHERE manga_id=? AND target_language=?",
                    (manga_id, target_language),
                )
            }

    def update(self, job_id: str, **changes) -> dict:
        allowed = {"status", "message", "attempts", "result_chapter_id"}
        if changes.keys() - allowed:
            raise ValueError("Unsupported translation job update")
        changes["updated_at"] = utc_now()
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE translation_job SET "
                + ",".join(f"{key}=?" for key in changes)
                + " WHERE id=?",
                [*changes.values(), job_id],
            )
        return self.get(job_id)
