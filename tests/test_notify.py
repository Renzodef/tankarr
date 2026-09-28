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


@pytest.mark.asyncio
@respx.mock
async def test_webhook_receives_one_json_document_per_event_with_its_token():
    from tankarr.notify import Notifier

    route = respx.post("https://hooks.example.test/tankarr").mock(
        return_value=httpx.Response(204)
    )
    notifier = Notifier(
        Settings(
            webhook_url="https://hooks.example.test/tankarr",
            webhook_token="fixture-token",
        )
    )

    delivered = await notifier.job_failed("Example", "network error")

    assert delivered is True
    request = route.calls[0].request
    assert request.headers["authorization"] == "Bearer fixture-token"
    payload = json.loads(request.content)
    assert payload["application"] == "Tankarr"
    assert payload["event"] == "download_failed"
    assert payload["priority"] == "high"
    assert payload["title"] == "Download failed — Example"
    assert payload["message"] == "network error"
    assert payload["tags"] == ["books", "warning"]
    assert payload["data"] == {"manga_title": "Example"}
    assert payload["at"]


@pytest.mark.asyncio
@respx.mock
async def test_discord_embed_and_telegram_message_carry_the_event():
    from tankarr.notify import Notifier

    discord = respx.post("https://discord.test/api/webhooks/1/fixture-secret").mock(
        return_value=httpx.Response(204)
    )
    telegram = respx.post("https://api.telegram.org/botfixture-token/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    notifier = Notifier(
        Settings(
            discord_webhook_url="https://discord.test/api/webhooks/1/fixture-secret",
            telegram_bot_token="fixture-token",
            telegram_chat_id="12345",
        )
    )

    delivered = await notifier.chapter_imported(
        {"id": "m1", "title": "Example"},
        {"chapter": "3", "language": "en", "provider": "local"},
    )

    assert delivered is True
    body = json.loads(discord.calls[0].request.content)
    assert body["username"] == "Tankarr"
    assert body["embeds"][0]["title"] == "Example — Chapter 3"
    assert body["embeds"][0]["description"] == "Imported [en] from local"
    assert body["embeds"][0]["color"] == 0x3498DB
    assert json.loads(telegram.calls[0].request.content) == {
        "chat_id": "12345",
        "text": "Example — Chapter 3\n\nImported [en] from local",
        "disable_web_page_preview": True,
    }


@pytest.mark.asyncio
@respx.mock
async def test_apprise_uses_the_configuration_key_or_the_urls():
    from tankarr.notify import Notifier

    keyed = respx.post("http://apprise.test/notify/tankarr").mock(
        return_value=httpx.Response(200)
    )
    direct = respx.post("http://apprise.test/notify").mock(
        return_value=httpx.Response(200)
    )
    with_key = Notifier(
        Settings(apprise_url="http://apprise.test/", apprise_key="tankarr")
    )
    assert await with_key.send("Title", "Body", priority="high") is True
    assert json.loads(keyed.calls[0].request.content) == {
        "title": "Title",
        "body": "Body",
        "type": "failure",
        "format": "text",
    }

    with_urls = Notifier(
        Settings(
            apprise_url="http://apprise.test", apprise_urls="json://example.test/hook"
        )
    )
    assert await with_urls.send("Title", "Body") is True
    payload = json.loads(direct.calls[0].request.content)
    assert payload["type"] == "info"
    assert payload["urls"] == "json://example.test/hook"


@pytest.mark.asyncio
@respx.mock
async def test_a_failing_channel_hides_neither_the_others_nor_its_secret(caplog):
    from tankarr.notify import Notifier

    respx.post("https://discord.test/api/webhooks/1/fixture-secret").mock(
        return_value=httpx.Response(500)
    )
    ntfy = respx.post(BASE_URL).mock(return_value=httpx.Response(200))
    notifier = Notifier(
        Settings(
            ntfy_url=BASE_URL,
            ntfy_topic="tankarr",
            discord_webhook_url="https://discord.test/api/webhooks/1/fixture-secret",
        )
    )
    assert notifier.configured_channels() == {
        "ntfy": True,
        "webhook": False,
        "discord": True,
        "telegram": False,
        "apprise": False,
    }

    with caplog.at_level("WARNING"):
        delivered = await notifier.send("Title", "Body")

    assert delivered is True
    assert ntfy.called
    assert "HTTP 500" in caplog.text
    assert "fixture-secret" not in caplog.text
    probe = await notifier.test_delivery("discord")
    assert probe == {"ok": False, "error": "Discord rejected the test with HTTP 500"}
    assert await notifier.test_delivery("pager") == {
        "ok": False,
        "error": "Unknown notification channel",
    }
    assert await notifier.test_delivery("telegram") == {
        "ok": False,
        "error": "Set both the Telegram bot token and chat ID before testing",
    }


@pytest.mark.asyncio
async def test_the_event_switches_gate_every_channel():
    from tankarr.notify import Notifier

    notifier = Notifier(
        Settings(
            webhook_url="https://hooks.example.test/tankarr",
            ntfy_on_download_failed=False,
            ntfy_on_chapter_imported=False,
            ntfy_on_decision_needed=False,
        )
    )
    assert await notifier.job_failed("Example", "boom") is False
    assert (
        await notifier.chapter_imported({"title": "Example"}, {"chapter": "1"}) is False
    )
    assert await notifier.decision_needed("review", "detail") is False


@respx.mock
def test_notification_test_endpoint_probes_one_channel_with_unsaved_values(
    tmp_path: Path,
):
    route = respx.post("https://hooks.example.test/tankarr").mock(
        return_value=httpx.Response(200)
    )
    settings = Settings(
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "library",
        monitor_enabled=False,
        metadata_enabled=False,
        suwayomi_enabled=False,
        update_check_enabled=False,
    )
    client = TestClient(create_app(settings))

    response = client.post(
        "/api/settings/test/notifications/webhook",
        json={"webhook_url": "https://hooks.example.test/tankarr"},
    )
    assert response.status_code == 200
    assert response.json() == {"ok": True, "status_code": 200}
    assert route.called
    assert settings.webhook_url is None
    assert (
        client.post("/api/settings/test/notifications/pager", json={}).status_code
        == 404
    )
    refused = client.post(
        "/api/settings/test/notifications/webhook", json={"ntfy_url": BASE_URL}
    )
    assert refused.status_code == 400
    status = client.get("/api/system/status").json()
    assert status["notifications"]["webhook"] is False
    client.close()
