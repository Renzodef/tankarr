"""Serve Tankarr under a sub-path behind a reverse proxy: the *arr "URL base".

``TANKARR_URL_BASE=/tankarr`` makes the interface and the API answer under
that prefix, as ``/tankarr/`` and ``/tankarr/api/...``. The middleware records
the prefix as the ASGI ``root_path`` of every request under it, which is how
Starlette routes a prefixed path and builds prefixed URLs, so the rest of
Tankarr keeps its root-relative routes. ``/tankarr`` redirects to ``/tankarr/``
so the interface's relative asset URLs resolve against the right directory.

The health checks stay reachable at the root too, so a Docker ``HEALTHCHECK``
or an uptime monitor does not have to know the base. Everything else outside
the prefix is not found, as with Sonarr.
"""

from __future__ import annotations

from starlette.responses import JSONResponse, RedirectResponse
from starlette.types import ASGIApp, Receive, Scope, Send

ROOT_HEALTH_PATHS = frozenset(
    {"/api/health", "/api/ready", "/api/system/health", "/api/system/ready"}
)


def route_path(scope: Scope) -> str:
    """The path inside the application: the request path without the URL base."""

    path = str(scope.get("path") or "")
    root = str(scope.get("root_path") or "")
    if (
        root
        and path.startswith(root)
        and (len(path) == len(root) or path[len(root)] == "/")
    ):
        return path[len(root) :] or "/"
    return path


def normalize_url_base(value: object) -> str:
    """``/tankarr`` from ``tankarr``, ``/tankarr/`` or `` /tankarr ``; ``""`` for the root."""

    text = str(value or "").strip()
    if text in {"", "/"}:
        return ""
    if any(character in text for character in "?#\\ ") or "//" in text:
        raise ValueError("url_base must be a plain path such as /tankarr")
    normalized = "/" + text.strip("/")
    if normalized in {"/api", "/assets"}:
        raise ValueError("url_base cannot shadow Tankarr's own /api or /assets paths")
    return normalized


class UrlBaseMiddleware:
    """Mount the whole application under ``url_base``."""

    def __init__(self, app: ASGIApp, *, url_base: str) -> None:
        self.app = app
        self.url_base = normalize_url_base(url_base)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        base = self.url_base
        if not base or scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        path = str(scope.get("path") or "")
        if path == base and scope["type"] == "http":
            query = bytes(scope.get("query_string") or b"").decode("latin-1")
            location = f"{base}/" + (f"?{query}" if query else "")
            await RedirectResponse(location, status_code=307)(scope, receive, send)
            return
        if path.startswith(f"{base}/"):
            prefixed = {**scope, "root_path": str(scope.get("root_path") or "") + base}
            await self.app(prefixed, receive, send)
            return
        if path in ROOT_HEALTH_PATHS and scope["type"] == "http":
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        await JSONResponse(
            {"detail": f"Tankarr is served under {base}"},
            status_code=404,
            headers={"Cache-Control": "no-store"},
        )(scope, receive, send)
