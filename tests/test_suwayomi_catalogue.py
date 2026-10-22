from __future__ import annotations

from pathlib import Path

import pytest

from tankarr.suwayomi_catalogue import (
    BATCH_ROWS,
    CatalogueHeapError,
    CatalogueSchemaError,
    apply_catalogue_pass,
    chapter_keep_sql,
    compact_due,
    csvwrite,
    delete_sql,
    parse_count,
    quote_ident,
    run_while_stopped,
    timestamp_keep_sql,
)

SCHEMA = """TABLE_NAME,COLUMN_NAME
MangaTable,id
MangaTable,in_library
MangaTable,last_fetched_at
MangaTable,chapters_last_fetched_at
MangaTable,last_modified_at
ChapterTable,manga
ChapterTable,is_downloaded
ChapterTable,read
ChapterTable,bookmark
ChapterTable,last_read_at
"""
FIELDS = {
    "id": "id",
    "in_library": "in_library",
    "last_fetched_at": "last_fetched_at",
    "chapters_last_fetched_at": "chapters_last_fetched_at",
    "last_modified_at": "last_modified_at",
}


def test_identifiers_and_deletes_stay_bounded():
    assert quote_ident("MangaTable") == '"MangaTable"'
    with pytest.raises(CatalogueSchemaError):
        quote_ident("MangaTable; DROP TABLE manga")
    statement = delete_sql("MangaTable", FIELDS, BATCH_ROWS)
    assert "LIMIT 10" in statement
    assert "SELECT *" not in statement.upper()
    assert "CACHE_SIZE" not in statement.upper()
    kept = timestamp_keep_sql("MangaTable", FIELDS, 1_700_000_000)
    assert ">= 0" not in kept
    assert "> 0" in kept
    query = "SELECT COUNT(*) AS remaining FROM MangaTable"
    rendered = csvwrite(Path("/tmp/count.csv"), query)
    assert "SELECT *" not in rendered
    with pytest.raises(CatalogueSchemaError):
        csvwrite(Path("/tmp/wide.csv"), "SELECT * FROM MangaTable")
    assert parse_count("remaining\n4\n") == 4


def test_a_chapter_table_without_flags_deletes_nothing():
    with pytest.raises(CatalogueSchemaError):
        chapter_keep_sql("ChapterTable", {"manga": "manga"})


def test_compact_only_when_the_file_is_large_and_has_grown():
    assert compact_due(1024, None) is False
    assert compact_due(64 * 1024 * 1024 + 1, None) is True
    assert compact_due(80 * 1024 * 1024, 80 * 1024 * 1024) is False
    assert compact_due(80 * 1024 * 1024, 40 * 1024 * 1024) is True


def _script_writes(statements: list[str], text: str) -> None:
    for statement in statements:
        if "CSVWRITE('" not in statement:
            continue
        start = statement.find("CSVWRITE('") + len("CSVWRITE('")
        end = statement.find("'", start)
        Path(statement[start:end]).write_text(text, encoding="utf-8")


def test_a_finished_pass_compacts_and_a_chapter_scan_oom_deletes_nothing(tmp_path):
    database = tmp_path / "database.mv.db"
    database.write_bytes(b"x" * 128)

    def execute(statements, _timeout):
        joined = "\n".join(statements)
        assert "CACHE_SIZE" not in joined.upper()
        assert "SELECT *" not in joined.upper()
        if "INFORMATION_SCHEMA" in joined:
            _script_writes(statements, SCHEMA)
            return ""
        if any(item.startswith("DELETE") for item in statements):
            _script_writes(statements, "remaining\n0\n")
            return ""
        _script_writes(statements, "remaining\n2\n")
        return ""

    outcome = apply_catalogue_pass(
        database_file=database,
        protected_ids=[7],
        max_age_hours=24,
        execute=execute,
    )
    assert outcome["deleted"] == 2
    assert outcome["remaining"] == 0
    assert outcome["compacted"] is True

    def oom(statements, _timeout):
        joined = "\n".join(statements)
        if "INFORMATION_SCHEMA" in joined:
            _script_writes(statements, SCHEMA)
            return ""
        raise CatalogueHeapError("heap")

    aborted = apply_catalogue_pass(
        database_file=database,
        protected_ids=[],
        max_age_hours=24,
        execute=oom,
    )
    assert aborted["deleted"] == 0
    assert aborted["compacted"] is False
    assert aborted["aborted"]


class _Runtime:
    def __init__(self) -> None:
        self.running = True
        self._catalogue_cleanup = False
        self._users = 0
        self.starts = 0
        self.stops = 0

    async def stop(self) -> None:
        self.running = False
        self.stops += 1

    async def start(self) -> None:
        if self._catalogue_cleanup:
            raise AssertionError("start during cleanup")
        self.running = True
        self.starts += 1


@pytest.mark.asyncio
async def test_catalogue_maintenance_can_leave_the_server_stopped():
    runtime = _Runtime()

    async def work():
        assert runtime.running is False
        assert runtime._catalogue_cleanup is True
        return {"deleted": 1}

    result = await run_while_stopped(runtime, work, restart=False)
    assert result == {"deleted": 1}
    assert runtime.stops == 1
    assert runtime.starts == 0
    assert runtime.running is False
    assert runtime._catalogue_cleanup is False


@pytest.mark.asyncio
async def test_a_waiting_caller_starts_the_server_after_maintenance():
    runtime = _Runtime()
    runtime._users = 1
    await run_while_stopped(runtime, lambda: {"ok": True}, restart=False)
    assert runtime.starts == 1
    assert runtime.running is True
