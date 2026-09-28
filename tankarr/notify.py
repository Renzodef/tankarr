"""Notifications to the operator: ntfy, a generic webhook, Discord, Telegram, Apprise.

Every configured channel receives every event, and the three event switches
(chapter imported, download failed, decision needed) gate all of them, like
the Connections of the other *arr applications. Delivery is best effort: a
failing channel never fails the import or the job that triggered it, and no
secret (a Discord webhook URL, a Telegram bot token) reaches a log line or an
API response; failures are described by their type and HTTP status only.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

from tankarr import USER_AGENT, __version__
from tankarr.config import Settings
from tankarr.http import async_client

logger = logging.getLogger(__name__)

PRIORITY_NUMBERS = {"min": 1, "low": 2, "default": 3, "high": 4, "max": 5, "urgent": 5}
_URGENT = {"high", "max", "urgent"}


@dataclass(frozen=True)
class Notification:
    event: str
    title: str
    message: str
    priority: str = "default"
    tags: tuple[str, ...] = ("books",)
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def urgent(self) -> bool:
        return self.priority.casefold() in _URGENT


def _describe(exc: BaseException) -> str:
    """A failure without the URL httpx puts in its messages (it may hold a token)."""

    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    return type(exc).__name__


class Channel:
    name = ""
    label = ""
    unconfigured_error = ""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @property
    def configured(self) -> bool:  # pragma: no cover - overridden
        return False

    @property
    def test_message(self) -> str:
        return f"Tankarr successfully reached {self.label}. Notifications are ready."

    def test_result(self, response: httpx.Response) -> dict[str, object]:
        return {"ok": True, "status_code": response.status_code}

    async def deliver(self, notification: Notification) -> httpx.Response:
        raise NotImplementedError


class NtfyChannel(Channel):
    name = "ntfy"
    label = "ntfy"
    unconfigured_error = "Set both the ntfy URL and topic before testing"

    @property
    def configured(self) -> bool:
        return bool(self.settings.ntfy_url and self.settings.ntfy_topic)

    @property
    def test_message(self) -> str:
        return "Tankarr successfully reached this ntfy topic. Notifications are ready."

    def test_result(self, response: httpx.Response) -> dict[str, object]:
        return {**super().test_result(response), "topic": self.settings.ntfy_topic}

    async def deliver(self, notification: Notification) -> httpx.Response:
        async with async_client(timeout=10) as client:
            response = await client.post(
                str(self.settings.ntfy_url).rstrip("/"),
                json={
                    "topic": self.settings.ntfy_topic,
                    "title": notification.title,
                    "message": notification.message,
                    "tags": list(notification.tags),
                    "priority": PRIORITY_NUMBERS.get(
                        notification.priority.casefold(), 3
                    ),
                },
            )
            response.raise_for_status()
        return response


class WebhookChannel(Channel):
    """One JSON document per event, for Home Assistant, n8n, a script of yours."""

    name = "webhook"
    label = "the webhook"
    unconfigured_error = "Set the webhook URL before testing"

    @property
    def configured(self) -> bool:
        return bool(self.settings.webhook_url)

    async def deliver(self, notification: Notification) -> httpx.Response:
        headers = {"User-Agent": USER_AGENT}
        if self.settings.webhook_token:
            headers["Authorization"] = f"Bearer {self.settings.webhook_token}"
        payload = {
            "application": "Tankarr",
            "version": __version__,
            "event": notification.event,
            "title": notification.title,
            "message": notification.message,
            "priority": notification.priority,
            "tags": list(notification.tags),
            "at": datetime.now(UTC).isoformat(timespec="seconds"),
            "data": notification.data,
        }
        async with async_client(timeout=10, follow_redirects=False) as client:
            response = await client.post(
                str(self.settings.webhook_url), json=payload, headers=headers
            )
            response.raise_for_status()
        return response


class DiscordChannel(Channel):
    name = "discord"
    label = "Discord"
    unconfigured_error = "Set the Discord webhook URL before testing"

    @property
    def configured(self) -> bool:
        return bool(self.settings.discord_webhook_url)

    async def deliver(self, notification: Notification) -> httpx.Response:
        embed = {
            "title": notification.title[:256],
            "description": notification.message[:4096],
            "color": 0xE74C3C if notification.urgent else 0x3498DB,
            "timestamp": datetime.now(UTC).isoformat(timespec="seconds"),
            "footer": {"text": f"Tankarr {__version__}"},
        }
        async with async_client(timeout=10, follow_redirects=False) as client:
            response = await client.post(
                str(self.settings.discord_webhook_url),
                json={"username": "Tankarr", "embeds": [embed]},
                headers={"User-Agent": USER_AGENT},
            )
            response.raise_for_status()
        return response


class TelegramChannel(Channel):
    name = "telegram"
    label = "Telegram"
    unconfigured_error = "Set both the Telegram bot token and chat ID before testing"

    @property
    def configured(self) -> bool:
        return bool(self.settings.telegram_bot_token and self.settings.telegram_chat_id)

    async def deliver(self, notification: Notification) -> httpx.Response:
        url = f"https://api.telegram.org/bot{self.settings.telegram_bot_token}/sendMessage"
        async with async_client(timeout=10) as client:
            response = await client.post(
                url,
                json={
                    "chat_id": self.settings.telegram_chat_id,
                    "text": f"{notification.title}\n\n{notification.message}"[:4096],
                    "disable_web_page_preview": True,
                },
                headers={"User-Agent": USER_AGENT},
            )
            response.raise_for_status()
        return response


class AppriseChannel(Channel):
    """An Apprise API server: one stored configuration key, or the URLs directly."""

    name = "apprise"
    label = "Apprise"
    unconfigured_error = (
        "Set the Apprise server URL and a configuration key or notification URLs"
    )

    @property
    def configured(self) -> bool:
        return bool(
            self.settings.apprise_url
            and (self.settings.apprise_key or self.settings.apprise_urls)
        )

    async def deliver(self, notification: Notification) -> httpx.Response:
        root = str(self.settings.apprise_url).rstrip("/")
        payload: dict[str, Any] = {
            "title": notification.title,
            "body": notification.message,
            "type": "failure" if notification.urgent else "info",
            "format": "text",
        }
        if self.settings.apprise_key:
            endpoint = f"{root}/notify/{self.settings.apprise_key}"
        else:
            endpoint = f"{root}/notify"
            payload["urls"] = str(self.settings.apprise_urls)
        async with async_client(timeout=15, follow_redirects=False) as client:
            response = await client.post(
                endpoint, json=payload, headers={"User-Agent": USER_AGENT}
            )
            response.raise_for_status()
        return response


CHANNELS: tuple[type[Channel], ...] = (
    NtfyChannel,
    WebhookChannel,
    DiscordChannel,
    TelegramChannel,
    AppriseChannel,
)
# The unsaved form fields each channel's connection test may use.
CHANNEL_SETTINGS: dict[str, frozenset[str]] = {
    "ntfy": frozenset({"ntfy_url", "ntfy_topic"}),
    "webhook": frozenset({"webhook_url", "webhook_token"}),
    "discord": frozenset({"discord_webhook_url"}),
    "telegram": frozenset({"telegram_bot_token", "telegram_chat_id"}),
    "apprise": frozenset({"apprise_url", "apprise_key", "apprise_urls"}),
}


class Notifier:
    """Fan every event out to the configured channels."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.channels: tuple[Channel, ...] = tuple(
            channel(settings) for channel in CHANNELS
        )

    @property
    def configured(self) -> bool:
        return any(channel.configured for channel in self.channels)

    def configured_channels(self) -> dict[str, bool]:
        return {channel.name: channel.configured for channel in self.channels}

    def channel(self, name: str) -> Channel | None:
        for channel in self.channels:
            if channel.name == name:
                return channel
        return None

    async def deliver(self, notification: Notification) -> bool:
        """True when at least one channel accepted the notification."""

        active = [channel for channel in self.channels if channel.configured]
        if not active:
            return False
        outcomes = await asyncio.gather(
            *(self._deliver_one(channel, notification) for channel in active)
        )
        return any(outcomes)

    async def _deliver_one(self, channel: Channel, notification: Notification) -> bool:
        try:
            await channel.deliver(notification)
            return True
        except Exception as exc:  # noqa: BLE001 - notifications are advisory
            logger.warning(
                "Unable to send the %s notification (%s)", channel.label, _describe(exc)
            )
            return False

    async def send(
        self,
        title: str,
        message: str,
        *,
        tags: str | tuple[str, ...] | list[str] = "books",
        priority: str = "default",
        event: str = "custom",
        data: dict[str, Any] | None = None,
    ) -> bool:
        """Best-effort delivery: a notification failure never fails the caller."""

        if not self.configured:
            return False
        tag_list = (
            tuple(tag.strip() for tag in tags.split(",") if tag.strip())
            if isinstance(tags, str)
            else tuple(tags)
        )
        return await self.deliver(
            Notification(event, title, message, priority, tag_list, dict(data or {}))
        )

    async def test_delivery(self, channel_name: str = "ntfy") -> dict[str, object]:
        """Publish a real probe through one channel; the result never quotes a URL."""

        channel = self.channel(channel_name)
        if channel is None:
            return {"ok": False, "error": "Unknown notification channel"}
        if not channel.configured:
            return {"ok": False, "error": channel.unconfigured_error}
        probe = Notification(
            "test",
            "Tankarr connection test",
            channel.test_message,
            tags=("test_tube", "white_check_mark"),
        )
        try:
            response = await channel.deliver(probe)
        except httpx.HTTPStatusError as exc:
            return {
                "ok": False,
                "error": f"{channel.label} rejected the test with HTTP {exc.response.status_code}",
            }
        except httpx.RequestError as exc:
            return {
                "ok": False,
                "error": f"Unable to reach {channel.label} ({type(exc).__name__})",
            }
        except Exception as exc:  # noqa: BLE001 - probe failure is the result
            return {
                "ok": False,
                "error": f"Unable to publish the test ({type(exc).__name__})",
            }
        return channel.test_result(response)

    async def chapter_imported(self, manga: dict, chapter: dict) -> bool:
        if not self.settings.ntfy_on_chapter_imported:
            return False
        parts: list[str] = []
        if chapter.get("volume") not in (None, ""):
            parts.append(f"Volume {chapter['volume']}")
        if chapter.get("chapter") not in (None, ""):
            parts.append(f"Chapter {chapter['chapter']}")
        release = " · ".join(parts) or "Special"
        return await self.send(
            f"{manga['title']} — {release}",
            f"Imported [{chapter.get('language', '?')}] from {chapter.get('provider', '?')}",
            tags="books,white_check_mark",
            event="chapter_imported",
            data={
                "manga_id": manga.get("id"),
                "manga_title": manga.get("title"),
                "chapter": chapter.get("chapter"),
                "volume": chapter.get("volume"),
                "language": chapter.get("language"),
                "provider": chapter.get("provider"),
                "library_path": chapter.get("library_path"),
            },
        )

    async def decision_needed(self, summary: str, detail: str) -> bool:
        """Something only the operator can settle: a review, or a slot that
        no channel will ever fill. Sent once per new item, never repeated."""

        if not self.settings.ntfy_on_decision_needed:
            return False
        return await self.send(
            f"Tankarr needs a decision — {summary}"[:120],
            detail[:1000],
            tags="books,raised_hand",
            priority="default",
            event="decision_needed",
            data={"summary": summary},
        )

    async def job_failed(self, manga_title: str, detail: str) -> bool:
        if not self.settings.ntfy_on_download_failed:
            return False
        return await self.send(
            f"Download failed — {manga_title}",
            detail[:500],
            tags="books,warning",
            priority="high",
            event="download_failed",
            data={"manga_title": manga_title},
        )


# The name the rest of Tankarr grew up with.
NtfyNotifier = Notifier
