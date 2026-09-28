from __future__ import annotations

import logging

import httpx

from tankarr.config import Settings
from tankarr.http import async_client

logger = logging.getLogger(__name__)


class NtfyNotifier:
    """Optional push notifications through a self-hosted ntfy topic."""

    def __init__(self, settings: Settings):
        self.settings = settings

    @property
    def configured(self) -> bool:
        return bool(self.settings.ntfy_url and self.settings.ntfy_topic)

    async def _publish(
        self,
        title: str,
        message: str,
        *,
        tags: str,
        priority: str,
    ) -> httpx.Response:
        priority_number = {
            "min": 1,
            "low": 2,
            "default": 3,
            "high": 4,
            "max": 5,
            "urgent": 5,
        }.get(priority.casefold(), 3)
        async with async_client(timeout=10) as client:
            response = await client.post(
                self.settings.ntfy_url.rstrip("/"),
                json={
                    "topic": self.settings.ntfy_topic,
                    "title": title,
                    "message": message,
                    "tags": [tag.strip() for tag in tags.split(",") if tag.strip()],
                    "priority": priority_number,
                },
            )
            response.raise_for_status()
        return response

    async def send(
        self,
        title: str,
        message: str,
        *,
        tags: str = "books",
        priority: str = "default",
    ) -> bool:
        """Best-effort delivery: a notification failure never fails the caller."""

        if not self.configured:
            return False
        try:
            await self._publish(
                title,
                message,
                tags=tags,
                priority=priority,
            )
            return True
        except Exception as exc:  # noqa: BLE001 - notifications are advisory
            logger.warning("Unable to send ntfy notification: %s", exc)
            return False

    async def test_delivery(self) -> dict[str, object]:
        """Publish a real probe while returning operator-safe diagnostics."""

        if not self.configured:
            return {
                "ok": False,
                "error": "Set both the ntfy URL and topic before testing",
            }
        try:
            response = await self._publish(
                "Tankarr connection test",
                "Tankarr successfully reached this ntfy topic. Notifications are ready.",
                tags="test_tube,white_check_mark",
                priority="default",
            )
        except httpx.HTTPStatusError as exc:
            return {
                "ok": False,
                "error": f"ntfy rejected the test with HTTP {exc.response.status_code}",
            }
        except httpx.RequestError as exc:
            return {
                "ok": False,
                "error": f"Unable to reach ntfy ({type(exc).__name__})",
            }
        except Exception as exc:  # noqa: BLE001 - probe failure is the result
            return {
                "ok": False,
                "error": f"Unable to publish the test ({type(exc).__name__})",
            }
        return {
            "ok": True,
            "status_code": response.status_code,
            "topic": self.settings.ntfy_topic,
        }

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
        )

    async def job_failed(self, manga_title: str, detail: str) -> bool:
        if not self.settings.ntfy_on_download_failed:
            return False
        return await self.send(
            f"Download failed — {manga_title}",
            detail[:500],
            tags="books,warning",
            priority="high",
        )
