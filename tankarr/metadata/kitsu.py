"""Kitsu, reached by the identifier MangaBaka already knows.

Kitsu is never searched by title. MangaBaka's record carries the Kitsu id of
the work, so the only request this source ever makes is ``GET /manga/{id}``:
deterministic, one work in, that same work out. What it contributes is a
third opinion on the counts - chapters, volumes, status - that lets the
canonical record say whether MangaBaka's numbers are corroborated or stand
alone.
"""

from __future__ import annotations

from typing import Any

from tankarr import USER_AGENT
from tankarr.config import Settings
from tankarr.metadata.base import MetadataSource, clean_text
from tankarr.providers.base import ProviderHTTP

API_URL = "https://kitsu.io/api/edge/manga"

PUBLICATION_STATUS = {
    "finished": "ended",
    "current": "ongoing",
    "tba": None,
    "unreleased": None,
    "upcoming": None,
}

WORK_TYPES = {
    "manga": "Manga",
    "manhwa": "Manhwa",
    "manhua": "Manhua",
    "oel": "OEL",
    "novel": "Novel",
    "oneshot": "One-shot",
    "doujin": "Doujinshi",
}


def _positive_integer(value: object) -> int | None:
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def normalize_kitsu_manga(payload: dict[str, Any]) -> dict[str, Any]:
    """The catalogue fields Tankarr keeps from one Kitsu manga resource."""

    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    attributes = data.get("attributes") or {}
    external_id = clean_text(data.get("id"))
    titles = attributes.get("titles") or {}
    canonical = clean_text(attributes.get("canonicalTitle"))
    alternate = [
        clean_text(value)
        for key, value in titles.items()
        if clean_text(value) and clean_text(value) != canonical
    ]
    for value in attributes.get("abbreviatedTitles") or []:
        if clean_text(value) and clean_text(value) not in alternate:
            alternate.append(clean_text(value))
    status = PUBLICATION_STATUS.get(clean_text(attributes.get("status")).casefold())
    start = clean_text(attributes.get("startDate")) or None
    end = clean_text(attributes.get("endDate")) or None
    poster = attributes.get("posterImage") or {}
    cover_url = poster.get("original") or poster.get("large") or poster.get("medium")
    return {
        "source": "kitsu",
        "external_id": external_id,
        "url": f"https://kitsu.io/manga/{attributes.get('slug') or external_id}",
        "title": canonical,
        "alternate_titles": alternate,
        "description": clean_text(attributes.get("synopsis")),
        "authors": [],
        "creators": [],
        "creator_links": [],
        "genres": [],
        "tags": [],
        "publisher": None,
        "year": int(start[:4]) if start and start[:4].isdigit() else None,
        "status": status,
        "work_type": WORK_TYPES.get(clean_text(attributes.get("mangaType")).casefold()),
        "original_language": None,
        "volume_count": _positive_integer(attributes.get("volumeCount")),
        "chapter_count": _positive_integer(attributes.get("chapterCount")),
        "latest_release_chapter": None,
        "rating": None,
        "published_start": start,
        "published_end": end,
        "cover": {"url": str(cover_url)} if cover_url else None,
        "links": [],
        "external_ids": {},
        "raw": data,
    }


class KitsuMetadataSource(MetadataSource):
    name = "kitsu"
    label = "Kitsu"
    # Exact identifier only: this catalogue corroborates, it never discovers.
    automatic_matching = False

    def __init__(self, settings: Settings):
        self.settings = settings
        self.api = ProviderHTTP(
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/vnd.api+json",
            },
            timeout_seconds=settings.request_timeout_seconds,
            requests_per_second=2.0,
        )

    @property
    def configured(self) -> bool:
        return True

    async def search_series(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        return []

    async def get_series(self, external_id: str) -> dict[str, Any]:
        token = clean_text(external_id)
        if not token.isdigit():
            raise ValueError("Kitsu identifiers are numeric")
        response = await self.api.request("GET", f"{API_URL}/{token}")
        payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
            raise RuntimeError("Kitsu returned no manga resource")
        return normalize_kitsu_manga(payload)

    async def aclose(self) -> None:
        await self.api.aclose()


__all__ = ["KitsuMetadataSource", "normalize_kitsu_manga"]
