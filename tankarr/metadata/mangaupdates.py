from __future__ import annotations

import re
from typing import Any

from tankarr import USER_AGENT
from tankarr.config import Settings
from tankarr.metadata.base import (
    MetadataSource,
    clean_text,
    creator_name_similarity,
    normalized_person,
)
from tankarr.providers.base import ProviderHTTP


class MangaUpdatesMetadataSource(MetadataSource):
    name = "mangaupdates"
    label = "MangaUpdates"
    supports_author_lookup = True

    def __init__(self, settings: Settings):
        self.settings = settings
        self.api = ProviderHTTP(
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            timeout_seconds=settings.request_timeout_seconds,
            requests_per_second=1.5,
        )
        self._author_series_cache: dict[tuple[str, ...], list[dict[str, Any]]] = {}

    @property
    def configured(self) -> bool:
        return True

    async def search_series(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        response = await self.api.request(
            "POST",
            f"{self.settings.mangaupdates_api_url.rstrip('/')}/series/search",
            json={"search": query, "page": 1, "perpage": min(limit, 25)},
        )
        results: list[dict[str, Any]] = []
        for item in response.json().get("results", [])[:limit]:
            record = item.get("record") or {}
            parsed = self._normalize(record)
            parsed["hit_title"] = clean_text(item.get("hit_title"))
            results.append(parsed)
        return results

    async def get_series(self, external_id: str) -> dict[str, Any]:
        payload = await self.api.get_json(
            f"{self.settings.mangaupdates_api_url.rstrip('/')}/series/{external_id}"
        )
        return self._normalize(payload)

    async def list_releases(
        self, external_id: str, *, max_pages: int = 10
    ) -> list[dict[str, Any]]:
        """Every indexed release for one work: volume, chapter, date, groups.

        This is the public chapter↔volume map for the manga family, written by
        the groups that produced the releases the download engines expose.
        """

        output: list[dict[str, Any]] = []
        for page in range(1, max_pages + 1):
            response = await self.api.request(
                "POST",
                f"{self.settings.mangaupdates_api_url.rstrip('/')}/releases/search",
                json={
                    "search": str(external_id),
                    "search_type": "series",
                    "perpage": 100,
                    "page": page,
                },
            )
            payload = response.json()
            results = payload.get("results") or []
            for item in results:
                record = item.get("record") or {}
                output.append(
                    {
                        "volume": clean_text(record.get("volume")) or None,
                        "chapter": clean_text(record.get("chapter")) or None,
                        "release_date": clean_text(record.get("release_date")) or None,
                        "groups": [
                            clean_text(group.get("name"))
                            for group in record.get("groups") or []
                            if clean_text(group.get("name"))
                        ],
                    }
                )
            total = int(payload.get("total_hits") or 0)
            if len(results) < 100 or page * 100 >= total:
                break
        return output

    async def search_series_by_author(
        self, author: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Resolve an author through MangaUpdates' official creator index.

        The returned candidate set is restricted to exact or conservative
        romanization-equivalent creator identities.  Title matching still
        happens in the shared matcher, so an author with several works does not
        make every work an automatic match.
        """

        cache_key = normalized_person(author)
        if not cache_key:
            return []
        cached = self._author_series_cache.get(cache_key)
        if cached is not None:
            return [dict(item) for item in cached[:limit]]

        response = await self.api.request(
            "POST",
            f"{self.settings.mangaupdates_api_url.rstrip('/')}/authors/search",
            json={"search": author, "page": 1, "perpage": 10},
        )
        matches: list[tuple[float, dict[str, Any]]] = []
        for item in response.json().get("results") or []:
            record = item.get("record") or {}
            name = clean_text(record.get("name"))
            similarity = creator_name_similarity(author, name)
            author_id = str(record.get("id") or record.get("author_id") or "")
            if author_id and similarity >= 0.90:
                matches.append((similarity, {**record, "id": author_id, "name": name}))
        matches.sort(key=lambda item: item[0], reverse=True)

        candidates: dict[str, dict[str, Any]] = {}
        # Multiple exact transliterations can exist, but following more than
        # two identities broadens the candidate pool without useful evidence.
        for similarity, matched_author in matches[:2]:
            author_id = str(matched_author["id"])
            series_response = await self.api.request(
                "POST",
                f"{self.settings.mangaupdates_api_url.rstrip('/')}/authors/{author_id}/series",
                json={},
            )
            payload = series_response.json()
            series_list = (
                payload.get("series_list") if isinstance(payload, dict) else payload
            )
            for series in series_list or []:
                external_id = str(series.get("series_id") or series.get("id") or "")
                title = clean_text(series.get("title"))
                if not external_id or not title:
                    continue
                candidates.setdefault(
                    external_id,
                    {
                        "source": self.name,
                        "catalogue_scope": "work",
                        "external_id": external_id,
                        "title": title,
                        "hit_title": title,
                        "alternate_titles": [],
                        "description": "",
                        "authors": [str(matched_author["name"])],
                        "creators": [
                            {"name": str(matched_author["name"]), "role": "writer"}
                        ],
                        "genres": [],
                        "tags": [],
                        # The compact author-series endpoint can expose the year
                        # of a translated catalogue entry. Details are fetched
                        # before acceptance and decide the correct field scope.
                        "publisher": None,
                        "year": None,
                        "status": None,
                        "work_type": None,
                        "original_language": None,
                        "volume_count": None,
                        "chapter_count": None,
                        "rating": None,
                        "cover": None,
                        "links": (
                            [
                                {
                                    "label": self.label,
                                    "url": clean_text(series.get("url")),
                                }
                            ]
                            if clean_text(series.get("url"))
                            else []
                        ),
                        "author_match_similarity": round(similarity, 3),
                        "raw": series,
                    },
                )

        result = list(candidates.values())
        self._author_series_cache[cache_key] = result
        return [dict(item) for item in result[:limit]]

    async def get_author_profile(self, author: str) -> dict[str, Any] | None:
        """Public profile of the creator index entry matching ``author``.

        Only an exact or romanization-equivalent identity is accepted: the
        author page must never show another person's biography.
        """

        if not normalized_person(author):
            return None
        base = self.settings.mangaupdates_api_url.rstrip("/")
        response = await self.api.request(
            "POST",
            f"{base}/authors/search",
            json={"search": author, "page": 1, "perpage": 10},
        )
        best: tuple[float, str] | None = None
        for item in response.json().get("results") or []:
            record = item.get("record") or {}
            author_id = str(record.get("id") or record.get("author_id") or "")
            similarity = creator_name_similarity(author, clean_text(record.get("name")))
            if (
                author_id
                and similarity >= 0.90
                and (best is None or similarity > best[0])
            ):
                best = (similarity, author_id)
        if best is None:
            return None
        detail = (await self.api.request("GET", f"{base}/authors/{best[1]}")).json()
        image = detail.get("image") or {}
        image_url = image.get("url") if isinstance(image, dict) else None
        birthday = detail.get("birthday") or {}
        status_date = detail.get("status_date") or {}
        return {
            "source": self.name,
            "label": self.label,
            "external_id": best[1],
            "name": clean_text(detail.get("name")),
            "native_name": clean_text(detail.get("actualname")) or None,
            "url": clean_text(detail.get("url")) or None,
            "image_url": (
                clean_text(image_url.get("original")) or None
                if isinstance(image_url, dict)
                else None
            ),
            "birthday": clean_text(birthday.get("as_string")) or None,
            "birthplace": clean_text(detail.get("birthplace")) or None,
            "status": clean_text(detail.get("status")) or None,
            "status_date": clean_text(status_date.get("as_string")) or None,
            "genres": [
                clean_text(genre)
                for genre in detail.get("genres") or []
                if clean_text(genre)
            ],
            "biography": clean_text(detail.get("comments")) or None,
            "total_series": (detail.get("stats") or {}).get("total_series"),
        }

    async def aclose(self) -> None:
        await self.api.aclose()

    @classmethod
    def _normalize(cls, payload: dict[str, Any]) -> dict[str, Any]:
        raw_status = clean_text(payload.get("status"))
        status = cls._publication_status(raw_status)

        work_type = clean_text(payload.get("type")) or None
        language = {
            "manga": "ja",
            "manhwa": "ko",
            "manhua": "zh",
        }.get((work_type or "").casefold())
        volume_count = None
        match = re.search(r"(\d+)\s+volumes?", raw_status, flags=re.IGNORECASE)
        if match:
            volume_count = int(match.group(1))

        authors: list[str] = []
        creators: list[dict[str, str]] = []
        creator_links: list[dict[str, str]] = []
        for entry in payload.get("authors") or []:
            name = clean_text(entry.get("name"))
            if not name:
                continue
            if name not in authors:
                authors.append(name)
            role = clean_text(entry.get("type")).casefold() or "writer"
            creators.append({"name": name, "role": role})
            author_id = str(entry.get("author_id") or "").strip()
            author_url = clean_text(entry.get("url"))
            if author_id and author_url:
                creator_links.append(
                    {
                        "name": name,
                        "role": role,
                        "source": cls.name,
                        "label": cls.label,
                        "external_id": author_id,
                        "url": author_url,
                    }
                )

        publishers = payload.get("publishers") or []
        original_publishers = [
            clean_text(item.get("publisher_name"))
            for item in publishers
            if clean_text(item.get("type")).casefold() == "original"
        ]
        all_publishers = [clean_text(item.get("publisher_name")) for item in publishers]
        publisher = next(
            (item for item in [*original_publishers, *all_publishers] if item), None
        )
        publisher_types = {
            clean_text(item.get("type")).casefold() for item in publishers
        }
        translated_only = (
            bool(publishers)
            and not original_publishers
            and bool(
                publisher_types & {"english", "french", "german", "italian", "spanish"}
            )
        )

        image = payload.get("image") or {}
        image_urls = image.get("url") or {}
        cover_url = image_urls.get("original") or image_urls.get("thumb")
        cover = None
        if cover_url:
            cover = {
                "url": str(cover_url),
                "width": image.get("width"),
                "height": image.get("height"),
            }

        external_id = str(payload.get("series_id") or "")
        source_url = clean_text(payload.get("url"))
        associated_titles = [
            title
            for item in payload.get("associated") or []
            if (title := clean_text(item.get("title")))
        ]
        record = {
            "source": cls.name,
            "catalogue_scope": "work",
            "external_id": external_id,
            "title": clean_text(payload.get("title")),
            "hit_title": "",
            "alternate_titles": associated_titles,
            "catalogue_alternate_titles": associated_titles,
            "description": clean_text(payload.get("description")),
            "authors": authors,
            "creators": creators,
            "creator_links": creator_links,
            "genres": [
                genre
                for item in payload.get("genres") or []
                if (genre := clean_text(item.get("genre")))
            ],
            "tags": [],
            "publisher": None if translated_only else publisher,
            # Every edition MangaUpdates lists, with its note ("12 Volumes",
            # "Complete"): the one place the published editions are described.
            "publishers": [
                {
                    "name": clean_text(item.get("publisher_name")),
                    "type": clean_text(item.get("type")),
                    "note": clean_text(item.get("notes")),
                }
                for item in publishers
                if clean_text(item.get("publisher_name"))
            ],
            "year": None if translated_only else cls._integer(payload.get("year")),
            # The API calls this field "Status in Country of Origin".  It is
            # work-level evidence even when the only publisher row describes
            # a translated edition, so edition scoping must not discard it.
            "status": status,
            "work_type": work_type,
            "original_language": language,
            # The volume total is part of that same country-of-origin status
            # string (for example "1 Volume (Complete)").
            "volume_count": volume_count,
            # MangaUpdates' latest_chapter is the latest release indexed by the
            # site, not the work's canonical chapter total. It must never be used
            # to decide completeness or to overwrite MAL's num_chapters.
            "chapter_count": None,
            "latest_release_chapter": cls._integer(payload.get("latest_chapter")),
            "rating": payload.get("bayesian_rating"),
            "cover": cover,
            "links": ([{"label": cls.label, "url": source_url}] if source_url else []),
            "raw": payload,
        }
        if translated_only:
            language = next(
                (
                    code
                    for label, code in (
                        ("english", "en"),
                        ("italian", "it"),
                        ("french", "fr"),
                        ("german", "de"),
                        ("spanish", "es"),
                    )
                    if label in publisher_types
                ),
                None,
            )
            record["publication_context"] = {
                "scope": "translated_edition",
                "language": language,
                "publisher": publisher,
                "publication_year": cls._integer(payload.get("year")),
                "volume_count": volume_count,
                "chapter_count": None,
            }
        return record

    @staticmethod
    def _publication_status(value: object) -> str | None:
        status_text = clean_text(value).casefold()
        if not status_text or status_text in {"n/a", "na", "unknown"}:
            return None
        if re.search(r"\b(?:complete|completed)\b", status_text):
            return "ended"
        if re.search(r"\bhiatus\b", status_text):
            return "hiatus"
        if re.search(r"\b(?:cancelled|canceled|discontinued|dropped)\b", status_text):
            return "abandoned"
        if re.search(r"\b(?:ongoing|publishing|releasing)\b", status_text):
            return "ongoing"
        return None

    @staticmethod
    def _integer(value: object) -> int | None:
        try:
            result = int(value) if value not in (None, "") else None
            return result if result is not None and result > 0 else None
        except (TypeError, ValueError):
            return None
