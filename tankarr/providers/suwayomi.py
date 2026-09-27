from __future__ import annotations

import asyncio
import io
import logging
import re
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx
from PIL import Image, UnidentifiedImageError

from tankarr import USER_AGENT
from tankarr.download_tuning import AdaptivePageConcurrency, local_download_pressure
from tankarr.providers.base import (
    ProgressCallback,
    Provider,
    ProviderHTTP,
    ProviderRequestError,
    ProviderUnavailableError,
    SearchDiagnostic,
)
from tankarr.source_numbering import renumber_from_titles

logger = logging.getLogger(__name__)

SOURCE_QUERY = """
query TankarrSources($language: String!) {
  sources(condition: {lang: $language}, first: 500) {
    nodes {
      id
      name
      displayName
      lang
      contentWarning
      homeUrl
    }
  }
}
"""

SEARCH_MUTATION = """
mutation TankarrSearch($input: FetchSourceMangaInput!) {
  fetchSourceManga(input: $input) {
    mangas {
      id
      sourceId
      title
      thumbnailUrl
      author
      artist
      description
      status
      realUrl
    }
    hasNextPage
  }
}
"""

DETAIL_MUTATION = """
mutation TankarrManga($input: FetchMangaAndChaptersInput!) {
  fetchMangaAndChapters(input: $input) {
    manga {
      id
      sourceId
      title
      thumbnailUrl
      author
      artist
      description
      status
      realUrl
      source {
        id
        name
        displayName
        lang
        homeUrl
      }
    }
    chapters {
      id
      name
      uploadDate
      chapterNumber
      scanlator
      realUrl
      pageCount
    }
  }
}
"""

PAGES_MUTATION = """
mutation TankarrPages($input: FetchChapterPagesInput!) {
  fetchChapterPages(input: $input) {
    pages
    chapter {
      id
      pageCount
    }
  }
}
"""

MANGA_STATUS = {
    "ONGOING": "ongoing",
    "COMPLETED": "completed",
    "LICENSED": "completed",
    "PUBLISHING_FINISHED": "completed",
    "CANCELLED": "cancelled",
    "ON_HIATUS": "hiatus",
}

IMAGE_SUFFIXES = {
    "image/avif": ".avif",
    "image/gif": ".gif",
    "image/jpeg": ".jpg",
    # Non-standard but common spellings some sources send for JPEG.
    "image/jpg": ".jpg",
    "image/pjpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}

# Every request targets the operator's local Suwayomi gateway.  The per-chapter
# semaphore remains the hard concurrency bound; this limiter only prevents a
# burst from overwhelming the JVM when more than one operation shares it.
LOCAL_REQUESTS_PER_SECOND = 24.0
PRIORITY_PAGE = 0
PRIORITY_PREPARE = 5
PRIORITY_UI = 10
PRIORITY_REFRESH = 20
PRIORITY_DISCOVERY = 30


ID_PATTERN = re.compile(r"suwayomi-(?:([0-9a-f]{8})-)?([1-9][0-9]*)")


def _verify_page(content: bytes) -> None:
    """Validate the payload before accepting HTTP success; never recompress it."""
    with Image.open(io.BytesIO(content)) as page:
        page.verify()


class StaleSuwayomiIdentity(ProviderRequestError):
    """The id was minted by a different Suwayomi instance.

    Suwayomi ids are local to one server database. When Tankarr switches
    instance (legacy container → managed runtime, or a new external server)
    every stored ``suwayomi-…`` id stops meaning what it meant.
    """


def suwayomi_id_is_current(value: object, instance_token: str | None) -> bool:
    match = ID_PATTERN.fullmatch(str(value or ""))
    if match is None:
        return False
    return not instance_token or match.group(1) == instance_token


class SuwayomiProvider(Provider):
    """Release source backed by installed Suwayomi/Mihon extensions."""

    name = "suwayomi"
    label = "Suwayomi sources"

    def __init__(
        self,
        base_url: str,
        *,
        username: str | None = None,
        password: str | None = None,
        language: str = "en",
        source_ids: set[int] | frozenset[int] = frozenset(),
        instance_token: str | None = None,
        search_timeout_seconds: float = 30.0,
        timeout_seconds: float = 30.0,
    ):
        self.base_url = base_url.rstrip("/") + "/"
        # Used only as a fallback when an old Suwayomi response omits the
        # source language. Searches themselves use the language selected in
        # Tankarr and query the matching installed extensions dynamically.
        self.default_language = language.strip().lower() or "en"
        self.source_ids = frozenset(int(item) for item in source_ids)
        self.instance_token = instance_token or None
        self.search_timeout_seconds = search_timeout_seconds
        self.page_prepare_timeout_seconds = min(20.0, max(5.0, float(timeout_seconds)))
        self._credentials = (
            (username, password)
            if username is not None and password is not None
            else None
        )
        self._session_generation = 0
        self._login_lock = asyncio.Lock()
        auth = (
            httpx.BasicAuth(username, password)
            if username is not None and password is not None
            else None
        )
        self.http = ProviderHTTP(
            auth=auth,
            headers={"Accept": "application/json", "User-Agent": USER_AGENT},
            timeout_seconds=timeout_seconds,
            requests_per_second=LOCAL_REQUESTS_PER_SECOND,
            follow_redirects=False,
        )
        self.page_concurrency = AdaptivePageConcurrency(
            self.http.limiter.requests_per_second
        )
        self._reported_page_concurrency: int | None = None

    def supports_language(self, language: str) -> bool:
        return bool(language.strip())

    async def aclose(self) -> None:
        await self.http.aclose()

    async def healthcheck(self) -> dict[str, Any]:
        """Verify endpoint, authentication, GraphQL, and installed sources."""

        source_details = await self.source_catalog([self.default_language])
        enabled = [item for item in source_details if item["enabled"]]
        if not enabled:
            raise ProviderRequestError(
                f"Suwayomi has no allowed {self.default_language} sources installed"
            )
        return {
            "ok": True,
            "language": self.default_language,
            "sources": len(enabled),
            "source_details": source_details,
        }

    async def _login(self, failed_generation: int) -> None:
        """Create Suwayomi's simple-login cookie after an API 401."""

        if self._credentials is None:
            raise ProviderRequestError("Suwayomi requires authentication")
        async with self._login_lock:
            # Another concurrent request already refreshed the shared cookie.
            if self._session_generation != failed_generation:
                return
            username, password = self._credentials
            try:
                response = await self.http.request(
                    "POST",
                    urljoin(self.base_url, "login.html"),
                    params={"redirect": "/"},
                    data={"user": username, "pass": password},
                    priority=PRIORITY_PAGE,
                )
            except httpx.HTTPStatusError as exc:
                # ProviderHTTP deliberately treats redirects as non-success.
                # Suwayomi signals a successful form login with 302/303.
                if exc.response.status_code not in {302, 303}:
                    raise
                response = exc.response
            if response.status_code not in {302, 303}:
                raise ProviderRequestError(
                    "Suwayomi rejected the configured username or password"
                )
            self._session_generation += 1

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: Any = None,
        headers: dict[str, str] | None = None,
        json: Any = None,
        priority: int = PRIORITY_REFRESH,
    ) -> httpx.Response:
        """Retry once with a native simple-login session when required."""

        generation = self._session_generation
        try:
            return await self.http.request(
                method,
                url,
                params=params,
                headers=headers,
                json=json,
                priority=priority,
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 401:
                raise
        await self._login(generation)
        return await self.http.request(
            method,
            url,
            params=params,
            headers=headers,
            json=json,
            priority=priority,
        )

    async def _graphql(
        self,
        query: str,
        variables: dict[str, Any],
        *,
        priority: int | None = None,
    ) -> dict[str, Any]:
        if priority is None:
            if query == PAGES_MUTATION:
                priority = PRIORITY_PREPARE
            elif query in {SOURCE_QUERY, SEARCH_MUTATION}:
                priority = PRIORITY_DISCOVERY
            else:
                priority = PRIORITY_REFRESH
        failed_generation = self._session_generation
        response = await self._request(
            "POST",
            urljoin(self.base_url, "api/graphql"),
            json={"query": query, "variables": variables},
            priority=priority,
        )
        payload = self._graphql_payload(response)
        errors = payload.get("errors") or []
        if self._graphql_unauthorized(errors):
            await self._login(failed_generation)
            response = await self._request(
                "POST",
                urljoin(self.base_url, "api/graphql"),
                json={"query": query, "variables": variables},
                priority=priority,
            )
            payload = self._graphql_payload(response)
            errors = payload.get("errors") or []
        data = payload.get("data")
        if errors or not isinstance(data, dict):
            messages = "; ".join(
                self._graphql_error_summary(item) for item in errors[:3]
            )
            if "missing source" in messages.casefold():
                # The extension behind this mapping was uninstalled: the
                # mapping cannot be refreshed and is dropped, exactly like one
                # that points into a Suwayomi database that no longer exists.
                raise StaleSuwayomiIdentity(
                    f"Suwayomi no longer has this source: {messages}"
                )
            if any(
                marker in messages.casefold()
                for marker in ("rate limit", "too many requests", "http 429")
            ):
                # A remote throttle says nothing about the chapter's validity.
                # Let the durable retry and source circuit cool the provider
                # down without permanently blocking this release.
                raise ProviderUnavailableError(
                    f"Suwayomi source is rate limited: {messages}"
                )
            raise ProviderRequestError(
                f"Suwayomi GraphQL request failed: {messages or 'missing data'}"
            )
        return data

    @staticmethod
    def _graphql_payload(response: httpx.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderRequestError(
                "Suwayomi returned invalid GraphQL JSON"
            ) from exc
        if not isinstance(payload, dict):
            raise ProviderRequestError("Suwayomi returned invalid GraphQL JSON")
        return payload

    @staticmethod
    def _graphql_unauthorized(errors: list[Any]) -> bool:
        return any(
            "unauthorized" in SuwayomiProvider._graphql_error_summary(error).casefold()
            for error in errors
        )

    @staticmethod
    def _graphql_error_summary(error: Any) -> str:
        raw = (
            str(error.get("message") or error)
            if isinstance(error, dict)
            else str(error)
        )
        # Extensions can attach a complete Java stack trace to GraphQL errors.
        # It belongs in Suwayomi's logs, not in Tankarr's operator-facing UI.
        first_line = raw.splitlines()[0] if raw else "unknown error"
        return re.sub(r"\s+", " ", first_line).strip()[:300] or "unknown error"

    async def _installed_sources(self, language: str) -> list[dict[str, Any]]:
        selected_language = language.strip().lower()
        data = await self._graphql(SOURCE_QUERY, {"language": selected_language})
        sources = list((data.get("sources") or {}).get("nodes") or [])
        installed: list[dict[str, Any]] = []
        for source in sources:
            source_language = str(source.get("lang") or "").strip().lower()
            if source_language and source_language != selected_language:
                continue
            installed.append(source)
        return installed

    def _source_selected(self, source: dict[str, Any]) -> bool:
        source_id = int(source["id"])
        return not self.source_ids or source_id in self.source_ids

    def _source_safe(self, source: dict[str, Any]) -> bool:
        # MIXED means a general catalogue may contain adult titles (for
        # example MangaDex), not that every result is adult. Exclude only
        # extensions explicitly classified as NSFW by default.
        # An installed extension is an operator decision; nothing is filtered.
        return True

    async def source_catalog(
        self, languages: list[str] | tuple[str, ...]
    ) -> list[dict[str, Any]]:
        """Return installed sources and the effective Tankarr selection."""

        catalog: list[dict[str, Any]] = []
        seen: set[int] = set()
        for language in dict.fromkeys(
            item.strip().lower() for item in languages if item.strip()
        ):
            for source in await self._installed_sources(language):
                source_id = int(source["id"])
                if source_id in seen:
                    continue
                seen.add(source_id)
                selected = self._source_selected(source)
                allowed = self._source_safe(source)
                catalog.append(
                    {
                        "id": str(source_id),
                        "name": str(
                            source.get("displayName") or source.get("name") or source_id
                        )[:200],
                        "language": str(source.get("lang") or language).strip().lower(),
                        "content_warning": str(
                            source.get("contentWarning") or "UNKNOWN"
                        )[:30],
                        "selected": selected,
                        "allowed": allowed,
                        "enabled": selected and allowed,
                    }
                )
        return sorted(
            catalog,
            key=lambda item: (
                not item["enabled"],
                item["language"],
                str(item["name"]).casefold(),
                item["id"],
            ),
        )

    async def _sources(self, language: str) -> list[dict[str, Any]]:
        return [
            source
            for source in await self._installed_sources(language)
            if self._source_selected(source) and self._source_safe(source)
        ]

    async def search(
        self, query: str, language: str, limit: int = 20
    ) -> list[dict[str, Any]]:
        results, _diagnostics = await self.search_with_diagnostics(
            query, language, limit
        )
        return results

    async def search_with_diagnostics(
        self, query: str, language: str, limit: int = 20
    ) -> tuple[list[dict[str, Any]], list[SearchDiagnostic]]:
        if not self.supports_language(language):
            return [], []
        selected_language = language.strip().lower()
        sources = await self._sources(selected_language)
        if not sources:
            raise ProviderRequestError(
                f"Suwayomi has no allowed {selected_language} sources installed"
            )
        semaphore = asyncio.Semaphore(4)

        async def search_source(source: dict[str, Any]) -> list[dict[str, Any]]:
            try:
                async with asyncio.timeout(self.search_timeout_seconds):
                    async with semaphore:
                        data = await self._graphql(
                            SEARCH_MUTATION,
                            {
                                "input": {
                                    # Suwayomi exposes source ids as the GraphQL
                                    # Long scalar. Its JSON coercion accepts
                                    # decimal strings (ids exceed JavaScript's
                                    # safe integer range), not JSON numbers.
                                    "source": str(source["id"]),
                                    "type": "SEARCH",
                                    "page": 1,
                                    "query": query,
                                }
                            },
                        )
            except TimeoutError as exc:
                raise ProviderRequestError(
                    f"Search exceeded {self.search_timeout_seconds:g}s"
                ) from exc
            payload = data.get("fetchSourceManga") or {}
            return [
                self._parse_manga(item, source=source, language=language)
                for item in payload.get("mangas") or []
            ]

        outcomes = await asyncio.gather(
            *(search_source(source) for source in sources), return_exceptions=True
        )
        source_results: list[list[dict[str, Any]]] = []
        errors: list[tuple[dict[str, Any], BaseException]] = []
        for source, outcome in zip(sources, outcomes):
            if isinstance(outcome, BaseException):
                errors.append((source, outcome))
            else:
                source_results.append(outcome)
        if not source_results and errors and len(errors) == len(outcomes):
            source, error = errors[0]
            source_name = str(
                source.get("displayName") or source.get("name") or source["id"]
            )
            raise ProviderRequestError(
                f"Every Suwayomi source search failed; {source_name}: {error}"
            )

        # Preserve each extension's relevance order while sharing the result
        # budget fairly. Without round-robin merging, a prolific first source
        # can fill all 20 slots and make the other installed sources invisible
        # even though they were queried successfully.
        results: list[dict[str, Any]] = []
        result_limit = max(1, limit)
        depth = 0
        while len(results) < result_limit:
            added = False
            for items in source_results:
                if depth < len(items):
                    results.append(items[depth])
                    added = True
                    if len(results) == result_limit:
                        break
            if not added:
                break
            depth += 1
        diagnostics = [
            {
                "provider": str(
                    source.get("displayName") or source.get("name") or source["id"]
                ),
                "error": str(error),
            }
            for source, error in errors
        ]
        return results, diagnostics

    async def _detail(self, manga_id: str) -> dict[str, Any]:
        identifier = self._numeric_id(manga_id, "manga")
        data = await self._graphql(
            DETAIL_MUTATION,
            {
                "input": {
                    "id": identifier,
                    "fetchManga": True,
                    "fetchChapters": True,
                }
            },
        )
        payload = data.get("fetchMangaAndChapters")
        if not isinstance(payload, dict) or not isinstance(payload.get("manga"), dict):
            raise ProviderRequestError(f"Suwayomi manga does not exist: {manga_id}")
        return payload

    async def get_manga(self, manga_id: str) -> dict[str, Any]:
        payload = await self._detail(manga_id)
        manga = payload["manga"]
        source = manga.get("source") or {}
        language = str(source.get("lang") or self.default_language).strip().lower()
        return self._parse_manga(manga, source=source, language=language)

    async def list_chapters(self, manga_id: str, language: str) -> list[dict[str, Any]]:
        if not self.supports_language(language):
            return []
        payload = await self._detail(manga_id)
        source_language = (
            str((payload.get("manga", {}).get("source") or {}).get("lang") or language)
            .strip()
            .lower()
        )
        if source_language != language.strip().lower():
            return []
        source = payload.get("manga", {}).get("source") or {}
        chapters = [
            self._parse_chapter(item, language=language, source=source)
            for item in payload.get("chapters") or []
        ]
        # A webtoon platform numbers episodes, not chapters: when its titles
        # carry the real chapter numbers, they win over the episode index.
        chapters = renumber_from_titles(chapters)
        chapters.sort(key=lambda item: self._chapter_sort_key(item["chapter"]))
        return chapters

    async def get_cover(self, manga_id: str, filename: str) -> tuple[bytes, str]:
        if filename != "cover":
            raise ValueError("Invalid Suwayomi cover identifier")
        payload = await self._detail(manga_id)
        thumbnail = str(payload["manga"].get("thumbnailUrl") or "")
        if not thumbnail:
            raise FileNotFoundError(f"Suwayomi manga has no cover: {manga_id}")
        response = await self._request(
            "GET",
            self._proxy_url(thumbnail),
            headers={"Accept": "image/avif,image/webp,image/*"},
            priority=PRIORITY_UI,
        )
        content_type = response.headers.get("content-type", "").split(";", 1)[0]
        if content_type not in IMAGE_SUFFIXES:
            raise ValueError("Suwayomi cover response was not a supported image")
        return response.content, content_type

    async def download_pages(
        self,
        chapter_id: str,
        target_dir: Path,
        progress: ProgressCallback,
        concurrency: int = 4,
    ) -> list[Path]:
        identifier = self._numeric_id(chapter_id, "chapter")
        try:
            async with asyncio.timeout(self.page_prepare_timeout_seconds):
                data = await self._graphql(
                    PAGES_MUTATION,
                    {"input": {"chapterId": identifier}},
                )
        except TimeoutError as exc:
            if await self._runtime_responding():
                raise ProviderRequestError(
                    "Suwayomi source timed out while preparing chapter pages"
                ) from exc
            raise ProviderUnavailableError(
                "Suwayomi runtime stopped responding while preparing chapter pages"
            ) from exc
        payload = data.get("fetchChapterPages") or {}
        page_urls = list(payload.get("pages") or [])
        if not page_urls:
            raise ProviderRequestError(
                f"Suwayomi returned no pages for chapter {chapter_id}"
            )
        target_dir.mkdir(parents=True, exist_ok=True)
        effective_concurrency = self.page_concurrency.recommend(
            concurrency, **local_download_pressure()
        )
        if effective_concurrency != self._reported_page_concurrency:
            logger.info(
                "Adaptive page concurrency is %s/%s (%s)",
                effective_concurrency,
                concurrency,
                self.page_concurrency.status(),
            )
            self._reported_page_concurrency = effective_concurrency
        completed = 0
        progress_lock = asyncio.Lock()
        work: asyncio.Queue[tuple[int, str]] = asyncio.Queue()
        for index, page in enumerate(page_urls):
            work.put_nowait((index, str(page)))
        results: list[Path | None] = [None] * len(page_urls)

        async def fetch(index: int, raw_url: str) -> Path:
            nonlocal completed
            try:
                for attempt in range(3):
                    response = await self._request(
                        "GET",
                        self._proxy_url(str(raw_url)),
                        priority=PRIORITY_PAGE,
                    )
                    try:
                        await asyncio.to_thread(_verify_page, response.content)
                        break
                    except (UnidentifiedImageError, OSError, ValueError) as exc:
                        if attempt == 2:
                            raise ProviderRequestError(
                                f"Page {index + 1} is not a valid image after 3 attempts"
                            ) from exc
                        await asyncio.sleep(0.25 * (attempt + 1))
            except ProviderUnavailableError as exc:
                status_code = getattr(exc, "status_code", None)
                self.page_concurrency.observe_failure(status_code)
                if await self._runtime_responding():
                    raise ProviderRequestError(
                        f"Suwayomi source failed while fetching page {index + 1}",
                        status_code=status_code,
                    ) from exc
                raise
            except (ProviderRequestError, httpx.HTTPError) as exc:
                status_code = getattr(exc, "status_code", None)
                if status_code is None and isinstance(exc, httpx.HTTPStatusError):
                    status_code = exc.response.status_code
                self.page_concurrency.observe_failure(status_code)
                raise
            for status_code in response.extensions.get("tankarr_retry_statuses", ()):
                self.page_concurrency.observe_failure(int(status_code))
            self.page_concurrency.observe_success(
                float(response.extensions.get("tankarr_network_seconds", 0.0)),
                len(response.content),
                float(response.extensions.get("tankarr_rate_wait_seconds", 0.0)),
            )
            content_type = (
                response.headers.get("content-type", "")
                .split(";", 1)[0]
                .strip()
                .casefold()
            )
            suffix = IMAGE_SUFFIXES.get(content_type)
            if suffix is None:
                raise ValueError(
                    f"Suwayomi page {index + 1} has unsupported type {content_type!r}"
                )
            destination = target_dir / f"{index + 1:04d}{suffix}"
            await asyncio.to_thread(destination.write_bytes, response.content)
            async with progress_lock:
                completed += 1
                await progress(completed, len(page_urls))
            return destination

        async def page_worker() -> None:
            while True:
                try:
                    index, page = work.get_nowait()
                except asyncio.QueueEmpty:
                    return
                results[index] = await fetch(index, page)

        workers = [
            asyncio.create_task(page_worker(), name=f"suwayomi-page-{index}")
            for index in range(min(effective_concurrency, len(page_urls)))
        ]
        try:
            await asyncio.gather(*workers)
        except BaseException:
            for worker in workers:
                worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            raise
        return [page for page in results if page is not None]

    async def _runtime_responding(self) -> bool:
        """Distinguish a stalled source extension from a dead local runtime."""

        try:
            async with asyncio.timeout(3.0):
                await self._request(
                    "GET",
                    self.base_url,
                    headers={"Accept": "text/html"},
                    priority=PRIORITY_PAGE,
                )
            return True
        except (ProviderRequestError, httpx.HTTPError, TimeoutError):
            return False

    def _parse_manga(
        self,
        item: dict[str, Any],
        *,
        source: dict[str, Any],
        language: str,
    ) -> dict[str, Any]:
        manga_id = self._public_id(item["id"])
        authors = list(
            dict.fromkeys(
                value.strip()
                for value in (item.get("author"), item.get("artist"))
                if isinstance(value, str) and value.strip()
            )
        )
        source_name = str(source.get("displayName") or source.get("name") or "")
        source_url = item.get("realUrl") or source.get("homeUrl")
        return {
            "id": manga_id,
            "provider": self.name,
            "title": str(item.get("title") or f"Suwayomi manga {manga_id}"),
            "description": str(item.get("description") or ""),
            "cover_url": (
                f"/api/covers/{self.name}/{manga_id}/cover"
                if item.get("thumbnailUrl")
                else None
            ),
            "authors": authors,
            "original_language": None,
            "status": MANGA_STATUS.get(str(item.get("status") or "")),
            "year": None,
            "last_volume": None,
            "last_chapter": None,
            "available_languages": [language],
            "source_url": source_url,
            "source_name": source_name,
            "source_id": str(source.get("id") or item.get("sourceId") or ""),
            "preferred_language": language,
        }

    _VOLUME_IN_NAME = re.compile(r"\b(?:vol(?:ume)?\.?|v\.)\s*0*(\d+)\b", re.IGNORECASE)
    # A chapter entry whose name is nothing but a volume marker ("Volume 21",
    # "Vol. 3", "Volume 2: The Storm") is a whole book that the source lists
    # in its chapter list, numbered by its running index. Weeb Central does
    # this for Nana: 21 "chapters" of 180-280 pages that are the 21 volumes.
    # Taking the index as a chapter number puts a book in a chapter slot and
    # leaves the real chapters to be downloaded again from another source.
    _WHOLE_BOOK_NAME = re.compile(
        r"^\s*(?:vol(?:ume)?\.?|tome|book)\s*0*(\d+)\s*(?:[:\-\u2013\u2014|].*)?$",
        re.IGNORECASE,
    )
    _CHAPTER_MARKER = re.compile(
        r"\b(?:chapter|chap\.?|ch\.?|episode|ep\.?)\s*\d", re.IGNORECASE
    )

    def _parse_chapter(
        self,
        item: dict[str, Any],
        *,
        language: str,
        source: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        scanlator = str(item.get("scanlator") or "").strip()
        source = source or {}
        source_id = str(source.get("id") or "").strip()
        # Tachiyomi chapters carry no volume field; many sources still write
        # it in the chapter name ("Vol.3 Ch.20"), which is worth keeping.
        name = str(item.get("name") or "")
        volume_match = self._VOLUME_IN_NAME.search(name)
        source_chapter = self._number(item.get("chapterNumber"))
        whole_book = self._WHOLE_BOOK_NAME.match(name)
        if whole_book is not None and not self._CHAPTER_MARKER.search(name):
            volume: str | None = str(int(whole_book.group(1)))
            chapter: str | None = None
            release_unit = "volume"
        else:
            volume = volume_match.group(1) if volume_match else None
            chapter = source_chapter
            release_unit = "chapter"
        return {
            "id": self._public_id(item["id"]),
            "volume": volume,
            "chapter": chapter,
            "source_chapter": chapter,
            "canonical_chapter": chapter,
            "release_unit": release_unit,
            "title": name,
            "language": language,
            "provider": self.name,
            "groups": [scanlator] if scanlator else [],
            "publish_at": self._timestamp(item.get("uploadDate")),
            "source_url": item.get("realUrl") or "",
            "pages": int(item.get("pageCount") or 0) or None,
            "version": 1,
            # Per-source identity: the ranking distinguishes MANGA Plus from
            # an aggregator even though both arrive through Suwayomi.
            "source_key": f"{self.name}:{source_id}" if source_id else self.name,
            "source_name": str(
                source.get("displayName") or source.get("name") or ""
            ).strip()
            or None,
        }

    def _numeric_id(self, raw: str, kind: str) -> int:
        match = ID_PATTERN.fullmatch(str(raw))
        if match is None:
            raise ValueError(f"Invalid Suwayomi {kind} id: {raw}")
        if self.instance_token and match.group(1) != self.instance_token:
            raise StaleSuwayomiIdentity(
                f"Suwayomi {kind} id {raw} belongs to a previous Suwayomi instance"
            )
        return int(match.group(2))

    def _public_id(self, raw: Any) -> str:
        value = str(raw)
        if not re.fullmatch(r"[1-9][0-9]*", value):
            raise ProviderRequestError(
                f"Suwayomi returned an invalid identifier: {raw}"
            )
        if self.instance_token:
            return f"suwayomi-{self.instance_token}-{value}"
        return f"suwayomi-{value}"

    def _proxy_url(self, raw: str) -> str:
        """Resolve only URLs served by the authenticated Suwayomi origin."""

        candidate = urljoin(self.base_url, raw)
        expected = urlsplit(self.base_url)
        actual = urlsplit(candidate)
        if (
            actual.scheme != expected.scheme
            or actual.hostname != expected.hostname
            or actual.port != expected.port
            or actual.username is not None
            or actual.password is not None
        ):
            raise ProviderRequestError(
                "Suwayomi returned a page or cover URL outside its authenticated origin"
            )
        return candidate

    @staticmethod
    def _number(raw: Any) -> str | None:
        if raw is None:
            return None
        number = Decimal(str(raw))
        if number == number.to_integral_value():
            # "20" must stay "20": stripping zeros only applies to a fraction.
            return str(int(number))
        normalized = format(number, "f").rstrip("0").rstrip(".")
        return normalized or "0"

    @staticmethod
    def _chapter_sort_key(raw: str | None) -> tuple[int, Decimal | str]:
        if raw is None:
            return (1, "")
        try:
            return (0, Decimal(raw))
        except Exception:  # noqa: BLE001 - provider chapter labels can be arbitrary
            return (1, raw.casefold())

    @staticmethod
    def _timestamp(raw: Any) -> str | None:
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return None
        if value <= 0:
            return None
        # Suwayomi stores chapter upload dates as Unix milliseconds.
        return datetime.fromtimestamp(value / 1000, tz=UTC).isoformat()
