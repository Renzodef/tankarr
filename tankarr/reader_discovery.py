"""Bounded discovery of reader services available to Tankarr.

Discovery only contacts explicitly configured roots and the conventional
Docker service names for supported readers.  It never scans the LAN and never
returns credentials to the caller.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Literal

import httpx

from tankarr.config import Settings
from tankarr.http import async_client
from tankarr.readers import READER_LABELS, effective_reader_kind

ReaderKind = Literal["stump", "komga", "kavita"]
CredentialState = Literal["accepted", "required", "rejected"]


@dataclass(frozen=True)
class ReaderTarget:
    kind: ReaderKind
    internal_url: str
    browser_url: str | None
    browser_port: int
    preferred: bool = False


_DEFAULT_TARGETS: tuple[ReaderTarget, ...] = (
    ReaderTarget("stump", "http://stump:10801", None, 10801),
    ReaderTarget("komga", "http://komga:25600", None, 25600),
    ReaderTarget("kavita", "http://kavita:5000", None, 5000),
)


def _service_root(value: str | None) -> str | None:
    normalized = str(value or "").strip().rstrip("/")
    return normalized or None


def reader_targets(settings: Settings) -> list[ReaderTarget]:
    """Return deterministic, de-duplicated roots that are safe to probe."""

    targets: list[ReaderTarget] = []
    current = effective_reader_kind(settings)
    if current in {"stump", "komga", "kavita"}:
        browser_url = _service_root(settings.reader_url)
        configured_internal = _service_root(settings.reader_internal_url)
        internal_url = configured_internal or browser_url
        if internal_url:
            port = {"stump": 10801, "komga": 25600, "kavita": 5000}[current]
            targets.append(
                ReaderTarget(
                    current,
                    internal_url,
                    browser_url,
                    port,
                    preferred=configured_internal is not None,
                )
            )
            if (
                configured_internal
                and browser_url
                and browser_url != configured_internal
            ):
                targets.append(ReaderTarget(current, browser_url, browser_url, port))

    legacy_komga_internal = _service_root(settings.komga_internal_url)
    legacy_komga = legacy_komga_internal or _service_root(settings.komga_url)
    if legacy_komga:
        targets.append(
            ReaderTarget(
                "komga",
                legacy_komga,
                _service_root(settings.komga_url),
                25600,
                preferred=current == "komga" and legacy_komga_internal is not None,
            )
        )
        legacy_browser = _service_root(settings.komga_url)
        if (
            legacy_komga_internal
            and legacy_browser
            and legacy_browser != legacy_komga_internal
        ):
            targets.append(ReaderTarget("komga", legacy_browser, legacy_browser, 25600))

    for default in _DEFAULT_TARGETS:
        browser_url = (
            _service_root(settings.reader_url)
            if current == default.kind
            else _service_root(settings.komga_url)
            if default.kind == "komga"
            else None
        )
        targets.append(
            ReaderTarget(
                default.kind,
                default.internal_url,
                browser_url,
                default.browser_port,
                preferred=current == default.kind,
            )
        )

    unique: list[ReaderTarget] = []
    seen: set[tuple[str, str]] = set()
    for target in targets:
        identity = (target.kind, target.internal_url.casefold())
        if identity in seen:
            continue
        seen.add(identity)
        unique.append(target)
    return unique


def _match(
    target: ReaderTarget,
    credentials: CredentialState,
    detail: str,
) -> dict[str, Any]:
    return {
        "kind": target.kind,
        "label": READER_LABELS[target.kind],
        "internal_url": target.internal_url,
        "browser_url": target.browser_url,
        "browser_port": target.browser_port,
        "credentials": credentials,
        "detail": detail,
        "preferred": target.preferred,
    }


async def _probe_stump(
    target: ReaderTarget, settings: Settings, client: httpx.AsyncClient
) -> dict[str, Any] | None:
    try:
        health = await client.get(f"{target.internal_url}/api/v2/health")
        if health.status_code != 200:
            return None
        payload = health.json()
        if not isinstance(payload, dict) or payload.get("status") != "ok":
            return None
    except (httpx.HTTPError, ValueError):
        return None

    username = str(settings.reader_username or "")
    password = str(settings.reader_password or "")
    if not username or not password:
        return _match(target, "required", "Stump found; enter a username and password")
    try:
        response = await client.post(
            f"{target.internal_url}/api/v2/auth/login",
            params={"generate_token": "true", "create_session": "false"},
            json={"username": username, "password": password},
        )
    except httpx.HTTPError:
        return _match(
            target, "rejected", "Stump found; credentials could not be checked"
        )
    if response.status_code in {401, 403}:
        return _match(
            target, "rejected", "Stump found; saved credentials were rejected"
        )
    try:
        response.raise_for_status()
        token = str((response.json() or {}).get("accessToken") or "")
    except (httpx.HTTPError, ValueError, AttributeError):
        token = ""
    if not token:
        return _match(target, "rejected", "Stump found; login returned no access token")
    return _match(target, "accepted", "Stump found; saved credentials accepted")


async def _html_identifies(
    target: ReaderTarget, client: httpx.AsyncClient, product: str
) -> bool:
    try:
        response = await client.get(target.internal_url)
    except httpx.HTTPError:
        return False
    if response.status_code != 200:
        return False
    content_type = response.headers.get("content-type", "").casefold()
    return (
        "text/html" in content_type
        and f"<title>{product.casefold()}" in response.text.casefold()
    )


async def _probe_komga(
    target: ReaderTarget, settings: Settings, client: httpx.AsyncClient
) -> dict[str, Any] | None:
    api_key = str(settings.reader_api_key or settings.komga_api_key or "")
    response: httpx.Response | None = None
    try:
        response = await client.get(
            f"{target.internal_url}/api/v1/libraries",
            headers={"X-API-Key": api_key} if api_key else None,
        )
        if response.status_code == 200 and isinstance(response.json(), list):
            return _match(target, "accepted", "Komga found; saved API key accepted")
    except (httpx.HTTPError, ValueError):
        pass

    if not await _html_identifies(target, client, "Komga"):
        return None
    if not api_key:
        return _match(target, "required", "Komga found; enter an API key")
    if response is not None and response.status_code in {401, 403}:
        return _match(target, "rejected", "Komga found; saved API key was rejected")
    return _match(target, "rejected", "Komga found; API key could not be checked")


async def _probe_kavita(
    target: ReaderTarget, settings: Settings, client: httpx.AsyncClient
) -> dict[str, Any] | None:
    if not await _html_identifies(target, client, "Kavita"):
        return None
    api_key = str(settings.reader_api_key or "")
    if not api_key:
        return _match(target, "required", "Kavita found; enter an API key")
    try:
        response = await client.post(
            f"{target.internal_url}/api/Plugin/authenticate",
            params={"apiKey": api_key, "pluginName": "Tankarr"},
        )
    except httpx.HTTPError:
        return _match(target, "rejected", "Kavita found; API key could not be checked")
    if response.status_code in {401, 403}:
        return _match(target, "rejected", "Kavita found; saved API key was rejected")
    try:
        response.raise_for_status()
        token = str((response.json() or {}).get("token") or "")
    except (httpx.HTTPError, ValueError, AttributeError):
        token = ""
    if not token:
        return _match(target, "rejected", "Kavita found; login returned no token")
    return _match(target, "accepted", "Kavita found; saved API key accepted")


async def _probe_target(
    target: ReaderTarget, settings: Settings, client: httpx.AsyncClient
) -> dict[str, Any] | None:
    if target.kind == "stump":
        return await _probe_stump(target, settings, client)
    if target.kind == "komga":
        return await _probe_komga(target, settings, client)
    return await _probe_kavita(target, settings, client)


async def discover_reader(
    settings: Settings, *, client: httpx.AsyncClient | None = None
) -> dict[str, Any]:
    """Discover supported readers and choose only an unambiguous result."""

    targets = reader_targets(settings)
    owned = client is None
    if client is None:
        client = async_client(
            timeout=httpx.Timeout(3.0, connect=1.0), follow_redirects=True
        )
    try:
        probed = await asyncio.gather(
            *(_probe_target(target, settings, client) for target in targets)
        )
    finally:
        if owned:
            await client.aclose()

    # Collapse alternate routes to the same product. Prefer a route that
    # authenticated, then the currently configured route.
    by_kind: dict[str, dict[str, Any]] = {}
    credential_score = {"accepted": 2, "required": 1, "rejected": 0}
    for result in probed:
        if result is None:
            continue
        existing = by_kind.get(result["kind"])
        score = (credential_score[result["credentials"]], bool(result["preferred"]))
        old_score = (
            (
                credential_score[existing["credentials"]],
                bool(existing["preferred"]),
            )
            if existing
            else (-1, False)
        )
        if existing is None or score > old_score:
            by_kind[result["kind"]] = result

    order = {"stump": 0, "komga": 1, "kavita": 2}
    matches = sorted(by_kind.values(), key=lambda item: order[item["kind"]])
    current = effective_reader_kind(settings)
    selected = next(
        (item for item in matches if item["kind"] == current),
        matches[0] if len(matches) == 1 else None,
    )
    for item in matches:
        item.pop("preferred", None)

    if selected is not None:
        message = selected["detail"]
    elif matches:
        labels = ", ".join(item["label"] for item in matches)
        message = (
            f"Several readers were found ({labels}); select one and run discovery again"
        )
    else:
        message = "No supported reader was found on configured or Docker service routes"
    return {
        "found": bool(matches),
        "selected": selected,
        "matches": matches,
        "checked": len(targets),
        "message": message,
    }


__all__ = ["ReaderTarget", "discover_reader", "reader_targets"]
