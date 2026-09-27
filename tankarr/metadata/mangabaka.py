"""MangaBaka: the aggregated identity index for the manga family.

MangaBaka merges AniList, MangaUpdates, MyAnimeList, Kitsu, Anime-Planet,
Shikimori and ANN into one record per work with every external identifier
attached. Tankarr uses it the way Sonarr uses SkyHook: the title search
happens here exactly once, and the identifiers it returns let every other
catalogue be fetched by ID instead of searched again.

The API is public and unauthenticated (``x-api-stability: stable``). It
publishes no rate limit, so Tankarr identifies itself, paces requests, and
relies on its own persisted records rather than repeated lookups.
"""

from __future__ import annotations

import html
import re
from typing import Any

from tankarr import USER_AGENT
from tankarr.config import Settings
from tankarr.metadata.base import (
    MetadataSource,
    clean_text,
    creator_name_similarity,
    normalized_title,
)
from tankarr.providers.base import ProviderHTTP

WORK_TYPES = {
    "manga": ("Manga", "ja"),
    "manhwa": ("Manhwa", "ko"),
    "manhua": ("Manhua", "zh"),
    "oel": ("OEL", "en"),
    "novel": ("Novel", None),
    "light_novel": ("Light Novel", None),
    "one_shot": ("One-shot", "ja"),
    "oneshot": ("One-shot", "ja"),
    "doujinshi": ("Doujinshi", "ja"),
}
PUBLICATION_STATUS = {
    "completed": "ended",
    "releasing": "ongoing",
    "ongoing": "ongoing",
    "hiatus": "hiatus",
    "cancelled": "abandoned",
    "canceled": "abandoned",
}
# MangaBaka's ``source`` map keyed by the Tankarr correlation name it feeds.
EXTERNAL_ID_SOURCES = {
    "manga_updates": "mangaupdates",
    "my_anime_list": "myanimelist",
    "anilist": "anilist",
    "kitsu": "kitsu",
    "anime_planet": "animeplanet",
}
# Every site MangaBaka aggregates, with the public page for one identifier.
SITE_PAGES: dict[str, tuple[str, str]] = {
    "manga_updates": ("MangaUpdates", "https://www.mangaupdates.com/series/{id}"),
    "anilist": ("AniList", "https://anilist.co/manga/{id}"),
    "my_anime_list": ("MyAnimeList", "https://myanimelist.net/manga/{id}"),
    "kitsu": ("Kitsu", "https://kitsu.app/manga/{id}"),
    "anime_planet": ("Anime-Planet", "https://www.anime-planet.com/manga/{id}"),
    "anime_news_network": (
        "Anime News Network",
        "https://www.animenewsnetwork.com/encyclopedia/manga.php?id={id}",
    ),
}


LINK_PATTERN = re.compile(r"\[([^\]]+)\]\(([^)]*)\)")
EMPHASIS_PATTERN = re.compile(r"(\*{1,2}|_{1,2})(\S(?:.*?\S)?)\1")
TAG_PATTERN = re.compile(r"<[^>]+>")


def plain_description(value: object) -> str:
    """Normalise MangaBaka's summary into markdown-lite the UI renders.

    Kept as-is: ``**bold**``, ``*italic*``, ``[text](https://…)`` links and
    paragraph breaks (one blank line). HTML tags become their markdown
    equivalent or disappear, non-http links keep only their text, and
    bullet lists become one ``• `` line per item.
    """

    text = html.unescape(str(value or ""))
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</?(?:i|em)>", "*", text, flags=re.IGNORECASE)
    text = re.sub(r"</?(?:b|strong)>", "**", text, flags=re.IGNORECASE)
    text = TAG_PATTERN.sub("", text)

    def keep_link(match: re.Match[str]) -> str:
        label, url = match.group(1), match.group(2).strip()
        if not url.startswith(("http://", "https://")):
            return label
        return f"[{label}]({url})"

    text = LINK_PATTERN.sub(keep_link, text)
    text = re.sub(r"[ \t]*(?:^|\s)[•·▪-]\s+(?=\S)", "\n• ", text)
    paragraphs = []
    for block in re.split(r"\n\s*\n", text):
        lines = [" ".join(line.split()) for line in block.split("\n")]
        block_text = "\n".join(line for line in lines if line)
        if block_text:
            paragraphs.append(block_text)
    return "\n\n".join(paragraphs).strip()


def _positive_integer(value: object) -> int | None:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _date_year(value: object) -> int | None:
    match = re.match(r"^\s*(\d{4})(?:-|$)", str(value or ""))
    return _positive_integer(match.group(1)) if match else None


def _mangaupdates_id(value: object) -> str | None:
    """MangaBaka stores the MangaUpdates ID as the base-36 URL slug."""

    token = str(value or "").strip().casefold()
    if not re.fullmatch(r"[0-9a-z]+", token):
        return None
    number = int(token, 10) if token.isdecimal() else int(token, 36)
    return str(number) if number > 0 else None


class MangaBakaMetadataSource(MetadataSource):
    name = "mangabaka"
    label = "MangaBaka"
    supports_author_lookup = True
    # The search payload already carries the full record, so candidates can
    # be assessed without a second round trip per hit.
    search_results_complete_for_matching = True
    identity_search_queries = 2

    def __init__(self, settings: Settings):
        self.settings = settings
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        self.api = ProviderHTTP(
            headers=headers,
            timeout_seconds=settings.request_timeout_seconds,
            requests_per_second=2.0,
        )
        # Staff pages are refreshed in the background and may span many search
        # pages. MangaBaka limits uncached search requests to 30/minute, so keep
        # a separate conservative budget without slowing interactive Add New.
        self.author_api = ProviderHTTP(
            headers=headers,
            timeout_seconds=settings.request_timeout_seconds,
            requests_per_second=0.35,
        )

    @property
    def configured(self) -> bool:
        return True

    def _url(self, path: str) -> str:
        return f"{self.settings.mangabaka_api_url.rstrip('/')}/{path.lstrip('/')}"

    async def search_series(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        payload = await self.api.get_json(
            self._url("series/search"), params={"q": query}
        )
        results: list[dict[str, Any]] = []
        for item in list(payload.get("data") or [])[:limit]:
            if not isinstance(item, dict) or item.get("merged_with"):
                continue
            record = self._normalize(item)
            record["hit_title"] = record["title"]
            results.append(record)
        return results

    async def search_series_by_author(
        self, author: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Return every work credited to ``author`` by MangaBaka.

        MangaBaka has no stable person identity here; the API exposes a
        paginated exact ``staff`` filter over work records.
        """

        if not clean_text(author):
            return []
        results: list[dict[str, Any]] = []
        seen: set[str] = set()
        url: str | None = self._url("series/search")
        params: dict[str, Any] | None = {"staff": author, "limit": 50}
        pages = 0
        while url and len(results) < limit and pages < 20:
            payload = await self.author_api.get_json(url, params=params, priority=40)
            pages += 1
            for item in payload.get("data") or []:
                if not isinstance(item, dict) or item.get("merged_with"):
                    continue
                record = self._normalize(item)
                if not record.get("external_id") or record["external_id"] in seen:
                    continue
                seen.add(record["external_id"])
                record["hit_title"] = record["title"]
                names = list(record.get("authors") or []) + [
                    str(creator.get("name") or "")
                    for creator in record.get("creators") or []
                    if isinstance(creator, dict)
                ]
                record["author_match_similarity"] = round(
                    max(
                        (creator_name_similarity(author, name) for name in names),
                        default=0.0,
                    ),
                    3,
                )
                results.append(record)
            pagination = payload.get("pagination") or {}
            url = pagination.get("next") if isinstance(pagination, dict) else None
            params = None
        return results[:limit]

    async def get_series(self, external_id: str) -> dict[str, Any]:
        payload = await self.api.get_json(self._url(f"series/{external_id}"))
        item = payload.get("data") or {}
        # A merged duplicate points at its surviving record; follow one hop so
        # a stale identifier keeps resolving to the canonical work.
        merged = _positive_integer(item.get("merged_with"))
        if merged is not None and str(merged) != str(external_id):
            payload = await self.api.get_json(self._url(f"series/{merged}"))
            item = payload.get("data") or {}
        return self._normalize(item)

    async def aclose(self) -> None:
        await self.api.aclose()
        await self.author_api.aclose()

    @classmethod
    def _normalize(cls, payload: dict[str, Any]) -> dict[str, Any]:
        title = clean_text(payload.get("title"))
        raw_type = clean_text(payload.get("type")).casefold().replace("-", "_")
        work_type, language = WORK_TYPES.get(
            raw_type, (raw_type.replace("_", " ").title() or None, None)
        )
        status = PUBLICATION_STATUS.get(clean_text(payload.get("status")).casefold())

        alternate_titles: list[str] = []
        localized_titles: dict[str, str] = {}
        candidates: list[tuple[str | None, str, bool]] = [
            (None, clean_text(payload.get("native_title")), False),
            (None, clean_text(payload.get("romanized_title")), False),
        ]
        for entry in payload.get("titles") or []:
            if not isinstance(entry, dict):
                continue
            candidates.append(
                (
                    clean_text(entry.get("language")).casefold() or None,
                    clean_text(entry.get("title")),
                    bool(entry.get("is_primary")),
                )
            )
        secondary = payload.get("secondary_titles") or {}
        if isinstance(secondary, dict):
            for lang, entries in secondary.items():
                for entry in entries or []:
                    if isinstance(entry, dict):
                        candidates.append(
                            (
                                clean_text(lang).casefold() or None,
                                clean_text(entry.get("title")),
                                False,
                            )
                        )
        # Non-Latin titles normalize to an empty key; keep them keyed on the
        # raw text so native and localized aliases survive for source matching.
        seen = {normalized_title(title) or title.casefold()}
        for lang, value, primary in candidates:
            if not value:
                continue
            if lang and primary and lang not in localized_titles:
                localized_titles[lang] = value
            key = normalized_title(value) or value.casefold()
            if key not in seen:
                seen.add(key)
                alternate_titles.append(value)
        # MangaBaka's display title is the English catalogue title unless an
        # official "en" entry says otherwise.
        if title and "en" not in localized_titles and title.isascii():
            localized_titles["en"] = title

        authors: list[str] = []
        creators: list[dict[str, str]] = []
        for role, field in (("writer", "authors"), ("artist", "artists")):
            for raw_name in payload.get(field) or []:
                name = clean_text(raw_name)
                if not name:
                    continue
                if name not in authors:
                    authors.append(name)
                if not any(c["name"] == name and c["role"] == role for c in creators):
                    creators.append({"name": name, "role": role})

        rating = payload.get("rating")
        publisher = next(
            (
                clean_text(item.get("name"))
                for item in payload.get("publishers") or []
                if isinstance(item, dict)
                and clean_text(item.get("type")).casefold() == "original"
                and clean_text(item.get("name"))
            ),
            None,
        )

        # Every publisher with its edition kind ("English", "Original"…):
        # the English one names the digital releases on the indexers.
        # The note is what the edition released: "2 Vols - Complete" on the
        # English publisher is the managed edition's own volume count.
        publishers = [
            {
                "name": clean_text(item.get("name")),
                "type": clean_text(item.get("type")),
                "note": clean_text(item.get("note")),
            }
            for item in payload.get("publishers") or []
            if isinstance(item, dict) and clean_text(item.get("name"))
        ]

        cover = None
        raw_cover = (
            ((payload.get("cover") or {}).get("raw") or {})
            if isinstance(payload.get("cover"), dict)
            else {}
        )
        if raw_cover.get("url"):
            cover = {
                "url": str(raw_cover["url"]),
                "width": raw_cover.get("width"),
                "height": raw_cover.get("height"),
            }

        external_id = str(payload.get("id") or "")
        links: list[dict[str, str]] = []
        if external_id:
            links.append(
                {"label": cls.label, "url": f"https://mangabaka.org/{external_id}"}
            )
        for entry in payload.get("links_v2") or []:
            if not isinstance(entry, dict) or not clean_text(entry.get("url")):
                continue
            label = clean_text(entry.get("name_display")) or clean_text(
                entry.get("name")
            )
            if label:
                links.append({"label": label, "url": clean_text(entry.get("url"))})

        # Linked sites with the score each community gave the work: one chip
        # per site in the series header, and the identifiers for ID chaining.
        external_sources: list[dict[str, Any]] = []
        for site, (label, page) in SITE_PAGES.items():
            entry = (payload.get("source") or {}).get(site)
            if not isinstance(entry, dict) or entry.get("id") in (None, ""):
                continue
            site_id = str(entry["id"]).strip()
            normalized_rating = entry.get("rating_normalized")
            external_sources.append(
                {
                    "source": EXTERNAL_ID_SOURCES.get(site, site),
                    "label": label,
                    "url": page.format(id=site_id),
                    "rating": (
                        round(float(normalized_rating) / 10.0, 1)
                        if isinstance(normalized_rating, (int, float))
                        else None
                    ),
                }
            )
        if external_id:
            external_sources.insert(
                0,
                {
                    "source": cls.name,
                    "label": cls.label,
                    "url": f"https://mangabaka.org/{external_id}",
                    "rating": (
                        round(float(rating) / 10.0, 1)
                        if isinstance(rating, (int, float))
                        else None
                    ),
                },
            )

        external_ids: dict[str, str] = {}
        for site, source in EXTERNAL_ID_SOURCES.items():
            entry = (payload.get("source") or {}).get(site)
            raw_id = entry.get("id") if isinstance(entry, dict) else entry
            if raw_id in (None, ""):
                continue
            value = (
                _mangaupdates_id(raw_id)
                if source == "mangaupdates"
                else str(raw_id).strip()
            )
            if value:
                external_ids[source] = value

        total_chapters = _positive_integer(payload.get("total_chapters"))
        published = payload.get("published") or {}
        # Official platforms/publishers carrying the work. Never shown as
        # "download sources": they tell the ranking which release is the
        # official one and let discovery map the official source by URL.
        official_links: list[dict[str, str]] = []
        for entry in payload.get("links_v2") or []:
            if not isinstance(entry, dict):
                continue
            link_type = clean_text(entry.get("type")).casefold()
            url = clean_text(entry.get("url"))
            if link_type not in {"webplatform", "publisher"} or not url.startswith(
                "http"
            ):
                continue
            official_links.append(
                {
                    "name": clean_text(entry.get("name_display") or entry.get("name")),
                    "url": url,
                    "language": clean_text(entry.get("language")).casefold()
                    or "unknown",
                    "type": link_type,
                }
            )
        return {
            "source": cls.name,
            "catalogue_scope": "work",
            "external_id": external_id,
            "title": title,
            "hit_title": "",
            "alternate_titles": alternate_titles,
            "catalogue_alternate_titles": alternate_titles,
            "localized_titles": localized_titles,
            "description": plain_description(payload.get("description")),
            "authors": authors,
            "creators": creators,
            "creator_links": [],
            "genres": [
                clean_text(genre).replace("_", " ")
                for genre in payload.get("genres") or []
                if clean_text(genre)
            ],
            "tags": [
                clean_text(tag) for tag in payload.get("tags") or [] if clean_text(tag)
            ],
            "publisher": publisher,
            "publishers": publishers,
            "year": _positive_integer(payload.get("year")),
            "publication_year": _date_year(published.get("start_date")),
            "status": status,
            "work_type": work_type,
            "original_language": language,
            "volume_count": _positive_integer(payload.get("final_volume")),
            # A running work's total is only "chapters so far"; it becomes the
            # canonical chapter count once the catalogue marks the work ended.
            "chapter_count": total_chapters if status == "ended" else None,
            "latest_release_chapter": None if status == "ended" else total_chapters,
            "rating": (float(rating) / 10.0)
            if isinstance(rating, (int, float))
            else None,
            "content_rating": clean_text(payload.get("content_rating")) or None,
            "published_start": clean_text(published.get("start_date")) or None,
            "published_end": clean_text(published.get("end_date")) or None,
            "cover": cover,
            "links": links,
            "external_ids": external_ids,
            "external_sources": external_sources,
            "official_links": official_links,
            "raw": payload,
        }
