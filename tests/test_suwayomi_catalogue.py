from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tankarr import suwayomi_catalogue as catalogue
from tankarr.suwayomi_catalogue import (
    BATCH_ROWS,
    CatalogueCommandError,
    CatalogueHeapError,
    CatalogueSchemaError,
    apply_catalogue_pass,
    catalogue_failure_message,
    catalogue_limits,
    catalogue_pass_is_recent,
    chapter_keep_sql,
    cleanup_jvm,
    compact_due,
    csvwrite,
    delete_sql,
    execute_h2,
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


def test_a_930_mebibyte_catalogue_gets_room_to_scan_and_compact():
    size = 930 * 1024 * 1024
    pass_seconds, round_seconds, compact_seconds = catalogue_limits(size)
    assert round_seconds >= 10 * 60
    assert pass_seconds >= round_seconds
    assert compact_seconds >= 20 * 60
    assert (pass_seconds, round_seconds, compact_seconds) == (60 * 60, 15 * 60, 45 * 60)
    assert catalogue_limits(1024) == (8 * 60, 180, 600)


def test_cleanup_heap_grows_with_the_database(tmp_path: Path):
    database = tmp_path / "database.mv.db"
    database.write_bytes(b"x")
    base = tmp_path / "database"
    assert cleanup_jvm(base)[0] == "-Xmx192m"
    with database.open("wb") as handle:
        handle.truncate(65 * 1024 * 1024)
    assert cleanup_jvm(base)[0] == "-Xmx320m"
    with database.open("wb") as handle:
        handle.truncate(512 * 1024 * 1024)
    assert cleanup_jvm(base)[0] == "-Xmx512m"


def test_h2_login_tries_a_blank_user_before_sa(tmp_path: Path, monkeypatch):
    catalogue._h2_users.clear()
    calls: list[str] = []

    def fake_run(command, **_kwargs):
        user = command[command.index("-user") + 1]
        calls.append(user)
        assert "-Xmx192m" in command
        if user == "":
            return subprocess.CompletedProcess(
                command,
                1,
                "",
                "Wrong user name or password [28000]",
            )
        return subprocess.CompletedProcess(command, 0, "ok\n", "")

    monkeypatch.setattr(catalogue.subprocess, "run", fake_run)
    base = tmp_path / "database"
    (tmp_path / "database.mv.db").write_bytes(b"x")
    output = execute_h2("java", tmp_path / "server.jar", base, ["SELECT 1;"], 5)
    assert calls == ["", "sa"]
    assert "ok" in output

    calls.clear()
    again = execute_h2("java", tmp_path / "server.jar", base, ["SELECT 1;"], 5)
    assert calls == ["sa"]
    assert "ok" in again

    def blank_works(command, **_kwargs):
        user = command[command.index("-user") + 1]
        calls.append(user)
        if user == "sa":
            return subprocess.CompletedProcess(
                command, 1, "", "Wrong user name or password [28000]"
            )
        return subprocess.CompletedProcess(command, 0, "blank\n", "")

    calls.clear()
    monkeypatch.setattr(catalogue.subprocess, "run", blank_works)
    switched = execute_h2("java", tmp_path / "server.jar", base, ["SELECT 1;"], 5)
    assert calls == ["sa", ""]
    assert "blank" in switched
    calls.clear()
    execute_h2("java", tmp_path / "server.jar", base, ["SELECT 1;"], 5)
    assert calls == [""]


def test_h2_login_does_not_retry_anything_but_a_rejected_user(
    tmp_path: Path, monkeypatch
):
    catalogue._h2_users.clear()
    calls: list[str] = []

    def fake_run(command, **_kwargs):
        calls.append(command[command.index("-user") + 1])
        return subprocess.CompletedProcess(
            command, 1, "", "Syntax error in SQL statement"
        )

    monkeypatch.setattr(catalogue.subprocess, "run", fake_run)
    base = tmp_path / "database"
    (tmp_path / "database.mv.db").write_bytes(b"x")
    with pytest.raises(CatalogueCommandError, match="Syntax error"):
        execute_h2("java", tmp_path / "server.jar", base, ["SELECT 1;"], 5)
    assert calls == [""]


def test_a_bloated_catalogue_pass_keeps_the_chapter_scan_alive(tmp_path: Path):
    database = tmp_path / "database.mv.db"
    with database.open("wb") as handle:
        handle.truncate(930 * 1024 * 1024)
    seen: list[float] = []

    def execute(statements, timeout):
        seen.append(timeout)
        joined = "\n".join(statements)
        if "INFORMATION_SCHEMA" in joined:
            _script_writes(statements, SCHEMA)
            return ""
        raise CatalogueHeapError("stop after the chapter scan is scheduled")

    outcome = apply_catalogue_pass(
        database_file=database,
        protected_ids=[7],
        max_age_hours=24,
        execute=execute,
    )
    assert seen[0] >= 10 * 60
    assert seen[1] >= 10 * 60
    assert outcome["aborted"]
    assert outcome["deleted"] == 0


def test_catalogue_failure_text_is_the_stored_error_or_abort():
    assert catalogue_failure_message(None) is None
    assert catalogue_failure_message({"result": {"skipped": "recent"}}) is None
    message = catalogue_failure_message(
        {
            "result": {
                "error": "CatalogueCommandError: Wrong user name or password [28000]"
            }
        }
    )
    assert message is not None
    assert "28000" in message
    aborted = catalogue_failure_message(
        {"result": {"aborted": "chapter scan ran out of memory"}}
    )
    assert aborted == "chapter scan ran out of memory"


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


def test_a_finished_catalogue_pass_is_recent_until_the_interval_elapses():
    now = datetime(2026, 10, 22, tzinfo=UTC)
    finished = {
        "at": (now - timedelta(hours=1)).isoformat(),
        "result": {"deleted": 3, "remaining": 0},
    }
    assert catalogue_pass_is_recent(finished, now=now, interval_seconds=6 * 3600)
    assert not catalogue_pass_is_recent(
        finished, now=now + timedelta(hours=6), interval_seconds=6 * 3600
    )
    deferred = {"at": now.isoformat(), "result": {"deferred": "monitor"}}
    failed = {"at": now.isoformat(), "result": {"error": "boom"}}
    aborted = {
        "at": now.isoformat(),
        "result": {"deleted": 0, "aborted": "chapter scan ran out of memory"},
    }
    assert not catalogue_pass_is_recent(deferred, now=now, interval_seconds=3600)
    assert not catalogue_pass_is_recent(failed, now=now, interval_seconds=3600)
    assert not catalogue_pass_is_recent(aborted, now=now, interval_seconds=3600)
    assert not catalogue_pass_is_recent(
        {"at": now.isoformat(), "result": {"skipped": "disabled"}},
        now=now,
        interval_seconds=3600,
    )


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
