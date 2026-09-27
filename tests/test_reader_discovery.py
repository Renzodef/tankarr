from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from tankarr.app import create_app
from tankarr.config import Settings
from tankarr.reader_discovery import discover_reader


def reader_settings(tmp_path: Path, **overrides: object) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "library",
        monitor_enabled=False,
        metadata_enabled=False,
        **overrides,
    )


def response(request: httpx.Request, status: int, **kwargs: object) -> httpx.Response:
    return httpx.Response(status, request=request, **kwargs)


@pytest.mark.asyncio
async def test_discovers_stump_and_verifies_saved_credentials(tmp_path: Path):
    settings = reader_settings(
        tmp_path,
        reader_kind="stump",
        reader_url="http://192.0.2.50:10801",
        reader_username="fixture-user",
        reader_password="not-returned",
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/health" and request.url.host in {
            "stump",
            "192.0.2.50",
        }:
            return response(request, 200, json={"status": "ok", "dependencies": {}})
        if request.url.path == "/api/v2/auth/login" and request.url.host in {
            "stump",
            "192.0.2.50",
        }:
            return response(request, 200, json={"accessToken": "jwt"})
        return response(request, 404, text="not found")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await discover_reader(settings, client=client)

    assert result["selected"] == {
        "kind": "stump",
        "label": "Stump",
        "internal_url": "http://stump:10801",
        "browser_url": "http://192.0.2.50:10801",
        "browser_port": 10801,
        "credentials": "accepted",
        "detail": "Stump found; saved credentials accepted",
    }
    assert "not-returned" not in json.dumps(result)


@pytest.mark.asyncio
async def test_detected_reader_can_fill_urls_before_credentials_exist(tmp_path: Path):
    settings = reader_settings(tmp_path)

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url == "http://stump:10801/api/v2/health":
            return response(request, 200, json={"status": "ok"})
        return response(request, 404, text="not found")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await discover_reader(settings, client=client)

    assert result["found"] is True
    assert result["selected"]["kind"] == "stump"
    assert result["selected"]["credentials"] == "required"
    assert result["selected"]["browser_url"] is None


@pytest.mark.asyncio
async def test_multiple_unconfigured_readers_are_not_chosen_arbitrarily(tmp_path: Path):
    settings = reader_settings(tmp_path)

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url == "http://stump:10801/api/v2/health":
            return response(request, 200, json={"status": "ok"})
        if request.url.host == "komga" and request.url.path == "/":
            return response(
                request,
                200,
                headers={"content-type": "text/html"},
                text="<!doctype html><title>Komga</title>",
            )
        if request.url.host == "komga" and request.url.path == "/api/v1/libraries":
            return response(request, 401, json={"status": 401})
        return response(request, 404, text="not found")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await discover_reader(settings, client=client)

    assert [item["kind"] for item in result["matches"]] == ["stump", "komga"]
    assert result["selected"] is None
    assert "select one" in result["message"]


@pytest.mark.asyncio
async def test_current_reader_wins_when_several_are_available(tmp_path: Path):
    settings = reader_settings(
        tmp_path,
        reader_kind="komga",
        reader_url="http://192.0.2.50:25600",
        reader_internal_url="http://komga:25600",
        reader_api_key="reader-key",
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url == "http://stump:10801/api/v2/health":
            return response(request, 200, json={"status": "ok"})
        if request.url.host == "komga" and request.url.path == "/api/v1/libraries":
            assert request.headers["X-API-Key"] == "reader-key"
            return response(request, 200, json=[])
        return response(request, 404, text="not found")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await discover_reader(settings, client=client)

    assert [item["kind"] for item in result["matches"]] == ["stump", "komga"]
    assert result["selected"]["kind"] == "komga"
    assert result["selected"]["credentials"] == "accepted"


def test_discovery_endpoint_returns_the_non_secret_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    settings = reader_settings(tmp_path)
    identity = "0123456789abcdef0123456789abcdef"
    settings.data_dir.mkdir(parents=True)
    settings.library_dir.mkdir(parents=True)
    for root in (settings.data_dir, settings.library_dir):
        (root / ".tankarr-library-id").write_text(f"{identity}\n", encoding="utf-8")
    expected = {
        "found": True,
        "selected": {
            "kind": "stump",
            "label": "Stump",
            "internal_url": "http://stump:10801",
            "browser_url": None,
            "browser_port": 10801,
            "credentials": "required",
            "detail": "Stump found; enter a username and password",
        },
        "matches": [],
        "checked": 3,
        "message": "Stump found; enter a username and password",
    }

    async def fake_discovery(candidate: Settings) -> dict[str, object]:
        assert candidate is settings
        return expected

    monkeypatch.setattr("tankarr.app.discover_local_reader", fake_discovery)
    with TestClient(create_app(settings)) as client:
        result = client.post("/api/settings/discover/reader")

    assert result.status_code == 200
    assert result.json() == expected
