from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tankarr.app import create_app
from tankarr.config import Settings
from tankarr.database import Database
from tankarr.settings_store import (
    apply_setting_overrides,
    coerce_setting,
    update_settings,
)


def test_download_pause_is_persisted_and_resumable_without_disabling_imports(tmp_path):
    database = Database(tmp_path / "database.sqlite3")
    database.initialize()
    settings = Settings(data_dir=tmp_path)
    update_settings(settings, database, {"downloads_paused": True})
    restored = Settings(data_dir=tmp_path)
    apply_setting_overrides(restored, database)
    assert restored.downloads_paused is True
    assert restored.restored_safe_mode is False
    update_settings(restored, database, {"downloads_paused": False})
    restarted = Settings(data_dir=tmp_path)
    apply_setting_overrides(restarted, database)
    assert restarted.downloads_paused is False


@pytest.mark.parametrize("value", ["nan", "Infinity", "-Infinity", "1e9999"])
@pytest.mark.parametrize("name", ["download_concurrency", "monitor_interval_seconds"])
def test_nonfinite_settings_are_validation_errors(name, value):
    with pytest.raises(ValueError):
        coerce_setting(name, value)


@pytest.mark.parametrize("value", ["1.9", "1.000000000000000001", "2.5"])
def test_integer_settings_are_not_silently_truncated(value):
    with pytest.raises(ValueError, match="whole number"):
        coerce_setting("download_concurrency", value)


@pytest.mark.parametrize("value", ["2", "2.0", "2e0"])
def test_integral_legacy_values_remain_supported(value):
    assert coerce_setting("download_concurrency", value) == 2


def test_invalid_numeric_update_is_atomic_and_invalid_saved_values_are_discarded(
    tmp_path: Path,
):
    database = Database(tmp_path / "database.sqlite3")
    database.initialize()
    settings = Settings(data_dir=tmp_path)
    initial = settings.download_concurrency
    initial_overrides = database.get_setting_overrides()
    with pytest.raises(ValueError):
        update_settings(
            settings,
            database,
            {"download_concurrency": "2", "monitor_interval_seconds": "NaN"},
        )
    assert settings.download_concurrency == initial
    assert database.get_setting_overrides() == initial_overrides
    database.save_setting("download_concurrency", "Infinity")
    database.save_setting("monitor_interval_seconds", "NaN")
    apply_setting_overrides(settings, database)
    assert database.get_setting_overrides() == initial_overrides
    assert settings.download_concurrency == initial


def test_numeric_overflow_returns_400_instead_of_crashing_api(tmp_path: Path):
    app = create_app(
        Settings(
            data_dir=tmp_path, library_dir=tmp_path / "library", suwayomi_enabled=False
        )
    )
    client = TestClient(app)
    initial_overrides = app.state.database.get_setting_overrides()
    response = client.put("/api/settings", json={"download_concurrency": "Infinity"})
    assert response.status_code == 400
    assert app.state.database.get_setting_overrides() == initial_overrides
