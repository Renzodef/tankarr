from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from tankarr.app import create_app
from tankarr.config import Settings
from tankarr.database import Database
from tankarr.notify import NtfyNotifier
from tankarr.settings_store import apply_setting_overrides, update_settings

BASE_URL = "http://ntfy.test"


@pytest.mark.asyncio
@respx.mock
async def test_ntfy_test_delivery_publishes_a_real_probe():
    route = respx.post(BASE_URL).mock(
        return_value=httpx.Response(200, json={"id": "message-1"})
    )
    notifier = NtfyNotifier(Settings(ntfy_url=BASE_URL, ntfy_topic="tankarr-test"))

    result = await notifier.test_delivery()

    assert result == {
        "ok": True,
        "status_code": 200,
        "topic": "tankarr-test",
    }
    assert route.called
    request = route.calls[0].request
    assert json.loads(request.content) == {
        "topic": "tankarr-test",
        "title": "Tankarr connection test",
        "message": (
            "Tankarr successfully reached this ntfy topic. Notifications are ready."
        ),
        "tags": ["test_tube", "white_check_mark"],
        "priority": 3,
    }


@pytest.mark.asyncio
@respx.mock
async def test_ntfy_event_toggles_suppress_only_automatic_notifications():
    notifier = NtfyNotifier(
        Settings(
            ntfy_url=BASE_URL,
            ntfy_topic="tankarr",
            ntfy_on_chapter_imported=False,
            ntfy_on_download_failed=False,
        )
    )

    imported = await notifier.chapter_imported(
        {"title": "Example"},
        {"chapter": "1", "language": "en", "provider": "mangadex"},
    )
    failed = await notifier.job_failed("Example", "network error")

    assert imported is False
    assert failed is False
    assert not respx.calls


@pytest.mark.asyncio
@respx.mock
async def test_ntfy_import_names_volumes_and_chapters_without_special_fallback():
    route = respx.post(BASE_URL).mock(
        return_value=httpx.Response(200, json={"id": "message-import"})
    )
    notifier = NtfyNotifier(Settings(ntfy_url=BASE_URL, ntfy_topic="tankarr"))

    delivered = await notifier.chapter_imported(
        {"title": "Example"},
        {
            "volume": "12",
            "chapter": "99.5",
            "language": "en",
            "provider": "prowlarr",
        },
    )

    assert delivered is True
    request = route.calls[0].request
    assert json.loads(request.content) == {
        "topic": "tankarr",
        "title": "Example — Volume 12 · Chapter 99.5",
        "message": "Imported [en] from prowlarr",
        "tags": ["books", "white_check_mark"],
        "priority": 3,
    }


@pytest.mark.asyncio
@respx.mock
async def test_ntfy_probe_returns_safe_http_failure():
    respx.post(BASE_URL).mock(return_value=httpx.Response(403))
    notifier = NtfyNotifier(Settings(ntfy_url=BASE_URL, ntfy_topic="tankarr"))

    result = await notifier.test_delivery()

    assert result == {
        "ok": False,
        "error": "ntfy rejected the test with HTTP 403",
    }
    assert BASE_URL not in str(result)


def test_ntfy_trigger_settings_roundtrip(tmp_path: Path):
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    settings = Settings(data_dir=tmp_path / "data", library_dir=tmp_path / "library")

    applied = update_settings(
        settings,
        database,
        {
            "ntfy_on_chapter_imported": "false",
            "ntfy_on_download_failed": "true",
        },
    )

    assert applied == ["ntfy_on_chapter_imported", "ntfy_on_download_failed"]
    assert settings.ntfy_on_chapter_imported is False
    assert settings.ntfy_on_download_failed is True

    fresh = Settings(data_dir=tmp_path / "data", library_dir=tmp_path / "library")
    apply_setting_overrides(fresh, database)
    assert fresh.ntfy_on_chapter_imported is False
    assert fresh.ntfy_on_download_failed is True


@respx.mock
def test_ntfy_test_endpoint_uses_unsaved_url_and_topic(tmp_path: Path):
    route = respx.post(BASE_URL).mock(
        return_value=httpx.Response(200, json={"id": "message-2"})
    )
    settings = Settings(
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "library",
        monitor_enabled=False,
        metadata_enabled=False,
        suwayomi_enabled=False,
        jam_enabled=False,
    )
    client = TestClient(create_app(settings))

    response = client.post(
        "/api/settings/test/ntfy",
        json={"ntfy_url": BASE_URL, "ntfy_topic": "draft-topic"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "status_code": 200,
        "topic": "draft-topic",
    }
    assert route.called
    assert settings.ntfy_url is None
    assert settings.ntfy_topic == "tankarr"
    client.close()
