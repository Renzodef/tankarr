"""Daily check for a newer Tankarr release, shown on the System page.

The check asks the GitHub releases API once a day, like the other *arr
applications do, and never downloads or installs anything: the operator pulls
the new image. It sends nothing but the request itself, whose user agent
carries the running version.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import UTC, datetime
from typing import Any

import httpx

from tankarr import PROJECT_URL, USER_AGENT

logger = logging.getLogger(__name__)

_REPOSITORY = PROJECT_URL.removeprefix("https://github.com/").strip("/")
RELEASES_API = f"https://api.github.com/repos/{_REPOSITORY}/releases/latest"
RELEASES_URL = f"{PROJECT_URL}/releases"
STARTUP_DELAY_SECONDS = 120.0
CHECK_INTERVAL_SECONDS = 24 * 3600.0
RETRY_INTERVAL_SECONDS = 3600.0
_VERSION = re.compile(r"^v?(\d+(?:\.\d+)*)")


def parse_version(text: str | None) -> tuple[int, ...] | None:
    """The numeric part of a version or tag: ``v1.2.0-beta.1`` is ``(1, 2, 0)``."""

    match = _VERSION.match(str(text or "").strip())
    if match is None:
        return None
    return tuple(int(part) for part in match.group(1).split("."))


def is_newer(candidate: str | None, current: str | None) -> bool:
    """Whether ``candidate`` names a higher version than ``current``."""

    latest = parse_version(candidate)
    running = parse_version(current)
    if latest is None or running is None:
        return False
    width = max(len(latest), len(running))
    return latest + (0,) * (width - len(latest)) > running + (0,) * (
        width - len(running)
    )


class UpdateChecker:
    def __init__(
        self,
        current: str,
        *,
        enabled: bool = True,
        api_url: str = RELEASES_API,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self.current = current
        self.enabled = enabled
        self.api_url = api_url
        self._http = http
        self._latest: str | None = None
        self._url: str | None = None
        self._checked_at: str | None = None
        self._error: str | None = None

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "current": self.current,
            "latest": self._latest,
            "update_available": is_newer(self._latest, self.current),
            "url": self._url,
            "checked_at": self._checked_at,
            "error": self._error,
        }

    async def check(self) -> dict[str, Any]:
        """Ask GitHub for the latest release; a failure is reported, not raised."""

        client = self._http or httpx.AsyncClient(
            timeout=httpx.Timeout(15.0, connect=10.0)
        )
        try:
            response = await client.get(
                self.api_url,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "application/vnd.github+json",
                },
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("unexpected release payload")
        except (httpx.HTTPError, ValueError) as exc:
            self._error = f"{type(exc).__name__}: {exc}"[:200]
            logger.info("Update check failed: %s", self._error)
        else:
            tag = str(payload.get("tag_name") or "").strip()
            self._latest = tag.removeprefix("v") or None
            self._url = str(payload.get("html_url") or RELEASES_URL)
            self._error = None
            if is_newer(self._latest, self.current):
                logger.info(
                    "Tankarr %s is available (running %s): %s",
                    self._latest,
                    self.current,
                    self._url,
                )
        finally:
            if self._http is None:
                await client.aclose()
        self._checked_at = datetime.now(UTC).isoformat(timespec="seconds")
        return self.status()

    async def run(self) -> None:
        """Check once after startup, then daily; hourly while GitHub is unreachable."""

        if not self.enabled:
            return
        await asyncio.sleep(STARTUP_DELAY_SECONDS)
        while True:
            try:
                await self.check()
            except Exception as exc:  # noqa: BLE001 - the loop must survive
                logger.warning("Update check crashed: %s", exc)
            await asyncio.sleep(
                RETRY_INTERVAL_SECONDS if self._error else CHECK_INTERVAL_SECONDS
            )
