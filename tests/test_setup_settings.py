from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tankarr.app import create_app
from tankarr.config import Settings
from tankarr.database import SCHEMA_VERSION, Database
from tankarr.settings_store import apply_setting_overrides, update_settings


def test_setup_and_retention_settings_roundtrip_through_api(tmp_path: Path):
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path,
        library_dir=tmp_path / "library",
        monitor_enabled=False,
        metadata_enabled=False,
        internet_archive_enabled=False,
    )
    app = create_app(settings)
    client = TestClient(app)
    initial = client.get("/api/settings").json()
    assert initial["setup_completed_at"]["value"] is None
    assert initial["backup_retention_count"]["value"] == 7
    assert initial["recycle_bin_retention_days"]["value"] == 7
    response = client.put(
        "/api/settings",
        json={
            "setup_completed_at": "2026-09-07T04:00:00+02:00",
            "backup_retention_count": 5,
            "recycle_bin_retention_days": 14,
        },
    )
    assert response.status_code == 200
    assert (
        client.get("/api/settings").json()["setup_completed_at"]["value"]
        == "2026-09-07T02:00:00+00:00"
    )
    fresh = Settings(_env_file=None, data_dir=tmp_path)
    apply_setting_overrides(fresh, app.state.database)
    assert fresh.setup_completed_at == "2026-09-07T02:00:00+00:00"
    assert fresh.backup_retention_count == 5
    assert fresh.recycle_bin_retention_days == 14
    assert (
        client.put("/api/settings", json={"setup_completed_at": None}).status_code
        == 200
    )
    assert client.get("/api/settings").json()["setup_completed_at"]["value"] is None


@pytest.mark.parametrize(
    "changes",
    [
        {"setup_completed_at": "2026-09-07"},
        {"setup_completed_at": "2026-09-07T02:00:00"},
        {"setup_completed_at": "nonsense"},
        {"backup_retention_count": 0},
        {"recycle_bin_retention_days": 0},
        {"recycle_bin_retention_days": 366},
    ],
)
def test_invalid_settings_do_not_change_persisted_values(tmp_path: Path, changes: dict):
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    settings = Settings(_env_file=None, data_dir=tmp_path)
    previous = database.get_setting_overrides()
    with pytest.raises(ValueError):
        update_settings(settings, database, changes)
    assert database.get_setting_overrides() == previous


def old_schema(tmp_path: Path) -> Database:
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    with database.connect() as connection:
        connection.execute("ALTER TABLE deletion_operation DROP COLUMN disposition")
        connection.execute("PRAGMA user_version=0")
    return database


def test_migration_backup_precedes_schema_changes_and_runs_once(tmp_path: Path):
    database = old_schema(tmp_path)
    calls = []

    def backup():
        with sqlite3.connect(database.path) as connection:
            columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(deletion_operation)")
            }
        assert "disposition" not in columns
        calls.append(True)

    database.initialize(before_migration=backup)
    database.initialize(before_migration=backup)
    assert calls == [True]
    with database.connect() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert "disposition" in {
            row[1]
            for row in connection.execute("PRAGMA table_info(deletion_operation)")
        }


def test_backup_failure_prevents_schema_migration(tmp_path: Path):
    database = old_schema(tmp_path)

    def fail():
        raise OSError("Backup disk unavailable")

    with pytest.raises(OSError, match="Backup disk unavailable"):
        database.initialize(before_migration=fail)
    with database.connect() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 0
        assert "disposition" not in {
            row[1]
            for row in connection.execute("PRAGMA table_info(deletion_operation)")
        }


def test_new_database_needs_no_migration_backup(tmp_path: Path):
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize(
        before_migration=lambda: pytest.fail("Fresh database needs no backup")
    )
