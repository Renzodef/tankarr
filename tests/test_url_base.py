from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from tankarr.app import create_app
from tankarr.config import Settings
from tankarr.url_base import normalize_url_base


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    frontend = tmp_path / "frontend"
    assets = frontend / "assets"
    assets.mkdir(parents=True)
    (frontend / "index.html").write_text("Tankarr UI", encoding="utf-8")
    (assets / "app.js").write_text("// Tankarr", encoding="utf-8")
    return Settings(
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "library",
        frontend_dir=frontend,
        monitor_enabled=False,
        metadata_enabled=False,
        update_check_enabled=False,
        **overrides,
    )


def test_the_url_base_is_normalised_to_one_leading_slash():
    assert normalize_url_base("tankarr/") == "/tankarr"
    assert normalize_url_base(" /Comics/tankarr/ ") == "/Comics/tankarr"
    assert normalize_url_base("") == ""
    assert normalize_url_base("/") == ""
    assert normalize_url_base(None) == ""
    for invalid in ("/tank arr", "/tankarr?x=1", "/tankarr#frag", "//tankarr", "/api"):
        with pytest.raises(ValueError):
            normalize_url_base(invalid)
    assert Settings(url_base="tankarr/").url_base == "/tankarr"
    with pytest.raises(ValidationError):
        Settings(url_base="/assets")


def test_interface_and_api_answer_under_the_base_only(tmp_path: Path):
    settings = _settings(tmp_path, url_base="/tankarr", auth_required=False)
    client = TestClient(create_app(settings))

    assert client.get("/tankarr/api/health").status_code == 200
    assert client.get("/tankarr/").text == "Tankarr UI"
    assert client.get("/tankarr/assets/app.js").text == "// Tankarr"
    assert client.get("/tankarr/api/manga").status_code == 200

    # The interface's relative asset URLs need the trailing slash.
    redirect = client.get("/tankarr", follow_redirects=False)
    assert redirect.status_code == 307
    assert redirect.headers["location"] == "/tankarr/"
    redirect = client.get("/tankarr?x=1", follow_redirects=False)
    assert redirect.headers["location"] == "/tankarr/?x=1"

    # Outside the base there is nothing, except the health checks a container
    # runtime or an uptime monitor asks for without knowing the base.
    assert client.get("/api/manga").status_code == 404
    assert client.get("/").status_code == 404
    assert client.get("/tankarrish/api/health").status_code == 404
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/ready").status_code in {200, 503}
    client.close()


def test_authentication_and_its_cookie_follow_the_base(tmp_path: Path):
    settings = _settings(
        tmp_path,
        url_base="/tankarr",
        auth_method="forms",
        auth_username="fixture-user",
        auth_password="correct horse battery staple",
    )
    client = TestClient(create_app(settings))

    assert client.get("/tankarr/api/manga").status_code == 401
    assert client.get("/tankarr/").text == "Tankarr UI"  # the login page shell
    login = client.post(
        "/tankarr/api/auth/login",
        json={"username": "fixture-user", "password": "correct horse battery staple"},
    )
    assert login.status_code == 200
    assert "Path=/tankarr" in login.headers["set-cookie"]
    assert client.get("/tankarr/api/manga").status_code == 200
    assert client.get("/tankarr/api/auth/status").json()["authenticated"] is True
    logout = client.post("/tankarr/api/auth/logout")
    assert "Path=/tankarr" in logout.headers["set-cookie"]
    assert client.get("/tankarr/api/manga").status_code == 401
    client.close()


def test_without_a_base_nothing_changes(tmp_path: Path):
    settings = _settings(tmp_path, auth_required=False)
    client = TestClient(create_app(settings))
    assert client.get("/api/health").status_code == 200
    assert client.get("/").text == "Tankarr UI"
    assert client.get("/tankarr/api/health").status_code == 200  # the SPA shell
    client.close()
