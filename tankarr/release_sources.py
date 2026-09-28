from __future__ import annotations

import asyncio
import logging
import re
from collections import defaultdict
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlsplit

from tankarr.chapter_mapping import canonical_number
from tankarr.database import Database, ReleaseSourceOwned
from tankarr.metadata.base import (
    SeriesMatchAssessment,
    assess_series_match,
    normalized_title,
    work_title_variants,
)
from tankarr.providers.base import (
    Provider,
    ProviderRequestError,
    ProviderUnavailableError,
)
from tankarr.providers.suwayomi import StaleSuwayomiIdentity
from tankarr.source_circuit import source_gate_key
from tankarr.source_ranking import official_hosts, release_host

logger = logging.getLogger(__name__)

AUTO_MATCH_THRESHOLD = 0.86
AUTO_MATCH_MIN_MARGIN = 0.06
# A candidate this close to the threshold is worth one extra round trip:
# counting its chapters is cheaper than asking a human to confirm it.
COUNT_CORROBORATION_FLOOR = 0.55
MAX_IDENTITY_QUERIES = 4


def _page_key(url: str) -> tuple[str, str]:
    parts = urlsplit(str(url or ""))
    host = (parts.hostname or "").casefold().removeprefix("www.")
    path = parts.path.rstrip("/").casefold()
    return host, path


_PAGE_ID_PARAMS = (
    "title_no",
    "titleid",
    "series_id",
    "seriesid",
    "id",
    "content",
    "productno",
)


def _page_id(url: str) -> str | None:
    """The identifier a platform keys its title pages by (``title_no=88`` on
    Webtoons, ``titleId=`` on Naver…); paths there are cosmetic and the
    catalogue writes them as placeholders (``/-/-/-/list``)."""

    query = parse_qs(urlsplit(str(url or "")).query)
    lowered = {key.casefold(): values for key, values in query.items()}
    for name in _PAGE_ID_PARAMS:
        values = lowered.get(name)
        if values and str(values[0]).strip():
            return f"{name}={str(values[0]).strip().casefold()}"
    return None


def official_link_for(
    target: dict[str, Any], candidate: dict[str, Any]
) -> dict[str, Any] | None:
    """The catalogue's official link that names this candidate's page, if any.

    Matching is by host and page path (a viewer URL under the title page
    still counts), restricted to the work's language or language-agnostic
    links. Title similarity plays no part: this is identity by URL.
    """

    host, path = _page_key(str(candidate.get("source_url") or ""))
    if not host or not path:
        return None
    language = str(target.get("language") or "").casefold()
    for link in target.get("official_links") or []:
        link_language = str(link.get("language") or "unknown").casefold()
        if language and link_language not in {language, "unknown"}:
            continue
        link_host, link_path = _page_key(str(link.get("url") or ""))
        if link_host != host:
            continue
        link_id = _page_id(str(link.get("url") or ""))
        if link_id is not None and link_id == _page_id(
            str(candidate.get("source_url") or "")
        ):
            return link
        if not link_path:
            continue
        if (
            path == link_path
            or path.startswith(link_path + "/")
            or link_path.startswith(path + "/")
        ):
            return link
    return None


def official_platform_candidate(
    target: dict[str, Any], candidates: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """A candidate hosted on one of the work's official platforms whose title
    is exactly the work's title. Platforms key their pages by opaque ids
    (Tapas ``/series/111423``) while the catalogue links the slug, so URL
    identity cannot always fire; on the publisher's own site an exact title
    is the work, whatever creator name the page shows."""

    hosts = official_hosts(
        target.get("official_links"), language=str(target.get("language") or "")
    )
    if not hosts:
        return None
    wanted = {
        normalized_title(value)
        for value in [target.get("title"), *(target.get("alternate_titles") or [])]
        if normalized_title(value)
    }
    for candidate in candidates:
        if release_host(candidate) not in hosts:
            continue
        if normalized_title(candidate.get("title")) in wanted:
            return candidate
    return None


_UNRELATED_ENTRY = re.compile(
    r"\b(doujin(?:shi)?|hentai|fan ?book|anthology)\b", re.IGNORECASE
)
ABSOLUTE_CHAPTER_CAP = 3000
CATALOGUE_TRUST_MIN = 10


def implausible_chapter_list(
    chapters: list[dict[str, Any]], target: dict[str, Any]
) -> str | None:
    """Why a source's chapter list cannot be this work's, or None.

    Niadd answered 2 163 "doujin N" entries for thirteen unrelated series and
    Tankarr mapped every one: 30 000 phantom releases, 486 queued downloads,
    eight adult books imported. A list is refused when most of its entries
    say they are something else, when it is several times longer than the
    catalogue's count, or when it is absurdly long for any comic.
    """

    if not chapters:
        return None
    total = len(chapters)
    if total > ABSOLUTE_CHAPTER_CAP:
        return f"{total} entries: no work has that many chapters"
    unrelated = sum(
        1 for item in chapters if _UNRELATED_ENTRY.search(str(item.get("title") or ""))
    )
    if unrelated * 2 >= total and unrelated >= 5:
        return f"{unrelated} of {total} entries are doujin/fan works"
    try:
        catalogue = int(target.get("chapter_count") or 0)
    except (TypeError, ValueError):
        catalogue = 0
    if catalogue <= 0:
        return None
    numbers = []
    for item in chapters:
        number = canonical_number(item.get("chapter"))
        if number is not None:
            numbers.append(float(number))
    finished = str(target.get("status") or "").casefold() in {
        "ended",
        "completed",
        "finished",
        "complete",
    }
    if finished or catalogue < CATALOGUE_TRUST_MIN:
        # A finished work has a firm count: a list twice as long (splits and
        # extras included) is another work. MangaK offered the 112 chapters
        # of "Sweat and Soap" for the 7 of "Sweat and Honey"; XCOMIC numbered
        # the webtoon episodes of Blade of the Phantom Master past its 76.
        allowance = max(catalogue * 2, catalogue + 10)
        reach = max(catalogue * 2, catalogue + 10)
    else:
        # A running work's catalogue may lag far behind the sources.
        allowance = max(catalogue * 3, catalogue + 200)
        reach = max(catalogue * 3, catalogue + 300)
    # Aggregators list every scanlation group: 752 rows for the 249 chapters
    # of 20th Century Boys are three groups, not three works. Count chapters.
    distinct = len(set(numbers)) if numbers else total
    if distinct > allowance:
        return f"{distinct} chapters against a catalogue of {catalogue} chapters"
    if numbers and max(numbers) > reach:
        return (
            f"chapter numbers reach {max(numbers):g} against a catalogue of {catalogue}"
        )
    return None


class ReleaseSourceManager:
    """Correlate one work with independent chapter-download catalogues.

    Metadata catalogue IDs and download source IDs are deliberately separate.
    A verified mapping can expose many provider releases for one canonical
    Tankarr series and remains refreshable by the normal monitoring cycle.
    """

    def __init__(self, database: Database, providers: dict[str, Provider]):
        self.database = database
        self.providers = providers
        self._lock = asyncio.Lock()
        # How an official platform's page is read for its own status flag;
        # replaceable in tests. None → httpx.
        self.fetch_page = None

    @staticmethod
    def _provider_label(provider: Provider) -> str:
        return str(getattr(provider, "label", None) or provider.name)

    def _target(self, manga_id: str) -> dict[str, Any]:
        manga = self.database.get_manga(manga_id)
        metadata_row = self.database.get_series_metadata(manga_id)
        metadata = metadata_row["data"] if metadata_row else manga.get("metadata") or {}
        source_title = str(manga.get("source_title") or manga["title"])
        automatic_title = str(manga.get("metadata_title") or source_title)
        aliases = list(
            dict.fromkeys(
                str(value).strip()
                for value in [
                    source_title,
                    *(metadata.get("alternate_titles") or []),
                ]
                if str(value or "").strip()
                and normalized_title(value) != normalized_title(automatic_title)
            )
        )
        return {
            "title": automatic_title,
            "alternate_titles": aliases,
            "authors": metadata.get("authors") or manga.get("authors") or [],
            "year": metadata.get("year") or manga.get("year"),
            "status": (
                manga.get("status_override")
                if manga.get("status_override") not in (None, "automatic")
                else metadata.get("status") or manga.get("status")
            ),
            "work_type": (
                (metadata.get("classification") or {}).get("kind")
                or metadata.get("work_type")
            ),
            "work_subtype": (metadata.get("classification") or {}).get("subtype"),
            "volume_count": metadata.get("volume_count"),
            "chapter_count": metadata.get("chapter_count"),
            "official_links": list(metadata.get("official_links") or []),
            "language": str(manga.get("preferred_language") or ""),
        }

    @staticmethod
    def _queries(target: dict[str, Any]) -> list[str]:
        output: list[str] = []
        seen: set[str] = set()
        for value in [target.get("title"), *(target.get("alternate_titles") or [])]:
            query = " ".join(str(value or "").split()).strip()
            key = normalized_title(query)
            if len(query) < 2 or not key or key in seen:
                continue
            seen.add(key)
            output.append(query)
            if len(output) == MAX_IDENTITY_QUERIES:
                break
        return output

    @staticmethod
    def _candidate_group(provider_name: str, candidate: dict[str, Any]) -> str:
        # Suwayomi is an aggregator: equal titles from different extensions are
        # complementary release catalogues, not ambiguous competing identities.
        if provider_name == "suwayomi":
            return str(candidate.get("source_id") or "unknown")
        return "provider"

    async def _official_status(
        self, provider: Any, mapping: dict[str, Any]
    ) -> str | None:
        """The publication status an official platform reports for the work.

        Only official sources are asked (one details call per refresh); any
        failure is not a refresh failure, the chapters already came through.
        """

        if str(mapping.get("source_role") or "") not in {
            "primary_official",
            "secondary_official",
        }:
            return None
        getter = getattr(provider, "get_manga", None)
        if getter is None:
            return None
        try:
            details = await getter(str(mapping["provider_manga_id"]))
        except Exception:  # noqa: BLE001 - a details hiccup must not lose chapters
            return None
        status = str((details or {}).get("status") or "").strip().casefold()
        # The Webtoons extension reports "ongoing" for a title the site itself
        # flags "On hiatus" (unORDINARY, 2026-09-05). The page carries the
        # flag in plain JSON; read it when the extension has no pause to
        # report, so a break shows as a pause instead of an overdue chapter.
        if status not in {"hiatus", "on_hiatus"}:
            page_status = await self._webtoons_page_status(
                str(mapping.get("source_url") or "")
            )
            if page_status:
                status = page_status
        return status or None

    _WEBTOONS_STATUS = re.compile(r'"titleStatusString"\s*:\s*"([^"]{1,40})"')
    # The desktop and mobile pages also carry the creator's note
    # ("unOrdinary will return!") while a title is paused.
    _WEBTOONS_RETURN_NOTE = re.compile(r"will\s+return", re.IGNORECASE)

    # Only the mobile site embeds the title JSON (with titleStatusString);
    # the desktop page does not carry it, and m.webtoons.com redirects a
    # desktop browser back to the desktop page. Ask as a phone.
    _MOBILE_USER_AGENT = (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 "
        "Safari/604.1"
    )

    @staticmethod
    def webtoons_status_url(url: str) -> str | None:
        parts = urlsplit(url)
        host = parts.hostname or ""
        if not host.endswith("webtoons.com"):
            return None
        return parts._replace(netloc="m.webtoons.com").geturl()

    async def _webtoons_page_status(self, url: str) -> str | None:
        mobile_url = self.webtoons_status_url(url)
        if mobile_url is None:
            return None
        try:
            if self.fetch_page is not None:
                text = await self.fetch_page(mobile_url)
            else:
                from tankarr.http import async_client

                async with async_client(
                    timeout=8.0,
                    follow_redirects=True,
                    headers={"User-Agent": self._MOBILE_USER_AGENT},
                ) as client:
                    response = await client.get(mobile_url)
                    response.raise_for_status()
                    text = response.text
        except Exception:  # noqa: BLE001 - the page is a bonus, never a failure
            return None
        match = self._WEBTOONS_STATUS.search(text or "")
        if not match:
            if self._WEBTOONS_RETURN_NOTE.search(text or ""):
                return "hiatus"
            return None
        flag = match.group(1).strip().casefold()
        if "hiatus" in flag:
            return "hiatus"
        if "complet" in flag or "end" in flag:
            return "completed"
        if "ongoing" in flag or "releas" in flag:
            return "ongoing"
        return None

    async def refresh_mappings(
        self, manga_id: str, *, monitor_new: bool, language: str | None = None
    ) -> dict[str, Any]:
        language = language or str(
            self.database.get_manga(manga_id)["preferred_language"]
        )
        target = self._target(manga_id)
        target["language"] = language
        seen = 0
        new_ids: list[str] = []
        statuses: list[dict[str, Any]] = []
        for mapping in self.database.list_release_sources(manga_id):
            provider_name = str(mapping["provider"])
            provider = self.providers.get(provider_name)
            if provider is None:
                statuses.append(
                    {
                        "provider": provider_name,
                        "label": provider_name.replace("_", " ").title(),
                        "state": "disabled",
                        "provider_manga_id": mapping["provider_manga_id"],
                        "error": "Provider is disabled in Settings",
                    }
                )
                continue
            mapping_language = str(mapping.get("language") or "").strip().casefold()
            if (
                mapping_language and mapping_language != language.strip().casefold()
            ) or not provider.supports_language(language):
                # An unavailable language says nothing about source health.
                # In particular, adapters may return [] without making a
                # request when they do not support the selected language.
                statuses.append(
                    {
                        "provider": provider_name,
                        "label": self._provider_label(provider),
                        "state": "unsupported",
                        "provider_manga_id": mapping["provider_manga_id"],
                        "error": f"Source does not support the selected language: {language}",
                    }
                )
                continue
            gate_key = source_gate_key(
                provider_name, mapping.get("source_url"), mapping.get("source_name")
            )
            retry_at = self.database.source_retry_at(gate_key)
            if retry_at > datetime.now(UTC).timestamp():
                statuses.append(
                    {
                        "provider": provider_name,
                        "state": "cooldown",
                        "provider_manga_id": mapping["provider_manga_id"],
                        "next_retry_at": retry_at,
                        "error": "Source temporarily unavailable; retry scheduled",
                    }
                )
                continue
            try:
                chapters = await provider.list_chapters(
                    str(mapping["provider_manga_id"]), language
                )
                self.database.record_source_circuit(gate_key)
                refusal = implausible_chapter_list(chapters, target)
                if refusal:
                    # The mapping stays (a human may look), its list does not
                    # enter the index: nothing of it is wanted or downloaded.
                    self.database.record_release_source_result(
                        manga_id,
                        provider_name,
                        str(mapping["provider_manga_id"]),
                        error=f"Chapter list refused: {refusal}",
                    )
                    statuses.append(
                        {
                            "provider": provider_name,
                            "label": self._provider_label(provider),
                            "state": "refused",
                            "provider_manga_id": mapping["provider_manga_id"],
                            "title": mapping["title"],
                            "error": f"Chapter list refused: {refusal}",
                        }
                    )
                    continue
                result = await asyncio.to_thread(
                    self.database.upsert_chapters,
                    manga_id,
                    chapters,
                    monitor_new=monitor_new,
                )
                self.database.record_release_source_result(
                    manga_id,
                    provider_name,
                    str(mapping["provider_manga_id"]),
                    publication_status=await self._official_status(provider, mapping),
                )
                seen += int(result["seen"])
                new_ids.extend(result["new_chapter_ids"])
                statuses.append(
                    {
                        "provider": provider_name,
                        "label": self._provider_label(provider),
                        "state": "matched",
                        "provider_manga_id": mapping["provider_manga_id"],
                        "title": mapping["title"],
                        "releases": int(result["seen"]),
                        "new": len(result["new_chapter_ids"]),
                        "confidence": mapping["match_confidence"],
                        "reason": mapping["match_reason"],
                    }
                )
            except StaleSuwayomiIdentity as exc:
                # The mapping points into a Suwayomi database that no longer
                # exists; discovery will map the series again on this one.
                self.database.delete_release_source(
                    manga_id, provider_name, str(mapping["provider_manga_id"])
                )
                logger.info(
                    "Dropped stale %s mapping %s for %s: %s",
                    provider_name,
                    mapping["provider_manga_id"],
                    manga_id,
                    exc,
                )
                statuses.append(
                    {
                        "provider": provider_name,
                        "label": self._provider_label(provider),
                        "state": "stale",
                        "provider_manga_id": mapping["provider_manga_id"],
                        "error": str(exc)[:300],
                    }
                )
            except Exception as exc:  # noqa: BLE001 - one source must not hide others
                if isinstance(
                    exc, (ProviderRequestError, ProviderUnavailableError, TimeoutError)
                ):
                    self.database.record_source_circuit(
                        gate_key, error=type(exc).__name__
                    )
                message = f"{type(exc).__name__}: {exc}"[:1000]
                self.database.record_release_source_result(
                    manga_id,
                    provider_name,
                    str(mapping["provider_manga_id"]),
                    error=message,
                )
                statuses.append(
                    {
                        "provider": provider_name,
                        "label": self._provider_label(provider),
                        "state": "error",
                        "provider_manga_id": mapping["provider_manga_id"],
                        "error": message,
                    }
                )
        return {"seen": seen, "new_chapter_ids": new_ids, "sources": statuses}

    async def _discover_provider(
        self,
        manga_id: str,
        provider: Provider,
        target: dict[str, Any],
        language: str,
        *,
        monitor_new: bool,
    ) -> list[dict[str, Any]]:
        candidates: dict[str, dict[str, Any]] = {}
        diagnostics: list[dict[str, Any]] = []
        successful_queries = 0
        rejected = self.database.release_source_rejections(manga_id)
        for query in self._queries(target):
            try:
                results, search_diagnostics = await provider.search_with_diagnostics(
                    query, language, limit=20
                )
                successful_queries += 1
                diagnostics.extend(
                    {
                        "provider": provider.name,
                        "label": str(
                            item.get("provider") or self._provider_label(provider)
                        ),
                        "state": "error",
                        "error": str(item.get("error") or "Search failed")[:1000],
                    }
                    for item in search_diagnostics
                )
            except Exception as exc:  # noqa: BLE001 - aliases can recover
                diagnostics.append(
                    {
                        "provider": provider.name,
                        "label": self._provider_label(provider),
                        "state": "error",
                        "error": f"{type(exc).__name__}: {exc}"[:1000],
                    }
                )
                continue
            for candidate in results:
                identifier = str(candidate.get("id") or "")
                if identifier:
                    candidates.setdefault(identifier, dict(candidate))

        if not candidates:
            if successful_queries:
                diagnostics.append(
                    {
                        "provider": provider.name,
                        "label": self._provider_label(provider),
                        "state": "no_match",
                        "reason": "No title result matched this work",
                    }
                )
            return diagnostics

        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for candidate in candidates.values():
            grouped[self._candidate_group(provider.name, candidate)].append(candidate)
        # A source that already serves this work (through a healthy mapping)
        # has nothing more to offer: its other entries are duplicates, side
        # works or mirrors. Measured live, they were half of every review.
        served_by = {
            str(mapping.get("source_name") or ""): mapping
            for mapping in self.database.list_release_sources(manga_id)
            if mapping.get("provider") == provider.name
            and mapping.get("source_name")
            and not mapping.get("last_error")
        }

        for group_candidates in grouped.values():
            group_source = str(group_candidates[0].get("source_name") or "")
            if provider.name == "suwayomi" and group_source in served_by:
                diagnostics.append(
                    {
                        "provider": provider.name,
                        "label": self._provider_label(provider),
                        "state": "mapped",
                        "title": group_candidates[0].get("title"),
                        "reason": (
                            f"{group_source} already serves this work through "
                            f"{served_by[group_source].get('title')}"
                        ),
                    }
                )
                continue
            preliminary = sorted(
                group_candidates,
                key=lambda candidate: (
                    assess_series_match(target, candidate).ranking_score
                ),
                reverse=True,
            )[:4]
            detailed: list[dict[str, Any]] = []
            for candidate in preliminary:
                try:
                    detail = await provider.get_manga(str(candidate["id"]))
                except Exception as exc:  # noqa: BLE001 - try another candidate
                    diagnostics.append(
                        {
                            "provider": provider.name,
                            "label": self._provider_label(provider),
                            "state": "error",
                            "error": f"Unable to inspect {candidate.get('title')}: {type(exc).__name__}: {exc}"[
                                :1000
                            ],
                        }
                    )
                    continue
                detail["alternate_titles"] = list(
                    dict.fromkeys(
                        [
                            *(detail.get("alternate_titles") or []),
                            *(candidate.get("alternate_titles") or []),
                        ]
                    )
                )
                for field in ("source_id", "source_name", "source_url"):
                    if not detail.get(field) and candidate.get(field):
                        detail[field] = candidate[field]
                detailed.append(detail)
            if not detailed:
                continue
            primary_variants = work_title_variants(target.get("title"))
            exact_count = sum(
                1
                for candidate in detailed
                if primary_variants & work_title_variants(candidate.get("title"))
            )
            ranked = sorted(
                (
                    (
                        assess_series_match(
                            target, candidate, exact_matches=exact_count
                        ),
                        candidate,
                    )
                    for candidate in detailed
                ),
                key=lambda item: (item[0].ranking_score, item[0].title_similarity),
                reverse=True,
            )
            best, candidate = ranked[0]
            runner = ranked[1][0] if len(ranked) > 1 else None
            margin = best.ranking_score - runner.ranking_score if runner else None
            official = next(
                (
                    (item, link)
                    for item in detailed
                    if (link := official_link_for(target, item)) is not None
                ),
                None,
            )
            if official is None:
                platform = official_platform_candidate(target, detailed)
                if platform is not None:
                    official = (
                        platform,
                        {
                            "name": str(
                                platform.get("source_name") or release_host(platform)
                            )
                        },
                    )
            if official is not None:
                # The catalogue names this exact page as the work's official
                # platform: an identity by URL, stronger than any title match.
                candidate, link = official
                best = replace(
                    assess_series_match(target, candidate, exact_matches=exact_count),
                    confidence=1.0,
                    ranking_score=1.0,
                    reason=f"Official platform page listed by the catalogue ({link.get('name') or release_host(link)})",
                )
                runner = None
                margin = None
            if (
                best.confidence < AUTO_MATCH_THRESHOLD
                and best.confidence >= COUNT_CORROBORATION_FLOOR
            ):
                # Suwayomi's search returns no author or year, so a title that
                # only matches an alias can never gather enough evidence to be
                # decided. How long the work is corroborates it instead: it is
                # the one fact the source always knows.
                best = await self._corroborate_with_length(
                    provider, target, candidate, best, exact_count, language
                )
            elif (
                best.confidence >= AUTO_MATCH_THRESHOLD
                and "source names no creator" in best.reason
            ):
                # A match standing on the title alone, because the source
                # names nobody, is exactly the one that must survive the one
                # check the source can always answer. Costs a request only at
                # mapping time, never on refresh.
                best = await self._corroborate_with_length(
                    provider,
                    target,
                    candidate,
                    best,
                    exact_count,
                    language,
                    prefer_measured=True,
                )
            side_work = self._side_work_verdict(
                best, candidate, detailed, served_by, primary_variants, official
            )
            if side_work is not None:
                diagnostics.append(
                    {
                        "provider": provider.name,
                        "label": self._provider_label(provider),
                        "state": "no_match",
                        "title": candidate.get("title"),
                        "confidence": best.confidence,
                        "reason": side_work,
                    }
                )
                self._queue_review(
                    manga_id,
                    provider.name,
                    candidate,
                    best.confidence,
                    best.reason,
                    assessment=best,
                    decided=side_work,
                )
                continue
            if best.confidence < AUTO_MATCH_THRESHOLD:
                diagnostics.append(
                    {
                        "provider": provider.name,
                        "label": self._provider_label(provider),
                        "state": "no_match",
                        "title": candidate.get("title"),
                        "confidence": best.confidence,
                        "reason": best.reason,
                    }
                )
                self._queue_review(
                    manga_id,
                    provider.name,
                    candidate,
                    best.confidence,
                    best.reason,
                    assessment=best,
                    decided=self._redundant_verdict(best, provider.name, served_by),
                )
                continue
            # Only one candidate carrying the work's exact title, with the
            # same creators, is not a tie: a rival that merely looks close
            # cannot make it ambiguous. Two exact titles stay a human call.
            exact_relations = {"primary_exact", "edition_qualified_exact"}
            # The catalogue's own alternate title is the work's title too:
            # "Shibuya District, Maruyama Neighborhood: After School" by the
            # same creator is the work, not a near-miss for a human.
            alias_relations = {
                "candidate_primary_matches_alias",
                "primary_matches_candidate_alias",
                "alias_exact",
            }
            # The work's length, when the catalogues agree on it, identifies
            # the work as surely as its creator does (Hansel & Gretel was the
            # counter-example: an exact title with a *contradicting* length).
            # An exact title whose length agrees with the catalogue is the
            # work even next to another exact title: a second listing with
            # the same length is the same work twice, and one with another
            # length is a homonym the count already told apart.
            decisive_title = bool(
                (
                    best.title_relation in exact_relations
                    and "same chapter count" in best.reason
                )
                or (
                    (
                        best.title_relation in exact_relations
                        and best.creator_similarity >= 0.90
                    )
                    or (
                        best.title_relation in alias_relations
                        and best.creator_similarity >= 0.90
                    )
                )
                and (runner is None or runner.title_relation not in exact_relations)
            )
            if (
                runner is not None
                and margin is not None
                and margin < AUTO_MATCH_MIN_MARGIN
                and not decisive_title
            ):
                diagnostics.append(
                    {
                        "provider": provider.name,
                        "label": self._provider_label(provider),
                        "state": "ambiguous",
                        "title": candidate.get("title"),
                        "confidence": best.confidence,
                        "reason": f"Candidate margin {margin:.3f}; {best.reason}",
                    }
                )
                self._queue_review(
                    manga_id,
                    provider.name,
                    candidate,
                    best.confidence,
                    f"ambiguous: {best.reason}",
                    assessment=best,
                    decided=self._redundant_verdict(best, provider.name, served_by),
                )
                continue

            provider_manga_id = str(candidate["id"])
            if (provider.name, provider_manga_id) in rejected:
                # Unmapped by the operator or refused before: discovery does
                # not get to undo that on the next pass.
                diagnostics.append(
                    {
                        "provider": provider.name,
                        "label": self._provider_label(provider),
                        "state": "rejected",
                        "provider_manga_id": provider_manga_id,
                        "title": candidate.get("title"),
                        "reason": rejected[(provider.name, provider_manga_id)],
                    }
                )
                continue
            try:
                mapping = self.database.upsert_release_source(
                    manga_id,
                    provider=provider.name,
                    provider_manga_id=provider_manga_id,
                    title=str(candidate.get("title") or provider_manga_id),
                    source_url=str(candidate.get("source_url") or "") or None,
                    source_name=str(candidate.get("source_name") or "") or None,
                    language=language,
                    match_confidence=best.confidence,
                    match_reason=best.reason,
                )
                if mapping.get("source_name"):
                    # Later groups in this pass see the work as served.
                    served_by[str(mapping["source_name"])] = mapping
            except ReleaseSourceOwned as exc:
                # Two Tankarr series matched one entry on the source (a work
                # and its spin-off usually). Mapping it twice would leave one
                # of them failing forever, so a human picks the owner — unless
                # the entry names the series that already holds it and not
                # this one, which settles it without asking.
                entry_title = normalized_title(candidate.get("title"))
                decided = None
                if (
                    entry_title
                    and entry_title == normalized_title(exc.owner_title)
                    and entry_title != normalized_title(target.get("title"))
                ):
                    decided = (
                        "rejected: the entry names "
                        f"{exc.owner_title}, which already holds it"
                    )
                diagnostics.append(
                    {
                        "provider": provider.name,
                        "label": self._provider_label(provider),
                        "state": "ambiguous",
                        "provider_manga_id": provider_manga_id,
                        "title": candidate.get("title"),
                        "confidence": best.confidence,
                        "reason": str(exc),
                    }
                )
                self._queue_review(
                    manga_id,
                    provider.name,
                    candidate,
                    best.confidence,
                    f"already the source of another series: {best.reason}",
                    assessment=best,
                    decided=decided,
                )
                continue
            try:
                chapters = await provider.list_chapters(provider_manga_id, language)
                refusal = implausible_chapter_list(chapters, target)
                if refusal:
                    # The title matched, the content does not: undo the
                    # mapping on the spot and remember not to redo it.
                    self.database.delete_release_source(
                        manga_id, provider.name, provider_manga_id
                    )
                    self.database.add_release_source_rejection(
                        manga_id,
                        provider.name,
                        provider_manga_id,
                        f"chapter list refused: {refusal}",
                    )
                    served_by.pop(str(mapping.get("source_name") or ""), None)
                    logger.warning(
                        "Refused %s on %s for %s: %s",
                        provider_manga_id,
                        provider.name,
                        target.get("title"),
                        refusal,
                    )
                    diagnostics.append(
                        {
                            "provider": provider.name,
                            "label": self._provider_label(provider),
                            "state": "refused",
                            "provider_manga_id": provider_manga_id,
                            "title": mapping["title"],
                            "confidence": best.confidence,
                            "reason": f"chapter list refused: {refusal}",
                        }
                    )
                    continue
                inserted = await asyncio.to_thread(
                    self.database.upsert_chapters,
                    manga_id,
                    chapters,
                    monitor_new=monitor_new,
                )
                self.database.record_release_source_result(
                    manga_id, provider.name, provider_manga_id
                )
                diagnostics.append(
                    {
                        "provider": provider.name,
                        "label": self._provider_label(provider),
                        "state": "matched",
                        "provider_manga_id": provider_manga_id,
                        "title": mapping["title"],
                        "confidence": best.confidence,
                        "reason": best.reason,
                        "releases": int(inserted["seen"]),
                        "new": len(inserted["new_chapter_ids"]),
                        "new_chapter_ids": inserted["new_chapter_ids"],
                    }
                )
            except Exception as exc:  # noqa: BLE001 - identity remains useful
                message = f"{type(exc).__name__}: {exc}"[:1000]
                self.database.record_release_source_result(
                    manga_id, provider.name, provider_manga_id, error=message
                )
                diagnostics.append(
                    {
                        "provider": provider.name,
                        "label": self._provider_label(provider),
                        "state": "error",
                        "provider_manga_id": provider_manga_id,
                        "title": mapping["title"],
                        "error": message,
                    }
                )
        return diagnostics

    REVIEW_MIN_CONFIDENCE = 0.5

    # Below this many healthy sources, an unverifiable exact title is still a
    # question worth asking; at or above it, it is noise.
    REDUNDANT_SOURCE_FLOOR = 3

    @staticmethod
    def _side_work_verdict(
        best: SeriesMatchAssessment,
        candidate: dict[str, Any],
        detailed: list[dict[str, Any]],
        served_by: dict[str, dict[str, Any]],
        primary_variants: set[str],
        official: tuple[dict[str, Any], dict[str, Any]] | None,
    ) -> str | None:
        """A title that extends the work's own with a subtitle is another
        work (a side story, a spin-off) whenever the work's own title is
        served too - by this source, by another, or by the official page.
        Tower of God: Urek Mazino, live: same creator, sixty chapters merged
        into the tower's own numbering."""

        if best.title_relation != "candidate_subtitle_expands_primary":
            return None
        if official is not None and official[0] is candidate:
            return None
        served_exact = any(
            primary_variants & work_title_variants(mapping.get("title"))
            for mapping in served_by.values()
        ) or any(
            other is not candidate
            and primary_variants & work_title_variants(other.get("title"))
            for other in detailed
        )
        if not served_exact:
            return None
        return (
            "rejected: side work - the title extends the work's own, which is "
            "already served under its exact name"
        )

    def _redundant_verdict(
        self,
        best: SeriesMatchAssessment,
        provider_name: str,
        served_by: dict[str, dict[str, Any]],
    ) -> str | None:
        """An exact title the source cannot back (no creator, no checkable
        length) is a fair question for a work with few sources and pure
        noise for one already served by several verified ones."""

        if provider_name != "suwayomi":
            return None
        alias_relations = {
            "candidate_primary_matches_alias",
            "primary_matches_candidate_alias",
            "alias_exact",
        }
        if best.title_relation not in self.EXACT_TITLE_RELATIONS | alias_relations:
            return None
        if not self._unverifiable(best.reason):
            return None
        if len(served_by) < self.REDUNDANT_SOURCE_FLOOR:
            return None
        return (
            "rejected: unverifiable (the source names no creator and its length "
            f"could not be checked) and the work already has {len(served_by)} "
            "verified sources"
        )

    EXACT_TITLE_RELATIONS = frozenset({"primary_exact", "edition_qualified_exact"})

    @staticmethod
    def _unverifiable(reason: str) -> bool:
        """A title nothing but the title backs: the source names no creator
        (or none was compared), its length was not checked, and nothing
        contradicts it. A contradiction or a matching length is evidence,
        for or against, and stays a question or a match."""

        text = str(reason or "")
        if "chapter count" in text or "conflicts" in text:
            return False
        return "source names no creator" in text or "creator" not in text

    def _queue_review(
        self,
        manga_id: str,
        provider_name: str,
        candidate: dict[str, Any],
        confidence: float,
        reason: str,
        assessment: SeriesMatchAssessment | None = None,
        decided: str | None = None,
    ) -> None:
        """A near-miss (exact-looking title the rules refused) is a human's
        call: queue it once, never silently drop it.

        A homonym is not a near-miss: a different creator on a title that is
        not the work's own (an alias, or one with a subtitle added) is another
        work, so it is refused on the spot and kept only as history.
        """

        if confidence < self.REVIEW_MIN_CONFIDENCE:
            return
        homonym = bool(
            assessment is not None
            and "creator disagreement" in assessment.contradictions
            and assessment.title_relation not in self.EXACT_TITLE_RELATIONS
        )
        try:
            self.database.add_match_review(
                manga_id,
                kind="source",
                provider=provider_name,
                candidate_id=str(candidate.get("id") or ""),
                title=str(candidate.get("title") or ""),
                source_name=str(candidate.get("source_name") or "") or None,
                source_url=str(candidate.get("source_url") or "") or None,
                confidence=float(confidence),
                reason=str(reason),
                payload={"provider_manga_id": str(candidate.get("id") or "")},
                resolution=(
                    decided
                    or (
                        "rejected: different creator on a title that is not the work's"
                        if homonym
                        else None
                    )
                ),
            )
        except Exception:  # noqa: BLE001 - reviews are best effort
            logger.debug(
                "Could not queue a match review for %s", manga_id, exc_info=True
            )

    async def _corroborate_with_length(
        self,
        provider: Provider,
        target: dict[str, Any],
        candidate: dict[str, Any],
        best: SeriesMatchAssessment,
        exact_count: int,
        language: str,
        *,
        prefer_measured: bool = False,
    ) -> SeriesMatchAssessment:
        """Re-assess one borderline candidate knowing how long it is.

        The catalogue knows the work's chapter and volume counts; the source
        knows how many chapters it carries. Comparing them settles cases that
        the missing author and year evidence leaves undecided, and costs one
        request only when the alternative is asking the operator.
        """

        if candidate.get("chapter_count") is not None:
            return best
        if target.get("chapter_count") is None and target.get("volume_count") is None:
            return best
        try:
            chapters = await provider.list_chapters(str(candidate["id"]), language)
        except Exception:  # noqa: BLE001 - the count is a bonus, never a gate
            return best
        if not chapters:
            return best
        measured = {**candidate, "chapter_count": len(chapters)}
        revised = assess_series_match(target, measured, exact_matches=exact_count)
        if prefer_measured:
            # The caller is double-checking an acceptance, not rescuing a
            # borderline one: what the measurement says stands, even when it
            # says worse. Katsuhiro Otomo's Hansel & Gretel reached 0.99 under
            # Junko Mizuno's because nothing ever compared the source's 22
            # chapters with the work's 1.
            return revised
        return revised if revised.confidence > best.confidence else best

    async def discover_and_refresh(
        self, manga_id: str, *, monitor_new: bool, language: str | None = None
    ) -> dict[str, Any]:
        async with self._lock:
            manga = self.database.get_manga(manga_id)
            target = self._target(manga_id)
            language = language or str(manga["preferred_language"])
            target["language"] = language
            if language != manga["preferred_language"]:
                monitor_new = False
            refreshed = await self.refresh_mappings(
                manga_id, monitor_new=monitor_new, language=language
            )
            existing_by_provider: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for mapping in self.database.list_release_sources(manga_id):
                if mapping.get("language") == language:
                    existing_by_provider[str(mapping["provider"])].append(mapping)
            statuses = list(refreshed["sources"])
            # Chapter providers index comics. A prose novel can share its exact
            # title with a webtoon adaptation, but that is a different work.
            prose_novel = (
                "novel"
                in str(
                    target.get("work_subtype") or target.get("work_type") or ""
                ).casefold()
            )
            for provider_name, provider in list(self.providers.items()):
                if prose_novel:
                    continue
                if provider_name in {"local", str(manga.get("provider") or "")}:
                    continue
                if provider.search_mode != "title" or not provider.supports_language(
                    language
                ):
                    continue
                # One normal provider maps to one work. Suwayomi can map the
                # same work through several independently installed sources.
                if (
                    existing_by_provider.get(provider_name)
                    and provider_name != "suwayomi"
                ):
                    continue
                statuses.extend(
                    await self._discover_provider(
                        manga_id,
                        provider,
                        target,
                        language,
                        monitor_new=monitor_new,
                    )
                )
            self._settle_stale_reviews(manga_id)
            return {
                "manga_id": manga_id,
                "sources": statuses,
                "matched_sources": sum(
                    item.get("state") == "matched" for item in statuses
                ),
                "new": sum(int(item.get("new") or 0) for item in statuses),
                "seen": sum(int(item.get("releases") or 0) for item in statuses),
                "errors": [item for item in statuses if item.get("state") == "error"],
            }

    def _settle_stale_reviews(self, manga_id: str) -> int:
        """Close the source questions a later pass has already answered.

        A review is queued once, at the moment a candidate is refused; the
        series keeps changing after that. A candidate a later pass mapped is
        no longer a question, and an unverifiable exact title (no creator,
        no checkable length) stops being one as soon as the work is served
        by enough verified sources - the same verdict the picker gives at
        queue time, applied to questions asked before the sources arrived.
        """

        mappings = self.database.list_release_sources(manga_id)
        mapped = {str(item.get("provider_manga_id") or "") for item in mappings}
        healthy = {
            str(item.get("source_name") or "")
            for item in mappings
            if item.get("provider") == "suwayomi"
            and item.get("source_name")
            and not item.get("last_error")
        }
        settled = 0
        for review in self.database.list_match_reviews(open_only=True):
            if str(review.get("manga_id")) != str(manga_id):
                continue
            if str(review.get("kind") or "") != "source":
                continue
            reason = str(review.get("reason") or "")
            candidate_id = str(review.get("candidate_id") or "")
            resolution: str | None = None
            if candidate_id and candidate_id in mapped:
                resolution = "accepted: mapped by a later discovery pass"
            elif (
                str(review.get("provider") or "") == "suwayomi"
                and self._unverifiable(reason)
                and len(healthy) >= self.REDUNDANT_SOURCE_FLOOR
            ):
                resolution = (
                    "rejected: unverifiable (the source names no creator and its "
                    "length could not be checked) and the work already has "
                    f"{len(healthy)} verified sources"
                )
            if resolution is None:
                continue
            try:
                self.database.resolve_match_review(int(review["id"]), resolution)
                settled += 1
            except Exception:  # noqa: BLE001 - the next pass retries it
                logger.debug("Could not settle review %s", review.get("id"))
        if settled:
            logger.info("Settled %s stale source reviews of %s", settled, manga_id)
        return settled

    def releases_for_slot(
        self, manga_id: str, *, chapter: str | None, volume: str | None
    ) -> list[dict[str, Any]]:
        manga = self.database.get_manga(manga_id)
        wanted_chapter = canonical_number(chapter)
        wanted_volume = canonical_number(volume)
        output: list[dict[str, Any]] = []
        for release in self.database.list_chapters(
            manga_id, str(manga["preferred_language"])
        ):
            release_chapter = canonical_number(release.get("chapter"))
            release_volume = canonical_number(release.get("volume"))
            if wanted_chapter is not None:
                if release_chapter == wanted_chapter:
                    output.append(release)
            elif wanted_volume is not None and release_volume == wanted_volume:
                output.append(release)
        return output
