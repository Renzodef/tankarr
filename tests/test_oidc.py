from __future__ import annotations

import time
from urllib.parse import parse_qs, urlsplit

import pytest
import respx
from fastapi.testclient import TestClient
from httpx import Response
from joserfc import jwt
from joserfc.jwk import RSAKey

from tankarr.app import create_app
from tankarr.auth import SESSION_COOKIE
from tankarr.config import Settings
from tankarr.oidc import StateCache, identity_allowed

ISSUER = "https://identity.example.test/application/o/tankarr/"
BASE = "https://tankarr.example.test"


def settings_at(tmp_path, **updates):
    return Settings(
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "library",
        monitor_enabled=False,
        auth_required=True,
        auth_username="fixture-user",
        auth_password="fixture-password",
        oidc_enabled=True,
        oidc_issuer_url=ISSUER,
        oidc_client_id="fixture-client",
        oidc_client_secret="fixture-client-secret",
        oidc_redirect_uri=f"{BASE}/api/auth/oidc/callback",
        oidc_allowed_subjects="fixture-subject",
        **updates,
    )


def mock_provider():
    respx.get(f"{ISSUER}.well-known/openid-configuration").mock(
        return_value=Response(
            200,
            json={
                "issuer": ISSUER,
                "authorization_endpoint": f"{ISSUER}authorize",
                "token_endpoint": f"{ISSUER}token",
                "jwks_uri": f"{ISSUER}jwks",
                "id_token_signing_alg_values_supported": ["RS256"],
            },
        )
    )
    key = RSAKey.generate_key(2048)
    key.ensure_kid()
    respx.get(f"{ISSUER}jwks").mock(
        return_value=Response(200, json={"keys": [key.as_dict(private=False)]})
    )
    return key


@pytest.mark.parametrize(
    "invalid",
    [
        None,
        "issuer",
        "audience",
        "nonce",
        "expired",
        "signature",
        "unauthorized",
        "missing_id_token",
    ],
)
@respx.mock
def test_oidc_validates_signed_identity_and_keeps_local_login(
    tmp_path, invalid, caplog
):
    settings = settings_at(tmp_path)
    app = create_app(settings)
    key = mock_provider()
    with TestClient(app, base_url=BASE) as client:
        assert client.get("/api/manga").status_code == 401
        assert (
            client.get("/api/auth/status").json()["oidc_login_url"]
            == "/api/auth/oidc/login"
        )
        start = client.get("/api/auth/oidc/login", follow_redirects=False)
        assert start.status_code == 302
        params = parse_qs(urlsplit(start.headers["location"]).query)
        assert params["code_challenge_method"] == ["S256"]
        assert "fixture-client-secret" not in start.headers["location"]
        cookie = start.headers["set-cookie"].casefold()
        assert "httponly" in cookie and "secure" in cookie and "samesite=lax" in cookie
        claims = {
            "iss": "https://other.example.test" if invalid == "issuer" else ISSUER,
            "sub": "other-subject" if invalid == "unauthorized" else "fixture-subject",
            "aud": "other-client" if invalid == "audience" else "fixture-client",
            "iat": int(time.time()),
            "exp": int(time.time()) + (-120 if invalid == "expired" else 300),
            "nonce": "other-nonce" if invalid == "nonce" else params["nonce"][0],
        }
        signing_key = RSAKey.generate_key(2048) if invalid == "signature" else key
        id_token = jwt.encode({"alg": "RS256", "kid": key.kid}, claims, signing_key)
        token = {"access_token": "fixture-access-token", "token_type": "Bearer"}
        if invalid != "missing_id_token":
            token["id_token"] = id_token
        endpoint = respx.post(f"{ISSUER}token").mock(
            return_value=Response(200, json=token)
        )
        callback = (
            f"/api/auth/oidc/callback?code=fixture-code&state={params['state'][0]}"
        )
        response = client.get(callback, follow_redirects=False)
        assert response.status_code == (401 if invalid else 303)
        if invalid:
            assert SESSION_COOKIE not in client.cookies
            assert client.get("/api/manga").status_code == 401
        else:
            assert client.get("/api/manga").status_code == 200
            assert "code_verifier=" in endpoint.calls[0].request.content.decode()
            # A consumed state is rejected even if the signed old flow cookie is replayed.
            original_flow = start.cookies.get("tankarr_oidc_flow")
            client.cookies.set(
                "tankarr_oidc_flow",
                original_flow,
                domain="tankarr.example.test",
                path="/",
            )
            assert client.get(callback, follow_redirects=False).status_code == 401
            client.post("/api/auth/logout")
            assert client.get("/api/manga").status_code == 401
        local = client.post(
            "/api/auth/login",
            json={"username": "fixture-user", "password": "fixture-password"},
        )
        assert local.status_code == 200
        assert client.get("/api/manga").status_code == 200
        assert "fixture-client-secret" not in caplog.text
        assert "fixture-access-token" not in caplog.text
        assert id_token not in caplog.text


@respx.mock
def test_oidc_callback_requires_the_browser_that_started_login(tmp_path):
    app = create_app(settings_at(tmp_path))
    mock_provider()
    token_endpoint = respx.post(f"{ISSUER}token").mock(
        return_value=Response(200, json={"access_token": "fixture-access-token"})
    )
    other = TestClient(app, base_url=BASE)
    with TestClient(app, base_url=BASE) as original:
        start = original.get("/api/auth/oidc/login", follow_redirects=False)
        state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
        response = other.get(f"/api/auth/oidc/callback?code=fixture-code&state={state}")
        assert response.status_code == 401
        assert SESSION_COOKIE not in other.cookies
        assert not token_endpoint.called
    other.close()


def test_oidc_is_disabled_by_default(tmp_path):
    settings = Settings(
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "library",
        auth_username="fixture-user",
        auth_password="fixture-password",
        monitor_enabled=False,
    )
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/auth/oidc/login").status_code == 404
        status = client.get("/api/auth/status").json()
        assert "oidc_login_url" not in status
        assert "local_login_enabled" not in status


def test_oidc_only_mode_rejects_local_login_and_keeps_api_access(tmp_path):
    settings = settings_at(tmp_path, oidc_disable_local_login=True)
    with TestClient(create_app(settings)) as client:
        status = client.get("/api/auth/status").json()
        assert status["local_login_enabled"] is False
        assert status["oidc_login_url"] == "/api/auth/oidc/login"
        wrong = client.post(
            "/api/auth/login",
            json={"username": "fixture-user", "password": "wrong-password"},
        )
        right = client.post(
            "/api/auth/login",
            json={
                "username": "fixture-user",
                "password": "fixture-password",
                "remember_me": True,
            },
        )
        assert wrong.status_code == right.status_code == 403
        assert wrong.json() == right.json() == {"detail": "Local login is disabled"}
        assert SESSION_COOKIE not in wrong.cookies
        assert SESSION_COOKIE not in right.cookies
        assert client.get("/api/manga").status_code == 401

        basic = ("fixture-user", "fixture-password")
        assert client.get("/api/manga", auth=basic).status_code == 200
        key = client.get("/api/auth/api-key", auth=basic).json()["api_key"]
        assert client.get("/api/manga", headers={"X-Api-Key": key}).status_code == 200
        assert (
            client.get(
                "/api/manga", headers={"Authorization": f"Bearer {key}"}
            ).status_code
            == 200
        )

        changed = client.put(
            "/api/settings",
            headers={"X-Api-Key": key},
            json={"auth_password": "replacement-password"},
        )
        assert changed.status_code == 200
        assert SESSION_COOKIE not in changed.cookies
        still_local = client.post(
            "/api/auth/login",
            json={
                "username": "fixture-user",
                "password": "replacement-password",
            },
        )
        assert still_local.status_code == 403
        assert SESSION_COOKIE not in still_local.cookies
        assert client.get("/api/manga").status_code == 401
        assert (
            client.get(
                "/api/manga", auth=("fixture-user", "replacement-password")
            ).status_code
            == 200
        )
        assert client.get("/api/manga", auth=basic).status_code == 401
        assert client.get("/api/manga", headers={"X-Api-Key": key}).status_code == 200
        blocked = client.put(
            "/api/settings",
            headers={"X-Api-Key": key},
            json={"auth_method": "basic"},
        )
        assert blocked.status_code == 400
        assert client.get("/api/auth/status").json()["method"] == "forms"


def test_oidc_only_mode_refuses_to_start_without_a_valid_provider(tmp_path):
    values = settings_at(tmp_path).model_dump()

    def rejected(**changes: object) -> str:
        candidate = dict(values)
        candidate.update(changes)
        with pytest.raises(ValueError) as error:
            Settings.model_validate(candidate)
        message = str(error.value)
        assert "TANKARR_OIDC_DISABLE_LOCAL_LOGIN" in message
        assert "fixture-client-secret" not in message
        assert "fixture-password" not in message
        return message

    assert "TANKARR_OIDC_ENABLED" in rejected(
        oidc_enabled=False, oidc_disable_local_login=True
    )
    assert "TANKARR_OIDC_ISSUER_URL" in rejected(
        oidc_issuer_url="http://identity.example.test",
        oidc_disable_local_login=True,
    )
    assert "forms authentication" in rejected(
        auth_method="basic", oidc_disable_local_login=True
    )
    assert settings_at(tmp_path, oidc_disable_local_login=True).oidc_disable_local_login


def test_oidc_group_allowlist_is_explicit_and_exact(tmp_path):
    settings = settings_at(tmp_path, oidc_allowed_groups="tankarr-admins")
    assert identity_allowed({"sub": "other", "groups": ["tankarr-admins"]}, settings)
    assert not identity_allowed(
        {"sub": "other", "groups": ["tankarr-admins-other"]}, settings
    )
    assert not identity_allowed({"sub": "other", "groups": "tankarr-admins"}, settings)
    assert not identity_allowed({"groups": ["tankarr-admins"]}, settings)


@pytest.mark.asyncio
async def test_oidc_state_cache_expires_and_bounds_flow_data():
    cache = StateCache()
    await cache.set("expired", "fixture", 0)
    assert await cache.get("expired") is None
    for index in range(1025):
        await cache.set(str(index), "fixture", 600)
    assert await cache.get("0") is None
    assert len(cache.entries) == 1024


@pytest.mark.parametrize(
    "changes",
    [
        {"oidc_issuer_url": "http://identity.example.test"},
        {"oidc_redirect_uri": "https://tankarr.example.test/other"},
        {"oidc_scopes": "profile email"},
        {"oidc_allowed_subjects": "", "oidc_allowed_groups": ""},
        {"auth_required": False},
        {"auth_method": "basic"},
    ],
)
def test_oidc_rejects_unsafe_configuration_without_echoing_secrets(tmp_path, changes):
    values = settings_at(tmp_path).model_dump()
    values.update(changes)
    with pytest.raises(ValueError) as error:
        Settings.model_validate(values)
    assert "fixture-client-secret" not in str(error.value)
    assert "fixture-password" not in str(error.value)
