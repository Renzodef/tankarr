import json
import sqlite3
import zipfile

import pytest

from tankarr.backups import ApplicationBackups, restore_bundle, verify_bundle
from tankarr.config import Settings, get_settings
from tankarr.database import Database


def fixture(tmp_path):
    data = tmp_path / "live"
    data.mkdir()
    settings = Settings(
        _env_file=None,
        data_dir=data,
        auth_username="fixture",
        auth_password="literal-${PASSWORD}",
        monitor_enabled=True,
    )
    database = Database(settings.database_path)
    database.initialize()
    database.save_setting("monitor_enabled", "true")
    with sqlite3.connect(settings.database_path) as connection:
        connection.execute(
            "INSERT INTO download_job (manga_id,chapter_id,requested_language,status,message,planned_path,created_at,updated_at) VALUES ('fixture','fixture-chapter','en','queued','pending','/library/keep.cbz','now','now')"
        )
    (data / "metadata.env").write_text('TANKARR_AUTH_PASSWORD="literal-${PASSWORD}"\n')
    (data / "auth-sessions.sqlite3").write_bytes(b"never export active sessions")
    (data / "import-operation.json").write_text('{"status":"running"}')
    (data / ".tankarr-library-id").write_text("fixture-library")
    return settings, ApplicationBackups(settings, tmp_path / "external")


def test_application_bundle_is_private_verified_and_restores_in_safe_mode(
    tmp_path, monkeypatch
):
    settings, backups = fixture(tmp_path)
    result = backups.create()
    bundle = backups.export_path(result["name"])
    assert bundle.stat().st_mode & 0o777 == 0o600
    assert "auth-sessions.sqlite3" not in verify_bundle(bundle)["files"]
    destination = tmp_path / "restored"
    restored = restore_bundle(bundle, destination)
    assert restored["started"] is False
    assert not (destination / "import-operation.json").exists()
    assert (destination / "restored-import-operation.json").is_file()
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in destination.iterdir())
    assert (destination / ".tankarr-library-id").read_text() == "fixture-library"
    with sqlite3.connect(destination / "tankarr.sqlite3") as connection:
        assert connection.execute(
            "SELECT value FROM setting WHERE key='monitor_enabled'"
        ).fetchone() == ("false",)
        assert connection.execute(
            "SELECT status,planned_path FROM download_job"
        ).fetchone() == ("failed", "/library/keep.cbz")
    monkeypatch.setenv("TANKARR_DATA_DIR", str(destination))
    get_settings.cache_clear()
    try:
        config = get_settings()
        assert config.restored_safe_mode is True
        assert config.auth_password == settings.auth_password
        assert config.monitor_enabled is False
    finally:
        get_settings.cache_clear()


def test_restore_refuses_existing_directory_and_preserves_live_data(tmp_path):
    settings, backups = fixture(tmp_path)
    result = backups.create()
    before = settings.database_path.read_bytes()
    with pytest.raises(FileExistsError):
        restore_bundle(backups.export_path(result["name"]), settings.data_dir)
    assert settings.database_path.read_bytes() == before


@pytest.mark.parametrize("name", ["../escape", "auth-sessions.sqlite3"])
def test_unexpected_backup_members_are_rejected(tmp_path, name):
    _settings, backups = fixture(tmp_path)
    bundle = backups.export_path(backups.create()["name"])
    with zipfile.ZipFile(bundle, "a") as archive:
        archive.writestr(name, "not allowed")
    with pytest.raises(ValueError, match="unexpected paths"):
        restore_bundle(bundle, tmp_path / "restored")
    assert not (tmp_path / "restored").exists()


def test_checksum_tampering_cannot_publish_a_restore(tmp_path):
    _settings, backups = fixture(tmp_path)
    bundle = backups.export_path(backups.create()["name"])
    altered = tmp_path / "altered.zip"
    with zipfile.ZipFile(bundle) as source, zipfile.ZipFile(altered, "w") as target:
        for name in source.namelist():
            data = source.read(name)
            if name == "manifest.json":
                manifest = json.loads(data)
                manifest["files"]["metadata.env"]["sha256"] = "0" * 64
                data = json.dumps(manifest).encode()
            target.writestr(name, data)
    with pytest.raises(ValueError, match="checksum"):
        restore_bundle(altered, tmp_path / "restored")
    assert not (tmp_path / "restored").exists()


def test_failed_bundle_does_not_prune_previous_verified_backup(tmp_path, monkeypatch):
    _settings, backups = fixture(tmp_path)
    original = backups.create()
    monkeypatch.setattr(
        "tankarr.backups.verify_bundle",
        lambda _path: (_ for _ in ()).throw(OSError("verify failed")),
    )
    with pytest.raises(OSError):
        backups.create()
    assert [entry["name"] for entry in backups.list()] == [original["name"]]


def test_application_retention_defaults_to_seven_and_tracks_setting_changes(tmp_path):
    settings, backups = fixture(tmp_path)
    assert backups.retain == 7
    settings.backup_retention_count = 2
    names = [backups.create()["name"] for _ in range(3)]
    assert {entry["name"] for entry in backups.list()} == set(names[-2:])
    settings.backup_retention_count = 1
    latest = backups.create()["name"]
    assert [entry["name"] for entry in backups.list()] == [latest]


def test_explicit_application_retention_override_remains_supported(tmp_path):
    settings, backups = fixture(tmp_path)
    custom = ApplicationBackups(settings, backups.directory, retain=2)
    settings.backup_retention_count = 1
    for _ in range(3):
        custom.create()
    assert len(custom.list()) == 2


def test_pre_migration_backup_preserves_old_schema_and_effective_overrides(tmp_path):
    from tankarr.backups import create_pre_migration_backup

    settings, _backups = fixture(tmp_path)
    settings.backup_directory = tmp_path / "pre-migration"
    database = Database(settings.database_path)
    database.save_setting("backup_retention_count", "2")
    database.save_setting("monitor_interval_seconds", "120")
    before = database.get_setting_overrides()
    result = create_pre_migration_backup(settings)
    with zipfile.ZipFile(settings.backup_directory / result["name"]) as archive:
        effective = json.loads(archive.read("effective-settings.json"))
    assert effective["backup_retention_count"] == 2
    assert effective["monitor_interval_seconds"] == 120
    assert settings.backup_retention_count == 7
    assert database.get_setting_overrides() == before


def test_pre_migration_backup_skips_missing_database_and_supports_legacy_schema(
    tmp_path,
):
    from tankarr.backups import create_pre_migration_backup

    settings = Settings(_env_file=None, data_dir=tmp_path / "legacy")
    settings.data_dir.mkdir()
    assert create_pre_migration_backup(settings) is None
    with sqlite3.connect(settings.database_path) as connection:
        connection.execute("CREATE TABLE legacy (value TEXT)")
        connection.execute("INSERT INTO legacy VALUES ('preserved')")
    result = create_pre_migration_backup(settings)
    assert result["verified"]
    with zipfile.ZipFile(settings.data_dir / "backups" / result["name"]) as archive:
        snapshot = tmp_path / "snapshot.sqlite3"
        snapshot.write_bytes(archive.read("tankarr.sqlite3"))
    with sqlite3.connect(snapshot) as connection:
        assert connection.execute("SELECT * FROM legacy").fetchall() == [("preserved",)]
        assert not connection.execute(
            "SELECT name FROM sqlite_master WHERE name='setting'"
        ).fetchall()


def test_pre_migration_backup_propagates_failure_without_changing_database(
    tmp_path, monkeypatch
):
    from tankarr.backups import create_pre_migration_backup

    settings, _backups = fixture(tmp_path)
    before = settings.database_path.read_bytes()
    monkeypatch.setattr(
        ApplicationBackups,
        "create",
        lambda _self: (_ for _ in ()).throw(OSError("No backup space")),
    )
    with pytest.raises(OSError, match="No backup space"):
        create_pre_migration_backup(settings)
    assert settings.database_path.read_bytes() == before
