"""Internet Archive as a direct-download book source.

archive.org keeps whole volumes of scanned and digital comics as CBZ, CBR
and PDF, reachable with three public, unauthenticated calls: the advanced
search (``advancedsearch.php``), the item metadata (``/metadata/{id}``,
which lists the files) and the plain file download (``/download/{id}/{f}``).
No torrent client is involved: Tankarr fetches the file itself and hands it
to the same import path a finished torrent takes, so every guard - identity
review, language audit, numbering, page quality - applies unchanged.

Measured live on 2026-09-03: archive.org silently drops requests whose
User-Agent names Prowlarr (100 s hangs, then a six-hour backoff), while it
answers Tankarr's own User-Agent in under a second. The site is asked at
most once per second and only for whole books, never chapter by chapter.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from tankarr import USER_AGENT
from tankarr.config import Settings
from tankarr.language_audit import automatic_english_decision
from tankarr.providers.base import ProviderHTTP
from tankarr.torrent_utils import release_match_score, release_number_hints

logger = logging.getLogger(__name__)

SEARCH_URL = "https://archive.org/advancedsearch.php"
METADATA_URL = "https://archive.org/metadata"
DOWNLOAD_URL = "https://archive.org/download"
DETAILS_URL = "https://archive.org/details"

PROVIDER_NAME = "internetarchive"
PROVIDER_LABEL = "Internet Archive"
PROTOCOL = "http"

# The archive's own format labels for the files Tankarr can import.
BOOK_FORMATS = {
    "Comic Book ZIP": ".cbz",
    "Comic Book RAR": ".cbr",
    "Text PDF": ".pdf",
}
# Items to inspect per search: each costs one metadata request.
MAX_ITEMS = 6
# A "book" smaller than this is a cover, a sample or a broken upload.
MIN_BOOK_BYTES = 1_000_000
# archive.org files above this are packs or scans nobody needs whole.
MAX_BOOK_BYTES = 2 * 1024 * 1024 * 1024
DOWNLOAD_CHUNK_BYTES = 1024 * 1024
PROGRESS_INTERVAL_SECONDS = 2.0
# Ranking: a scan from the archive is the fallback, below any indexer.
INDEXER_PRIORITY = 60
# A title the archive always holds, so a probe measures the archive and not
# the library: a connectivity test, not a library-coverage one.
PROBE_TITLE = "Berserk"

_QUOTE_STRIP = re.compile(r'["()\[\]{}:\\]')


def pseudo_hash(identifier: str, name: str) -> str:
    """A stable 40-hex key for one file, playing the info hash's role."""

    return hashlib.sha1(f"ia:{identifier}/{name}".encode()).hexdigest()


def _lucene_phrase(text: str) -> str:
    cleaned = " ".join(_QUOTE_STRIP.sub(" ", str(text or "")).split())
    return f'"{cleaned}"' if cleaned else ""


def search_query(title: str) -> str:
    """The archive query for one work: its title as a phrase, books only."""

    phrase = _lucene_phrase(title)
    formats = " OR ".join(f'format:("{name}")' for name in BOOK_FORMATS)
    return f"title:({phrase}) AND mediatype:(texts) AND ({formats})"


# Uploaders do not spell a title the way a catalogue does. The archive holds
# Dark Horse's Ghost in the Shell under "MANGA: Ghost in the Shell 1", which the
# phrase "The Ghost in the Shell" never matches: the books were invisible, and
# the series stayed on chapters because no book was ever offered for it.
_LEADING_ARTICLES = ("the ", "a ", "an ", "il ", "lo ", "la ", "le ", "l'")
_TITLE_PUNCTUATION = re.compile(r"[:;,\-\u2010-\u2015/_]+")
# A subtitle shorter than this is too generic to search on its own.
MIN_SUBTITLE_CHARS = 10


def search_titles(title: str) -> list[str]:
    """The phrasings to try for one work, most faithful first.

    Each is tried only while the previous found nothing, so the common case
    still costs exactly one request.
    """

    original = " ".join(str(title or "").split())
    if not original:
        return []
    attempts = [original]

    relaxed = original
    lowered = relaxed.casefold()
    for article in _LEADING_ARTICLES:
        if lowered.startswith(article):
            relaxed = relaxed[len(article) :].lstrip()
            break
    relaxed = " ".join(_TITLE_PUNCTUATION.sub(" ", relaxed).split())
    if relaxed and relaxed.casefold() != original.casefold():
        attempts.append(relaxed)

    # "The Ghost in the Shell 1.5: Human-Error Processor" is filed under
    # "01.5", so no whole-title phrasing reaches it; the subtitle does.
    head, sep, tail = original.partition(":")
    if sep:
        subtitle = " ".join(_TITLE_PUNCTUATION.sub(" ", tail).split())
        if len(subtitle) >= MIN_SUBTITLE_CHARS and subtitle not in attempts:
            attempts.append(subtitle)
    return attempts


def _integer(value: object) -> int:
    try:
        return max(0, int(float(str(value))))
    except (TypeError, ValueError):
        return 0


def _archive_language_allows_english(value: object) -> bool:
    """An explicit archive language must agree with an English search.

    Search results omit language, but item metadata can declare a different
    language. An unknown declaration is not evidence for English.
    """

    if value is None or value == "":
        return True
    declarations = value if isinstance(value, list) else [value]
    tokens = [
        token.strip().casefold()
        for declaration in declarations
        for token in re.split(r"[,;/]", str(declaration))
        if token.strip()
    ]
    return bool(tokens) and all(
        token in {"en", "eng", "english"} or token.startswith("en-") for token in tokens
    )


def normalize_item_files(
    item: dict[str, Any],
    files: list[dict[str, Any]],
    *,
    query: str,
    language: str,
) -> list[dict[str, Any]]:
    """One release per importable original file of one archive item."""

    identifier = str(item.get("identifier") or "").strip()
    if not identifier:
        return []
    item_title = " ".join(str(item.get("title") or identifier).split())
    downloads = _integer(item.get("downloads"))
    published = str(item.get("publicdate") or "")[:100] or None
    releases: list[dict[str, Any]] = []
    for entry in files:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()
        fmt = str(entry.get("format") or "").strip()
        if not name or fmt not in BOOK_FORMATS:
            continue
        if str(entry.get("source") or "original").casefold() != "original":
            continue  # the archive's own derivatives duplicate the upload
        size = _integer(entry.get("size"))
        if size < MIN_BOOK_BYTES or size > MAX_BOOK_BYTES:
            continue
        if language == "en":
            language_claim = automatic_english_decision(
                {"verdict": "review"},
                declared_language=language,
                expected_language=language,
                names=[item_title, name],
            )
            if language_claim.get("basis") == "filename_language":
                continue
        stem = Path(name).stem
        title = " ".join(stem.replace("_", " ").split())
        if len(title) < 8 or not re.search(r"[a-zA-Z]", title):
            title = f"{item_title} {title}".strip()
        volume, chapter = release_number_hints(title)
        info_hash = pseudo_hash(identifier, name)
        releases.append(
            {
                # The 40-hex file key doubles as the id: safe in a URL and a
                # form, unlike "identifier/File Name.cbz".
                "id": info_hash,
                "provider": PROVIDER_NAME,
                "protocol": PROTOCOL,
                "provider_label": PROVIDER_LABEL,
                "indexer": PROVIDER_LABEL,
                "indexer_id": 0,
                "indexer_priority": INDEXER_PRIORITY,
                "title": title[:500],
                "language": language,
                "category_id": "7000",
                "category_ids": [7000],
                "category": f"Internet Archive · {item_title}"[:500],
                "size": _format_size(size),
                "size_bytes": size,
                "seeders": 0,
                "leechers": 0,
                "downloads": downloads,
                "comments": 0,
                "trusted": False,
                "remake": False,
                "info_hash": info_hash,
                "publish_at": published,
                "source_url": f"{DETAILS_URL}/{quote(identifier)}",
                "download_ref": f"{DOWNLOAD_URL}/{quote(identifier)}/{quote(name)}",
                "archive_identifier": identifier,
                "archive_file": name,
                "volume": volume,
                "chapter": chapter,
                "match_score": release_match_score(query, title),
                "archive_item": item_title[:200],
                "archive_format": BOOK_FORMATS[fmt],
            }
        )
    return releases


def _format_size(value: int) -> str:
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} GiB"


class InternetArchiveError(RuntimeError):
    pass


class InternetArchiveClient:
    name = PROVIDER_NAME
    label = PROVIDER_LABEL

    def __init__(self, settings: Settings):
        self.settings = settings
        self.api = ProviderHTTP(
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            timeout_seconds=max(30.0, float(settings.request_timeout_seconds)),
            requests_per_second=1.0,
        )

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.settings, "internet_archive_enabled", False))

    @property
    def configured(self) -> bool:
        return True

    async def search(
        self, title: str, language: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Every importable book file the archive holds under this title."""

        attempts = search_titles(title)
        if not attempts:
            return []
        query = attempts[0]
        docs: list[dict[str, Any]] = []
        for attempt in attempts:
            response = await self.api.request(
                "GET",
                SEARCH_URL,
                params={
                    "q": search_query(attempt),
                    "fl[]": [
                        "identifier",
                        "title",
                        "item_size",
                        "downloads",
                        "publicdate",
                    ],
                    "rows": str(MAX_ITEMS),
                    "sort[]": "downloads desc",
                    "output": "json",
                },
            )
            payload = response.json()
            docs = ((payload or {}).get("response") or {}).get("docs") or []
            query = attempt
            releases = await self._books_in(docs, query, language, limit)
            if releases:
                if attempt != attempts[0]:
                    logger.info(
                        "Internet Archive: %r offered no book, %r did",
                        attempts[0],
                        attempt,
                    )
                return releases[:limit]
        return []

    async def _books_in(
        self,
        docs: list[dict[str, Any]],
        query: str,
        language: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        """The importable books held by these items, read one item at a time."""

        releases: list[dict[str, Any]] = []
        for item in docs[:MAX_ITEMS]:
            identifier = str(item.get("identifier") or "").strip()
            if not identifier:
                continue
            try:
                meta = await self.api.request(
                    "GET", f"{METADATA_URL}/{quote(identifier)}"
                )
                metadata = meta.json() or {}
                if language == "en" and not _archive_language_allows_english(
                    (metadata.get("metadata") or {}).get("language")
                ):
                    continue
                files = metadata.get("files") or []
            except Exception as exc:  # noqa: BLE001 - one item must not hide others
                logger.info("Internet Archive item %s unreadable: %s", identifier, exc)
                continue
            releases.extend(
                normalize_item_files(item, files, query=query, language=language)
            )
            if len(releases) >= limit:
                break
        return releases[:limit]

    async def download(
        self,
        url: str,
        destination: Path,
        *,
        expected_size: int = 0,
        progress=None,
    ) -> Path:
        """Fetch one file to ``destination``, resuming a partial download."""

        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = destination.with_suffix(destination.suffix + ".part")
        offset = partial.stat().st_size if partial.exists() else 0
        headers = {"User-Agent": USER_AGENT}
        if offset:
            headers["Range"] = f"bytes={offset}-"
        timeout = httpx.Timeout(60.0, read=120.0)
        async with httpx.AsyncClient(
            headers=headers, timeout=timeout, follow_redirects=True
        ) as client:
            async with client.stream("GET", url) as response:
                if response.status_code == 416 and offset:
                    # Everything was already fetched; fall through to finish.
                    pass
                elif response.status_code not in (200, 206):
                    raise InternetArchiveError(
                        f"archive.org answered HTTP {response.status_code}"
                    )
                else:
                    if response.status_code == 200 and offset:
                        offset = 0  # the server ignored the range: start over
                    total = expected_size
                    length = response.headers.get("Content-Length")
                    if length and length.isdigit():
                        total = offset + int(length)
                    mode = "ab" if offset else "wb"
                    written = offset
                    last = time.monotonic()
                    with open(partial, mode) as handle:
                        async for chunk in response.aiter_bytes(DOWNLOAD_CHUNK_BYTES):
                            handle.write(chunk)
                            written += len(chunk)
                            now = time.monotonic()
                            if progress is not None and (
                                now - last >= PROGRESS_INTERVAL_SECONDS
                            ):
                                last = now
                                await progress(written, total)
        final_size = partial.stat().st_size
        if expected_size and final_size != expected_size:
            raise InternetArchiveError(
                f"downloaded {final_size} bytes, archive.org lists {expected_size}"
            )
        partial.replace(destination)
        if progress is not None:
            await progress(final_size, final_size)
        return destination

    async def probe(self, title: str = PROBE_TITLE) -> dict[str, Any]:
        """Prove the archive answers Tankarr: one real search, timed."""

        started = time.monotonic()
        try:
            releases = await self.search(title, "en", limit=20)
        except Exception as exc:  # noqa: BLE001 - the failure is the result
            return {
                "ok": False,
                "enabled": self.enabled,
                "error": f"{type(exc).__name__}: {exc}"[:300],
                "latency_ms": round((time.monotonic() - started) * 1000),
                "user_agent": USER_AGENT,
            }
        items = sorted(
            {
                str(r.get("archive_item") or "")
                for r in releases
                if r.get("archive_item")
            }
        )
        return {
            "ok": True,
            "enabled": self.enabled,
            "latency_ms": round((time.monotonic() - started) * 1000),
            "probe_title": title,
            "items": len(items),
            "releases": len(releases),
            "sample": [str(r.get("title") or "") for r in releases[:5]],
            "user_agent": USER_AGENT,
        }

    async def aclose(self) -> None:
        await self.api.aclose()


__all__ = [
    "InternetArchiveClient",
    "InternetArchiveError",
    "PROTOCOL",
    "PROVIDER_NAME",
    "normalize_item_files",
    "pseudo_hash",
    "search_query",
]
