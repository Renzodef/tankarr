from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from tankarr.app import create_app
from tankarr.config import Settings
from tankarr.updates import RELEASES_API, UpdateChecker, is_newer, parse_version


def test_versions_compare_numerically_and_ignore_prerelease_suffixes():
    assert parse_version("v1.2.0-beta.1") == (1, 2, 0)
    assert parse_version("0.9") == (0, 9)
    assert parse_version("nightly") is None
    assert is_newer("0.10.0", "0.9.0")
    assert is_newer("1.0", "0.9.9")
    assert not is_newer("0.9.0", "0.9.0")
    assert not is_newer("0.9", "0.9.0")
    assert not is_newer("0.8.5", "0.9.0")
    assert not is_newer(None, "0.9.0")
    assert not is_newer("garbage", "0.9.0")


@respx.mock
async def test_check_reports_a_newer_release_with_its_page():
    respx.get(RELEASES_API).mock(
        return_value=httpx.Response(
            200,
            json={"tag_name": "v9.9.9", "html_url": "https://example.test/rel/v9.9.9"},
        )
    )
    checker = UpdateChecker("0.9.0")
    status = await checker.check()
    assert status["update_available"] is True
    assert status["latest"] == "9.9.9"
    assert status["url"] == "https://example.test/rel/v9.9.9"
    assert status["error"] is None
    assert status["checked_at"]
    assert respx.calls.last.request.headers["user-agent"].startswith("Tankarr/")


@respx.mock
async def test_a_failed_check_is_reported_not_raised():
    respx.get(RELEASES_API).mock(return_value=httpx.Response(503))
    checker = UpdateChecker("0.9.0")
    status = await checker.check()
    assert status["update_available"] is False
    assert status["latest"] is None
    assert status["error"].startswith("HTTPStatusError")


async def test_a_disabled_checker_never_contacts_anything():
    checker = UpdateChecker("0.9.0", enabled=False)
    await asyncio.wait_for(checker.run(), timeout=1)
    assert checker.status() == {
        "enabled": False,
        "current": "0.9.0",
        "latest": None,
        "update_available": False,
        "url": None,
        "checked_at": None,
        "error": None,
    }


@pytest.mark.parametrize("enabled", [True, False])
def test_system_status_and_alerts_carry_the_update_state(tmp_path: Path, enabled):
    settings = Settings(
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "library",
        monitor_enabled=False,
        auth_required=False,
        update_check_enabled=enabled,
    )
    app = create_app(settings)
    with TestClient(app) as client:
        status = client.get("/api/system/status").json()
        assert status["update"]["enabled"] is enabled
        assert status["update"]["current"] == status["version"]
        assert status["update"]["update_available"] is False
        assert not [a for a in status["alerts"] if a["key"] == "update_available"]
