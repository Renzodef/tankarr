from __future__ import annotations

import os
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tankarr import backups as backup_module
from tankarr.backups import DatabaseBackups


@pytest.fixture
def backups(tmp_path: Path):
    source = tmp_path / "live.sqlite3"
    with sqlite3.connect(source) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE item (value TEXT)")
        connection.execute("INSERT INTO item VALUES ('preserved')")
    return DatabaseBackups(source, tmp_path / "backups", retain=2)


def test_backups_in_the_same_instant_are_distinct_private_and_readable(
    backups, monkeypatch
):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2030, 1, 1, tzinfo=UTC)

    monkeypatch.setattr(backup_module, "datetime", Clock)
    first = backups.create()
    with sqlite3.connect(backups.source) as connection:
        connection.execute("INSERT INTO item VALUES ('new')")
    second = backups.create()
    assert first["name"] != second["name"]
    assert first["verified"] and second["verified"]
    assert len(backups.list()) == 2
    for record, expected_count in [(first, 1), (second, 2)]:
        path = backups.directory / record["name"]
        if os.name == "posix":
            assert path.stat().st_mode & 0o777 == 0o600
        with sqlite3.connect(path) as connection:
            assert connection.execute("PRAGMA quick_check").fetchall() == [("ok",)]
            assert connection.execute("SELECT COUNT(*) FROM item").fetchone() == (
                expected_count,
            )


def test_uncommitted_wal_changes_do_not_enter_backup(backups):
    connection = sqlite3.connect(backups.source)
    try:
        connection.execute("INSERT INTO item VALUES ('uncommitted')")
        result = backups.create()
        with sqlite3.connect(backups.directory / result["name"]) as snapshot:
            assert snapshot.execute("SELECT value FROM item").fetchall() == [
                ("preserved",)
            ]
    finally:
        connection.rollback()
        connection.close()


@pytest.mark.parametrize(
    "error", [sqlite3.DatabaseError("copy failed"), OSError("disk full")]
)
def test_failed_backup_keeps_previous_snapshots_and_removes_partial(
    backups, monkeypatch, error
):
    original = backups.create()
    original_path = backups.directory / original["name"]
    original_bytes = original_path.read_bytes()

    def fail(path):
        path.write_bytes(b"partial backup")
        raise error

    monkeypatch.setattr(backups, "_copy", fail)
    with pytest.raises(type(error)):
        backups.create()
    assert original_path.read_bytes() == original_bytes
    assert [entry["name"] for entry in backups.list()] == [original["name"]]
    assert list(backups.directory.glob("*.partial")) == []


def test_copy_is_hidden_until_complete_and_parallel_requests_are_serialized(
    backups, monkeypatch
):
    started = threading.Event()
    release = threading.Event()
    real_copy = backups._copy
    copies = []

    def slow_copy(path):
        copies.append(path)
        started.set()
        assert release.wait(10)
        real_copy(path)

    monkeypatch.setattr(backups, "_copy", slow_copy)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(backups.create)
        try:
            assert started.wait(10)
            second = executor.submit(backups.create)
            assert backups.list() == []
            assert len(copies) == 1
        finally:
            release.set()
        assert first.result(timeout=10)["name"] != second.result(timeout=10)["name"]
    assert len(backups.list()) == 2


def test_retention_does_not_follow_links_or_remove_unrelated_files(backups):
    first = backups.create()
    first_path = backups.directory / first["name"]
    os.utime(first_path, ns=(1, 1))
    outsider = backups.source.parent / "external.sqlite3"
    outsider.write_bytes(b"not a backup")
    link = backups.directory / "tankarr-linked.sqlite3"
    link.symlink_to(outsider)
    unrelated = backups.directory / "other.sqlite3"
    unrelated.write_bytes(b"keep me")
    backups.create()
    backups.create()
    assert len(backups.list()) == 2
    assert not first_path.exists()
    assert link.is_symlink()
    assert outsider.read_bytes() == b"not a backup"
    assert unrelated.read_bytes() == b"keep me"


def test_missing_source_does_not_create_an_empty_database_or_prune(backups):
    original = backups.create()
    backups.source = backups.source.parent / "missing.sqlite3"
    with pytest.raises(sqlite3.OperationalError):
        backups.create()
    assert not backups.source.exists()
    assert [entry["name"] for entry in backups.list()] == [original["name"]]


def test_corrupt_source_does_not_publish_or_prune(backups):
    original = backups.create()
    corrupt = backups.source.parent / "corrupt.sqlite3"
    corrupt.write_bytes(b"not a SQLite database")
    backups.source = corrupt
    with pytest.raises(sqlite3.DatabaseError):
        backups.create()
    assert [entry["name"] for entry in backups.list()] == [original["name"]]
    assert list(backups.directory.glob("*.partial")) == []


def test_backup_timeout_does_not_publish_or_prune(backups, monkeypatch):
    original = backups.create()
    readings = iter([0, 181])
    monkeypatch.setattr(backup_module, "monotonic", lambda: next(readings))
    with pytest.raises(TimeoutError):
        backups.create()
    assert [entry["name"] for entry in backups.list()] == [original["name"]]


def test_retention_keeps_the_new_backup_when_clock_moves_backwards(backups):
    backups.retain = 1
    original = backups.create()
    original_path = backups.directory / original["name"]
    future = datetime(2099, 1, 1, tzinfo=UTC).timestamp()
    os.utime(original_path, (future, future))

    newest = backups.create()

    assert (backups.directory / newest["name"]).is_file()
    assert not original_path.exists()
    assert [entry["name"] for entry in backups.list()] == [newest["name"]]
    assert backups.list(limit=0) == []
