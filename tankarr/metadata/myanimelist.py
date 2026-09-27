"""MyAnimeList, reached by the identifier MangaBaka already knows.

Read through Jikan (the public MAL mirror API) and only ever by id:
``GET /v4/manga/{mal_id}``. Never searched by title. Jikan is a best-effort
voter - it answers 5xx whenever MAL itself is unreachable - so a failure here
costs nothing but one missing opinion; the canonical record simply counts
one fewer catalogue when it decides whether a number is corroborated.
"""

from __future__ import annotations

from typing import Any

from tankarr import USER_AGENT
from tankarr.config import Settings
from tankarr.metadata.base import MetadataSource, clean_text
from tankarr.providers.base import ProviderHTTP

API_URL = "https://api.jikan.moe/v4/manga"

PUBLICATION_STATUS = {
    "finished": "ended",
    "publishing": "ongoing",
    "on hiatus": "hiatus",
    "discontinued": "abandoned",
    "not yet published": None,
}

WORK_TYPES = {
    "manga": "Manga",
    "manhwa": "Manhwa",
    "manhua": "Manhua",
    "novel": "Novel",
    "light novel": "Light Novel",
    "one-shot": "One-shot",
    "doujinshi": "Doujinshi",
}


def _positive_integer(value: object) -> int | None:
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _name(value: object) -> str:
    # Jikan writes people "Family, Given"; the catalogue convention here is
    # natural order, which the creator matcher compares either way.
    text = clean_text(value)
    if "," in text:
        family, _comma, given = text.partition(",")
        return " ".join(part for part in (given.strip(), family.strip()) if part)
    return text


def normalize_mal_manga(payload: dict[str, Any]) -> dict[str, Any]:
    """The catalogue fields Tankarr keeps from one Jikan manga resource."""

    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    external_id = clean_text(data.get("mal_id"))
    title = clean_text(data.get("title"))
    alternate: list[str] = []
    for item in data.get("titles") or []:
        value = clean_text((item or {}).get("title"))
        if value and value != title and value not in alternate:
            alternate.append(value)
    for key in ("title_english", "title_japanese"):
        value = clean_text(data.get(key))
        if value and value != title and value not in alternate:
            alternate.append(value)
    authors = [
        _name(item.get("name"))
        for item in data.get("authors") or []
        if item.get("name")
    ]
    published = data.get("published") or {}
    start = clean_text(published.get("from")) or None
    end = clean_text(published.get("to")) or None
    status = PUBLICATION_STATUS.get(clean_text(data.get("status")).casefold())
    images = ((data.get("images") or {}).get("jpg") or {}) if data.get("images") else {}
    cover_url = images.get("large_image_url") or images.get("image_url")
    return {
        "source": "myanimelist",
        "external_id": external_id,
        "url": clean_text(data.get("url"))
        or f"https://myanimelist.net/manga/{external_id}",
        "title": title,
        "alternate_titles": alternate,
        "description": clean_text(data.get("synopsis")),
        "authors": authors,
        "creators": [{"name": name, "role": "writer"} for name in authors],
        "creator_links": [],
        "genres": [
            clean_text(item.get("name"))
            for item in data.get("genres") or []
            if clean_text(item.get("name"))
        ],
        "tags": [],
        "publisher": None,
        "year": int(start[:4]) if start and start[:4].isdigit() else None,
        "status": status,
        "work_type": WORK_TYPES.get(clean_text(data.get("type")).casefold()),
        "original_language": None,
        "volume_count": _positive_integer(data.get("volumes")),
        "chapter_count": _positive_integer(data.get("chapters")),
        "latest_release_chapter": None,
        "rating": None,
        "published_start": start[:10] if start else None,
        "published_end": end[:10] if end else None,
        "cover": {"url": str(cover_url)} if cover_url else None,
        "links": [],
        "external_ids": {},
        "raw": data,
    }


class MyAnimeListMetadataSource(MetadataSource):
    name = "myanimelist"
    label = "MyAnimeList"
    # Exact identifier only, and best effort: Jikan fails whenever MAL does.
    automatic_matching = False

    def __init__(self, settings: Settings):
        self.settings = settings
        # Jikan asks for at most three requests per second; one keeps a
        # library refresh polite and well clear of its daily budget.
        self.api = ProviderHTTP(
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            timeout_seconds=settings.request_timeout_seconds,
            requests_per_second=1.0,
        )

    @property
    def configured(self) -> bool:
        return True

    async def search_series(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        return []

    async def get_series(self, external_id: str) -> dict[str, Any]:
        token = clean_text(external_id)
        if not token.isdigit():
            raise ValueError("MyAnimeList identifiers are numeric")
        response = await self.api.request("GET", f"{API_URL}/{token}")
        payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
            message = (
                clean_text((payload or {}).get("message"))
                if isinstance(payload, dict)
                else ""
            )
            raise RuntimeError(f"MyAnimeList: {message or 'no manga resource'}")
        return normalize_mal_manga(payload)

    async def aclose(self) -> None:
        await self.api.aclose()


__all__ = ["MyAnimeListMetadataSource", "normalize_mal_manga"]
