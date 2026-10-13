"""Optional OpenID Connect login into Tankarr's existing admin session.

Authorization Code with PKCE and nonce; Authlib validates signed ID tokens.
The provider's tokens and PKCE verifier never enter the browser session.
"""

from __future__ import annotations

import logging
import time
from collections import OrderedDict
from typing import Any
from urllib.parse import urlsplit

from authlib.integrations.starlette_client import OAuth
from fastapi import FastAPI, HTTPException, Request
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import RedirectResponse

from tankarr.auth import (
    SESSION_COOKIE,
    AuthenticationManager,
    client_address,
    too_many_attempts,
)
from tankarr.config import Settings

logger = logging.getLogger(__name__)
FLOW_SECONDS = 600


class StateCache:
    """Bounded, expiring, server-side flow data; a consumed state cannot replay."""

    def __init__(self) -> None:
        self.entries: OrderedDict[str, tuple[float, str]] = OrderedDict()

    async def get(self, key: str) -> str | None:
        stored = self.entries.get(key)
        if stored and stored[0] > time.monotonic():
            return stored[1]
        self.entries.pop(key, None)
        return None

    async def set(self, key: str, value: str, expires: int) -> None:
        now = time.monotonic()
        for old in list(self.entries):
            if self.entries[old][0] <= now:
                self.entries.pop(old)
        self.entries[key] = (now + min(expires, FLOW_SECONDS), value)
        while len(self.entries) > 1024:
            self.entries.popitem(last=False)

    async def delete(self, key: str) -> None:
        self.entries.pop(key, None)


def _secure_url(value: str) -> bool:
    try:
        parts = urlsplit(value)
        return bool(
            parts.scheme == "https"
            and parts.hostname
            and not parts.username
            and not parts.password
            and not parts.fragment
        )
    except ValueError:
        return False


def validate_oidc_settings(settings: Settings) -> None:
    if not settings.oidc_enabled:
        return
    if settings.auth_method != "forms" or not settings.auth_required:
        raise ValueError(
            "OpenID Connect requires forms authentication and a required login"
        )
    if (
        not _secure_url(settings.oidc_issuer_url)
        or urlsplit(settings.oidc_issuer_url).query
    ):
        raise ValueError(
            "TANKARR_OIDC_ISSUER_URL must be an HTTPS issuer without credentials or query"
        )
    if not settings.oidc_client_id or not settings.oidc_client_secret:
        raise ValueError(
            "TANKARR_OIDC_CLIENT_ID and TANKARR_OIDC_CLIENT_SECRET are required"
        )
    callback = urlsplit(settings.oidc_redirect_uri)
    if (
        not _secure_url(settings.oidc_redirect_uri)
        or callback.query
        or callback.path != f"{settings.url_base}/api/auth/oidc/callback"
    ):
        raise ValueError(
            "TANKARR_OIDC_REDIRECT_URI must be the HTTPS Tankarr callback URL"
        )
    if "openid" not in settings.oidc_scopes.split():
        raise ValueError("TANKARR_OIDC_SCOPES must include openid")
    if not _csv(settings.oidc_allowed_subjects) and not _csv(
        settings.oidc_allowed_groups
    ):
        raise ValueError(
            "Set TANKARR_OIDC_ALLOWED_SUBJECTS or TANKARR_OIDC_ALLOWED_GROUPS"
        )


def _csv(value: str) -> set[str]:
    return {item.strip() for item in value.split(",") if item.strip()}


def identity_allowed(claims: dict[str, Any], settings: Settings) -> bool:
    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject:
        return False
    if subject in _csv(settings.oidc_allowed_subjects):
        return True
    groups = claims.get("groups")
    return isinstance(groups, list) and any(
        isinstance(group, str) and group in _csv(settings.oidc_allowed_groups)
        for group in groups
    )


def register_oidc_routes(
    app: FastAPI, settings: Settings, authentication: AuthenticationManager
) -> None:
    validate_oidc_settings(settings)
    if not settings.oidc_enabled:

        async def disabled():
            raise HTTPException(404, "OpenID Connect is disabled")

        # Keep the frontend's SPA fallback from treating disabled API routes
        # as successful HTML pages when a production frontend is installed.
        app.add_api_route("/api/auth/oidc/login", disabled, methods=["GET"])
        app.add_api_route("/api/auth/oidc/callback", disabled, methods=["GET"])
        return
    cache = StateCache()
    oauth = OAuth(cache=cache)
    client = oauth.register(
        "tankarr",
        client_id=settings.oidc_client_id,
        client_secret=settings.oidc_client_secret,
        server_metadata_url=f"{settings.oidc_issuer_url.rstrip('/')}/.well-known/openid-configuration",
        client_kwargs={
            "scope": settings.oidc_scopes,
            "code_challenge_method": "S256",
            "timeout": 15,
        },
    )
    client.framework.expires_in = FLOW_SECONDS
    app.state.oidc_client = client
    app.add_middleware(
        SessionMiddleware,
        secret_key=authentication.oidc_state_signing_key(),
        session_cookie="tankarr_oidc_flow",
        max_age=FLOW_SECONDS,
        path=settings.url_base or "/",
        same_site="lax",
        https_only=True,
    )

    @app.get("/api/auth/oidc/login")
    async def oidc_login(request: Request):
        wait = authentication.throttle.retry_after(client_address(request.scope))
        if wait > 0:
            return too_many_attempts(wait)
        try:
            metadata = await client.load_server_metadata()
            if metadata.get("issuer") != settings.oidc_issuer_url or not all(
                _secure_url(str(metadata.get(name) or ""))
                for name in ("authorization_endpoint", "token_endpoint", "jwks_uri")
            ):
                raise ValueError("Invalid OpenID Connect discovery document")
            response = await client.authorize_redirect(
                request, settings.oidc_redirect_uri
            )
            response.headers["Cache-Control"] = "no-store"
            return response
        except Exception as exc:  # noqa: BLE001 - provider errors can contain secrets
            logger.warning(
                "OpenID Connect provider unavailable (%s)", type(exc).__name__
            )
            raise HTTPException(
                502, "OpenID Connect provider unavailable; use local login"
            ) from None

    @app.get("/api/auth/oidc/callback")
    async def oidc_callback(request: Request):
        address = client_address(request.scope)
        wait = authentication.throttle.retry_after(address)
        if wait > 0:
            return too_many_attempts(wait)
        try:
            token = await client.authorize_access_token(
                request,
                claims_options={
                    "iss": {"essential": True, "value": settings.oidc_issuer_url}
                },
                leeway=30,
            )
            claims = token.get("userinfo")
            if (
                not token.get("id_token")
                or not isinstance(claims, dict)
                or not identity_allowed(claims, settings)
            ):
                raise ValueError("Identity is not allowed")
            session = authentication.create_session(remember=False)
        except Exception as exc:  # noqa: BLE001 - never echo tokens, codes or provider URLs
            request.session.clear()
            authentication.throttle.failed(address)
            logger.warning("OpenID Connect sign-in rejected (%s)", type(exc).__name__)
            raise HTTPException(
                401, "OpenID Connect sign-in failed; use local login"
            ) from None
        request.session.clear()
        authentication.throttle.succeeded(address)
        response = RedirectResponse(
            f"{settings.url_base}/",
            status_code=303,
            headers={"Cache-Control": "no-store"},
        )
        response.set_cookie(
            SESSION_COOKIE,
            session,
            httponly=True,
            secure=True,
            samesite="lax",
            path=settings.url_base or "/",
        )
        return response
