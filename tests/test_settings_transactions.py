from pathlib import Path

import pytest

import tankarr.settings_store as store
from tankarr.config import Settings
from tankarr.database import Database


def fixture(tmp_path):
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path,
        auth_username="old-user",
        auth_password="old-password",
    )
    database = Database(settings.database_path)
    database.initialize()
    store.update_settings(
        settings,
        database,
        {"auth_username": "old-user", "auth_password": "old-password"},
    )
    return settings, database


@pytest.mark.parametrize("after_write", [False, True])
def test_settings_file_failure_rolls_back_database_file_and_runtime(
    tmp_path: Path, monkeypatch, after_write
):
    settings, database = fixture(tmp_path)
    previous = settings.metadata_secrets_path.read_bytes()
    write = store._write_metadata_secrets

    def failing(*args):
        if after_write:
            write(*args)
        raise OSError("simulated settings disk failure")

    monkeypatch.setattr(store, "_write_metadata_secrets", failing)
    with pytest.raises(OSError):
        store.update_settings(
            settings,
            database,
            {"auth_username": "new-user", "auth_password": "new-password"},
        )
    assert settings.auth_username == "old-user"
    assert settings.auth_password == "old-password"
    assert database.get_setting_overrides()["auth_username"] == "old-user"
    assert settings.metadata_secrets_path.read_bytes() == previous
    assert not (tmp_path / ".settings-transaction.json").exists()


def test_settings_crash_before_commit_is_recovered_before_secrets_load(
    tmp_path: Path, monkeypatch
):
    settings, database = fixture(tmp_path)
    write = store._write_metadata_secrets

    class Crash(BaseException):
        pass

    def interrupted(*args):
        write(*args)
        raise Crash()

    monkeypatch.setattr(store, "_write_metadata_secrets", interrupted)
    with pytest.raises(Crash):
        store.update_settings(
            settings,
            database,
            {"auth_username": "new-user", "auth_password": "new-password"},
        )
    assert (tmp_path / ".settings-transaction.json").stat().st_mode & 0o777 == 0o600
    fresh = Settings(_env_file=None, data_dir=tmp_path)
    store.load_metadata_secret_overrides(fresh)
    store.apply_setting_overrides(fresh, database)
    assert fresh.auth_username == "old-user"
    assert fresh.auth_password == "old-password"


def test_settings_committed_journal_is_not_rolled_back_on_startup(
    tmp_path: Path, monkeypatch
):
    settings, database = fixture(tmp_path)
    unlink = Path.unlink

    def leave_journal(path, *args, **kwargs):
        if path.name == ".settings-transaction.json":
            raise OSError("simulated cleanup failure")
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", leave_journal)
    store.update_settings(
        settings,
        database,
        {"auth_username": "new-user", "auth_password": "new-password"},
    )
    monkeypatch.undo()
    fresh = Settings(_env_file=None, data_dir=tmp_path)
    store.load_metadata_secret_overrides(fresh)
    store.apply_setting_overrides(fresh, database)
    assert fresh.auth_username == "new-user"
    assert fresh.auth_password == "new-password"
    assert not (tmp_path / ".settings-transaction.json").exists()
