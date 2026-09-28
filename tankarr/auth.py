from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from http.cookies import CookieError, SimpleCookie
from pathlib import Path
from secrets import compare_digest
from typing import Any
from urllib.parse import urlsplit

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from tankarr.config import Settings

logger = logging.getLogger(__name__)

PUBLIC_HEALTHCHECK_PATHS = frozenset({"/api/health", "/api/ready"})
LOGIN_PATH = "/api/auth/login"
PUBLIC_AUTH_PATHS = frozenset({"/api/auth/status", LOGIN_PATH, "/api/auth/logout"})
SESSION_COOKIE = "tankarr_session"
GENERATED_USERNAME = "admin"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_BASIC_PREFIX = "Basic "
_MAX_ENCODED_CREDENTIAL_LENGTH = 8192
_MAX_LOGIN_BODY_BYTES = 8192
_SESSION_SECONDS = 12 * 60 * 60
_REMEMBERED_SESSION_SECONDS = 30 * 24 * 60 * 60
_MAX_CLOCK_SKEW_SECONDS = 300
_SIGNING_SECRET_BYTES = 32
# Other applications authenticate with this header instead of the login.
API_KEY_HEADER = "x-api-key"
_API_KEY_BYTES = 32
_API_KEY_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def authenticate_basic_header(
    authorization: str | None,
    expected_username: str,
    expected_password: str,
) -> bool:
    """Validate one HTTP Basic header without timing-sensitive string equality."""

    if not authorization:
        return False
    scheme, separator, encoded = authorization.partition(" ")
    if not separator or scheme.casefold() != _BASIC_PREFIX.rstrip().casefold():
        return False
    if not encoded or len(encoded) > _MAX_ENCODED_CREDENTIAL_LENGTH:
        return False
    try:
        decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return False
    if ":" not in decoded:
        return False
    username, password = decoded.split(":", 1)

    # Evaluate both comparisons so a valid username does not create a distinct
    # short-circuit path. Bytes also support non-ASCII UTF-8 credentials.
    username_matches = compare_digest(
        username.encode("utf-8"), expected_username.encode("utf-8")
    )
    password_matches = compare_digest(
        password.encode("utf-8"), expected_password.encode("utf-8")
    )
    return username_matches & password_matches


def basic_username(authorization: str | None) -> str | None:
    """The user name a Basic header claims, for logging failed attempts only."""

    if not authorization:
        return None
    scheme, separator, encoded = authorization.partition(" ")
    if not separator or scheme.casefold() != _BASIC_PREFIX.rstrip().casefold():
        return None
    if len(encoded) > _MAX_ENCODED_CREDENTIAL_LENGTH:
        return None
    try:
        decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None
    return decoded.split(":", 1)[0]


def client_address(scope: Scope) -> str:
    """The peer address uvicorn resolved (behind a trusted proxy: the client)."""

    client = scope.get("client")
    return str(client[0]) if client else "unknown"


class LoginThrottle:
    """Slow password guessing down: backoff per client after repeated failures.

    The first attempts are free, then every failure doubles the wait up to a
    minute, so a person who mistypes is barely affected while a dictionary
    attack drops to about one guess per minute. State is in memory: a restart
    forgets it, which only helps someone who can already restart Tankarr.
    """

    FREE_ATTEMPTS = 5
    MAX_DELAY_SECONDS = 60.0
    FORGET_AFTER_SECONDS = 3600.0
    MAX_CLIENTS = 4096

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        # client -> (failures, last failure, blocked until)
        self._clients: OrderedDict[str, tuple[int, float, float]] = OrderedDict()

    def retry_after(self, client: str) -> float:
        with self._lock:
            entry = self._clients.get(client)
            if entry is None:
                return 0.0
            now = self._clock()
            if now - entry[1] > self.FORGET_AFTER_SECONDS:
                del self._clients[client]
                return 0.0
            return max(0.0, entry[2] - now)

    def failed(self, client: str) -> None:
        with self._lock:
            now = self._clock()
            failures, last, _ = self._clients.pop(client, (0, now, now))
            if now - last > self.FORGET_AFTER_SECONDS:
                failures = 0
            failures += 1
            delay = 0.0
            if failures >= self.FREE_ATTEMPTS:
                delay = min(
                    self.MAX_DELAY_SECONDS, 2.0 ** (failures - self.FREE_ATTEMPTS)
                )
            self._clients[client] = (failures, now, now + delay)
            while len(self._clients) > self.MAX_CLIENTS:
                self._clients.popitem(last=False)

    def succeeded(self, client: str) -> None:
        with self._lock:
            self._clients.pop(client, None)


def too_many_attempts(retry_after: float) -> JSONResponse:
    return JSONResponse(
        {"detail": "Too many failed sign-in attempts; try again shortly"},
        status_code=429,
        headers={
            "Cache-Control": "no-store",
            "Retry-After": str(max(1, int(retry_after + 0.999))),
        },
    )


def _generated_login_path(settings: Settings) -> Path:
    return settings.data_dir / "generated-login.json"


def _read_generated_login(settings: Settings) -> tuple[str, str] | None:
    path = _generated_login_path(settings)
    try:
        if path.is_symlink():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    username, password = payload.get("username"), payload.get("password")
    if (
        isinstance(username, str)
        and isinstance(password, str)
        and username
        and password
    ):
        return username, password
    return None


def generated_login_active(settings: Settings) -> bool:
    """Whether the live login is the one Tankarr created on its first start."""

    generated = _read_generated_login(settings)
    return generated is not None and generated == (
        settings.auth_username,
        settings.auth_password,
    )


def forget_generated_login(settings: Settings) -> None:
    """Drop the fallback once a login has been saved from Settings."""

    _generated_login_path(settings).unlink(missing_ok=True)


def ensure_login(settings: Settings) -> str | None:
    """Never serve an instance without a login by accident.

    Credentials come from Settings > Security first, then from the
    environment. Without either, Tankarr uses the login it created on an
    earlier start, or creates one now: ``admin`` with a random password, kept
    in an owner-only file in the data directory and printed once to the log.
    Credentials configured later in the environment or in Settings take over.
    Returns the password when one was created.
    """

    if settings.auth_configured or not settings.auth_required:
        return None
    generated = _read_generated_login(settings)
    created = None
    if generated is None:
        created = secrets.token_urlsafe(18)
        generated = (GENERATED_USERNAME, created)
        path = _generated_login_path(settings)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{secrets.token_hex(4)}")
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump({"username": generated[0], "password": created}, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    settings.auth_username, settings.auth_password = generated
    if created is not None:
        logger.warning(
            "No login was configured, so Tankarr created one. Username: %s  "
            "Password: %s  Sign in and change it under Settings > Security. "
            "Until then it is kept in %s.",
            generated[0],
            created,
            _generated_login_path(settings),
        )
    return created


def _read_secret(path: Path, minimum_length: int) -> bytes | None:
    """The stored secret, or None when the file is missing or too short."""

    try:
        if path.is_symlink():
            raise OSError(f"{path} must not be a symlink")
        secret = path.read_bytes()
    except FileNotFoundError:
        return None
    return secret if len(secret) >= minimum_length else None


def _load_signing_secret(path: Path) -> bytes:
    """A random per-installation key, created once with owner-only access."""

    secret = _read_secret(path, _SIGNING_SECRET_BYTES)
    if secret is None:
        secret = secrets.token_bytes(_SIGNING_SECRET_BYTES)
        _write_secret(path, secret)
    return secret


def _write_secret(path: Path, secret: bytes) -> None:
    """Replace the secret file atomically, readable by the owner only."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(4)}")
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(secret)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _urlsafe_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _urlsafe_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.b64decode(value + padding, altchars=b"-_", validate=True)


class AuthenticationManager:
    """Dynamic ARR-style authentication backed by the live Settings object."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        # An allow-list makes logout durable across restarts. Restoring an
        # installation without this disposable store invalidates old sessions.
        self.session_path = settings.data_dir / "auth-sessions.sqlite3"
        self.signing_secret_path = settings.data_dir / "auth-signing-key"
        self._signing_secret: bytes | None = None
        self._signing_secret_lock = threading.Lock()
        self.api_key_path = settings.data_dir / "api-key"
        self._api_key: str | None = None
        self._api_key_lock = threading.Lock()
        self.throttle = LoginThrottle()

    def _sessions(self) -> sqlite3.Connection:
        self.session_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.session_path, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(descriptor)
        connection = sqlite3.connect(self.session_path, timeout=5)
        connection.execute(
            "CREATE TABLE IF NOT EXISTS sessions "
            "(digest TEXT PRIMARY KEY, expires_at INTEGER NOT NULL)"
        )
        return connection

    @staticmethod
    def _token_digest(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def revoke_session(self, token: str | None) -> None:
        if not token or len(token) > 8192:
            return
        connection = self._sessions()
        try:
            with connection:
                connection.execute(
                    "DELETE FROM sessions WHERE digest = ? OR expires_at <= ?",
                    (self._token_digest(token), int(time.time())),
                )
        finally:
            connection.close()

    @property
    def configured(self) -> bool:
        return self.settings.auth_configured

    @property
    def method(self) -> str:
        return self.settings.auth_method

    def credentials_valid(self, username: str, password: str) -> bool:
        expected_username = self.settings.auth_username or ""
        expected_password = self.settings.auth_password or ""
        username_matches = compare_digest(
            username.encode("utf-8"), expected_username.encode("utf-8")
        )
        password_matches = compare_digest(
            password.encode("utf-8"), expected_password.encode("utf-8")
        )
        return bool(self.configured and username_matches & password_matches)

    def create_session(self, *, remember: bool, now: int | None = None) -> str:
        if not self.configured:
            raise RuntimeError("Tankarr authentication is not configured")
        issued_at = int(time.time() if now is None else now)
        lifetime = _REMEMBERED_SESSION_SECONDS if remember else _SESSION_SECONDS
        payload = {
            "username": self.settings.auth_username,
            "issued_at": issued_at,
            "expires_at": issued_at + lifetime,
            "nonce": secrets.token_urlsafe(18),
        }
        encoded = _urlsafe_encode(
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        )
        signature = hmac.new(
            self._signing_key(), encoded.encode("ascii"), hashlib.sha256
        ).digest()
        token = f"{encoded}.{_urlsafe_encode(signature)}"
        connection = self._sessions()
        try:
            with connection:
                connection.execute(
                    "DELETE FROM sessions WHERE expires_at <= ?", (issued_at,)
                )
                connection.execute(
                    "INSERT INTO sessions(digest, expires_at) VALUES (?, ?)",
                    (self._token_digest(token), payload["expires_at"]),
                )
        finally:
            connection.close()
        return token

    def validate_session(self, token: str | None, *, now: int | None = None) -> bool:
        if not token or not self.configured:
            return False
        encoded, separator, supplied_signature = token.partition(".")
        if not separator or not encoded or not supplied_signature:
            return False
        if len(encoded) > 4096 or len(supplied_signature) > 256:
            return False
        try:
            expected_signature = hmac.new(
                self._signing_key(), encoded.encode("ascii"), hashlib.sha256
            ).digest()
            decoded_signature = _urlsafe_decode(supplied_signature)
            payload = json.loads(_urlsafe_decode(encoded))
        except (binascii.Error, UnicodeDecodeError, ValueError, json.JSONDecodeError):
            return False
        if not hmac.compare_digest(decoded_signature, expected_signature):
            return False
        if not isinstance(payload, dict):
            return False
        try:
            issued_at = int(payload["issued_at"])
            expires_at = int(payload["expires_at"])
        except (KeyError, TypeError, ValueError):
            return False
        current = int(time.time() if now is None else now)
        if issued_at > current + _MAX_CLOCK_SKEW_SECONDS or expires_at <= current:
            return False
        if expires_at - issued_at not in {
            _SESSION_SECONDS,
            _REMEMBERED_SESSION_SECONDS,
        }:
            return False
        if not compare_digest(
            str(payload.get("username") or "").encode("utf-8"),
            str(self.settings.auth_username or "").encode("utf-8"),
        ):
            return False
        # Never resurrect a logged-out token if storage is missing/unreadable.
        try:
            connection = sqlite3.connect(
                f"{self.session_path.resolve().as_uri()}?mode=ro", uri=True, timeout=5
            )
            try:
                return (
                    connection.execute(
                        "SELECT 1 FROM sessions WHERE digest = ? AND expires_at > ?",
                        (self._token_digest(token), current),
                    ).fetchone()
                    is not None
                )
            finally:
                connection.close()
        except (sqlite3.Error, OSError):
            return False

    def api_key(self) -> str:
        """The installation's API key, created on first use and kept on disk.

        Other applications send it in the ``X-Api-Key`` header instead of the
        login. It is independent of the password, so changing the password
        does not break every integration, and it can be regenerated on its
        own when it leaks.
        """

        if self._api_key is None:
            with self._api_key_lock:
                if self._api_key is None:
                    stored = _read_secret(self.api_key_path, 2 * _API_KEY_BYTES)
                    key = stored.decode("ascii", "replace").strip() if stored else ""
                    if not _API_KEY_PATTERN.match(key):
                        key = secrets.token_hex(_API_KEY_BYTES)
                        _write_secret(self.api_key_path, f"{key}\n".encode("ascii"))
                    self._api_key = key
        return self._api_key

    def regenerate_api_key(self) -> str:
        """Replace the API key; the previous one stops working at once."""

        with self._api_key_lock:
            key = secrets.token_hex(_API_KEY_BYTES)
            _write_secret(self.api_key_path, f"{key}\n".encode("ascii"))
            self._api_key = key
        return key

    def api_key_valid(self, presented: str | None) -> bool:
        if not presented:
            return False
        return compare_digest(
            presented.strip().encode("utf-8"), self.api_key().encode("ascii")
        )

    def request_authenticated(self, scope: Scope) -> bool:
        if not self.configured:
            return True
        headers = Headers(scope=scope)
        if authenticate_basic_header(
            headers.get("authorization"),
            self.settings.auth_username or "",
            self.settings.auth_password or "",
        ):
            return True
        if self.api_key_valid(headers.get(API_KEY_HEADER)):
            return True
        if self.method != "forms":
            return False
        return self.validate_session(self._session_cookie(headers.get("cookie")))

    def status(self, scope: Scope) -> dict[str, Any]:
        authenticated = self.request_authenticated(scope)
        return {
            "configured": self.configured,
            "method": self.method,
            "authenticated": authenticated,
            "username": self.settings.auth_username if authenticated else None,
        }

    def _signing_key(self) -> bytes:
        # A random secret keeps a captured cookie from being an offline
        # password oracle; mixing in the password still ends every session
        # when the password changes.
        if self._signing_secret is None:
            with self._signing_secret_lock:
                if self._signing_secret is None:
                    self._signing_secret = _load_signing_secret(
                        self.signing_secret_path
                    )
        password = (self.settings.auth_password or "").encode("utf-8")
        return hmac.new(
            self._signing_secret, b"tankarr-session-v2\0" + password, hashlib.sha256
        ).digest()

    @staticmethod
    def _session_cookie(raw_cookie: str | None) -> str | None:
        if not raw_cookie or len(raw_cookie) > 16384:
            return None
        parsed = SimpleCookie()
        try:
            parsed.load(raw_cookie)
        except CookieError:
            return None
        morsel = parsed.get(SESSION_COOKIE)
        return morsel.value if morsel is not None else None


def _public_forms_asset(path: str) -> bool:
    return (
        path == "/"
        or path.startswith("/assets/")
        or path
        in {
            "/favicon.ico",
            "/manifest.webmanifest",
        }
    )


class AuthenticationMiddleware:
    """Protect Tankarr with Basic auth or an internal forms session."""

    def __init__(self, app: ASGIApp, *, manager: AuthenticationManager) -> None:
        self.app = app
        self.manager = manager

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = str(scope.get("path") or "")
        if scope["type"] == "websocket":
            if self.manager.configured and not self.manager.request_authenticated(
                scope
            ):
                await send({"type": "websocket.close", "code": 1008})
                return
            await self.app(scope, receive, send)
            return
        if scope["type"] == "http" and path == LOGIN_PATH:
            # The only public route that reads a body: keep it small.
            length = Headers(scope=scope).get("content-length")
            if scope.get("method") == "POST" and (
                length is None
                or not length.isdigit()
                or int(length) > _MAX_LOGIN_BODY_BYTES
            ):
                await JSONResponse(
                    {"detail": "Login request body is missing or too large"},
                    status_code=413,
                    headers={"Cache-Control": "no-store"},
                )(scope, receive, send)
                return
        if (
            scope["type"] != "http"
            or not self.manager.configured
            or path in PUBLIC_HEALTHCHECK_PATHS
            or path in PUBLIC_AUTH_PATHS
            or (
                self.manager.method == "forms"
                and scope.get("method") in {"GET", "HEAD"}
                and _public_forms_asset(path)
            )
        ):
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        authorization = headers.get("authorization")
        presented_key = headers.get(API_KEY_HEADER)
        client = client_address(scope)
        if authorization or presented_key:
            wait = self.manager.throttle.retry_after(client)
            if wait > 0:
                await too_many_attempts(wait)(scope, receive, send)
                return

        if self.manager.request_authenticated(scope):
            await self.app(scope, receive, send)
            return

        claimed = basic_username(authorization)
        if claimed is not None:
            self.manager.throttle.failed(client)
            logger.warning(
                "Authentication failed for user %r from %s", claimed[:64], client
            )
        elif presented_key:
            # A wrong key is guessed like a wrong password: same backoff.
            self.manager.throttle.failed(client)
            logger.warning("Authentication failed with an API key from %s", client)

        headers = {"Cache-Control": "no-store"}
        if self.manager.method == "basic":
            headers["WWW-Authenticate"] = 'Basic realm="Tankarr", charset="UTF-8"'
        response = JSONResponse(
            {"detail": "Authentication required"},
            status_code=401,
            headers=headers,
        )
        await response(scope, receive, send)


def _same_origin_request(headers: Headers) -> bool:
    """Whether a browser sent this request from Tankarr's own pages.

    Browsers label every request with ``Sec-Fetch-Site`` (older ones only with
    ``Origin``); API clients such as curl or scripts send neither and are
    authenticated on their own. A form or script on another site, including
    another application on the same host under a different port, is refused
    before it can change anything with the user's session or cached login.
    """

    fetch_site = headers.get("sec-fetch-site")
    if fetch_site is not None:
        return fetch_site.strip().casefold() in {"same-origin", "none"}
    origin = headers.get("origin")
    if origin is None:
        return True
    try:
        origin_host = urlsplit(origin.strip()).netloc.casefold()
    except ValueError:
        return False
    if not origin_host:
        return False
    hosts = {
        value.split(",")[0].strip().casefold()
        for value in (headers.get("host"), headers.get("x-forwarded-host"))
        if value
    }
    return origin_host in hosts


class CrossOriginProtectionMiddleware:
    """Refuse state-changing requests that another site makes a browser send."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] == "http"
            and scope.get("method") not in SAFE_METHODS
            and not _same_origin_request(Headers(scope=scope))
        ):
            await JSONResponse(
                {"detail": "Cross-origin request refused"},
                status_code=403,
                headers={"Cache-Control": "no-store"},
            )(scope, receive, send)
            return
        await self.app(scope, receive, send)


SECURITY_HEADERS: tuple[tuple[bytes, bytes], ...] = (
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"same-origin"),
    (b"cross-origin-opener-policy", b"same-origin"),
    (
        b"content-security-policy",
        b"frame-ancestors 'none'; base-uri 'self'; object-src 'none'",
    ),
)
_SECURITY_HEADER_NAMES = frozenset(name for name, _ in SECURITY_HEADERS)


class SecurityHeadersMiddleware:
    """Headers that stop framing, MIME sniffing and referrer leaks."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = [
                    (name, value)
                    for name, value in message.get("headers", [])
                    if name.lower() not in _SECURITY_HEADER_NAMES
                ]
                message = {**message, "headers": [*headers, *SECURITY_HEADERS]}
            await send(message)

        await self.app(scope, receive, send_with_headers)


class RestoreSafetyMiddleware:
    """A restored installation is inspectable but cannot resume old work."""

    def __init__(self, app: ASGIApp, *, settings: Settings) -> None:
        self.app, self.settings = app, settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        inspections = {
            "/api/system/preflight",
            "/api/acquisition/preview",
            "/api/library/list/preview",
            "/api/library/repair/preview",
            "/api/system/backup",
        }
        if (
            scope["type"] == "http"
            and self.settings.restored_safe_mode
            and scope.get("method") not in {"GET", "HEAD", "OPTIONS"}
            and path not in PUBLIC_AUTH_PATHS
            and path not in inspections
            and not (
                path.startswith("/api/system/backups/") and path.endswith("/verify")
            )
        ):
            await JSONResponse(
                {
                    "detail": "Restored safe mode: inspect storage and configuration, then restart with TANKARR_RESTORED_SAFE_MODE=false to enable changes"
                },
                status_code=409,
                headers={"Cache-Control": "no-store"},
            )(scope, receive, send)
            return
        await self.app(scope, receive, send)


class BasicAuthMiddleware(AuthenticationMiddleware):
    """Backward-compatible constructor for callers using the old middleware."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        username: str | None,
        password: str | None,
    ) -> None:
        settings = Settings(
            auth_method="basic",
            auth_username=username,
            auth_password=password,
        )
        super().__init__(app, manager=AuthenticationManager(settings))
