"""AniList: keyless corroboration catalogue for the manga family.

AniList is normally reached by the ID MangaBaka supplies, so it contributes
structured chapter/volume totals, publication dates, creator roles, and
sequel/spin-off relations without ever being searched by title. Title search
remains available as a fallback for works MangaBaka does not know.
"""

from __future__ import annotations

import html
import re
from typing import Any

from tankarr import USER_AGENT
from tankarr.config import Settings
from tankarr.metadata.base import MetadataSource, clean_text, normalized_title
from tankarr.providers.base import ProviderHTTP

MEDIA_FIELDS = """
id
idMal
title { romaji english native }
synonyms
format
status
chapters
volumes
countryOfOrigin
isAdult
startDate { year month day }
endDate { year month day }
description(asHtml: false)
genres
tags { name }
averageScore
coverImage { extraLarge large }
siteUrl
externalLinks { site url type }
staff(perPage: 12) { edges { role node { name { full } } } }
relations { edges { relationType node { id title { romaji } format } } }
"""
SEARCH_QUERY = (
    "query ($search: String, $perPage: Int) {"
    " Page(perPage: $perPage) { media(search: $search, type: MANGA) {"
    + MEDIA_FIELDS
    + "} } }"
)
DETAIL_QUERY = "query ($id: Int) { Media(id: $id, type: MANGA) {" + MEDIA_FIELDS + "} }"

PUBLICATION_STATUS = {
    "FINISHED": "ended",
    "RELEASING": "ongoing",
    "HIATUS": "hiatus",
    "CANCELLED": "abandoned",
}
COUNTRY_LANGUAGE = {"JP": "ja", "KR": "ko", "CN": "zh", "TW": "zh"}
TAG_PATTERN = re.compile(r"<[^>]+>")


def _positive_integer(value: object) -> int | None:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _date(value: object) -> str | None:
    if not isinstance(value, dict) or not value.get("year"):
        return None
    year = int(value["year"])
    month = int(value.get("month") or 1)
    day = int(value.get("day") or 1)
    return f"{year:04d}-{month:02d}-{day:02d}"


class AniListMetadataSource(MetadataSource):
    name = "anilist"
    label = "AniList"
    search_results_complete_for_matching = True
    identity_search_queries = 2

    def __init__(self, settings: Settings):
        self.settings = settings
        # AniList allows ~90 requests per minute; one per second keeps a full
        # library refresh well inside that budget.
        self.api = ProviderHTTP(
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            timeout_seconds=settings.request_timeout_seconds,
            requests_per_second=1.0,
        )

    @property
    def configured(self) -> bool:
        return True

    async def _graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        response = await self.api.request(
            "POST",
            self.settings.anilist_api_url,
            json={"query": query, "variables": variables},
        )
        payload = response.json()
        errors = payload.get("errors")
        if errors:
            message = clean_text((errors[0] or {}).get("message")) or "GraphQL error"
            raise RuntimeError(f"AniList: {message}")
        return payload.get("data") or {}

    async def search_series(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        data = await self._graphql(
            SEARCH_QUERY, {"search": query, "perPage": max(1, min(limit, 25))}
        )
        results: list[dict[str, Any]] = []
        for item in ((data.get("Page") or {}).get("media") or [])[:limit]:
            record = self._normalize(item)
            record["hit_title"] = record["title"]
            results.append(record)
        return results

    async def get_series(self, external_id: str) -> dict[str, Any]:
        data = await self._graphql(DETAIL_QUERY, {"id": int(external_id)})
        media = data.get("Media")
        if not media:
            raise RuntimeError(f"AniList: manga {external_id} not found")
        return self._normalize(media)

    async def aclose(self) -> None:
        await self.api.aclose()

    @classmethod
    def _normalize(cls, media: dict[str, Any]) -> dict[str, Any]:
        titles = media.get("title") or {}
        english = clean_text(titles.get("english"))
        romaji = clean_text(titles.get("romaji"))
        native = clean_text(titles.get("native"))
        title = english or romaji or native
        country = clean_text(media.get("countryOfOrigin")).upper()
        language = COUNTRY_LANGUAGE.get(country)
        fmt = clean_text(media.get("format")).upper()
        if fmt == "ONE_SHOT":
            work_type = "One-shot"
        elif fmt == "NOVEL":
            work_type = "Novel"
        elif fmt == "LIGHT_NOVEL":
            work_type = "Light Novel"
        elif fmt == "MANGA":
            work_type = {"ja": "Manga", "ko": "Manhwa", "zh": "Manhua"}.get(
                language or "", "OEL" if country else "Manga"
            )
            if work_type == "OEL":
                language = "en"
        else:
            work_type = fmt.replace("_", " ").title() or None

        alternate_titles: list[str] = []
        seen = {normalized_title(title) or title.casefold()}
        for value in (romaji, native, *(media.get("synonyms") or [])):
            value = clean_text(value)
            if not value:
                continue
            key = normalized_title(value) or value.casefold()
            if key not in seen:
                seen.add(key)
                alternate_titles.append(value)
        localized_titles: dict[str, str] = {}
        if english:
            localized_titles["en"] = english
        if native and language:
            localized_titles[language] = native

        authors: list[str] = []
        creators: list[dict[str, str]] = []
        for edge in (media.get("staff") or {}).get("edges") or []:
            name = clean_text(((edge.get("node") or {}).get("name") or {}).get("full"))
            role_text = clean_text(edge.get("role")).casefold()
            if not name:
                continue
            # "Story & Art" credits one person with both roles.
            roles = []
            if "story" in role_text or "original" in role_text or "writ" in role_text:
                roles.append("writer")
            if "art" in role_text or "illustrat" in role_text:
                roles.append("artist")
            if not roles:
                continue
            if name not in authors:
                authors.append(name)
            for role in roles:
                if not any(c["name"] == name and c["role"] == role for c in creators):
                    creators.append({"name": name, "role": role})

        description = html.unescape(
            TAG_PATTERN.sub(" ", str(media.get("description") or ""))
        )
        description = " ".join(description.split())

        cover_image = media.get("coverImage") or {}
        cover_url = cover_image.get("extraLarge") or cover_image.get("large")
        links: list[dict[str, str]] = []
        if clean_text(media.get("siteUrl")):
            links.append({"label": cls.label, "url": clean_text(media.get("siteUrl"))})
        for entry in media.get("externalLinks") or []:
            if isinstance(entry, dict) and clean_text(entry.get("url")):
                links.append(
                    {
                        "label": clean_text(entry.get("site")) or "Link",
                        "url": clean_text(entry.get("url")),
                    }
                )

        external_ids: dict[str, str] = {}
        mal_id = _positive_integer(media.get("idMal"))
        if mal_id is not None:
            external_ids["myanimelist"] = str(mal_id)

        related_works = [
            {
                "relation": clean_text(edge.get("relationType")).casefold(),
                "external_id": str((edge.get("node") or {}).get("id") or ""),
                "title": clean_text(
                    ((edge.get("node") or {}).get("title") or {}).get("romaji")
                ),
                "format": clean_text((edge.get("node") or {}).get("format")),
            }
            for edge in ((media.get("relations") or {}).get("edges") or [])
            if isinstance(edge, dict)
        ]

        score = media.get("averageScore")
        return {
            "source": cls.name,
            "catalogue_scope": "work",
            "external_id": str(media.get("id") or ""),
            "title": title,
            "hit_title": "",
            "alternate_titles": alternate_titles,
            "catalogue_alternate_titles": alternate_titles,
            "localized_titles": localized_titles,
            "description": description,
            "authors": authors,
            "creators": creators,
            "creator_links": [],
            "genres": [
                clean_text(g) for g in media.get("genres") or [] if clean_text(g)
            ],
            "tags": [
                clean_text(tag.get("name"))
                for tag in media.get("tags") or []
                if isinstance(tag, dict) and clean_text(tag.get("name"))
            ],
            "publisher": None,
            "year": _positive_integer((media.get("startDate") or {}).get("year")),
            "status": PUBLICATION_STATUS.get(clean_text(media.get("status")).upper()),
            "work_type": work_type,
            "original_language": language,
            "volume_count": _positive_integer(media.get("volumes")),
            "chapter_count": _positive_integer(media.get("chapters")),
            "latest_release_chapter": None,
            "rating": (float(score) / 10.0)
            if isinstance(score, (int, float))
            else None,
            "content_rating": "nsfw" if media.get("isAdult") else "safe",
            "published_start": _date(media.get("startDate")),
            "published_end": _date(media.get("endDate")),
            "related_works": related_works,
            "cover": {"url": str(cover_url)} if cover_url else None,
            "links": links,
            "external_ids": external_ids,
            "raw": media,
        }
