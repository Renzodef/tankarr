"""Identity-first series: a series is a catalogue record, not a download URL.

Add New searches the metadata catalogue (MangaBaka) and adds *works*. A
work carries no download provider of its own: chapters arrive only through
release sources (Suwayomi extensions, Prowlarr indexers) that Tankarr maps
to the work by identity. This mirrors Sonarr, where a series is a TVDB
identity and indexers are just where releases come from.

``provider == "catalogue"`` therefore joins ``"local"`` as a provider with
no remote to refresh; everything that used to special-case local series
now consults :data:`NO_REMOTE_PROVIDERS`.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

CATALOGUE_PROVIDER = "catalogue"
CATALOGUE_SOURCE = "mangabaka"
CATALOGUE_LABEL = "MangaBaka"
NO_REMOTE_PROVIDERS = frozenset({"local", CATALOGUE_PROVIDER})

_ID_PATTERN = re.compile(r"^(?:(mb|mangabaka):)?([1-9][0-9]*)$", re.IGNORECASE)
_URL_PATTERN = re.compile(
    r"mangabaka\.(?:org|dev)/(?:series/)?([1-9][0-9]*)", re.IGNORECASE
)


def parse_catalogue_id(value: object) -> str | None:
    """``"mb:123"``, ``"123"`` or a MangaBaka URL → ``"123"``; else ``None``."""

    raw = str(value or "").strip()
    match = _ID_PATTERN.match(raw)
    if match:
        return match.group(2)
    match = _URL_PATTERN.search(raw)
    return match.group(1) if match else None


def catalogue_id(external_id: object) -> str:
    return f"mb:{external_id}"


def cover_url(record: dict[str, Any]) -> str | None:
    cover = record.get("cover")
    if isinstance(cover, dict) and cover.get("url"):
        return str(cover["url"])
    return None


def catalogue_card(record: dict[str, Any]) -> dict[str, Any]:
    """Add New card for one catalogue record (MangaSummary-compatible)."""

    external_id = str(record.get("external_id") or "")
    title = str(record.get("title") or "").strip()
    localized = record.get("localized_titles") or {}
    display_title = str(localized.get("en") or title or "").strip() or title
    return {
        "id": catalogue_id(external_id),
        "provider": CATALOGUE_PROVIDER,
        "catalogue_source": CATALOGUE_SOURCE,
        "external_id": external_id,
        "title": display_title,
        "native_title": title if title != display_title else None,
        "alternate_titles": list(record.get("alternate_titles") or []),
        "description": str(record.get("description") or ""),
        "cover_url": cover_url(record),
        "authors": list(record.get("authors") or []),
        "year": record.get("year"),
        "publication_year": record.get("publication_year"),
        "status": record.get("status"),
        "work_type": record.get("work_type"),
        "original_language": record.get("original_language"),
        "volume_count": record.get("volume_count"),
        "chapter_count": record.get("chapter_count"),
        "latest_release_chapter": record.get("latest_release_chapter"),
        "rating": record.get("rating"),
        "genres": list(record.get("genres") or [])[:8],
        "source_name": CATALOGUE_LABEL,
        "source_url": f"https://mangabaka.org/{external_id}",
        "external_sources": list(record.get("external_sources") or []),
        "sources": [],
    }


def manga_from_record(
    record: dict[str, Any], *, language: str, manga_id: str | None = None
) -> dict[str, Any]:
    """Series row for a catalogue work. Metadata enrichment refines it later."""

    card = catalogue_card(record)
    volume_count = record.get("volume_count")
    chapter_count = record.get("chapter_count")
    return {
        "id": manga_id or str(uuid.uuid4()),
        "provider": CATALOGUE_PROVIDER,
        "title": card["title"],
        "description": card["description"],
        "cover_url": card["cover_url"],
        "authors": card["authors"],
        "creator_links": list(record.get("creator_links") or []),
        "original_language": card["original_language"],
        "status": card["status"],
        "year": card["year"],
        "last_volume": str(volume_count) if volume_count else None,
        "last_chapter": str(chapter_count) if chapter_count else None,
        "available_languages": [language],
        "alternate_titles": card["alternate_titles"],
        "source_url": card["source_url"],
        "source_name": CATALOGUE_LABEL,
        "source_id": card["external_id"],
        "preferred_language": language,
    }


__all__ = [
    "CATALOGUE_LABEL",
    "CATALOGUE_PROVIDER",
    "CATALOGUE_SOURCE",
    "NO_REMOTE_PROVIDERS",
    "catalogue_card",
    "catalogue_id",
    "manga_from_record",
    "parse_catalogue_id",
]
