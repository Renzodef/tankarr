from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tankarr.app import create_app
from tankarr.config import Settings
from tankarr.database import Database
from tankarr.settings_store import (
    SECRET_PLACEHOLDER,
    apply_setting_overrides,
    load_metadata_secret_overrides,
    preview_settings,
    settings_view,
    update_settings,
)
from tests.test_queue_management import seed_manga


def test_settings_roundtrip_masking_and_validation(tmp_path: Path):
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    settings = Settings(data_dir=tmp_path / "data", library_dir=tmp_path / "library")

    applied = update_settings(
        settings,
        database,
        {
            "download_pipeline_max": "9",
            "monitor_interval_seconds": "600",
            "wanted_search_enabled": "true",
            "wanted_search_interval_seconds": "7200",
            "qbittorrent_username": "admin",
            "qbittorrent_password": "hunter2",
            "qbittorrent_url": "http://qbittorrent:8080",
        },
    )
    assert applied == [
        "download_pipeline_max",
        "monitor_interval_seconds",
        "qbittorrent_password",
        "qbittorrent_url",
        "qbittorrent_username",
        "wanted_search_enabled",
        "wanted_search_interval_seconds",
    ]
    assert settings.monitor_interval_seconds == 600
    assert settings.download_pipeline_max == 9
    assert settings.wanted_search_enabled is True
    assert settings.wanted_search_interval_seconds == 7200
    assert settings.qbittorrent_password == "hunter2"

    view = settings_view(settings, database)
    assert view["qbittorrent_password"]["value"] == SECRET_PLACEHOLDER
    assert view["monitor_interval_seconds"]["overridden"] is True
    assert view["default_language"]["overridden"] is False

    # A masked secret sent back means "keep the stored value".
    update_settings(settings, database, {"qbittorrent_password": SECRET_PLACEHOLDER})
    assert settings.qbittorrent_password == "hunter2"

    # Clearing an optional field removes the value.
    update_settings(settings, database, {"qbittorrent_url": ""})
    assert settings.qbittorrent_url is None

    with pytest.raises(ValueError):
        update_settings(settings, database, {"monitor_interval_seconds": "1"})
    with pytest.raises(ValueError):
        update_settings(settings, database, {"wanted_search_interval_seconds": "60"})
    with pytest.raises(ValueError):
        update_settings(settings, database, {"download_pipeline_max": "65"})
    with pytest.raises(ValueError):
        update_settings(settings, database, {"unknown_key": "x"})
    with pytest.raises(ValueError, match="local HTTP"):
        update_settings(
            settings,
            database,
            {"qbittorrent_url": "https://attacker.example/collect"},
        )
    with pytest.raises(ValueError, match="without credentials"):
        update_settings(
            settings,
            database,
            {"qbittorrent_url": "http://user:secret@qbittorrent:8080"},
        )

    update_settings(settings, database, {"qbittorrent_url": "http://100.64.0.10:8080"})
    assert settings.qbittorrent_url == "http://100.64.0.10:8080"

    # A fresh process re-applies persisted overrides.
    fresh = Settings(data_dir=tmp_path / "data", library_dir=tmp_path / "library")
    apply_setting_overrides(fresh, database)
    assert fresh.monitor_interval_seconds == 600
    assert fresh.download_pipeline_max == 9
    assert fresh.wanted_search_enabled is True
    assert fresh.wanted_search_interval_seconds == 7200
    load_metadata_secret_overrides(fresh)
    assert fresh.qbittorrent_password == "hunter2"


def test_retired_edition_settings_cannot_restore_publisher_gating(tmp_path: Path):
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    retired = {
        "prefer_official_releases": "true",
        "release_acquisition_policy": "official_only",
        "official_upgrade_enabled": "true",
        "official_edition_alignment_enabled": "true",
    }
    for key, value in retired.items():
        database.save_setting(key, value)
    settings = Settings(data_dir=tmp_path / "data", library_dir=tmp_path / "library")
    apply_setting_overrides(settings, database)
    assert not set(retired) & settings_view(settings, database).keys()
    for key in retired:
        with pytest.raises(ValueError):
            update_settings(settings, database, {key: retired[key]})


def test_search_language_profile_is_normalized_and_keeps_default_enabled(
    tmp_path: Path,
):
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    settings = Settings(data_dir=tmp_path / "data", library_dir=tmp_path / "library")

    update_settings(
        settings,
        database,
        {"search_languages": "IT, en,IT", "default_language": "it"},
    )
    assert settings.search_language_codes == ("it", "en")
    assert settings.default_language == "it"

    with pytest.raises(ValueError, match="Default language"):
        update_settings(settings, database, {"search_languages": "en"})
    with pytest.raises(ValueError, match="Unsupported translation language"):
        update_settings(settings, database, {"search_languages": "en,xx"})


def test_fresh_install_enables_only_english_search_by_default():
    settings = Settings()

    assert settings.default_language == "en"
    assert settings.search_language_codes == ("en",)


def test_connection_preview_uses_unsaved_values_without_mutating_settings(
    tmp_path: Path,
):
    settings = Settings(data_dir=tmp_path / "data", library_dir=tmp_path / "library")
    settings.qbittorrent_url = "http://qbittorrent:8080"
    settings.qbittorrent_username = "saved-user"
    settings.qbittorrent_password = "saved-password"

    candidate = preview_settings(
        settings,
        {
            "qbittorrent_url": "http://nas.local:8080",
            "qbittorrent_username": "draft-user",
            "qbittorrent_password": "draft-password",
            "qbittorrent_category": "tankarr-test",
        },
        allowed={
            "qbittorrent_url",
            "qbittorrent_username",
            "qbittorrent_password",
            "qbittorrent_category",
        },
    )

    assert candidate.qbittorrent_url == "http://nas.local:8080"
    assert candidate.qbittorrent_username == "draft-user"
    assert candidate.qbittorrent_password == "draft-password"
    assert settings.qbittorrent_url == "http://qbittorrent:8080"
    assert settings.qbittorrent_username == "saved-user"
    assert settings.qbittorrent_password == "saved-password"

    with pytest.raises(ValueError, match="cannot be used"):
        preview_settings(
            settings,
            {"komga_url": "http://komga:25600"},
            allowed={"qbittorrent_url"},
        )


def test_download_provider_settings_roundtrip_and_keep_password_masked(
    tmp_path: Path,
):
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    settings = Settings(data_dir=tmp_path / "data", library_dir=tmp_path / "library")

    applied = update_settings(
        settings,
        database,
        {
            "suwayomi_enabled": "true",
            "suwayomi_url": "http://suwayomi:4567/",
            "suwayomi_username": "tankarr",
            "suwayomi_password": "provider-secret",
            "suwayomi_language": "EN",
            "suwayomi_source_ids": "101, 202",
        },
    )

    assert "suwayomi_password" in applied
    assert settings.suwayomi_url == "http://suwayomi:4567"
    assert settings.suwayomi_language == "en"
    assert settings.suwayomi_source_id_set == frozenset({101, 202})
    assert settings_view(settings, database)["suwayomi_password"]["value"] == (
        SECRET_PLACEHOLDER
    )
    assert "suwayomi_password" not in database.get_setting_overrides()
    assert "TANKARR_SUWAYOMI_PASSWORD" in settings.metadata_secrets_path.read_text(
        encoding="utf-8"
    )

    fresh = Settings(data_dir=tmp_path / "data", library_dir=tmp_path / "library")
    load_metadata_secret_overrides(fresh)
    apply_setting_overrides(fresh, database)
    assert fresh.suwayomi_enabled is True
    assert fresh.suwayomi_username == "tankarr"
    assert fresh.suwayomi_password == "provider-secret"

    with pytest.raises(ValueError, match="local service hostname"):
        update_settings(
            settings,
            database,
            {"suwayomi_url": "https://public.example"},
        )
    with pytest.raises(ValueError, match="set together"):
        update_settings(settings, database, {"suwayomi_password": ""})
    with pytest.raises(ValueError, match="positive numeric IDs"):
        update_settings(settings, database, {"suwayomi_source_ids": "101,nope"})


def test_komga_shortcut_settings_are_optional_masked_and_live(tmp_path: Path):
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    settings = Settings(data_dir=tmp_path / "data", library_dir=tmp_path / "library")

    applied = update_settings(
        settings,
        database,
        {
            "komga_link_enabled": "true",
            "komga_url": "http://192.0.2.50:25600/",
            "komga_library_id": "library-1",
            "komga_auth_method": "basic",
            "komga_username": "reader@example.test",
            "komga_password": "reader-secret",
            "komga_refresh_interval_minutes": "120",
        },
    )

    assert "komga_link_enabled" in applied
    assert settings.komga_link_enabled is True
    assert settings.komga_url == "http://192.0.2.50:25600"
    assert settings.komga_effective_auth_method == "basic"
    assert settings.komga_refresh_interval_minutes == 120
    assert settings_view(settings, database)["komga_auth_method"]["value"] == "basic"
    assert settings_view(settings, database)["komga_password"]["value"] == (
        SECRET_PLACEHOLDER
    )
    assert "komga_password" not in database.get_setting_overrides()

    update_settings(settings, database, {"komga_url": "https://reader.example.test/"})
    assert settings.komga_url == "https://reader.example.test"

    fresh = Settings(data_dir=tmp_path / "data", library_dir=tmp_path / "library")
    load_metadata_secret_overrides(fresh)
    apply_setting_overrides(fresh, database)
    assert fresh.komga_link_enabled is True
    assert fresh.komga_auth_method == "basic"
    assert fresh.komga_password == "reader-secret"
    assert fresh.komga_refresh_interval_minutes == 120


def test_komga_authentication_method_requires_only_the_selected_credentials(
    tmp_path: Path,
):
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    settings = Settings(
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "library",
        komga_url="http://komga:25600",
        komga_username="reader@example.test",
        komga_password="reader-secret",
    )

    # Legacy configurations are presented as one explicit choice in the UI.
    assert settings.komga_auth_method == "auto"
    assert settings_view(settings, database)["komga_auth_method"]["value"] == "basic"

    with pytest.raises(ValueError, match="API key is required"):
        update_settings(
            settings,
            database,
            {"komga_link_enabled": "true", "komga_auth_method": "api_key"},
        )

    applied = update_settings(
        settings,
        database,
        {
            "komga_link_enabled": "true",
            "komga_auth_method": "api_key",
            "komga_api_key": "dedicated-reader-key",
        },
    )

    assert "komga_auth_method" in applied
    assert settings.komga_effective_auth_method == "api_key"
    assert settings.komga_username == "reader@example.test"
    assert settings.komga_password == "reader-secret"
    assert settings_view(settings, database)["komga_api_key"]["value"] == (
        SECRET_PLACEHOLDER
    )


def test_komga_shortcut_requires_a_complete_read_only_connection(tmp_path: Path):
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    settings = Settings(data_dir=tmp_path / "data", library_dir=tmp_path / "library")

    with pytest.raises(ValueError, match="API key"):
        update_settings(
            settings,
            database,
            {
                "komga_link_enabled": "true",
                "komga_url": "http://komga:25600",
            },
        )


def test_prowlarr_settings_roundtrip_and_keep_api_key_masked(tmp_path: Path):
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    settings = Settings(data_dir=tmp_path / "data", library_dir=tmp_path / "library")

    applied = update_settings(
        settings,
        database,
        {
            "prowlarr_enabled": "true",
            "prowlarr_url": "http://prowlarr:9696/",
            "prowlarr_api_key": "0123456789abcdef0123456789abcdef",
            "prowlarr_indexer_ids": "10,18,10",
            "prowlarr_categories": "7000,7020,7030",
        },
    )

    assert "prowlarr_api_key" in applied
    assert settings.prowlarr_url == "http://prowlarr:9696"
    assert settings.prowlarr_indexer_id_set == frozenset({10, 18})
    assert settings.prowlarr_category_id_set == frozenset({7000, 7020, 7030})
    assert settings_view(settings, database)["prowlarr_api_key"]["value"] == (
        SECRET_PLACEHOLDER
    )
    assert "prowlarr_api_key" not in database.get_setting_overrides()
    assert "TANKARR_PROWLARR_API_KEY" in settings.metadata_secrets_path.read_text(
        encoding="utf-8"
    )

    fresh = Settings(data_dir=tmp_path / "data", library_dir=tmp_path / "library")
    load_metadata_secret_overrides(fresh)
    apply_setting_overrides(fresh, database)
    assert fresh.prowlarr_enabled is True
    assert fresh.prowlarr_api_key == "0123456789abcdef0123456789abcdef"

    with pytest.raises(ValueError, match="local service hostname"):
        update_settings(
            settings,
            database,
            {"prowlarr_url": "https://attacker.example"},
        )
    with pytest.raises(ValueError, match="positive numeric IDs"):
        update_settings(settings, database, {"prowlarr_indexer_ids": "10,nope"})
    with pytest.raises(ValueError, match="positive numeric IDs"):
        update_settings(settings, database, {"prowlarr_categories": "7000,-1"})
    with pytest.raises(ValueError, match="API key is required"):
        update_settings(settings, database, {"prowlarr_api_key": ""})


def test_provider_settings_hot_reload_shared_registry_without_restart(tmp_path: Path):
    settings = Settings(
        host="127.0.0.1",
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "library",
        monitor_enabled=False,
        metadata_enabled=False,
        suwayomi_enabled=False,
    )
    identity = "0123456789abcdef0123456789abcdef"
    settings.data_dir.mkdir(parents=True)
    settings.library_dir.mkdir(parents=True)
    (settings.data_dir / ".tankarr-library-id").write_text(
        f"{identity}\n", encoding="utf-8"
    )
    (settings.library_dir / ".tankarr-library-id").write_text(
        f"{identity}\n", encoding="utf-8"
    )

    app = create_app(settings)
    with TestClient(app) as client:
        assert "suwayomi" not in {
            item["name"]
            for item in client.get("/api/system/health").json()["providers"]
        }
        enabled = client.put(
            "/api/settings",
            json={
                "suwayomi_enabled": "true",
                "suwayomi_mode": "external",
                "suwayomi_url": "http://suwayomi:4567",
            },
        )
        assert enabled.status_code == 200, enabled.text
        assert "suwayomi" in {
            item["name"]
            for item in client.get("/api/system/health").json()["providers"]
        }
        disabled_probe = client.post(
            "/api/settings/test/provider/suwayomi",
            json={"suwayomi_enabled": "false"},
        )
        assert disabled_probe.status_code == 200
        assert disabled_probe.json() == {
            "ok": False,
            "provider": "suwayomi",
            "error": "suwayomi is disabled",
        }
        disabled = client.put("/api/settings", json={"suwayomi_enabled": "false"})
        assert disabled.status_code == 200, disabled.text
        assert "suwayomi" not in {
            item["name"]
            for item in client.get("/api/system/health").json()["providers"]
        }


def test_authentication_is_configurable_and_password_stays_out_of_database(
    tmp_path: Path,
):
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    settings = Settings(data_dir=tmp_path / "data", library_dir=tmp_path / "library")

    applied = update_settings(
        settings,
        database,
        {
            "auth_method": "forms",
            "auth_username": "fixture-user",
            "auth_password": "replacement-password",
        },
    )

    assert applied == ["auth_method", "auth_password", "auth_username"]
    assert settings.auth_configured is True
    assert settings_view(settings, database)["auth_password"]["value"] == (
        SECRET_PLACEHOLDER
    )
    overrides = database.get_setting_overrides()
    assert overrides["auth_method"] == "forms"
    assert overrides["auth_username"] == "fixture-user"
    assert "auth_password" not in overrides
    assert "TANKARR_AUTH_PASSWORD" in settings.metadata_secrets_path.read_text(
        encoding="utf-8"
    )

    fresh = Settings(data_dir=tmp_path / "data", library_dir=tmp_path / "library")
    load_metadata_secret_overrides(fresh)
    apply_setting_overrides(fresh, database)
    assert fresh.auth_method == "forms"
    assert fresh.auth_username == "fixture-user"
    assert fresh.auth_password == "replacement-password"

    with pytest.raises(ValueError, match="set together"):
        update_settings(settings, database, {"auth_password": ""})
    with pytest.raises(ValueError, match="cannot be disabled"):
        update_settings(
            settings,
            database,
            {"auth_username": "", "auth_password": ""},
        )


def test_chapter_monitor_toggle_excludes_from_candidates(tmp_path: Path):
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    seed_manga(database, monitor_mode="all")
    assert [c["id"] for c in database.preferred_download_candidates("manga-1")] == [
        "chapter-1"
    ]
    updated = database.set_chapter_monitored("chapter-1", False)
    assert updated == 1
    assert database.get_chapter("chapter-1")["monitored"] is False
    assert database.preferred_download_candidates("manga-1") == []
    database.set_chapter_monitored("chapter-1", True)
    assert [c["id"] for c in database.preferred_download_candidates("manga-1")] == [
        "chapter-1"
    ]


def test_provider_priority_setting_normalizes_and_persists(tmp_path: Path):
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    settings = Settings(data_dir=tmp_path / "data", library_dir=tmp_path / "library")

    applied = update_settings(
        settings, database, {"provider_priority": " Suwayomi ,suwayomi "}
    )

    assert applied == ["provider_priority"]
    # Duplicates collapse and omitted providers keep their default relative
    # order after the explicit ones, so the order stays total.
    assert settings.provider_priority == "suwayomi"
    assert settings.provider_priority_order == ("suwayomi",)

    restored = Settings(data_dir=tmp_path / "data", library_dir=tmp_path / "library")
    apply_setting_overrides(restored, database)
    assert restored.provider_priority == "suwayomi"

    with pytest.raises(ValueError, match="Unknown download provider"):
        update_settings(settings, database, {"provider_priority": "mangadex,bogus"})
    assert settings.provider_priority == "suwayomi"


def test_wanted_reports_the_locked_share_of_unmapped_items():
    # TBATE 251 is on Tapas behind its paywall: dropped from the slots as
    # locked, but "not offered by any source" would be a lie - the publisher
    # has it. The entry says how many unmapped items are just locked.
    from tankarr.chapter_mapping import build_chapter_index

    manga = {
        "id": "m1",
        "title": "Paywalled",
        "preferred_language": "en",
        "monitor_mode": "all",
    }
    releases = [
        {
            "id": f"tapas-{number}",
            "manga_id": "m1",
            "volume": None,
            "chapter": str(number),
            "title": f"{number}. Episode",
            "language": "en",
            "provider": "suwayomi",
            "source_name": "Tapas (EN)",
            "downloaded": number < 6,
            "monitored": True,
            "release_unit": "chapter",
        }
        for number in range(1, 6)
    ] + [
        {
            "id": "tapas-6",
            "manga_id": "m1",
            "volume": None,
            "chapter": "6",
            "title": "\U0001f512 6. New Message",
            "language": "en",
            "provider": "suwayomi",
            "source_name": "Tapas (EN)",
            "downloaded": False,
            "monitored": True,
            "release_unit": "chapter",
        }
    ]
    index = build_chapter_index(manga, {"chapter_count": 6}, releases)

    # The locked episode is not a slot (it cannot be fetched) and not an
    # extra: it is counted apart, which is what lets Wanted say "locked at
    # the official source" instead of "offered by nobody".
    assert int(index["dropped_releases"].get("locked", 0)) == 1
    assert all(slot["chapter"] != "6" for slot in index["slots"])
