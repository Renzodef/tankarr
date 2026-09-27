from __future__ import annotations

import logging
from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from tankarr.app import create_app
from tankarr.auth import (
    GENERATED_USERNAME,
    AuthenticationManager,
    AuthenticationMiddleware,
    LoginThrottle,
)
from tankarr.config import Settings
from tankarr.redaction import redact_secrets
from tankarr.sabnzbd import SABnzbdClient, SABnzbdError


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    frontend = tmp_path / "frontend"
    (frontend / "assets").mkdir(parents=True, exist_ok=True)
    (frontend / "index.html").write_text("Tankarr UI", encoding="utf-8")
    values: dict[str, object] = {
        "data_dir": tmp_path / "data",
        "library_dir": tmp_path / "library",
        "frontend_dir": frontend,
        "monitor_enabled": False,
    }
    values.update(overrides)
    return Settings(**values)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_first_start_without_a_login_creates_one_and_keeps_it(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    settings = _settings(tmp_path, auth_required=True)
    with caplog.at_level(logging.WARNING, logger="tankarr.auth"):
        client = TestClient(create_app(settings))

    assert settings.auth_username == GENERATED_USERNAME
    password = settings.auth_password
    assert password and len(password) >= 20
    assert password in caplog.text
    stored = settings.data_dir / "generated-login.json"
    assert stored.stat().st_mode & 0o077 == 0
    assert client.get("/api/manga").status_code == 401
    assert client.get("/api/manga", auth=("admin", password)).status_code == 200
    client.close()

    caplog.clear()
    restarted = _settings(tmp_path, auth_required=True)
    with caplog.at_level(logging.WARNING, logger="tankarr.auth"):
        TestClient(create_app(restarted)).close()
    assert (restarted.auth_username, restarted.auth_password) == ("admin", password)
    assert "created one" not in caplog.text


def test_credentials_configured_later_replace_the_generated_login(tmp_path: Path):
    TestClient(create_app(_settings(tmp_path, auth_required=True))).close()
    settings = _settings(
        tmp_path,
        auth_required=True,
        auth_username="fixture-user",
        auth_password="fixture-password",
    )
    client = TestClient(create_app(settings))
    assert (settings.auth_username, settings.auth_password) == (
        "fixture-user",
        "fixture-password",
    )
    good = ("fixture-user", "fixture-password")
    assert client.get("/api/manga", auth=good).status_code == 200
    client.close()


def test_changing_the_generated_password_in_settings_keeps_the_login(tmp_path: Path):
    settings = _settings(tmp_path, auth_required=True)
    client = TestClient(create_app(settings))
    generated = (settings.auth_username, settings.auth_password)
    response = client.put(
        "/api/settings",
        json={"auth_password": "a-brand-new-password"},
        auth=generated,
    )
    assert response.status_code == 200, response.text
    client.close()
    assert not (settings.data_dir / "generated-login.json").exists()

    restarted = _settings(tmp_path, auth_required=True)
    client = TestClient(create_app(restarted))
    assert (restarted.auth_username, restarted.auth_password) == (
        "admin",
        "a-brand-new-password",
    )
    assert client.get("/api/manga", auth=generated).status_code == 401
    assert (
        client.get("/api/manga", auth=("admin", "a-brand-new-password")).status_code
        == 200
    )
    client.close()


def test_configured_credentials_are_never_replaced(tmp_path: Path):
    settings = _settings(
        tmp_path,
        auth_required=True,
        auth_username="fixture-user",
        auth_password="fixture-password",
    )
    TestClient(create_app(settings)).close()
    assert settings.auth_username == "fixture-user"
    assert settings.auth_password == "fixture-password"
    assert not (settings.data_dir / "generated-login.json").exists()


def test_a_required_login_cannot_be_removed_from_settings(tmp_path: Path):
    settings = _settings(
        tmp_path,
        auth_required=True,
        auth_username="fixture-user",
        auth_password="fixture-password",
    )
    client = TestClient(create_app(settings))
    response = client.put(
        "/api/settings",
        json={"auth_username": "", "auth_password": ""},
        auth=("fixture-user", "fixture-password"),
    )
    assert response.status_code == 400
    assert "cannot be disabled" in response.json()["detail"]
    client.close()


@pytest.mark.parametrize(
    ("headers", "allowed"),
    [
        ({}, True),
        ({"Sec-Fetch-Site": "same-origin"}, True),
        ({"Sec-Fetch-Site": "cross-site"}, False),
        ({"Sec-Fetch-Site": "same-site"}, False),
        ({"Origin": "http://testserver"}, True),
        ({"Origin": "http://attacker.example"}, False),
        ({"Origin": "null"}, False),
        ({"Origin": "http://proxy.example", "X-Forwarded-Host": "proxy.example"}, True),
    ],
)
def test_state_changing_requests_from_other_sites_are_refused(
    tmp_path: Path, headers: dict[str, str], allowed: bool
):
    client = TestClient(create_app(_settings(tmp_path)))
    response = client.post("/api/auth/logout", headers=headers)
    assert (response.status_code == 200) is allowed
    if not allowed:
        assert response.status_code == 403
    # Reading is never blocked: only unsafe methods can change state.
    assert client.get("/api/health", headers=headers).status_code == 200
    client.close()


def test_every_response_carries_the_security_headers(tmp_path: Path):
    settings = _settings(
        tmp_path, auth_username="fixture-user", auth_password="fixture-password"
    )
    client = TestClient(create_app(settings))
    for response in (client.get("/api/health"), client.get("/api/manga")):
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["x-frame-options"] == "DENY"
        assert response.headers["referrer-policy"] == "same-origin"
        assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    client.close()


def test_repeated_wrong_passwords_are_slowed_down_and_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    settings = _settings(
        tmp_path,
        auth_method="forms",
        auth_username="fixture-user",
        auth_password="fixture-password",
    )
    app = create_app(settings)
    clock = FakeClock()
    app.state.authentication.throttle = LoginThrottle(clock=clock)
    client = TestClient(app)
    wrong = {"username": "fixture-user", "password": "wrong"}

    with caplog.at_level(logging.WARNING):
        for _ in range(LoginThrottle.FREE_ATTEMPTS):
            assert client.post("/api/auth/login", json=wrong).status_code == 401
    assert "Sign-in failed for user 'fixture-user' from testclient" in caplog.text

    blocked = client.post(
        "/api/auth/login",
        json={"username": "fixture-user", "password": "fixture-password"},
    )
    assert blocked.status_code == 429
    assert int(blocked.headers["retry-after"]) >= 1

    clock.now += 2
    allowed = client.post(
        "/api/auth/login",
        json={"username": "fixture-user", "password": "fixture-password"},
    )
    assert allowed.status_code == 200
    # A success forgets the earlier failures.
    assert app.state.authentication.throttle.retry_after("testclient") == 0
    client.close()


def test_wrong_basic_credentials_count_towards_the_same_backoff(tmp_path: Path):
    settings = _settings(
        tmp_path,
        auth_method="basic",
        auth_username="fixture-user",
        auth_password="fixture-password",
    )
    app = create_app(settings)
    clock = FakeClock()
    app.state.authentication.throttle = LoginThrottle(clock=clock)
    client = TestClient(app)
    for _ in range(LoginThrottle.FREE_ATTEMPTS):
        assert client.get("/api/manga", auth=("fixture-user", "x")).status_code == 401
    good = ("fixture-user", "fixture-password")
    assert client.get("/api/manga", auth=good).status_code == 429
    clock.now += 2
    assert client.get("/api/manga", auth=good).status_code == 200
    client.close()


def test_the_backoff_grows_and_is_capped():
    clock = FakeClock()
    throttle = LoginThrottle(clock=clock)
    waits = []
    for _ in range(LoginThrottle.FREE_ATTEMPTS + 10):
        throttle.failed("203.0.113.9")
        waits.append(throttle.retry_after("203.0.113.9"))
    assert waits[: LoginThrottle.FREE_ATTEMPTS - 1] == [0.0] * (
        LoginThrottle.FREE_ATTEMPTS - 1
    )
    assert waits[LoginThrottle.FREE_ATTEMPTS - 1] == 1.0
    assert max(waits) == LoginThrottle.MAX_DELAY_SECONDS
    assert throttle.retry_after("198.51.100.7") == 0.0
    clock.now += LoginThrottle.FORGET_AFTER_SECONDS + 1
    assert throttle.retry_after("203.0.113.9") == 0.0


def test_login_bodies_must_be_small_and_declared(tmp_path: Path):
    settings = _settings(
        tmp_path, auth_username="fixture-user", auth_password="fixture-password"
    )
    client = TestClient(create_app(settings))
    huge = '{"username": "x", "password": "' + "a" * 10_000 + '"}'
    response = client.post(
        "/api/auth/login",
        content=huge,
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 413
    client.close()


def test_sessions_are_signed_with_a_private_random_key(tmp_path: Path):
    settings = _settings(
        tmp_path, auth_username="fixture-user", auth_password="fixture-password"
    )
    manager = AuthenticationManager(settings)
    token = manager.create_session(remember=False, now=10_000)
    assert manager.validate_session(token, now=10_001)
    key = manager.signing_secret_path
    assert key.stat().st_mode & 0o077 == 0
    assert len(key.read_bytes()) == 32

    # Knowing the password is not enough to forge or check a session offline.
    key.write_bytes(b"\0" * 32)
    assert not AuthenticationManager(settings).validate_session(token, now=10_001)


@pytest.mark.asyncio
async def test_websockets_need_the_same_authentication(tmp_path: Path):
    settings = _settings(
        tmp_path, auth_username="fixture-user", auth_password="fixture-password"
    )
    reached: list[str] = []

    async def app(scope, receive, send):
        reached.append(scope["type"])

    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    async def receive():
        return {"type": "websocket.connect"}

    middleware = AuthenticationMiddleware(app, manager=AuthenticationManager(settings))
    await middleware({"type": "websocket", "path": "/ws", "headers": []}, receive, send)
    assert reached == []
    assert sent == [{"type": "websocket.close", "code": 1008}]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "Client error '403 Forbidden' for url "
            "'http://sabnzbd:8080/api?mode=version&output=json&apikey=0123abcd'",
            "Client error '403 Forbidden' for url "
            "'http://sabnzbd:8080/api?mode=version&output=json&apikey=[redacted]'",
        ),
        (
            "http://kavita:5000/api/Plugin/authenticate?apiKey=secret-key&pluginName=x",
            "http://kavita:5000/api/Plugin/authenticate?apiKey=[redacted]&pluginName=x",
        ),
        (
            "addurl name=http%3A%2F%2Fprowlarr%3A9696%2F1%2Fdownload%3Fapikey%3Dfeedface",
            "addurl name=http%3A%2F%2Fprowlarr%3A9696%2F1%2Fdownload%3Fapikey%3D[redacted]",
        ),
        (
            "failed to reach http://user:hunter2@qbittorrent:8080/api",
            "failed to reach http://user:[redacted]@qbittorrent:8080/api",
        ),
        ("nothing secret here", "nothing secret here"),
    ],
)
def test_credentials_are_redacted_from_messages(text: str, expected: str):
    assert redact_secrets(text) == expected


@pytest.mark.asyncio
@respx.mock
async def test_sabnzbd_errors_never_quote_the_api_key(tmp_path: Path):
    settings = _settings(
        tmp_path,
        sabnzbd_url="http://sabnzbd:8080",
        sabnzbd_api_key="sab-secret-sentinel",
    )
    respx.get("http://sabnzbd:8080/api").mock(return_value=httpx.Response(403))
    with pytest.raises(SABnzbdError) as failure:
        await SABnzbdClient(settings).probe()
    assert "sab-secret-sentinel" not in str(failure.value)
    assert "403" in str(failure.value)
