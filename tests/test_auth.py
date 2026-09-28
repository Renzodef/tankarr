from __future__ import annotations

import base64
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from tankarr.app import create_app
from tankarr.auth import AuthenticationManager, authenticate_basic_header
from tankarr.config import Settings


def _basic(username: str, password: str, *, scheme: str = "Basic") -> str:
    credentials = base64.b64encode(f"{username}:{password}".encode()).decode()
    return f"{scheme} {credentials}"


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
        **overrides,
    )


def test_authentication_protects_ui_docs_assets_and_api(tmp_path: Path):
    settings = _settings(
        tmp_path,
        auth_method="basic",
        auth_username="fixture-user",
        auth_password="correct horse battery staple",
    )
    client = TestClient(create_app(settings))

    for path in ("/", "/assets/app.js", "/docs", "/api/manga"):
        response = client.get(path)
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == (
            'Basic realm="Tankarr", charset="UTF-8"'
        )
        assert response.headers["cache-control"] == "no-store"

    assert (
        client.get("/api/manga", auth=("fixture-user", "wrong password")).status_code
        == 401
    )
    assert (
        client.get(
            "/api/manga", auth=("fixture-user", "correct horse battery staple")
        ).status_code
        == 200
    )
    assert (
        client.get("/", auth=("fixture-user", "correct horse battery staple")).text
        == "Tankarr UI"
    )
    client.close()


def test_forms_authentication_uses_login_page_cookie_and_logout(tmp_path: Path):
    settings = _settings(
        tmp_path,
        auth_method="forms",
        auth_username="fixture-user",
        auth_password="correct horse battery staple",
    )
    client = TestClient(create_app(settings))

    assert client.get("/").status_code == 200
    assert client.get("/assets/app.js").status_code == 200
    assert client.get("/api/manga").status_code == 401
    assert "www-authenticate" not in client.get("/api/manga").headers
    assert client.get("/api/auth/status").json() == {
        "configured": True,
        "method": "forms",
        "authenticated": False,
        "username": None,
    }

    rejected = client.post(
        "/api/auth/login",
        json={
            "username": "fixture-user",
            "password": "wrong password",
            "remember_me": False,
        },
    )
    assert rejected.status_code == 401
    assert "tankarr_session" not in rejected.cookies

    logged_in = client.post(
        "/api/auth/login",
        json={
            "username": "fixture-user",
            "password": "correct horse battery staple",
            "remember_me": True,
        },
    )
    assert logged_in.status_code == 200
    assert logged_in.json()["authenticated"] is True
    cookie = logged_in.headers["set-cookie"]
    old_token = logged_in.cookies["tankarr_session"]
    assert "tankarr_session=" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie
    assert "Max-Age=2592000" in cookie
    assert client.get("/api/manga").status_code == 200
    assert client.get("/api/auth/status").json()["username"] == "fixture-user"

    logged_out = client.post("/api/auth/logout")
    assert logged_out.status_code == 200
    assert client.get("/api/manga").status_code == 401
    assert (
        client.get(
            "/api/manga", headers={"Cookie": f"tankarr_session={old_token}"}
        ).status_code
        == 401
    )
    restarted = AuthenticationManager(settings)
    assert not restarted.validate_session(old_token)
    client.close()


def test_forms_auth_keeps_basic_fallback_for_automation(tmp_path: Path):
    settings = _settings(
        tmp_path,
        auth_method="forms",
        auth_username="fixture-user",
        auth_password="automation-password",
    )
    client = TestClient(create_app(settings))

    assert (
        client.get(
            "/api/manga", auth=("fixture-user", "automation-password")
        ).status_code
        == 200
    )
    client.close()


def test_forms_session_is_invalid_after_password_change_or_tampering(tmp_path: Path):
    settings = _settings(
        tmp_path,
        auth_method="forms",
        auth_username="fixture-user",
        auth_password="initial-password",
    )
    manager = AuthenticationManager(settings)
    token = manager.create_session(remember=False, now=10_000)

    assert manager.validate_session(token, now=10_001) is True
    assert manager.validate_session(token + "x", now=10_001) is False
    assert manager.validate_session(token, now=10_000 + 12 * 60 * 60) is False
    settings.auth_password = "replacement-password"
    assert manager.validate_session(token, now=10_001) is False


def test_session_survives_restart_but_missing_store_fails_closed(tmp_path: Path):
    settings = _settings(tmp_path, auth_username="user", auth_password="password")
    manager = AuthenticationManager(settings)
    token = manager.create_session(remember=True, now=10_000)
    assert AuthenticationManager(settings).validate_session(token, now=10_001)
    assert manager.session_path.stat().st_mode & 0o077 == 0
    manager.session_path.unlink()
    assert not AuthenticationManager(settings).validate_session(token, now=10_001)


def test_healthchecks_remain_public_and_report_auth_state(tmp_path: Path):
    settings = _settings(
        tmp_path,
        auth_username="credential-user-sentinel",
        auth_password="credential-password-sentinel",
    )
    client = TestClient(create_app(settings))

    health = client.get("/api/health")
    assert health.status_code == 200
    assert health.json()["auth_configured"] is True
    assert "credential-user-sentinel" not in health.text
    assert "credential-password-sentinel" not in health.text
    assert health.json() == {"status": "ok", "auth_configured": True}
    assert client.get("/api/system/health").status_code == 401
    detailed = client.get(
        "/api/system/health", auth=(settings.auth_username, settings.auth_password)
    )
    assert detailed.status_code == 200 and "library" in detailed.json()
    assert client.get("/api/ready").status_code == 503
    assert client.get("/api/ready").json() == {"status": "not_ready"}

    # Only the exact health-check routes are public.
    assert client.get("/api/health/").status_code == 401
    assert client.get("/api/ready/anything").status_code == 401
    client.close()


def test_authentication_is_optional_for_local_development(tmp_path: Path):
    client = TestClient(create_app(_settings(tmp_path)))
    assert client.get("/api/manga").status_code == 200
    health = client.get("/api/health")
    assert health.status_code == 200
    assert health.json()["auth_configured"] is False
    client.close()


def test_auth_password_is_hidden_from_settings_repr(tmp_path: Path):
    settings = _settings(
        tmp_path,
        auth_username="fixture-user",
        auth_password="do-not-print-this",
    )
    assert settings.auth_configured is True
    assert "do-not-print-this" not in repr(settings)


@pytest.mark.parametrize(
    "header",
    [
        None,
        "Bearer token",
        "Basic",
        "Basic !!!not-base64!!!",
        "Basic bm8tY29sb24=",
        "Basic //46",
        f"Basic {'A' * 8193}",
    ],
)
def test_malformed_authorization_headers_fail_closed(header: str | None):
    assert authenticate_basic_header(header, "user", "password") is False


def test_basic_scheme_is_case_insensitive_and_password_can_contain_colons():
    assert authenticate_basic_header(
        _basic("üser", "one:two", scheme="basic"), "üser", "one:two"
    )


def test_credential_checks_use_compare_digest_for_both_fields(monkeypatch):
    compared: list[tuple[bytes, bytes]] = []

    def fake_compare_digest(left: bytes, right: bytes) -> bool:
        compared.append((left, right))
        return left == right

    monkeypatch.setattr("tankarr.auth.compare_digest", fake_compare_digest)
    assert (
        authenticate_basic_header(_basic("user", "wrong"), "user", "password") is False
    )
    assert compared == [(b"user", b"user"), (b"wrong", b"password")]


@pytest.mark.parametrize(
    ("username", "password"),
    [
        ("user", None),
        (None, "password"),
        ("bad:user", "password"),
        ("user\nname", "password"),
        ("user", "pass\rword"),
    ],
)
def test_invalid_auth_settings_are_rejected(
    tmp_path: Path, username: str | None, password: str | None
):
    with pytest.raises(ValidationError):
        _settings(tmp_path, auth_username=username, auth_password=password)


def test_api_key_authenticates_api_clients_without_the_login(tmp_path: Path):
    settings = _settings(
        tmp_path,
        auth_method="forms",
        auth_username="fixture-user",
        auth_password="correct horse battery staple",
    )
    client = TestClient(create_app(settings))
    login = ("fixture-user", "correct horse battery staple")
    key_path = settings.data_dir / "api-key"

    # Created at startup, readable by the owner only, shown to a signed-in user.
    assert key_path.exists()
    assert key_path.stat().st_mode & 0o777 == 0o600
    assert client.get("/api/manga").status_code == 401
    response = client.get("/api/auth/api-key", auth=login)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    key = response.json()["api_key"]
    assert len(key) == 64
    assert key_path.read_text(encoding="utf-8").strip() == key

    assert client.get("/api/manga", headers={"X-Api-Key": key}).status_code == 200
    assert client.get("/api/manga", headers={"X-Api-Key": "0" * 64}).status_code == 401
    status = client.get("/api/auth/status", headers={"X-Api-Key": key}).json()
    assert status["authenticated"] is True

    # Regenerating retires the old key at once and rewrites the file.
    renewed = client.post("/api/auth/api-key/regenerate", headers={"X-Api-Key": key})
    assert renewed.status_code == 200
    new_key = renewed.json()["api_key"]
    assert new_key != key and len(new_key) == 64
    assert client.get("/api/manga", headers={"X-Api-Key": key}).status_code == 401
    assert client.get("/api/manga", headers={"X-Api-Key": new_key}).status_code == 200
    assert key_path.read_text(encoding="utf-8").strip() == new_key
    client.close()


def test_the_stored_api_key_survives_a_restart_and_a_password_change(tmp_path: Path):
    settings = _settings(
        tmp_path,
        auth_method="basic",
        auth_username="fixture-user",
        auth_password="correct horse battery staple",
    )
    first = AuthenticationManager(settings)
    key = first.api_key()
    settings.auth_password = "another perfectly fine password"
    second = AuthenticationManager(settings)
    assert second.api_key() == key
    assert second.api_key_valid(key)
    assert not second.api_key_valid(key.upper())
    assert not second.api_key_valid(None)


def test_wrong_api_keys_are_throttled_like_wrong_passwords(tmp_path: Path):
    settings = _settings(
        tmp_path,
        auth_method="basic",
        auth_username="fixture-user",
        auth_password="correct horse battery staple",
    )
    client = TestClient(create_app(settings))
    for _ in range(5):
        assert (
            client.get("/api/manga", headers={"X-Api-Key": "f" * 64}).status_code == 401
        )
    throttled = client.get("/api/manga", headers={"X-Api-Key": "f" * 64})
    assert throttled.status_code == 429
    client.close()
