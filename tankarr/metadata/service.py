from __future__ import annotations

import asyncio
import ctypes
import gc
import hashlib
import json
import math
import os
import re
import tempfile
import warnings
import zipfile
from collections.abc import Awaitable, Callable
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from difflib import SequenceMatcher
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlparse

import httpx
from PIL import Image, ImageOps, ImageStat, UnidentifiedImageError

from tankarr import USER_AGENT
from tankarr.archive import IMAGE_SUFFIXES
from tankarr.artwork_urls import series_artwork_url, volume_artwork_url
from tankarr.catalogue_consensus import AGREED, corroborate
from tankarr.chapter_map import entries_from_releases
from tankarr.completeness import assess_chapter_completeness
from tankarr.config import Settings
from tankarr.database import Database, utc_now
from tankarr.komga import KomgaClient
from tankarr.metadata import MetadataSource
from tankarr.metadata.base import (
    SeriesMatchAssessment,
    assess_series_match,
    canonical_person_name,
    creator_name_similarity,
    normalized_person,
    normalized_title,
    work_title_variants,
)
from tankarr.metadata.correlations import (
    CORRELATION_LABELS,
    EDITABLE_CORRELATION_SOURCES,
    correlation_url,
    normalize_correlation,
)
from tankarr.metadata.publications import (
    publication_kind,
    publication_metadata_key,
    publication_number,
)
from tankarr.providers.base import Provider, ProviderHTTP
from tankarr.series_summary import automatic_updates_paused

AUTO_MATCH_THRESHOLD = 0.86
AUTO_MATCH_MIN_MARGIN = 0.06
MATCH_POLICY_VERSION = 16
MATCH_POLICY_SETTING_KEY = "_metadata_match_policy_version"
KOMGA_SYNC_POLICY_SETTING_KEY = "_komga_sync_policy_version"
KOMGA_SYNC_POLICY_VERSION = 1
TITLE_SELECTION_VERSION = 5
IDENTITY_QUERY_MAX_CHARS = 64
MAX_ARTWORK_BYTES = 20 * 1024 * 1024
MAX_LOCAL_ARTWORK_BYTES = 64 * 1024 * 1024
# Komga's default multipart ceiling is roughly 1 MiB. Leave enough room for
# headers and boundary framing instead of relying on an exact server setting.
MAX_STORED_ARTWORK_BYTES = 900_000
MAX_ARTWORK_PIXELS = 30_000_000
MAX_LOCAL_ARTWORK_PIXELS = 80_000_000
MAX_JPEG_SOURCE_PIXELS = 120_000_000
CANONICAL_ARTWORK_DIMENSIONS = (1000, 1500)
CANONICAL_ARTWORK_RATIO = (
    CANONICAL_ARTWORK_DIMENSIONS[0] / CANONICAL_ARTWORK_DIMENSIONS[1]
)
JPEG_DRAFT_DIMENSIONS = (1500, 1500)
ARTWORK_NORMALIZATION_VERSION = 2
ARTWORK_SELECTION_VERSION = 4
READER_ARTWORK_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}

try:
    _MALLOC_TRIM = ctypes.CDLL(None).malloc_trim
    _MALLOC_TRIM.argtypes = [ctypes.c_size_t]
    _MALLOC_TRIM.restype = ctypes.c_int
except AttributeError:  # pragma: no cover - available in the Linux image
    _MALLOC_TRIM = None


def _release_process_memory() -> None:
    """Return native Pillow buffers between series in long bulk refreshes."""

    gc.collect()
    if _MALLOC_TRIM is not None:
        _MALLOC_TRIM(0)


@dataclass(frozen=True)
class SourceMatchOutcome:
    state: str
    record: dict[str, Any] | None
    confidence: float
    reason: str
    candidate: dict[str, Any] | None = None
    runner_up_confidence: float | None = None
    margin: float | None = None
    assessment: SeriesMatchAssessment | None = None


def _json_hash(value: object) -> str:
    serialized = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _unique_strings(values: list[object]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        key = text.casefold()
        if text and key not in seen:
            seen.add(key)
            result.append(text)
    return result


def _manga_identity_title(manga: dict[str, Any]) -> str:
    """Keep automatic identity anchored to the immutable provider title.

    Canonical and manual display titles are durable presentation layers. Feeding
    either back into automatic matching would let one mistaken correlation
    become self-confirming on the next refresh.
    """

    return str(manga.get("source_title") or manga.get("title") or "")


def _unique_identity_values(first: list[Any], second: list[Any]) -> list[Any]:
    """Merge compact provider candidate fields without losing structured data."""

    result: list[Any] = []
    seen: set[str] = set()
    for value in [*first, *second]:
        if value in (None, "", [], {}):
            continue
        key = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        if key not in seen:
            seen.add(key)
            result.append(value)
    return result


_NON_PERSON_CREDITS = frozenset(
    {
        "anthology",
        "various",
        "various artists",
        "va",
        "aa vv",
        "collective",
        "anonymous",
        "unknown",
        "unknown author",
        "author unknown",
        "n a",
        "staff",
    }
)


def _source_failure_status(exc: Exception, *, cached: bool) -> dict[str, Any]:
    """Classify a metadata call failure the operator has to act on.

    A spent daily quota is not a fault of the setup: the source stays
    configured, the next cycle simply asks again, so it must not be reported
    as an error.
    """

    status = getattr(exc, "status_code", None)
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
    if status == 429:
        return {
            "state": "rate_limited",
            "matched": cached,
            "error": None,
            "reason": "Daily quota reached; Tankarr asks again on the next cycle",
        }
    # A catalogue that is down (5xx, refused connection, timeout) is not a
    # fault of the setup either: MyAnimeList regularly refuses Jikan for
    # hours. Say it is unreachable and move on; the next refresh asks again.
    unreachable = (
        (isinstance(status, int) and status >= 500)
        or isinstance(exc, (httpx.TransportError, TimeoutError))
        or type(exc).__name__ in {"ProviderUnavailableError", "ConnectError"}
    )
    if unreachable:
        return {
            "state": "unavailable",
            "matched": cached,
            "error": None,
            "reason": "Not reachable at the last refresh; Tankarr asks again on the next cycle",
        }
    return {
        "state": "cached_error" if cached else "error",
        "matched": cached,
        "error": f"{type(exc).__name__}: metadata request failed",
    }


def _is_person_credit(value: object) -> bool:
    """False for catalogue placeholders that occupy the creator field.

    MangaBaka credits an anthology to "Anthology" and a shared work to
    "Various"; those are publication facts, not people, and must not become
    the series' author or reach Komga/Stump as one.
    """

    normalized = " ".join(normalized_person(value))
    return bool(normalized) and normalized not in _NON_PERSON_CREDITS


def _unique_people(values: list[object]) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        key = normalized_person(text)
        if (
            text
            and key
            and _is_person_credit(text)
            and not any(
                creator_name_similarity(text, existing) >= 0.90 for existing in result
            )
        ):
            result.append(text)
    return result


def _catalogue_scope(record: dict[str, Any]) -> str:
    """Infer scope for snapshots created before scope-aware persistence."""

    source = str(record.get("source") or "").strip().casefold()
    context_scope = (
        str((record.get("publication_context") or {}).get("scope") or "")
        .strip()
        .casefold()
    )
    if source == "google_books" and context_scope == "edition":
        return "edition"
    explicit = str(record.get("catalogue_scope") or "").strip().casefold()
    if explicit:
        return explicit
    return {
        "myanimelist": "work",
        "mangaupdates": "work",
        "mangadex": "work",
        "comicvine": "publication",
        "google_books": "edition",
    }.get(source, "release")


def _preferred_person_names(values: list[object]) -> dict[tuple[str, ...], str]:
    """Choose one display name for conservative romanization aliases."""

    clusters: list[dict[str, Any]] = []
    for value in values:
        raw = " ".join(str(value or "").split())
        display = canonical_person_name(raw)
        key = normalized_person(raw)
        if not display or not key:
            continue
        reordered = display != raw
        cluster = next(
            (
                item
                for item in clusters
                if creator_name_similarity(raw, item["representative"]) >= 0.90
            ),
            None,
        )
        if cluster is None:
            clusters.append(
                {
                    "representative": raw,
                    "keys": {key},
                    "display": display,
                    "reordered": reordered,
                }
            )
            continue
        cluster["keys"].add(key)
        if reordered and not cluster["reordered"]:
            cluster["display"] = display
            cluster["reordered"] = True

    result: dict[tuple[str, ...], str] = {}
    for cluster in clusters:
        for key in cluster["keys"]:
            result[key] = cluster["display"]
    return result


def _canonical_creator_credits(values: list[dict[str, Any]]) -> list[dict[str, str]]:
    preferred = _preferred_person_names(
        [item.get("name") for item in values if item.get("name")]
    )
    result: list[dict[str, str]] = []
    seen: set[tuple[tuple[str, ...], str]] = set()
    for item in values:
        if not _is_person_credit(item.get("name")):
            continue
        person = normalized_person(item.get("name"))
        role = str(item.get("role") or "writer")
        name = preferred.get(person, canonical_person_name(item.get("name")))
        display_person = normalized_person(name)
        key = (display_person, role)
        if display_person and name and key not in seen:
            seen.add(key)
            result.append({"name": name, "role": role})
    return result


def _merge_external_links(
    *collections: list[dict[str, Any]],
) -> list[dict[str, str]]:
    """Keep unique browser-safe links in deterministic display order."""

    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for collection in collections:
        for link in collection:
            label = str(link.get("label") or "").strip()
            url = str(link.get("url") or "").strip()
            parsed = urlparse(url)
            key = url.rstrip("/")
            if (
                not label
                or parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or key in seen
            ):
                continue
            seen.add(key)
            result.append({"label": label, "url": url})
    return result


def _provider_correlations(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build a persisted provider identity matrix from accepted matches only.

    Search-result URLs are deliberately excluded: a correlation exists only
    when Tankarr has an exact source identifier produced by the origin provider
    or by the metadata matcher's confidence policy.
    """

    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for record in records:
        provider = str(record.get("source") or "").strip().casefold()
        external_id = str(record.get("external_id") or "").strip()
        if (
            not provider
            or provider == "local"
            or not external_id
            or provider in seen
            # A provider-supplied correlation is useful matching evidence, but
            # it is not a verified metadata identity until Tankarr has fetched
            # and accepted the corresponding catalogue record.
            or not str(record.get("title") or "").strip()
        ):
            continue
        exact_links = _merge_external_links(list(record.get("links") or []))
        exact_link = exact_links[0] if exact_links else None
        result.append(
            {
                "provider": provider,
                "label": (
                    str(exact_link["label"])
                    if exact_link
                    else provider.replace("_", " ").title()
                ),
                "external_id": external_id,
                "url": str(exact_link["url"]) if exact_link else None,
            }
        )
        seen.add(provider)
    return result


def _book_external_links(
    canonical: dict[str, Any], volume_data: dict[str, Any]
) -> list[dict[str, str]]:
    return _merge_external_links(
        list(volume_data.get("links") or []), list(canonical.get("links") or [])
    )


def _creator_roles(value: object) -> tuple[str, ...]:
    """Map provider-specific credit labels to Komga/ComicInfo roles."""

    raw = str(value or "writer").strip().casefold()
    normalized = normalized_title(raw)
    roles: list[str] = []
    if any(token in normalized.split() for token in ("author", "story", "writer")):
        roles.append("writer")
    if any(
        token in normalized.split()
        for token in ("art", "artist", "illustrator", "penciller", "penciler")
    ):
        roles.append("penciller")
    aliases = {
        "script": "writer",
        "inker": "inker",
        "colorist": "colorist",
        "colourist": "colorist",
        "letterer": "letterer",
        "translator": "translator",
        "editor": "editor",
        "cover": "cover",
    }
    if not roles and normalized in aliases:
        roles.append(aliases[normalized])
    if not roles:
        roles.append(raw or "writer")
    return tuple(dict.fromkeys(roles))


class ArtworkStore:
    SOURCE_HOSTS: dict[str, tuple[str, ...]] = {
        # MangaBaka serves covers from its own CDN or the catalogue it took
        # them from (AniList, MangaUpdates).
        "mangabaka": (
            ".mangabaka.dev",
            ".mangabaka.org",
            ".anilist.co",
            "cdn.mangaupdates.com",
            ".kitsu.app",
            ".kitsu.io",
            "cdn.myanimelist.net",
            ".anime-planet.com",
            "cdn.animenewsnetwork.com",
        ),
        "anilist": (".anilist.co",),
        "mangaupdates": ("cdn.mangaupdates.com",),
        "myanimelist": ("cdn.myanimelist.net", "myanimelist.net"),
        "comicvine": (
            "comicvine.gamespot.com",
            "comicvine1.cbsistatic.com",
            "static.comicvine.com",
        ),
        "google_books": (
            "books.google.com",
            "books.googleusercontent.com",
            ".googleusercontent.com",
        ),
    }

    def __init__(self, settings: Settings):
        self.settings = settings
        self.root = settings.data_dir / "metadata" / "artwork"
        self.http = ProviderHTTP(
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "image/avif,image/webp,image/png,image/jpeg,image/gif",
            },
            timeout_seconds=60,
            requests_per_second=2,
        )

    async def aclose(self) -> None:
        await self.http.aclose()

    async def store_remote(
        self, manga_id: str, key: str, source: str, url: str
    ) -> dict[str, Any]:
        prepared = await self.prepare_remote(source, url)
        return await asyncio.to_thread(self.store_prepared, manga_id, key, prepared)

    async def prepare_remote(self, source: str, url: str) -> dict[str, Any]:
        self._validate_remote_url(source, url)
        response = await self.http.request("GET", url)
        self._validate_remote_url(source, str(response.url))
        # Pillow decoding/resizing is CPU/native work, not asynchronous I/O.
        # A large trusted cover must not stall health, queues and API requests.
        return await asyncio.to_thread(self.prepare_bytes, response.content)

    def store_bytes(
        self,
        manga_id: str,
        key: str,
        content: bytes,
        *,
        trusted_local: bool = False,
    ) -> dict[str, Any]:
        prepared = self.prepare_bytes(content, trusted_local=trusted_local)
        return self.store_prepared(manga_id, key, prepared)

    def prepare_bytes(
        self,
        content: bytes,
        *,
        trusted_local: bool = False,
    ) -> dict[str, Any]:
        byte_limit = MAX_LOCAL_ARTWORK_BYTES if trusted_local else MAX_ARTWORK_BYTES
        pixel_limit = MAX_LOCAL_ARTWORK_PIXELS if trusted_local else MAX_ARTWORK_PIXELS
        if not content or len(content) > byte_limit:
            raise ValueError(
                "Artwork is empty or exceeds the "
                f"{byte_limit // (1024 * 1024)} MiB safety limit"
            )
        return self._prepare_image(content, max_source_pixels=pixel_limit)

    def store_prepared(
        self,
        manga_id: str,
        key: str,
        prepared: dict[str, Any],
    ) -> dict[str, Any]:
        content = bytes(prepared["content"])
        suffix = str(prepared["suffix"])
        media_type = str(prepared["media_type"])
        if (
            not content
            or len(content) > MAX_STORED_ARTWORK_BYTES
            or suffix != ".jpg"
            or media_type != "image/jpeg"
        ):
            raise ValueError("Prepared artwork is not a canonical JPEG")
        directory_name = self._safe_component(manga_id)
        file_name = f"{self._safe_component(key)}{suffix}"
        directory = self.root / directory_name
        directory.mkdir(parents=True, exist_ok=True)
        destination = directory / file_name
        digest = hashlib.sha256(content).hexdigest()
        if (
            not destination.is_file()
            or hashlib.sha256(destination.read_bytes()).hexdigest() != digest
        ):
            temporary_name: str | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    dir=directory,
                    prefix=f".{file_name}.",
                    delete=False,
                ) as handle:
                    temporary_name = handle.name
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
                Path(temporary_name).replace(destination)
            finally:
                if temporary_name:
                    Path(temporary_name).unlink(missing_ok=True)
        stem = destination.stem
        for old_suffix in (*READER_ARTWORK_SUFFIXES, ".gif", ".avif"):
            alternate = destination.with_name(f"{stem}{old_suffix}")
            if alternate != destination:
                alternate.unlink(missing_ok=True)
        relative = destination.relative_to(self.settings.data_dir).as_posix()
        return {
            "path": relative,
            "sha256": digest,
            "media_type": media_type,
            "source_width": int(prepared["source_width"]),
            "source_height": int(prepared["source_height"]),
            "source_bytes": int(prepared["source_bytes"]),
            "normalized_width": CANONICAL_ARTWORK_DIMENSIONS[0],
            "normalized_height": CANONICAL_ARTWORK_DIMENSIONS[1],
            "detail_stddev": float(prepared["detail_stddev"]),
            "normalization_version": ARTWORK_NORMALIZATION_VERSION,
        }

    @staticmethod
    def is_canonical(path: Path) -> bool:
        if (
            path.suffix.casefold() != ".jpg"
            or not path.is_file()
            or path.is_symlink()
            or path.stat().st_size > MAX_STORED_ARTWORK_BYTES
        ):
            return False
        try:
            with Image.open(path) as image:
                return (
                    image.format == "JPEG"
                    and image.mode == "RGB"
                    and image.size == CANONICAL_ARTWORK_DIMENSIONS
                )
        except (Image.DecompressionBombError, UnidentifiedImageError, OSError):
            return False

    @classmethod
    def _validate_remote_url(cls, source: str, url: str) -> None:
        parsed = urlparse(url)
        hostname = (parsed.hostname or "").casefold().rstrip(".")
        allowed = cls.SOURCE_HOSTS.get(source, ())
        matches = any(
            hostname == entry or (entry.startswith(".") and hostname.endswith(entry))
            for entry in allowed
        )
        if parsed.scheme != "https" or not hostname or not matches:
            raise ValueError(f"Untrusted {source} artwork URL")

    @staticmethod
    def _safe_component(value: object) -> str:
        raw = str(value or "")
        cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", raw).strip(".-")[:80]
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:10]
        return f"{cleaned or 'item'}-{digest}"

    @classmethod
    def _prepare_image(
        cls,
        content: bytes,
        *,
        max_source_pixels: int = MAX_ARTWORK_PIXELS,
    ) -> dict[str, Any]:
        """Validate and render every cover as one portable 2:3 RGB JPEG."""

        try:
            # Artwork preparation runs alongside imports. Never disable the
            # process-wide pixel guard even briefly: another decoder could
            # open an untrusted archive page during that window.
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                opened = Image.open(BytesIO(content))
            with opened, ExitStack() as images:
                opened.seek(0)
                source_format = str(opened.format or "").upper()
                if source_format not in {"JPEG", "MPO", "PNG", "GIF", "WEBP", "AVIF"}:
                    raise ValueError("Artwork response is not a supported image")
                orientation = opened.getexif().get(274, 1)
                source_width, source_height = opened.size
                if orientation in {5, 6, 7, 8}:
                    source_width, source_height = source_height, source_width
                source_pixels = source_width * source_height
                if source_pixels > MAX_JPEG_SOURCE_PIXELS or (
                    source_pixels > max_source_pixels
                    and source_format not in {"JPEG", "MPO"}
                ):
                    raise ValueError("Artwork dimensions exceed the safety limit")
                if source_format in {"JPEG", "MPO"}:
                    # A square request lets libjpeg choose DCT downsampling for
                    # both tall covers and ultra-wide two-page scans before the
                    # canonical portrait canvas is rendered.
                    opened.draft("RGB", JPEG_DRAFT_DIMENSIONS)
                image = (
                    images.enter_context(ImageOps.exif_transpose(opened))
                    if orientation not in (None, 1)
                    else opened
                )
                image.load()
                if "A" in image.getbands():
                    background = images.enter_context(
                        Image.new("RGB", image.size, "white")
                    )
                    background.paste(image, mask=image.getchannel("A"))
                    image = background
                elif image.mode != "RGB":
                    image = images.enter_context(image.convert("RGB"))

                # Keep enough detail for the canonical 1000x1500 poster while
                # releasing oversized decoder surfaces before making copies.
                image.thumbnail((3000, 3000), Image.Resampling.LANCZOS)

                sample = images.enter_context(image.copy())
                sample.thumbnail((64, 64), Image.Resampling.LANCZOS)
                grayscale = images.enter_context(sample.convert("L"))
                detail_stddev = float(ImageStat.Stat(grayscale).stddev[0])
                average_image = images.enter_context(
                    image.resize((1, 1), Image.Resampling.BOX)
                )
                average = average_image.getpixel((0, 0))
                background = tuple(
                    max(0, min(255, int(channel))) for channel in average
                )
                normalized = images.enter_context(
                    Image.new("RGB", CANONICAL_ARTWORK_DIMENSIONS, background)
                )
                contained = images.enter_context(
                    ImageOps.contain(
                        image,
                        CANONICAL_ARTWORK_DIMENSIONS,
                        Image.Resampling.LANCZOS,
                    )
                )
                offset = (
                    (CANONICAL_ARTWORK_DIMENSIONS[0] - contained.width) // 2,
                    (CANONICAL_ARTWORK_DIMENSIONS[1] - contained.height) // 2,
                )
                normalized.paste(contained, offset)

                for quality in (90, 86, 82, 78, 74, 68, 60, 52):
                    with BytesIO() as output:
                        normalized.save(
                            output,
                            format="JPEG",
                            quality=quality,
                            optimize=True,
                            progressive=True,
                        )
                        normalized_bytes = output.getvalue()
                    if len(normalized_bytes) <= MAX_STORED_ARTWORK_BYTES:
                        return {
                            "content": normalized_bytes,
                            "suffix": ".jpg",
                            "media_type": "image/jpeg",
                            "source_width": source_width,
                            "source_height": source_height,
                            "source_bytes": len(content),
                            "detail_stddev": detail_stddev,
                        }
        except (
            Image.DecompressionBombError,
            Image.DecompressionBombWarning,
            UnidentifiedImageError,
            OSError,
        ) as exc:
            raise ValueError("Artwork could not be decoded safely") from exc
        raise ValueError("Artwork could not be reduced below the Komga upload limit")


_EXACT_TITLE_RELATIONS = frozenset({"primary_exact", "edition_qualified_exact"})


def _duplicate_work_key(record: dict[str, Any]) -> tuple[int, int, int, int]:
    """Deterministic preference among catalogue records describing one work.

    A local English book corresponds to the record that lists an English
    edition; otherwise the record that knows its volume count (a published
    collection rather than a lone story), then the one with more external
    links. Ties keep the ambiguity.
    """

    publishers = record.get("publishers") or []
    english_edition = any(
        "english" in str((item or {}).get("type") or "").casefold()
        for item in publishers
        if isinstance(item, dict)
    )
    return (
        int(english_edition),
        int(record.get("volume_count") not in (None, 0, "")),
        len(record.get("links") or []),
        len(publishers),
    )


def _resolve_near_tie(
    target: dict[str, Any],
    assessed: list[tuple[SeriesMatchAssessment, dict[str, Any]]],
) -> tuple[SeriesMatchAssessment, dict[str, Any], str] | None:
    """Decide a near tie when the rivals are not equally supported.

    Two situations are not real ambiguity. A catalogue ranks the anthology
    that contains a story just below the story itself: only one candidate
    carries the work's own title, so it wins. And a catalogue often holds the
    same work several times (lone story, collection, reprint) with identical
    title and creators: the record that carries the English edition wins,
    then the one that knows its volume count. Anything else stays ambiguous.
    """

    if len(assessed) < 2:
        return None
    best, best_record = assessed[0]
    if best.title_relation not in _EXACT_TITLE_RELATIONS:
        return None
    if best.creator_similarity < 0.90:
        return None
    target_variants = work_title_variants(target.get("title"))
    if not target_variants:
        return None
    same_work = [(best, best_record)]
    for assessment, record in assessed[1:]:
        if best.ranking_score - assessment.ranking_score >= AUTO_MATCH_MIN_MARGIN:
            break
        if assessment.title_relation not in _EXACT_TITLE_RELATIONS:
            # A differently titled work: weaker identity evidence than an
            # exact title, so it cannot make the best candidate ambiguous.
            continue
        if assessment.creator_similarity < 0.90 or not (
            target_variants & work_title_variants(record.get("title"))
        ):
            return None
        same_work.append((assessment, record))
    if len(same_work) == 1:
        return (
            best,
            best_record,
            "near ties carry a different work title; exact title and creator decide",
        )
    ranked = sorted(
        same_work, key=lambda item: _duplicate_work_key(item[1]), reverse=True
    )
    if _duplicate_work_key(ranked[0][1]) == _duplicate_work_key(ranked[1][1]):
        return None
    return (
        ranked[0][0],
        ranked[0][1],
        "duplicate catalogue records, preferred the edition-bearing record",
    )


class MetadataService:
    """Resolve, cache, merge, and publish series/volume metadata."""

    def __init__(
        self,
        settings: Settings,
        database: Database,
        komga: KomgaClient | None,
        providers: dict[str, Provider],
        sources: list[MetadataSource],
        *,
        library_publisher: Callable[[str], Awaitable[dict[str, Any]]] | None = None,
        origin_refresher: Callable[[str], Awaitable[dict[str, Any]]] | None = None,
        canonical_title_publisher: (
            Callable[[str, str | None], Awaitable[dict[str, Any]]] | None
        ) = None,
    ):
        self.settings = settings
        self.database = database
        self.komga = komga
        self.library_publisher = library_publisher
        self.origin_refresher = origin_refresher
        self.canonical_title_publisher = canonical_title_publisher
        self.providers = providers
        self.sources = sources
        self.artwork = ArtworkStore(settings)
        self.task: asyncio.Task | None = None
        self.bulk_task: asyncio.Task | None = None
        self._bulk_refresh_pending = False
        self._bulk_refresh_pending_force = False
        self._policy_refresh_required = self.database.get_setting_overrides().get(
            MATCH_POLICY_SETTING_KEY
        ) != str(MATCH_POLICY_VERSION)
        self._komga_sync_required = bool(
            self.komga is not None
            and self.database.get_setting_overrides().get(KOMGA_SYNC_POLICY_SETTING_KEY)
            != str(KOMGA_SYNC_POLICY_VERSION)
        )
        self._stop = asyncio.Event()
        self._refresh_lock = asyncio.Lock()
        self._series_locks: dict[str, asyncio.Lock] = {}
        self.current_manga_id: str | None = None
        self.last_cycle_at: str | None = None
        self.last_cycle_error: str | None = None
        self.last_cycle_result: dict[str, Any] | None = None
        self.last_title_migration: dict[str, Any] | None = None

    async def start(self) -> None:
        if self.task is not None:
            return
        self._stop.clear()
        self.task = asyncio.create_task(self._run(), name="tankarr-metadata-monitor")
        if self._policy_refresh_required:
            self.start_bulk_refresh(force=True)

    async def stop(self) -> None:
        self._stop.set()
        for task in (self.task, self.bulk_task):
            if task is not None:
                task.cancel()
        for task in (self.task, self.bulk_task):
            if task is not None:
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self.task = None
        self.bulk_task = None
        self._bulk_refresh_pending = False
        self._bulk_refresh_pending_force = False
        await self.artwork.aclose()
        for source in self.sources:
            await source.aclose()

    def set_komga(self, komga: KomgaClient | None) -> None:
        """Rebind the managed reader after a Settings change."""

        self.komga = komga
        self._komga_sync_required = bool(
            komga is not None
            and self.database.get_setting_overrides().get(KOMGA_SYNC_POLICY_SETTING_KEY)
            != str(KOMGA_SYNC_POLICY_VERSION)
        )

    def komga_sync_pending(self) -> bool:
        if self.komga is None:
            return False
        coverage = self.database.metadata_overview()
        if self._komga_sync_required or coverage["sync_errors"]:
            return True
        for manga in self.database.list_manga():
            metadata = self.database.get_series_metadata(str(manga["id"]))
            if metadata is None or metadata.get("last_synced_at"):
                continue
            if any(
                chapter.get("downloaded") and chapter.get("library_path")
                for chapter in self.database.list_all_chapters(str(manga["id"]))
            ):
                return True
        return False

    async def _run(self) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=30)
            return
        except TimeoutError:
            pass
        while True:
            await self.run_cycle(force=False)
            try:
                # Missing/new series are discovered quickly; already enriched
                # entries remain cached until their configured stale deadline.
                await asyncio.wait_for(self._stop.wait(), timeout=300)
                return
            except TimeoutError:
                continue

    def start_bulk_refresh(self, *, force: bool = True) -> dict[str, Any]:
        if not self.settings.metadata_enabled:
            return {"started": False, "reason": "Metadata enrichment is disabled"}
        if self.bulk_task is not None and not self.bulk_task.done():
            self._bulk_refresh_pending = True
            self._bulk_refresh_pending_force |= force
            return {
                "started": False,
                "queued": True,
                "force": self._bulk_refresh_pending_force,
                "reason": "A follow-up metadata refresh was queued",
            }
        self.bulk_task = asyncio.create_task(
            self._run_bulk_refresh_loop(force=force),
            name="tankarr-metadata-bulk-refresh",
        )
        return {"started": True, "queued": False, "force": force}

    async def _run_bulk_refresh_loop(self, *, force: bool) -> None:
        """Coalesce refresh requests while guaranteeing credential rotation.

        A settings save can happen while a weekly refresh is in progress. The
        source clients read credentials dynamically, but titles already checked
        by that run must still be revisited with the new credential.
        """

        next_force = force
        while True:
            self._bulk_refresh_pending = False
            self._bulk_refresh_pending_force = False
            backfill_errors: list[dict[str, str]] = []
            if next_force and self._policy_refresh_required:
                backfill_errors = await self._backfill_provider_correlations()
            cycle_result = await self.run_cycle(force=next_force)
            cycle_errors = list(cycle_result.get("errors") or [])
            if backfill_errors:
                cycle_result["provider_backfill_errors"] = backfill_errors
                cycle_result["errors"] = [*backfill_errors, *cycle_errors]
                self.last_cycle_result = cycle_result
                self.last_cycle_error = backfill_errors[0]["error"]
            if (
                next_force
                and self._policy_refresh_required
                and not backfill_errors
                and not cycle_errors
            ):
                self.database.save_setting(
                    MATCH_POLICY_SETTING_KEY, str(MATCH_POLICY_VERSION)
                )
                self._policy_refresh_required = False
            if not self._bulk_refresh_pending:
                return
            next_force = self._bulk_refresh_pending_force

    async def _backfill_provider_correlations(self) -> list[dict[str, str]]:
        """Refresh legacy MangaDex rows before applying a new match policy."""

        if self.origin_refresher is None:
            return []
        errors: list[dict[str, str]] = []
        for manga in self.database.list_manga():
            if automatic_updates_paused(manga):
                continue
            if manga.get("provider") != "mangadex":
                continue
            try:
                await self.origin_refresher(manga["id"])
            except Exception as exc:  # noqa: BLE001 - retry next policy startup
                errors.append(
                    {
                        "manga_id": str(manga["id"]),
                        "error": (
                            f"MangaDex identity refresh failed ({type(exc).__name__})"
                        ),
                    }
                )
        return errors

    async def run_cycle(self, *, force: bool = False) -> dict[str, Any]:
        if not self.settings.metadata_enabled:
            return {"enabled": False, "checked": 0, "errors": []}
        async with self._refresh_lock:
            stale_before = (
                datetime.now(UTC)
                - timedelta(hours=self.settings.metadata_refresh_interval_hours)
            ).isoformat()
            manga_list = (
                self.database.list_manga()
                if force
                else self.database.list_manga_needing_metadata(stale_before)
            )
            checked = 0
            synced = 0
            errors: list[dict[str, str]] = []
            for manga in manga_list:
                if not self.settings.metadata_enabled:
                    break
                if not force and automatic_updates_paused(manga):
                    continue
                self.current_manga_id = manga["id"]
                try:
                    result = await self.enrich_series(manga["id"], force=force)
                    publication = result.get("library") or result.get("komga", {})
                    for source_status in result.get("metadata", {}).get(
                        "source_status", []
                    ):
                        if source_status.get("state") not in {
                            "error",
                            "exact_error",
                            "cached_error",
                        }:
                            continue
                        errors.append(
                            {
                                "manga_id": manga["id"],
                                "error": (
                                    f"{source_status.get('label') or source_status.get('name')}: "
                                    f"{source_status.get('error') or source_status.get('reason') or 'metadata request failed'}"
                                )[:1000],
                            }
                        )
                    synced += int(
                        bool(
                            publication.get("synced")
                            or (
                                publication.get("reader_independent")
                                and not publication.get("errors")
                            )
                        )
                    )
                    if publication.get("errors"):
                        errors.append(
                            {
                                "manga_id": manga["id"],
                                "error": "Library metadata: "
                                + str(publication["errors"][0].get("error") or "")[
                                    :900
                                ],
                            }
                        )
                except Exception as exc:  # noqa: BLE001 - one title must not stop a cycle
                    message = f"{type(exc).__name__}: {exc}"[:1000]
                    errors.append({"manga_id": manga["id"], "error": message})
                finally:
                    _release_process_memory()
                checked += 1
            self.current_manga_id = None
            result = {
                "enabled": True,
                "force": force,
                "checked": checked,
                "synced": synced,
                "errors": errors,
            }
            # A queued automatic no-op must not erase the useful result of a
            # force refresh that completed immediately before it.
            if checked or self.last_cycle_result is None:
                self.last_cycle_at = utc_now()
                self.last_cycle_error = errors[0]["error"] if errors else None
                self.last_cycle_result = result
            return result

    async def preview_chapter_completeness(
        self,
        manga: dict[str, Any],
        chapters: list[dict[str, Any]],
        language: str,
        available_chapter_count: int,
    ) -> dict[str, Any]:
        """Verify a pre-add feed without persisting an unapproved series.

        Preview matching deliberately uses the same identity policy as normal
        enrichment. Each applicable catalogue is bounded independently so an
        offline metadata service cannot make the Add dialog unusable.
        """

        manga = {**manga, "preferred_language": language}
        if not self.settings.metadata_enabled:
            assessment = assess_chapter_completeness(
                manga,
                chapters,
                language=language,
                available_chapter_count=available_chapter_count,
                source_checks=[],
            )
            assessment["message"] = (
                "Metadata verification is disabled in Settings, so Tankarr cannot "
                "confirm whether this translation is complete."
            )
            return assessment

        origin_records = [self._origin_record(manga)]
        applicable_sources = [
            source
            for source in self.sources
            if source.supports_series
            and source.automatic_matching
            and self._source_applicability(manga, origin_records, source)[0]
        ]
        timeout_seconds = min(
            20.0, max(5.0, float(self.settings.request_timeout_seconds))
        )

        async def inspect(
            source: MetadataSource, evidence_records: list[dict[str, Any]]
        ) -> dict[str, Any]:
            if not source.configured:
                return {
                    "name": source.name,
                    "label": source.label,
                    "state": "unavailable",
                    "confidence": None,
                    "reason": source.unavailable_reason or "Not configured",
                    "record": None,
                }
            try:
                async with asyncio.timeout(timeout_seconds):
                    outcome = await self._match_source(manga, source, evidence_records)
                return {
                    "name": source.name,
                    "label": source.label,
                    "state": outcome.state,
                    "confidence": outcome.confidence,
                    "reason": outcome.reason,
                    "record": outcome.record,
                }
            except TimeoutError:
                return {
                    "name": source.name,
                    "label": source.label,
                    "state": "error",
                    "confidence": None,
                    "reason": (
                        f"Completeness check exceeded {timeout_seconds:g} seconds"
                    ),
                    "record": None,
                }
            except Exception as exc:  # noqa: BLE001 - other sources remain useful
                return {
                    "name": source.name,
                    "label": source.label,
                    "state": "error",
                    "confidence": None,
                    # HTTP client exceptions can include credential-bearing query
                    # strings (notably Comic Vine). Keep preview diagnostics useful
                    # without reflecting provider URLs or secrets into the browser.
                    "reason": f"{type(exc).__name__}: metadata request failed",
                    "record": None,
                }

        source_checks: list[dict[str, Any]] = []
        evidence_records = list(origin_records)
        # Preserve the normal enrichment order. MangaUpdates often contributes
        # exact alternate titles that make a later MAL correlation provable.
        for source in applicable_sources:
            check = await inspect(source, evidence_records)
            source_checks.append(check)
            if check.get("record"):
                evidence_records.append(check["record"])
        return assess_chapter_completeness(
            manga,
            chapters,
            language=language,
            available_chapter_count=available_chapter_count,
            source_checks=source_checks,
        )

    @staticmethod
    def _correlation_stub(
        manga: dict[str, Any], correlation: dict[str, Any]
    ) -> dict[str, Any]:
        """Create link-only identity evidence without inventing catalogue data."""

        source = str(correlation.get("source") or "").casefold()
        return {
            "source": source,
            "catalogue_scope": "publication" if source == "comicvine" else "work",
            "external_id": str(correlation.get("external_id") or ""),
            "title": "",
            "hit_title": "",
            "alternate_titles": [],
            "description": "",
            "authors": [],
            "creators": [],
            "genres": [],
            "tags": ["Comic"] if source == "comicvine" else [],
            "publisher": None,
            "year": None,
            "status": None,
            "work_type": "Comic" if source == "comicvine" else None,
            "original_language": None,
            "volume_count": None,
            "chapter_count": None,
            "rating": None,
            "cover": None,
            "links": _merge_external_links(
                [
                    {
                        "label": correlation.get("label")
                        or CORRELATION_LABELS.get(source)
                        or source.replace("_", " ").title(),
                        "url": correlation.get("url"),
                    }
                ]
            ),
            "match_origin": str(correlation.get("origin") or "provider"),
            "raw": {
                "manga_id": manga["id"],
                "external_id": correlation.get("external_id"),
            },
        }

    def _origin_correlations(self, manga: dict[str, Any]) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for item in self.database.list_provider_metadata_correlations(manga["id"]):
            source = str(item.get("source") or "").strip().casefold()
            external_id = str(item.get("external_id") or "").strip()
            links = _merge_external_links(
                [
                    {
                        "label": item.get("label") or CORRELATION_LABELS.get(source),
                        "url": item.get("url"),
                    }
                ]
            )
            if not source or not external_id or not links:
                continue
            result[source] = {
                "source": source,
                "label": links[0]["label"],
                "external_id": external_id,
                "url": links[0]["url"],
                "origin": "mangadex",
            }
        return result

    async def _refresh_chapter_map(
        self, manga_id: str, records: list[dict[str, Any]]
    ) -> None:
        """Persist the chapter↔volume map from MangaUpdates' release feed.

        Structure is a separate concern from identity: it is fetched by the
        already-verified MangaUpdates ID and never influences matching.
        """

        record = next(
            (item for item in records if item.get("source") == "mangaupdates"), None
        )
        source = next((s for s in self.sources if s.name == "mangaupdates"), None)
        external_id = str((record or {}).get("external_id") or "").strip()
        if record is None or source is None or not external_id:
            return
        list_releases = getattr(source, "list_releases", None)
        if list_releases is None:
            return
        try:
            releases = await list_releases(external_id)
        except Exception:  # noqa: BLE001 - structure refresh must not fail identity
            return
        entries = entries_from_releases(releases, source="mangaupdates")
        if entries:
            self.database.replace_chapter_map(manga_id, "mangaupdates", entries)
        # The dated feed is also the work's publishing rhythm for the calendar.
        self.database.replace_release_history(manga_id, "mangaupdates", releases)

    @staticmethod
    def _correlation_supplier(correlation: dict[str, Any]) -> str:
        supplier = str(correlation.get("supplied_by") or "").strip()
        if supplier:
            return CORRELATION_LABELS.get(supplier) or supplier.title()
        return "MangaDex" if correlation.get("origin") == "mangadex" else "catalogue"

    @staticmethod
    def _adopt_catalogue_identities(
        exact_correlations: dict[str, dict[str, Any]], record: dict[str, Any]
    ) -> list[str]:
        """Let one verified catalogue record pin the others by exact ID.

        This is the SkyHook pattern: the title search happens once, and every
        catalogue the record already identifies is fetched by ID afterwards,
        so ambiguity never multiplies across sources. Manual and origin
        correlations keep precedence over adopted ones.
        """

        adopted: list[str] = []
        supplier = str(record.get("source") or "")
        for raw_source, raw_id in (record.get("external_ids") or {}).items():
            source = str(raw_source).strip().casefold()
            external_id = str(raw_id or "").strip()
            if not source or not external_id or source in exact_correlations:
                continue
            url = correlation_url(source, external_id)
            if not url:
                continue
            exact_correlations[source] = {
                "source": source,
                "label": CORRELATION_LABELS.get(source) or source.title(),
                "external_id": external_id,
                "url": url,
                "origin": "catalogue",
                "supplied_by": supplier,
            }
            adopted.append(source)
        return adopted

    def _effective_correlations(
        self, manga: dict[str, Any]
    ) -> dict[str, dict[str, Any]]:
        result = self._origin_correlations(manga)
        for item in self.database.list_manual_metadata_correlations(manga["id"]):
            source = str(item["source"])
            result[source] = {
                "source": source,
                "label": CORRELATION_LABELS.get(source)
                or source.replace("_", " ").title(),
                "external_id": str(item["external_id"]),
                "url": str(item["url"]),
                "origin": "manual",
            }
        return result

    def correlations_for(self, manga_id: str) -> dict[str, Any]:
        """Return editable overrides and their deterministic fallback identities."""

        manga = self.database.get_manga(manga_id)
        origin = self._origin_correlations(manga)
        manual = {
            str(item["source"]): item
            for item in self.database.list_manual_metadata_correlations(manga_id)
        }
        records = {
            str(item["source"]): item
            for item in self.database.list_metadata_source_records(
                manga_id, entity_key=""
            )
            if item["entity_type"] in {"series", "work", "edition"}
            and item["source"] != manga.get("provider")
        }
        sources_by_name = {source.name: source for source in self.sources}
        ordered_names = [item.name for item in EDITABLE_CORRELATION_SOURCES]
        ordered_names.extend(
            sorted(name for name in origin if name not in set(ordered_names))
        )
        output: list[dict[str, Any]] = []
        for source_name in ordered_names:
            source = sources_by_name.get(source_name)
            pinned = manual.get(source_name)
            provider = origin.get(source_name)
            record = records.get(source_name)
            record_data = (record or {}).get("data", {})
            record_links = _merge_external_links(list(record_data.get("links") or []))
            automatic = (
                {
                    "external_id": str(record["external_id"]),
                    "url": record_links[0]["url"] if record_links else None,
                }
                if record
                and record_data.get("match_origin") not in {"manual", "mangadex"}
                else None
            )
            effective = (
                (
                    {
                        "external_id": str(pinned["external_id"]),
                        "url": str(pinned["url"]),
                        "origin": "manual",
                    }
                    if pinned
                    else None
                )
                or (
                    {
                        "external_id": str(provider["external_id"]),
                        "url": str(provider["url"]),
                        "origin": "mangadex",
                    }
                    if provider
                    else None
                )
                or ({**automatic, "origin": "automatic"} if automatic else None)
            )
            output.append(
                {
                    "source": source_name,
                    "label": CORRELATION_LABELS.get(source_name)
                    or (source.label if source else source_name.title()),
                    "editable": source_name
                    in {item.name for item in EDITABLE_CORRELATION_SOURCES},
                    "configured": bool(source and source.configured),
                    "allows_link_only": bool(
                        source and source.allows_link_only_correlation
                    ),
                    "supports_enrichment": bool(source and source.supports_series),
                    "manual_url": str(pinned["url"]) if pinned else None,
                    "provider_url": str(provider["url"]) if provider else None,
                    "automatic_url": automatic.get("url") if automatic else None,
                    "effective_url": effective.get("url") if effective else None,
                    "external_id": effective.get("external_id") if effective else None,
                    "origin": effective.get("origin") if effective else None,
                    "matched_title": str(record_data.get("title") or "") or None,
                    "unavailable_reason": source.unavailable_reason if source else None,
                }
            )
        return {"manga_id": manga_id, "sources": output}

    async def update_manual_correlations(
        self, manga_id: str, values: dict[str, str | None]
    ) -> dict[str, Any]:
        """Validate exact IDs, atomically pin them, then rebuild canonical metadata."""

        self.database.get_manga(manga_id)
        editable = {item.name for item in EDITABLE_CORRELATION_SOURCES}
        if not values or len(values) > len(editable):
            raise ValueError("Submit at least one supported metadata source")
        unknown = sorted(set(values) - editable)
        if unknown:
            raise ValueError(f"Unsupported metadata source: {', '.join(unknown)}")
        source_by_name = {source.name: source for source in self.sources}
        normalized: dict[str, dict[str, str] | None] = {}
        fetched: dict[str, dict[str, Any]] = {}
        for source_name, value in values.items():
            if value is None or not value.strip():
                normalized[source_name] = None
                continue
            correlation = normalize_correlation(source_name, value)
            source = source_by_name.get(source_name)
            if source is None or not source.supports_series:
                raise ValueError(f"{correlation['label']} enrichment is not supported")
            if not source.configured and not source.allows_link_only_correlation:
                raise ValueError(
                    source.unavailable_reason
                    or f"Configure {correlation['label']} in Settings first"
                )
            if not source.configured:
                normalized[source_name] = correlation
                continue
            try:
                record = await source.get_series(correlation["external_id"])
            except Exception as exc:  # noqa: BLE001 - never reflect credential URLs
                if source.allows_link_only_correlation:
                    normalized[source_name] = correlation
                    continue
                raise ValueError(
                    f"{correlation['label']} could not load that exact ID "
                    f"({type(exc).__name__})"
                ) from exc
            if str(record.get("external_id") or "") != correlation["external_id"]:
                raise ValueError(
                    f"{correlation['label']} returned a different series identity"
                )
            exact_links = _merge_external_links(list(record.get("links") or []))
            if exact_links:
                correlation["url"] = exact_links[0]["url"]
            record["match_policy_version"] = MATCH_POLICY_VERSION
            record["match_origin"] = "manual"
            normalized[source_name] = correlation
            fetched[source_name] = record

        async with self._series_lock(manga_id):
            current_records = {
                str(item["source"]): item
                for item in self.database.list_metadata_source_records(
                    manga_id, entity_key=""
                )
            }
            for source_name, correlation in normalized.items():
                current = current_records.get(source_name)
                if (
                    correlation is None
                    and current
                    and current["data"].get("match_origin") == "manual"
                ):
                    self.database.delete_metadata_source_records(
                        manga_id, source=source_name, identity_only=True
                    )
                elif (
                    correlation is not None
                    and current
                    and str(current.get("external_id") or "")
                    != correlation["external_id"]
                ):
                    self.database.delete_metadata_source_records(
                        manga_id, source=source_name, identity_only=True
                    )
            self.database.replace_manual_metadata_correlations(manga_id, normalized)
            for record in fetched.values():
                self._persist_source_match(
                    manga_id,
                    record,
                    1.0,
                    "Exact metadata identity selected manually",
                )
            refresh = await self._enrich_series(manga_id, force=False)
        return {"correlations": self.correlations_for(manga_id), "refresh": refresh}

    def _series_lock(self, manga_id: str) -> asyncio.Lock:
        return self._series_locks.setdefault(manga_id, asyncio.Lock())

    async def enrich_series(
        self, manga_id: str, *, force: bool = True
    ) -> dict[str, Any]:
        async with self._series_lock(manga_id):
            return await self._enrich_series(manga_id, force=force)

    async def apply_cached_titles(self) -> dict[str, Any]:
        """Adopt verified titles already present in canonical metadata.

        This makes the schema migration useful immediately without forcing a
        network-heavy metadata refresh. The normal enrichment path keeps the
        same title layer current afterwards.
        """

        checked = 0
        updated = 0
        errors: list[dict[str, str]] = []
        for manga in self.database.list_manga():
            metadata = self.database.get_series_metadata(str(manga["id"]))
            if metadata is None:
                continue
            canonical = dict(metadata.get("data") or {})
            if canonical.get("title_selection_version") != TITLE_SELECTION_VERSION:
                manual_sources = {
                    str(item["source"])
                    for item in self.database.list_manual_metadata_correlations(
                        str(manga["id"])
                    )
                }
                exact_records = [
                    item["data"]
                    for item in self.database.list_metadata_source_records(
                        str(manga["id"]), entity_key=""
                    )
                    if item["source"] in manual_sources
                    and item["entity_type"] in {"series", "work", "edition"}
                    and item["data"].get("title")
                ]
                title_record = max(
                    exact_records,
                    key=lambda item: self._title_record_priority(
                        item, work_type=canonical.get("work_type")
                    ),
                    default=None,
                )
                if title_record is None:
                    continue
                canonical["title"] = str(title_record["title"])
                canonical["work"] = {
                    **dict(canonical.get("work") or {}),
                    "title": str(title_record["title"]),
                }
                canonical["provenance"] = {
                    **dict(canonical.get("provenance") or {}),
                    "title": str(title_record["source"]),
                }
                canonical["matched_sources"] = _unique_strings(
                    [
                        *(canonical.get("matched_sources") or []),
                        title_record["source"],
                    ]
                )
            try:
                result = await self._apply_canonical_title(str(manga["id"]), canonical)
                checked += 1
                changed = bool(result.get("updated"))
                updated += int(changed)
                if changed and self.library_publisher is not None:
                    publication = await self.library_publisher(str(manga["id"]))
                    if publication.get("errors"):
                        raise RuntimeError(
                            str(publication["errors"][0].get("error") or "")
                        )
                if changed and self.komga is not None:
                    reader = await self.sync_to_komga(str(manga["id"]))
                    if reader.get("errors"):
                        raise RuntimeError(str(reader["errors"][0].get("error") or ""))
            except Exception as exc:  # noqa: BLE001 - continue the startup migration
                errors.append(
                    {
                        "manga_id": str(manga["id"]),
                        "error": f"{type(exc).__name__}: {exc}"[:500],
                    }
                )
        return {"checked": checked, "updated": updated, "errors": errors}

    async def _apply_canonical_title(
        self, manga_id: str, canonical: dict[str, Any]
    ) -> dict[str, Any]:
        provenance = canonical.get("provenance") or {}
        title_source = str(provenance.get("title") or "").casefold()
        # A description/creator match must never promote the provider filename
        # to a canonical metadata title. The chosen title itself needs a
        # non-local catalogue provenance.
        verified = bool(title_source and title_source != "local")
        work = canonical.get("work") or {}
        candidate = " ".join(
            str(work.get("title") or canonical.get("title") or "").split()
        )
        if len(candidate) > 200:
            raise ValueError("Canonical metadata title exceeds 200 characters")
        target = candidate if verified and candidate else None
        before = self.database.get_manga(manga_id)
        if self.canonical_title_publisher is not None:
            result = await self.canonical_title_publisher(manga_id, target)
        else:
            after = self.database.set_manga_metadata_title(manga_id, target)
            result = {
                "updated": before.get("metadata_title") != after.get("metadata_title"),
                "previous_title": before["title"],
                "title": after["title"],
                "manga": after,
            }
        current = self.database.get_manga(manga_id)
        canonical["display_title"] = current["title"]
        if isinstance(canonical.get("work"), dict):
            canonical["work"]["display_title"] = current["title"]
        return result

    async def _enrich_series(
        self, manga_id: str, *, force: bool = True
    ) -> dict[str, Any]:
        manga = self.database.get_manga(manga_id)
        records = [self._origin_record(manga)]
        exact_correlations = self._effective_correlations(manga)
        series_source_names = {
            source.name for source in self.sources if source.supports_series
        }
        records.extend(
            self._correlation_stub(manga, correlation)
            for source_name, correlation in exact_correlations.items()
            if source_name not in series_source_names
        )
        stored_records = [
            record
            for record in self.database.list_metadata_source_records(
                manga_id, entity_key=""
            )
            if record["entity_type"] in {"series", "work", "edition"}
            and record["source"] != manga.get("provider")
        ]
        stale_sources = {
            str(record["source"])
            for record in stored_records
            # A forced refresh ("Refresh Metadata") re-reads every catalogue;
            # otherwise only records from an older matching policy are redone.
            if force
            or record["data"].get("match_policy_version") != MATCH_POLICY_VERSION
        }
        for source_name in stale_sources:
            self.database.delete_metadata_source_records(
                manga_id,
                source=source_name,
                identity_only=True,
            )
        cached_records = sorted(
            (
                record
                for record in stored_records
                if record["data"].get("match_policy_version") == MATCH_POLICY_VERSION
            ),
            key=lambda item: str(item.get("fetched_at") or ""),
        )
        cached_by_source = {record["source"]: record for record in cached_records}
        source_status: list[dict[str, Any]] = []
        self._persist_source_match(manga_id, records[0], 1.0, "Tankarr source record")

        source_order = {
            # MangaBaka is the identity index: its record pins every other
            # catalogue by exact ID before any of them is searched by title.
            "mangabaka": 5,
            "mangaupdates": 10,
            "anilist": 15,
            "myanimelist": 20,
            "comicvine": 30,
            # Google Books is intentionally last: for manga it is an edition
            # fallback only after both specialist work catalogues had a chance
            # to establish the identity.
            "google_books": 40,
        }
        ordered_sources = sorted(
            enumerate(self.sources),
            key=lambda item: (
                0 if item[1].name in exact_correlations else 1,
                source_order.get(item[1].name, 100),
                item[0],
            ),
        )
        attempted: set[str] = set()
        for _, source in ordered_sources:
            status = source.status()
            exact = exact_correlations.get(source.name)
            applicable, applicability_reason = self._source_applicability(
                manga, records, source, attempted
            )
            # A user-selected exact identity is stronger than the automatic
            # content-family router. The fetched record will then supply the
            # classification evidence used by subsequent catalogues.
            if exact and exact.get("origin") == "manual":
                applicable, applicability_reason = True, ""
            if not applicable:
                # Routing decisions are part of the match policy. Remove an
                # older cross-catalogue identity instead of letting it survive
                # after a title has been classified more precisely.
                self.database.delete_metadata_source_records(
                    manga_id, source=source.name, identity_only=True
                )
                status.update(
                    {
                        "state": "not_applicable",
                        "matched": False,
                        "reason": applicability_reason,
                    }
                )
                source_status.append(status)
                continue
            if not source.supports_series:
                status["state"] = (
                    "exact_link"
                    if exact
                    else "volume_only"
                    if source.supports_volumes
                    else "unavailable"
                )
                if exact:
                    status.update(
                        {
                            "matched": True,
                            "external_id": exact["external_id"],
                            "correlation_origin": exact["origin"],
                            "reason": "Exact link supplied by "
                            + self._correlation_supplier(exact),
                        }
                    )
                source_status.append(status)
                continue
            if exact:
                cached = cached_by_source.get(source.name)
                cached_matches = bool(
                    cached
                    and str(cached.get("external_id") or "")
                    == str(exact["external_id"])
                )
                if not source.configured:
                    records.append(
                        cached["data"]
                        if cached_matches
                        else self._correlation_stub(manga, exact)
                    )
                    status.update(
                        {
                            "state": "cached" if cached_matches else "exact_link",
                            "matched": True,
                            "external_id": exact["external_id"],
                            "correlation_origin": exact["origin"],
                            "reason": source.unavailable_reason
                            or "Exact identity retained without catalogue access",
                        }
                    )
                    source_status.append(status)
                    continue
                try:
                    if cached_matches and not force:
                        record = dict(cached["data"])
                        state = "cached"
                    else:
                        record = await source.get_series(str(exact["external_id"]))
                        if str(record.get("external_id") or "") != str(
                            exact["external_id"]
                        ):
                            raise ValueError(
                                f"{source.label} returned a different series identity"
                            )
                        record["match_policy_version"] = MATCH_POLICY_VERSION
                        record["match_origin"] = exact["origin"]
                        self._persist_source_match(
                            manga_id,
                            record,
                            1.0,
                            (
                                "Exact metadata identity selected manually"
                                if exact["origin"] == "manual"
                                else "Exact metadata identity supplied by "
                                + self._correlation_supplier(exact)
                            ),
                        )
                        state = "matched"
                    records.append(record)
                    self._adopt_catalogue_identities(exact_correlations, record)
                    status.update(
                        {
                            "state": state,
                            "matched": True,
                            "external_id": record["external_id"],
                            "confidence": 1.0,
                            "correlation_origin": exact["origin"],
                            "reason": (
                                "Exact manual correlation"
                                if exact["origin"] == "manual"
                                else f"Exact {self._correlation_supplier(exact)} correlation"
                            ),
                        }
                    )
                except Exception as exc:  # noqa: BLE001 - retain exact identity
                    records.append(
                        cached["data"]
                        if cached_matches
                        else self._correlation_stub(manga, exact)
                    )
                    failure = _source_failure_status(exc, cached=bool(cached_matches))
                    if failure["state"] == "cached_error" and not cached_matches:
                        failure["state"] = "exact_error"
                    status.update(
                        {
                            **failure,
                            "matched": True,
                            "external_id": exact["external_id"],
                            "correlation_origin": exact["origin"],
                        }
                    )
                source_status.append(status)
                continue
            if not source.automatic_matching:
                self.database.delete_metadata_source_records(
                    manga_id, source=source.name, identity_only=True
                )
                status.update(
                    {
                        "state": "manual_only",
                        "matched": False,
                        "reason": (
                            f"Add an exact {source.label} book URL or ID in "
                            "Metadata sources"
                        ),
                    }
                )
                source_status.append(status)
                continue
            if not source.configured:
                cached = cached_by_source.get(source.name)
                if cached:
                    records.append(cached["data"])
                    status["state"] = "cached"
                    status["matched"] = True
                else:
                    status["state"] = "unavailable"
                source_status.append(status)
                continue
            attempted.add(source.name)
            try:
                outcome = await self._match_source(manga, source, records)
                if outcome.record is None:
                    # A completed query supersedes old automatic correlations.
                    # Keeping a formerly accepted ID here would make matcher
                    # fixes and credential refreshes ineffective indefinitely.
                    self.database.delete_metadata_source_records(
                        manga_id, source=source.name, identity_only=True
                    )
                    status.update(
                        {
                            "state": outcome.state,
                            "matched": False,
                            "confidence": outcome.confidence,
                            "reason": outcome.reason,
                            "candidate": outcome.candidate,
                            "runner_up_confidence": outcome.runner_up_confidence,
                            "margin": outcome.margin,
                        }
                    )
                else:
                    record = outcome.record
                    records.append(record)
                    self._adopt_catalogue_identities(exact_correlations, record)
                    self._persist_source_match(
                        manga_id, record, outcome.confidence, outcome.reason
                    )
                    status.update(
                        {
                            "state": "matched",
                            "matched": True,
                            "external_id": record["external_id"],
                            "confidence": outcome.confidence,
                            "reason": outcome.reason,
                            "runner_up_confidence": outcome.runner_up_confidence,
                            "margin": outcome.margin,
                        }
                    )
            except Exception as exc:  # noqa: BLE001 - retain other source results
                cached = cached_by_source.get(source.name)
                if cached:
                    records.append(cached["data"])
                status.update(_source_failure_status(exc, cached=bool(cached)))
            source_status.append(status)

        await self._refresh_chapter_map(manga_id, records)
        present_sources = {str(record.get("source") or "") for record in records}
        records.extend(
            self._correlation_stub(manga, correlation)
            for source_name, correlation in exact_correlations.items()
            if source_name not in series_source_names
            and source_name not in present_sources
        )
        canonical = self._merge_series(manga, records)
        title_result = await self._apply_canonical_title(manga_id, canonical)
        manga = self.database.get_manga(manga_id)
        previous = self.database.get_series_metadata(manga_id)
        artwork = await self._select_series_artwork(
            manga, records, previous=previous, force=force
        )
        canonical["cover_url"] = (
            series_artwork_url(manga_id, artwork.get("sha256")) if artwork else None
        )
        self._apply_artwork_metadata(canonical, artwork)
        saved = self.database.save_series_metadata(
            manga_id,
            canonical,
            artwork_path=artwork.get("path") if artwork else None,
            artwork_sha256=artwork.get("sha256") if artwork else None,
            artwork_media_type=artwork.get("media_type") if artwork else None,
            source_status=source_status,
        )
        volumes = await self._enrich_volumes(manga, canonical, force=force)
        if self.library_publisher is not None:
            library_result = await self.library_publisher(manga_id)
        else:
            library_result = {
                "reader_independent": True,
                "checked": 0,
                "updated": 0,
                "errors": [],
            }
        komga_result: dict[str, Any] | None = None
        if self.komga is not None:
            komga_result = await self.sync_to_komga(manga_id)
            if self.library_publisher is None:
                library_result = komga_result
        result = {
            "manga_id": manga_id,
            "title": title_result,
            "metadata": saved,
            "volumes": volumes,
            "library": library_result,
        }
        if komga_result is not None:
            result["komga"] = komga_result
        return result

    async def _match_source(
        self,
        manga: dict[str, Any],
        source: MetadataSource,
        evidence_records: list[dict[str, Any]],
    ) -> SourceMatchOutcome:
        target = self._work_identity(manga, evidence_records)
        queries = self._identity_queries(target)[
            : max(1, int(source.identity_search_queries))
        ]
        candidates_by_id: dict[str, dict[str, Any]] = {}
        query_failures: list[Exception] = []
        successful_queries = 0

        def add_candidate(candidate: dict[str, Any]) -> None:
            external_id = str(candidate.get("external_id") or "")
            key = external_id or normalized_title(candidate.get("title"))
            if not key:
                return
            existing = candidates_by_id.get(key)
            if existing is None:
                candidates_by_id[key] = candidate
                return
            # An author index and a title search may return the same work with
            # different compact fields. Preserve every piece of identity
            # evidence until the full details record is fetched.
            merged = {**existing}
            for field in ("authors", "creators", "links"):
                merged[field] = _unique_identity_values(
                    list(existing.get(field) or []), list(candidate.get(field) or [])
                )
            # Author-first discovery deliberately runs before title search.
            # Preserve every title-search hit as an alias instead of allowing
            # the compact author result's hit_title to hide stronger evidence.
            merged["alternate_titles"] = _unique_strings(
                [
                    *(existing.get("alternate_titles") or []),
                    *(candidate.get("alternate_titles") or []),
                    existing.get("hit_title"),
                    candidate.get("hit_title"),
                ]
            )
            for field in (
                "title",
                "hit_title",
                "year",
                "work_type",
                "original_language",
            ):
                if merged.get(field) in (None, "", [], {}):
                    merged[field] = candidate.get(field)
            candidates_by_id[key] = merged

        if source.supports_author_lookup:
            for author in _unique_people(list(target.get("authors") or []))[:3]:
                try:
                    author_results = await source.search_series_by_author(
                        author, limit=100
                    )
                    successful_queries += 1
                except Exception as exc:  # noqa: BLE001 - title lookup can recover
                    query_failures.append(exc)
                    continue
                for candidate in author_results:
                    add_candidate(candidate)
        for query in queries:
            try:
                query_results = await source.search_series(query, limit=12)
                successful_queries += 1
            except Exception as exc:  # noqa: BLE001 - retry other valid aliases
                query_failures.append(exc)
                continue
            for candidate in query_results:
                add_candidate(candidate)
        candidates = list(candidates_by_id.values())
        if not candidates:
            if not successful_queries and query_failures:
                raise query_failures[0]
            return SourceMatchOutcome("no_match", None, 0.0, "no search results")

        primary = normalized_title(target.get("title"))
        primary_variants = work_title_variants(target.get("title"))
        exact_primary_count = sum(
            1
            for candidate in candidates
            if primary
            and primary_variants & work_title_variants(candidate.get("title"))
        )
        preliminary = sorted(
            (
                (
                    assess_series_match(
                        target,
                        candidate,
                        exact_matches=exact_primary_count,
                    ),
                    candidate,
                )
                for candidate in candidates
            ),
            key=lambda item: (
                item[0].ranking_score,
                item[0].title_similarity,
                bool(item[1].get("external_id")),
            ),
            reverse=True,
        )

        if source.search_results_complete_for_matching:
            assessed = preliminary
        else:
            details: list[dict[str, Any]] = []
            for _, candidate in preliminary[:4]:
                external_id = str(candidate.get("external_id") or "")
                if not external_id:
                    continue
                detail = await source.get_series(external_id)
                hit_title = candidate.get("hit_title")
                if hit_title:
                    detail["hit_title"] = hit_title
                detail["alternate_titles"] = _unique_strings(
                    [
                        *(detail.get("alternate_titles") or []),
                        *(candidate.get("alternate_titles") or []),
                        hit_title,
                    ]
                )
                details.append(detail)
            exact_primary_count = sum(
                1
                for item in details
                if primary and primary_variants & work_title_variants(item.get("title"))
            )
            assessed = sorted(
                (
                    (
                        assess_series_match(
                            target, item, exact_matches=exact_primary_count
                        ),
                        item,
                    )
                    for item in details
                ),
                key=lambda item: (item[0].ranking_score, item[0].title_similarity),
                reverse=True,
            )

        if not assessed:
            return SourceMatchOutcome("no_match", None, 0.0, "no usable results")
        best, best_record = assessed[0]
        runner = assessed[1][0] if len(assessed) > 1 else None
        runner_confidence = runner.confidence if runner else None
        margin = round(best.ranking_score - runner.ranking_score, 3) if runner else None
        candidate_summary = {
            "external_id": str(best_record.get("external_id") or ""),
            "title": str(best_record.get("title") or ""),
            "title_relation": best.title_relation,
        }
        if best.confidence < AUTO_MATCH_THRESHOLD:
            return SourceMatchOutcome(
                "no_match",
                None,
                best.confidence,
                best.reason,
                candidate_summary,
                runner_confidence,
                margin,
                best,
            )
        tie_note = ""
        if runner is not None and margin is not None and margin < AUTO_MATCH_MIN_MARGIN:
            resolved = _resolve_near_tie(
                target, [(item[0], item[1]) for item in assessed]
            )
            if resolved is not None:
                best, best_record, tie_note = resolved
                candidate_summary = {
                    "external_id": str(best_record.get("external_id") or ""),
                    "title": str(best_record.get("title") or ""),
                    "title_relation": best.title_relation,
                }
                runner = None
                runner_confidence = None
                margin = None
        if runner is not None and margin is not None and margin < AUTO_MATCH_MIN_MARGIN:
            return SourceMatchOutcome(
                "ambiguous",
                None,
                best.confidence,
                f"ambiguous candidates (margin {margin:.3f}); {best.reason}",
                candidate_summary,
                runner_confidence,
                margin,
                best,
            )

        accepted_record = best_record
        if source.search_results_complete_for_matching:
            external_id = str(best_record.get("external_id") or "")
            accepted_record = await source.get_series(external_id)
            accepted_record["alternate_titles"] = _unique_strings(
                [
                    *(accepted_record.get("alternate_titles") or []),
                    *(best_record.get("alternate_titles") or []),
                ]
            )
            confirmed = assess_series_match(
                target,
                accepted_record,
                exact_matches=exact_primary_count,
            )
            if confirmed.confidence < AUTO_MATCH_THRESHOLD:
                return SourceMatchOutcome(
                    "no_match",
                    None,
                    confirmed.confidence,
                    f"detail revalidation failed; {confirmed.reason}",
                    candidate_summary,
                    runner_confidence,
                    margin,
                    confirmed,
                )
            best = confirmed

        if tie_note:
            best = SeriesMatchAssessment(
                best.confidence,
                best.ranking_score,
                f"{best.reason}; {tie_note}",
                best.title_relation,
                best.title_similarity,
                best.creator_similarity,
                best.evidence,
                best.contradictions,
            )
        collision_reason = self._identity_collision_reason(
            manga, source.name, accepted_record, best
        )
        if collision_reason:
            return SourceMatchOutcome(
                "no_match",
                None,
                best.confidence,
                f"identity collision; {collision_reason}",
                candidate_summary,
                runner_confidence,
                margin,
                best,
            )

        accepted_record["match_policy_version"] = MATCH_POLICY_VERSION
        return SourceMatchOutcome(
            "matched",
            accepted_record,
            best.confidence,
            best.reason,
            candidate_summary,
            runner_confidence,
            margin,
            best,
        )

    def _identity_collision_reason(
        self,
        manga: dict[str, Any],
        source: str,
        record: dict[str, Any],
        assessment: SeriesMatchAssessment,
    ) -> str | None:
        """Reject a contained-story alias when another series owns the work title."""

        if assessment.title_relation not in {
            "candidate_primary_matches_alias",
            "primary_matches_candidate_alias",
            "alias_exact",
        }:
            return None
        external_id = str(record.get("external_id") or "")
        if not external_id:
            return None
        for owner in self.database.list_metadata_identity_owners(
            source, external_id, exclude_manga_id=str(manga["id"])
        ):
            owner_title = str(owner.get("source_title") or owner.get("title") or "")
            if work_title_variants(owner_title) & work_title_variants(
                record.get("title")
            ):
                return (
                    f"{source} {external_id} is already identified by the "
                    "primary work title of another local series"
                )
        return None

    @staticmethod
    def _identity_queries(target: dict[str, Any]) -> list[str]:
        queries: list[str] = []
        for value in _unique_strings(
            [target.get("title"), *(target.get("alternate_titles") or [])]
        ):
            compact = " ".join(value.split())
            if len(compact) > IDENTITY_QUERY_MAX_CHARS:
                compact = compact[:IDENTITY_QUERY_MAX_CHARS].rsplit(" ", 1)[0]
            if compact and compact not in queries:
                queries.append(compact)
        return queries

    def _work_identity(
        self, manga: dict[str, Any], records: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Build matching evidence only from the original work catalogues.

        Translated publication dates and counts describe editions, so feeding
        them back into MAL matching would corrupt the original manga identity.
        """

        work_records = [
            record
            for record in records
            if _catalogue_scope(record) not in {"edition", "publication"}
        ]
        ordered = sorted(
            work_records,
            key=lambda item: self._record_priority(item, work_type="Manga"),
            reverse=True,
        )

        def first(field: str, fallback: Any = None) -> Any:
            return next(
                (
                    record.get(field)
                    for record in ordered
                    if record.get(field) not in (None, "", [], {})
                ),
                fallback,
            )

        identity_title = _manga_identity_title(manga)
        aliases = _unique_strings(
            [
                *(record.get("title") for record in ordered),
                *(
                    title
                    for record in ordered
                    for title in record.get("alternate_titles") or []
                ),
            ]
        )
        primary = normalized_title(identity_title)
        return {
            "title": identity_title,
            "alternate_titles": [
                value for value in aliases if normalized_title(value) != primary
            ],
            "authors": _unique_people(
                [
                    *(manga.get("authors") or []),
                    *(
                        author
                        for record in ordered
                        for author in record.get("authors") or []
                    ),
                ]
            ),
            "year": first("year", manga.get("year")),
            "work_type": first(
                "work_type",
                "Manga"
                if manga.get("original_language") == "ja"
                or manga.get("provider") == "mangadex"
                else None,
            ),
            "volume_count": first("volume_count", manga.get("last_volume")),
            "chapter_count": first("chapter_count", manga.get("last_chapter")),
            "catalogue_scope": "work",
        }

    def _persist_source_match(
        self,
        manga_id: str,
        record: dict[str, Any],
        confidence: float,
        reason: str,
        *,
        entity_type: str | None = None,
        entity_key: str = "",
    ) -> None:
        if entity_type is None:
            entity_type = {
                "work": "work",
                "publication": "edition",
                "edition": "edition",
            }.get(_catalogue_scope(record), "series")
        normalized = {key: value for key, value in record.items() if key != "raw"}
        if not entity_key and entity_type in {"series", "work", "edition"}:
            self.database.delete_metadata_source_records(
                manga_id,
                source=str(record["source"]),
                identity_only=True,
            )
        self.database.save_metadata_source_record(
            manga_id,
            entity_type=entity_type,
            entity_key=entity_key,
            source=str(record["source"]),
            external_id=str(record.get("external_id") or ""),
            match_confidence=confidence,
            match_reason=reason,
            data=normalized,
            raw=dict(record.get("raw") or {}),
        )

    async def _enrich_volumes(
        self, manga: dict[str, Any], canonical: dict[str, Any], *, force: bool
    ) -> list[dict[str, Any]]:
        releases = self.database.list_all_chapters(manga["id"])
        publications: dict[str, dict[str, str]] = {}
        for release in releases:
            key = publication_metadata_key(release, canonical)
            number = publication_number(release, canonical)
            if key and number:
                publications.setdefault(
                    key,
                    {
                        "key": key,
                        "number": number,
                        "kind": publication_kind(release, canonical),
                    },
                )
        ordered_publications = sorted(
            publications.values(),
            key=lambda item: (
                item["kind"] == "issue",
                self._number_sort_key(item["number"]),
            ),
        )
        is_comic = canonical.get("content_kind") == "comic"
        comicvine = (
            next(
                (
                    source
                    for source in self.sources
                    if source.name == "comicvine"
                    and source.configured
                    and source.supports_volumes
                ),
                None,
            )
            if is_comic
            else None
        )
        previous_by_volume = {
            item["volume_key"]: item
            for item in self.database.list_volume_metadata(manga["id"])
        }
        cached_comicvine = (
            {
                item["entity_key"]: item
                for item in self.database.list_metadata_source_records(manga["id"])
                if item["source"] == "comicvine"
                and item["entity_type"] in {"volume", "issue"}
            }
            if is_comic
            else {}
        )
        output: list[dict[str, Any]] = []
        for publication in ordered_publications:
            key = publication["key"]
            number = publication["number"]
            entity_kind = publication["kind"]
            cached = cached_comicvine.get(key)
            matched: dict[str, Any] | None = cached["data"] if cached else None
            confidence = float(cached["match_confidence"]) if cached else 0.0
            reason = str(cached["match_reason"]) if cached else ""
            source_state = "cached" if cached else "unavailable"
            source_error: str | None = None
            if comicvine is not None and (force or cached is None):
                try:
                    candidates = await comicvine.search_volume(
                        canonical, number, limit=10
                    )
                    exact_issues = [
                        item
                        for item in candidates
                        if str(item.get("issue_number") or "").casefold()
                        == number.casefold()
                    ]
                    if len(exact_issues) == 1:
                        matched = exact_issues[0]
                        confidence = 1.0
                        reason = (
                            "verified Comic Vine volume ID and exact issue "
                            f"number {number}"
                        )
                        source_state = "matched"
                        self._persist_source_match(
                            manga["id"],
                            matched,
                            confidence,
                            reason,
                            entity_type=entity_kind,
                            entity_key=key,
                        )
                    else:
                        self.database.delete_metadata_source_records(
                            manga["id"], source="comicvine", entity_key=key
                        )
                        matched = None
                        confidence = 0.0
                        reason = ""
                        source_state = (
                            "ambiguous" if len(exact_issues) > 1 else "no_match"
                        )
                except Exception as exc:  # noqa: BLE001 - cached data stays usable
                    source_state = "cached" if cached else "error"
                    source_error = f"{type(exc).__name__}: {exc}"[:500]

            default_title = (
                f"Issue {number}" if entity_kind == "issue" else f"Volume {number}"
            )
            matched_creators = (matched or {}).get("creators") or []
            matched_authors = (matched or {}).get("authors") or []
            volume_data = {
                "number": number,
                "entity_kind": entity_kind,
                "title": (matched or {}).get("issue_title") or default_title,
                "description": (matched or {}).get("description") or "",
                "authors": matched_authors or canonical.get("authors") or [],
                "creators": matched_creators or canonical.get("creators") or [],
                "publisher": (matched or {}).get("publisher")
                or canonical.get("publisher"),
                "release_date": (matched or {}).get("release_date"),
                "isbn": (matched or {}).get("isbn"),
                "page_count": (matched or {}).get("page_count"),
                "genres": _unique_strings(
                    [
                        *(canonical.get("genres") or []),
                        *((matched or {}).get("genres") or []),
                    ]
                ),
                "links": (matched or {}).get("links") or [],
                "external_ids": (
                    {str(matched["source"]): str(matched["external_id"])}
                    if matched
                    else {}
                ),
                "match_confidence": confidence if matched else None,
                "source_status": (
                    {
                        "comicvine": {
                            "state": source_state,
                            "error": source_error,
                        }
                    }
                    if is_comic
                    else {}
                ),
            }
            previous = previous_by_volume.get(key)
            artwork = await self._select_volume_artwork(
                manga,
                key,
                matched,
                previous=previous,
                force=force,
            )
            volume_data["cover_url"] = (
                volume_artwork_url(manga["id"], key, artwork.get("sha256"))
                if artwork
                else None
            )
            self._apply_artwork_metadata(volume_data, artwork)
            saved = self.database.save_volume_metadata(
                manga["id"],
                key,
                volume_data,
                artwork_path=artwork.get("path") if artwork else None,
                artwork_sha256=artwork.get("sha256") if artwork else None,
                artwork_media_type=artwork.get("media_type") if artwork else None,
            )
            output.append(saved)
        return output

    @staticmethod
    def _artwork_candidate_records(
        records: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """One candidate per image URL.

        The origin record of a catalogue work points at the same image as
        the MangaBaka record but only through ``origin_url``; the direct
        catalogue ``url`` (trusted host, known dimensions) must win the
        deduplication, otherwise MangaBaka's cover never becomes a candidate.
        """

        by_identifier: dict[str, dict[str, Any]] = {}
        order: list[str] = []
        for record in records:
            cover = record.get("cover") or {}
            identifier = str(cover.get("url") or cover.get("origin_url") or "")
            if not identifier:
                continue
            candidate = {
                "source": record["source"],
                "url": cover.get("url"),
                "origin_url": cover.get("origin_url"),
                "identifier": identifier,
                "reported_width": int(cover.get("width") or 0),
                "reported_height": int(cover.get("height") or 0),
            }
            current = by_identifier.get(identifier)
            if current is None:
                by_identifier[identifier] = candidate
                order.append(identifier)
            elif not current.get("url") and candidate.get("url"):
                by_identifier[identifier] = candidate
        return [by_identifier[identifier] for identifier in order]

    async def _select_series_artwork(
        self,
        manga: dict[str, Any],
        records: list[dict[str, Any]],
        *,
        previous: dict[str, Any] | None,
        force: bool,
    ) -> dict[str, Any] | None:
        cached = await asyncio.to_thread(
            self._cached_artwork, manga["id"], "series", previous
        )
        candidates = self._artwork_candidate_records(records)
        # Every mapped download source shows its own cover for the work:
        # often a cleaner scan or a newer edition than the catalogue's. They
        # join the candidates so the chooser offers them and the automatic
        # pick can fall back to them when the catalogue image is unusable.
        candidates.extend(self._release_source_cover_candidates(manga))
        signature = self._artwork_candidate_signature(candidates)
        work_type = self._resolve_work_type(manga, records)
        contenders: list[dict[str, Any]] = []
        for candidate in candidates:
            try:
                if candidate.get("origin_url"):
                    prepared = await self._prepare_origin_cover(
                        manga, str(candidate["origin_url"])
                    )
                else:
                    prepared = await self.artwork.prepare_remote(
                        str(candidate["source"]), str(candidate["url"])
                    )
                quality = self._artwork_quality(
                    str(candidate["source"]),
                    prepared,
                    work_type=work_type,
                    entity_kind="series",
                )
                candidate_id = _json_hash(
                    {
                        "source": str(candidate["source"]),
                        "identifier": str(candidate["identifier"]),
                    }
                )[:24]
                stored = await asyncio.to_thread(
                    self.artwork.store_prepared,
                    manga["id"],
                    f"series-candidate-{candidate_id}",
                    prepared,
                )
                contenders.append(
                    {
                        **candidate,
                        **stored,
                        "candidate_id": candidate_id,
                        "prepared": prepared,
                        "quality": quality,
                        "score": round(float(quality["score"]), 3),
                        "source_priority": int(quality["source_priority"]),
                        "source_url": str(candidate["identifier"]),
                    }
                )
            except Exception:  # noqa: BLE001 - compare every usable trusted cover
                continue

        if not contenders:
            return cached
        automatic = max(contenders, key=self._artwork_selection_key)
        persisted = self.database.replace_series_artwork_candidates(
            manga["id"],
            contenders,
            automatic_candidate_id=str(automatic["candidate_id"]),
        )
        preferred_id = self.database.get_series_artwork_preference(manga["id"])
        selected_row = next(
            (
                row
                for row in persisted
                if row["candidate_id"]
                == (preferred_id or str(automatic["candidate_id"]))
            ),
            None,
        )
        if selected_row is None or not self._artwork_candidate_path(selected_row):
            if preferred_id:
                self.database.set_series_artwork_preference(manga["id"], None)
                preferred_id = None
                persisted = self.database.replace_series_artwork_candidates(
                    manga["id"],
                    contenders,
                    automatic_candidate_id=str(automatic["candidate_id"]),
                )
            selected_row = next(row for row in persisted if row.get("is_automatic"))
        if selected_row is None:
            return cached
        return {
            **self._candidate_as_artwork(selected_row),
            "candidate_signature": signature,
            "candidates_evaluated": len(contenders),
            "candidate_id": str(selected_row["candidate_id"]),
            "automatic_candidate_id": str(automatic["candidate_id"]),
            "selection_mode": "manual" if preferred_id else "automatic",
            "selection_version": ARTWORK_SELECTION_VERSION,
        }

    async def _select_volume_artwork(
        self,
        manga: dict[str, Any],
        volume: str,
        matched: dict[str, Any] | None,
        *,
        previous: dict[str, Any] | None,
        force: bool,
    ) -> dict[str, Any] | None:
        cached = await asyncio.to_thread(
            self._cached_artwork, manga["id"], f"volume-{volume}", previous
        )
        candidates: list[dict[str, Any]] = []
        cover = (matched or {}).get("cover") or {}
        url = str(cover.get("url") or "")
        if url:
            candidates.append(
                {
                    "source": str(matched["source"]),
                    "url": url,
                    "identifier": url,
                }
            )
        local = await asyncio.to_thread(
            self._local_volume_cover_candidate, manga, volume
        )
        if local is not None:
            candidates.append(local)
        signature = self._artwork_candidate_signature(candidates)
        work_type = (matched or {}).get("work_type")
        if not work_type and (
            str(manga.get("provider") or "").casefold() == "mangadex"
            or str(manga.get("original_language") or "").casefold() == "ja"
        ):
            work_type = "Manga"
        selected: dict[str, Any] | None = None
        evaluated_count = 0
        for candidate in candidates:
            try:
                if candidate.get("content") is not None:
                    prepared = await asyncio.to_thread(
                        self.artwork.prepare_bytes,
                        bytes(candidate["content"]),
                        trusted_local=True,
                    )
                else:
                    prepared = await self.artwork.prepare_remote(
                        str(candidate["source"]), str(candidate["url"])
                    )
                quality = self._artwork_quality(
                    str(candidate["source"]),
                    prepared,
                    work_type=work_type,
                    entity_kind="volume",
                )
                evaluated_count += 1
                contender = {**candidate, "prepared": prepared, "quality": quality}
                if selected is None or self._artwork_selection_key(
                    contender
                ) > self._artwork_selection_key(selected):
                    selected = contender
            except Exception:  # noqa: BLE001 - retain other candidate sources
                continue
        if selected is None:
            return cached
        stored = await asyncio.to_thread(
            self.artwork.store_prepared,
            manga["id"],
            f"volume-{volume}",
            selected["prepared"],
        )
        return {
            **stored,
            "source": str(selected["source"]),
            "source_url": str(selected["identifier"]),
            "score": round(float(selected["quality"]["score"]), 3),
            "source_priority": int(selected["quality"]["source_priority"]),
            "candidate_signature": signature,
            "candidates_evaluated": evaluated_count,
            "selection_version": ARTWORK_SELECTION_VERSION,
        }

    @staticmethod
    def _artwork_selection_key(item: dict[str, Any]) -> tuple[int, float, int]:
        """Choose authority first, then quality within that source tier."""

        return (
            int(item["quality"]["source_priority"]),
            float(item["quality"]["score"]),
            int(item["prepared"]["source_width"])
            * int(item["prepared"]["source_height"]),
        )

    def _release_source_cover_candidates(
        self, manga: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """One cover candidate per mapped download source of the work."""

        result: list[dict[str, Any]] = []
        for mapping in self.database.list_release_sources(str(manga["id"])):
            provider_name = str(mapping.get("provider") or "")
            provider_manga_id = str(mapping.get("provider_manga_id") or "")
            if provider_name not in self.providers or not provider_manga_id:
                continue
            origin_url = (
                f"/api/covers/{provider_name}/{quote(provider_manga_id, safe='')}/cover"
            )
            # "suwayomi/MangaK (EN)": the chooser names the site, the priority
            # table sees the provider.
            result.append(
                {
                    "source": f"{provider_name}/{mapping.get('source_name') or provider_name}",
                    "url": None,
                    "origin_url": origin_url,
                    "identifier": origin_url,
                    "reported_width": 0,
                    "reported_height": 0,
                }
            )
        return result

    async def _prepare_origin_cover(
        self, manga: dict[str, Any], cover_url: str
    ) -> dict[str, Any]:
        match = re.fullmatch(
            r"/api/covers/(?P<provider>[a-z0-9_-]+)/(?P<id>[^/]+)/(?P<file>[^/]+)",
            unquote(urlparse(cover_url).path),
        )
        if not match:
            raise ValueError("Unsupported Tankarr origin cover URL")
        provider_name = match.group("provider")
        provider = self.providers.get(provider_name)
        origin_id = match.group("id")
        # The id is the series itself, or one of the provider identities
        # mapped to it: a cover from an unrelated work is never accepted.
        mapped_ids = {
            str(item.get("provider_manga_id") or "")
            for item in self.database.list_release_sources(str(manga["id"]))
            if str(item.get("provider") or "") == provider_name
        }
        if provider is None or (
            origin_id != manga["id"] and origin_id not in mapped_ids
        ):
            raise ValueError("Origin cover provider or series does not match")
        content, _ = await provider.get_cover(origin_id, match.group("file"))
        return await asyncio.to_thread(
            self.artwork.prepare_bytes, content, trusted_local=True
        )

    def _local_volume_cover_candidate(
        self, manga: dict[str, Any], volume: str
    ) -> dict[str, Any] | None:
        candidates = [
            chapter
            for chapter in self.database.list_all_chapters(manga["id"])
            if str(chapter.get("volume") or "") == volume
            and chapter.get("chapter") in (None, "")
            and chapter.get("downloaded")
            and chapter.get("library_path")
        ]
        candidates.sort(
            key=lambda chapter: (
                str(chapter.get("language") or "")
                != str(manga.get("preferred_language") or ""),
                str(chapter["id"]),
            )
        )
        try:
            root = self.settings.library_dir.resolve(strict=True)
        except (FileNotFoundError, OSError):
            return None
        for chapter in candidates:
            try:
                recorded = Path(str(chapter["library_path"]))
                path = (
                    recorded.resolve(strict=True)
                    if recorded.is_absolute()
                    else (root / recorded).resolve(strict=True)
                )
                if (
                    root not in path.parents
                    or path.is_symlink()
                    or not path.is_file()
                    or path.suffix.casefold() != ".cbz"
                ):
                    continue
                with zipfile.ZipFile(path) as archive:
                    images = sorted(
                        (
                            item
                            for item in archive.infolist()
                            if not item.is_dir()
                            and Path(item.filename).suffix.casefold() in IMAGE_SUFFIXES
                            and 0 < item.file_size <= MAX_LOCAL_ARTWORK_BYTES
                        ),
                        key=lambda item: self._natural_artwork_key(item.filename),
                    )
                    if not images:
                        continue
                    content = archive.read(images[0])
                digest = str(chapter.get("library_sha256") or "")[:16]
                return {
                    "source": "local",
                    "content": content,
                    "identifier": f"local-volume:{chapter['id']}:{digest}",
                }
            except (OSError, ValueError, zipfile.BadZipFile, RuntimeError):
                continue
        return None

    @staticmethod
    def _natural_artwork_key(value: str) -> tuple[tuple[int, object], ...]:
        return tuple(
            (0, int(part)) if part.isdigit() else (1, part.casefold())
            for part in re.split(r"(\d+)", value)
            if part
        )

    def _origin_record(self, manga: dict[str, Any]) -> dict[str, Any]:
        source = str(manga.get("provider") or "local")
        source_title = _manga_identity_title(manga)
        provider_title = str(manga.get("source_title") or source_title)
        original_language = manga.get("original_language")
        work_type = {
            "ja": "Manga",
            "ko": "Manhwa",
            "zh": "Manhua",
        }.get(str(original_language or "").casefold())
        if work_type is None and source == "mangadex":
            work_type = "Manga"
        source_url = str(manga.get("source_url") or "")
        source_label = str(manga.get("source_name") or source)
        if source == "suwayomi":
            source_label = f"Suwayomi → {source_label}"
        return {
            "source": source,
            "catalogue_scope": "work" if source == "mangadex" else "release",
            "external_id": str(manga.get("source_id") or manga["id"]),
            "title": source_title,
            "localized_titles": (
                {str(manga.get("preferred_language") or "en"): source_title}
                if source == "mangadex"
                else {}
            ),
            "hit_title": source_title,
            "alternate_titles": _unique_strings(
                value
                for value in (provider_title,)
                if value and normalized_title(value) != normalized_title(source_title)
            ),
            "catalogue_alternate_titles": _unique_strings(
                value
                for value in (provider_title,)
                if value and normalized_title(value) != normalized_title(source_title)
            ),
            "description": manga.get("description") or "",
            "authors": list(manga.get("authors") or []),
            "creators": [
                {"name": name, "role": "writer"} for name in manga.get("authors") or []
            ],
            "creator_links": list(manga.get("creator_links") or []),
            "genres": [],
            "tags": [],
            "publisher": None,
            "year": manga.get("year"),
            "status": manga.get("status"),
            "work_type": work_type,
            "original_language": original_language,
            "volume_count": self._positive_integer(manga.get("last_volume")),
            "chapter_count": self._positive_integer(manga.get("last_chapter")),
            "rating": None,
            "cover": (
                {"origin_url": manga["cover_url"]} if manga.get("cover_url") else None
            ),
            "links": (
                [{"label": source_label, "url": source_url}] if source_url else []
            ),
            "raw": {
                key: manga.get(key)
                for key in (
                    "id",
                    "provider",
                    "title",
                    "source_title",
                    "metadata_title",
                    "title_override",
                    "description",
                    "cover_url",
                    "authors",
                    "original_language",
                    "status",
                    "year",
                    "last_volume",
                    "last_chapter",
                    "source_url",
                    "source_name",
                    "source_id",
                    "external_correlations",
                )
            },
        }

    def _merge_series(
        self, manga: dict[str, Any], records: list[dict[str, Any]]
    ) -> dict[str, Any]:
        # Link-only correlations remain available in the source editor, but do
        # not masquerade as downloaded metadata in canonical fields or badges.
        records = [
            record for record in records if str(record.get("title") or "").strip()
        ]
        work_type = self._resolve_work_type(manga, records)
        classification = self._classify_content(manga, records)
        normalized_type = str(work_type or "").casefold()
        is_asian_work = normalized_type in {
            "manga",
            "manhwa",
            "manhua",
            "webtoon",
            "one_shot",
            "doujinshi",
        } or str(manga.get("original_language") or "").casefold() in {
            "ja",
            "ko",
            "zh",
        }
        edition_records = [
            record
            for record in records
            if is_asian_work and _catalogue_scope(record) in {"edition", "publication"}
        ]
        work_records = [record for record in records if record not in edition_records]
        ordered = sorted(
            work_records,
            key=lambda item: self._record_priority(item, work_type=work_type),
            reverse=True,
        )
        provenance: dict[str, str] = {}

        def first(field: str, fallback: Any = None) -> Any:
            for record in ordered:
                value = record.get(field)
                if value not in (None, "", [], {}):
                    provenance[field] = str(record["source"])
                    return value
            return fallback

        # Use the strongest creator-bearing record as an identity anchor, while
        # still allowing exact-title catalogues to contribute legitimate
        # co-creators. A lower-priority parent anthology whose alternate title
        # names the target may only refine already anchored people.
        creator_records = [
            record
            for record in ordered
            if record.get("authors") or record.get("creators")
        ]
        target_titles = {
            normalized_title(value)
            for value in (
                _manga_identity_title(manga),
                manga.get("source_title"),
                manga.get("title"),
            )
            if normalized_title(value)
        }

        def primary_identity_match(record: dict[str, Any]) -> bool:
            primary_titles = {
                normalized_title(record.get("title")),
                *(
                    normalized_title(value)
                    for value in (record.get("localized_titles") or {}).values()
                ),
            }
            return bool(target_titles & (primary_titles - {""}))

        # Prefer a source whose primary/localized title names the managed work.
        # This prevents an authoritative anthology catalogue record from
        # becoming the creator anchor merely because one contained story is an
        # alternate title. Authority order resolves ties and remains the
        # fallback when every source represents the work through an alias.
        external_creator_records = [
            record
            for record in creator_records
            if str(record.get("source") or "").casefold() != "local"
        ]
        creator_anchor = next(
            (
                record
                for record in external_creator_records
                if primary_identity_match(record)
            ),
            (
                external_creator_records[0]
                if external_creator_records
                else creator_records[0]
                if creator_records
                else None
            ),
        )

        # Exact book/edition catalogues commonly expose an undifferentiated
        # contributor array. If the managed release already names a compatible
        # strict subset, keep that subset as the work-author anchor. The full
        # publication credits remain in the source/edition record, but editors,
        # translators and essay contributors are not promoted to work authors.
        origin_source = str(manga.get("provider") or "local").casefold()
        origin_creator_record = next(
            (
                record
                for record in creator_records
                if str(record.get("source") or "").casefold() == origin_source
            ),
            None,
        )

        def record_people(record: dict[str, Any] | None) -> list[str]:
            if not record:
                return []
            return _unique_people(
                [
                    *(record.get("authors") or []),
                    *(
                        creator.get("name")
                        for creator in record.get("creators") or []
                        if creator.get("name")
                    ),
                ]
            )

        anchor_people_before_refinement = record_people(creator_anchor)
        origin_people = record_people(origin_creator_record)
        if (
            creator_anchor is not None
            and origin_creator_record is not None
            and creator_anchor is not origin_creator_record
            and _catalogue_scope(creator_anchor) in {"edition", "publication"}
            and origin_people
            and len(anchor_people_before_refinement) > len(origin_people)
            and all(
                any(
                    creator_name_similarity(origin_name, edition_name) >= 0.90
                    for edition_name in anchor_people_before_refinement
                )
                for origin_name in origin_people
            )
        ):
            creator_anchor = origin_creator_record
        anchor_authors = list((creator_anchor or {}).get("authors") or [])
        anchor_creator_names = [
            creator.get("name")
            for creator in (creator_anchor or {}).get("creators") or []
            if creator.get("name")
        ]
        anchor_people = _unique_people([*anchor_authors, *anchor_creator_names])

        def matches_anchor(name: object) -> bool:
            return any(
                creator_name_similarity(name, anchor_name) >= 0.90
                for anchor_name in anchor_people
            )

        def creator_allowed(record: dict[str, Any], name: object) -> bool:
            # Creator identity comes from the strongest creator-bearing work
            # record. Lower tiers may corroborate that person (and contribute
            # another verified author-page link or role), but may not append a
            # different person merely because title matching accepted a broad
            # alias/anthology record.
            if record is creator_anchor or not anchor_people:
                return True
            return matches_anchor(name)

        author_candidates = [
            author
            for record in ordered
            for author in record.get("authors") or []
            if creator_allowed(record, author) and _is_person_credit(author)
        ]
        creator_candidates = [
            creator.get("name")
            for record in ordered
            for creator in record.get("creators") or []
            if creator.get("name") and creator_allowed(record, creator.get("name"))
        ]
        preferred_people = _preferred_person_names(
            [*author_candidates, *creator_candidates]
        )
        authors = _unique_people(
            [
                preferred_people.get(
                    normalized_person(author), canonical_person_name(author)
                )
                for author in author_candidates
            ]
        )
        # A hand-corrected credit is the operator's answer to a catalogue that
        # credits the wrong person, or none: it survives every refresh.
        authors_override = _unique_people(list(manga.get("authors_override") or []))
        if authors_override:
            authors = authors_override
        creators: list[dict[str, str]] = []
        creator_keys: set[tuple[str, str]] = set()
        for record in ordered:
            for creator in record.get("creators") or []:
                person = normalized_person(creator.get("name"))
                if not creator_allowed(record, creator.get("name")):
                    continue
                if not _is_person_credit(creator.get("name")):
                    continue
                name = preferred_people.get(
                    person, canonical_person_name(creator.get("name"))
                )
                for role in _creator_roles(creator.get("role")):
                    key = (" ".join(normalized_person(name)), role)
                    if name and key not in creator_keys:
                        creator_keys.add(key)
                        creators.append({"name": name, "role": role})
        if authors_override:
            overridden = {normalized_person(name) for name in authors_override}
            creators = [
                item
                for item in creators
                if normalized_person(item.get("name")) in overridden
            ]
        credited_people = {
            normalized_person(item["name"]) for item in creators if item.get("name")
        }
        for name in authors:
            person = normalized_person(name)
            if person and person not in credited_people:
                creators.append({"name": name, "role": "writer"})
                credited_people.add(person)

        creator_links: list[dict[str, str]] = []
        creator_link_keys: set[tuple[tuple[str, ...], str, str]] = set()
        for record in ordered:
            for link in record.get("creator_links") or []:
                raw_name = link.get("name")
                if not raw_name or not creator_allowed(record, raw_name):
                    continue
                safe = _merge_external_links(
                    [
                        {
                            "label": link.get("label") or record.get("source"),
                            "url": link.get("url"),
                        }
                    ]
                )
                if not safe:
                    continue
                name = preferred_people.get(
                    normalized_person(raw_name), canonical_person_name(raw_name)
                )
                source = str(link.get("source") or record.get("source") or "")
                external_id = str(link.get("external_id") or "")
                key = (normalized_person(name), source, external_id or safe[0]["url"])
                if not name or not source or key in creator_link_keys:
                    continue
                creator_link_keys.add(key)
                creator_links.append(
                    {
                        "name": name,
                        "role": str(link.get("role") or "writer"),
                        "source": source,
                        "label": safe[0]["label"],
                        "external_id": external_id,
                        "url": safe[0]["url"],
                    }
                )

        genres = _unique_strings(
            [value for record in ordered for value in record.get("genres") or []]
        )
        tags = _unique_strings(
            [
                *(value for record in ordered for value in record.get("tags") or []),
                work_type,
            ]
        )
        title_record = max(
            (record for record in work_records if record.get("title")),
            key=lambda item: self._title_selection_key(
                item, manga=manga, work_type=work_type
            ),
            default=None,
        )
        canonical_year = first("year", manga.get("year"))
        title_choice = self._select_display_title(
            manga,
            work_records,
            title_record,
            work_type=work_type,
            canonical_year=canonical_year,
        )
        canonical_work_title = str(
            title_choice["title"] or manga.get("source_title") or manga["title"]
        ).strip()
        selected_title_record = title_choice.get("record") or title_record or {}
        provenance["title"] = str(title_choice["source"])
        preferred_title_language = str(
            manga.get("preferred_language") or "en"
        ).casefold()
        title_localized = bool(title_choice["localized"])
        alternate_titles = _unique_strings(
            [
                *(
                    value
                    for value in (_manga_identity_title(manga),)
                    if normalized_title(value) != normalized_title(canonical_work_title)
                ),
                *(
                    value
                    for record in ordered
                    for value in (record.get("title"),)
                    if normalized_title(value) != normalized_title(canonical_work_title)
                ),
                *(
                    value
                    for record in ordered
                    for value in (record.get("localized_titles") or {}).values()
                    if normalized_title(value) != normalized_title(canonical_work_title)
                ),
                *(
                    value
                    for record in ordered
                    for value in record.get("alternate_titles") or []
                    if normalized_title(value) != normalized_title(canonical_work_title)
                ),
                *(
                    value
                    for record in ordered
                    for value in record.get("catalogue_alternate_titles") or []
                    if normalized_title(value) != normalized_title(canonical_work_title)
                ),
            ]
        )
        work_source_links = [
            {
                "label": str(link.get("label") or record["source"]),
                "url": str(link.get("url") or ""),
            }
            for record in ordered
            for link in record.get("links") or []
        ]
        source_links = [
            {
                "label": str(link.get("label") or record["source"]),
                "url": str(link.get("url") or ""),
            }
            for record in records
            for link in record.get("links") or []
        ]
        external_ids = {
            str(record["source"]): str(record.get("external_id") or "")
            for record in records
            if record.get("external_id")
        }
        work_external_ids = {
            str(record["source"]): str(record.get("external_id") or "")
            for record in work_records
            if record.get("external_id")
        }
        links = _merge_external_links(source_links)
        provider_correlations = _provider_correlations(records)
        original_language = first("original_language", manga.get("original_language"))
        reading_direction = None
        if normalized_type == "manga" or (
            original_language == "ja"
            and normalized_type in {"one_shot", "doujinshi", "novel"}
        ):
            reading_direction = "RIGHT_TO_LEFT"
        elif "webtoon" in normalized_type:
            reading_direction = "WEBTOON"
        elif normalized_type in {"comic", "manhwa", "manhua", "book"}:
            reading_direction = "LEFT_TO_RIGHT"
        volume_count = first("volume_count")
        chapter_count = first("chapter_count")
        issue_count = first("issue_count")
        status = first("status", manga.get("status"))
        # The followers were read by MangaBaka's own identifiers, so every
        # record describes this work; what they may disagree on is its size.
        corroboration = corroborate(ordered)
        if corroboration["chapter_count"]["confidence"] == AGREED:
            chapter_count = corroboration["chapter_count"]["value"]
            provenance["chapter_count"] = "consensus:" + ",".join(
                corroboration["chapter_count"]["agreeing"]
            )
        if corroboration["volume_count"]["confidence"] == AGREED:
            volume_count = corroboration["volume_count"]["value"]
            provenance["volume_count"] = "consensus:" + ",".join(
                corroboration["volume_count"]["agreeing"]
            )
        if not status and corroboration["status"]["value"]:
            status = corroboration["status"]["value"]
            provenance["status"] = "consensus"
        if normalized_title(status) in {"", "unknown", "n a", "none"}:
            status = None
            provenance.pop("status", None)
        status_inference = None
        if not status:
            status_inference = self._standalone_publication_status(
                manga,
                records,
                work_type=work_type,
                issue_count=issue_count,
            )
            if status_inference:
                status = "ended"
                provenance["status"] = str(status_inference["source"])
        editions: list[dict[str, Any]] = []
        edition_keys: set[tuple[str, str, str | None]] = set()
        for record in sorted(
            records,
            key=lambda item: (
                str(item.get("source") or ""),
                str(item.get("external_id") or ""),
            ),
        ):
            context = record.get("publication_context") or {}
            if record not in edition_records and not context:
                continue
            language = context.get("language", record.get("language"))
            key = (
                str(record.get("source") or ""),
                str(record.get("external_id") or ""),
                str(language) if language else None,
            )
            if key in edition_keys:
                continue
            edition_keys.add(key)
            editions.append(
                {
                    "source": key[0],
                    "external_id": key[1],
                    "title": str(record.get("title") or manga["title"]),
                    "alternate_titles": list(record.get("alternate_titles") or []),
                    "language": language,
                    "publisher": context.get("publisher", record.get("publisher")),
                    "publication_year": context.get(
                        "publication_year", record.get("year")
                    ),
                    "volume_count": context.get(
                        "volume_count", record.get("volume_count")
                    ),
                    "issue_count": context.get(
                        "issue_count", record.get("issue_count")
                    ),
                    "description": record.get("description") or "",
                    "links": _merge_external_links(list(record.get("links") or [])),
                }
            )
        canonical = {
            "title": canonical_work_title,
            "title_selection_version": TITLE_SELECTION_VERSION,
            "title_selection": {
                "source": provenance["title"],
                "language": preferred_title_language if title_localized else None,
                "localized": title_localized,
                "strategy": title_choice["strategy"],
                "priority": self._title_selection_key(
                    selected_title_record, manga=manga, work_type=work_type
                )[0]
                if selected_title_record
                else 0,
            },
            "alternate_titles": alternate_titles,
            "description": first("description", ""),
            "authors": authors,
            "creators": creators,
            "creator_links": creator_links,
            "genres": genres,
            "tags": tags,
            "publisher": first("publisher"),
            "publishers": first("publishers", []),
            "year": canonical_year,
            "status": status,
            "status_inference": status_inference,
            "work_type": work_type,
            "content_kind": classification["kind"],
            "classification": classification,
            "original_language": original_language,
            "translation_language": manga.get("preferred_language"),
            "reading_direction": reading_direction,
            "volume_count": volume_count,
            "chapter_count": chapter_count,
            # Running works have no final total; the catalogue's latest
            # release number is the honest "chapters so far".
            "latest_release_chapter": first("latest_release_chapter"),
            "issue_count": issue_count,
            "count_confidence": {
                "chapter": corroboration["chapter_count"]["confidence"],
                "volume": corroboration["volume_count"]["confidence"],
                "status": corroboration["status"]["confidence"],
            },
            "count_votes": {
                "chapter": corroboration["chapter_count"]["votes"],
                "volume": corroboration["volume_count"]["votes"],
                "status": corroboration["status"]["votes"],
            },
            "managed_edition": corroboration["english_edition"],
            "book_count": issue_count if normalized_type == "comic" else volume_count,
            "rating": first("rating"),
            "external_ids": external_ids,
            "external_sources": first("external_sources", []),
            "official_links": first("official_links", []),
            "links": links,
            "provider_correlations": provider_correlations,
            "provenance": provenance,
            "matched_sources": _unique_strings(
                [
                    str(record["source"])
                    for record in records
                    if record["source"] != "local" and record.get("title")
                ]
            ),
            "editions": editions,
        }
        canonical["work"] = {
            "title": canonical_work_title,
            "display_title": manga["title"],
            "alternate_titles": alternate_titles,
            "authors": authors,
            "creators": creators,
            "creator_links": creator_links,
            "year": canonical["year"],
            "status": canonical["status"],
            "status_inference": status_inference,
            "work_type": work_type,
            "content_kind": classification["kind"],
            "original_language": original_language,
            "volume_count": volume_count,
            "chapter_count": chapter_count,
            "external_ids": work_external_ids,
            "links": _merge_external_links(work_source_links),
        }
        return canonical

    def _reader_relative_paths(self, chapters: list[dict[str, Any]]) -> dict[str, str]:
        library_root = self.settings.library_dir.resolve()
        relative: dict[str, str] = {}
        for chapter in chapters:
            if not chapter.get("downloaded") or not chapter.get("library_path"):
                continue
            path = Path(str(chapter["library_path"])).resolve()
            try:
                relative[chapter["id"]] = path.relative_to(library_root).as_posix()
            except ValueError:
                continue
        return relative

    def _reader_sync_inventory(self) -> tuple[list[str], list[str]]:
        relative_paths: list[str] = []
        eligible: list[str] = []
        for manga in self.database.list_manga():
            paths = self._reader_relative_paths(
                self.database.list_all_chapters(str(manga["id"]))
            )
            relative_paths.extend(paths.values())
            if paths and self.database.get_series_metadata(str(manga["id"])):
                eligible.append(str(manga["id"]))
        return relative_paths, eligible

    async def sync_all_to_komga(self, *, force: bool | None = None) -> dict[str, Any]:
        """Converge Komga on every cached Tankarr title and selected artwork."""

        if self.komga is None:
            return {
                "configured": False,
                "synced": 0,
                "reason": "Managed Komga synchronization is disabled",
            }
        force_sync = self._komga_sync_required if force is None else bool(force)
        relative_paths, eligible = await asyncio.to_thread(self._reader_sync_inventory)

        catalogue = await self.komga.catalogue_for_paths(relative_paths)
        if not catalogue.get("configured"):
            return {
                "configured": False,
                "synced": 0,
                "eligible": len(eligible),
                "reason": "Komga is not configured",
            }
        missing = set(relative_paths) - set(catalogue.get("books", {}))
        if missing and hasattr(self.komga, "scan"):
            await self.komga.scan(relative_paths)
            catalogue = await self.komga.catalogue_for_paths(relative_paths)

        results: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        for manga_id in eligible:
            try:
                result = await self.sync_to_komga(
                    manga_id,
                    catalogue=catalogue,
                    force=force_sync,
                )
                results.append({"manga_id": manga_id, **result})
                if result.get("errors") or not result.get("synced"):
                    detail = (
                        (result.get("errors") or [{}])[0].get("error")
                        or result.get("reason")
                        or f"{result.get('missing_books', 0)} Komga books missing"
                    )
                    errors.append({"manga_id": manga_id, "error": str(detail)})
            except Exception as exc:  # noqa: BLE001 - retry the failed title later
                error = f"{type(exc).__name__}: {exc}"[:1000]
                self.database.record_series_sync_error(manga_id, error)
                errors.append({"manga_id": manga_id, "error": error})

        synced = sum(1 for result in results if result.get("synced"))
        complete = not errors and synced == len(eligible)
        # A forced policy sweep must happen once, not every maintenance minute.
        # Individual failures remain pending through the per-target sync state
        # and each series' last_synced_at marker, so later retries are narrow.
        if complete or force_sync:
            self.database.save_setting(
                KOMGA_SYNC_POLICY_SETTING_KEY, str(KOMGA_SYNC_POLICY_VERSION)
            )
            self._komga_sync_required = False
        return {
            "configured": True,
            "force": force_sync,
            "eligible": len(eligible),
            "synced": synced,
            "updated": sum(int(result.get("updated") or 0) for result in results),
            "artwork_uploaded": sum(
                int(result.get("artwork_uploaded") or 0) for result in results
            ),
            "errors": errors,
            "complete": complete,
        }

    async def sync_to_komga(
        self,
        manga_id: str,
        *,
        catalogue: dict[str, Any] | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        if self.komga is None:
            return {"synced": False, "reason": "No reader integration configured"}
        manga = self.database.get_manga(manga_id)
        series_metadata = self.database.get_series_metadata(manga_id)
        if series_metadata is None:
            return {"synced": False, "reason": "No canonical metadata"}
        chapters = [
            chapter
            for chapter in self.database.list_all_chapters(manga_id)
            if chapter.get("downloaded") and chapter.get("library_path")
        ]
        if not chapters:
            return {"synced": False, "reason": "No downloaded books in Komga"}
        relative_by_chapter = await asyncio.to_thread(
            self._reader_relative_paths, chapters
        )
        shared_catalogue = catalogue is not None
        if catalogue is None:
            catalogue = await self.komga.catalogue_for_paths(
                relative_by_chapter.values()
            )
        if not catalogue.get("configured"):
            return {"synced": False, "reason": "Komga is not configured"}
        missing_paths = set(relative_by_chapter.values()) - set(
            catalogue.get("books", {})
        )
        if missing_paths and not shared_catalogue and hasattr(self.komga, "scan"):
            await self.komga.scan(relative_by_chapter.values())
            catalogue = await self.komga.catalogue_for_paths(
                relative_by_chapter.values()
            )

        canonical = dict(series_metadata["data"])
        # Manual aliases and title overrides are first-class Tankarr state.
        # Komga must display the same current title, while the authoritative
        # catalogue title remains available in alternate titles/provenance.
        canonical["display_title"] = str(manga["title"])
        volume_by_key = {
            item["volume_key"]: item
            for item in self.database.list_volume_metadata(manga_id)
        }
        series_ids = {
            str(catalogue["books"][relative]["series_id"])
            for relative in relative_by_chapter.values()
            if relative in catalogue["books"]
            and catalogue["books"][relative].get("series_id")
        }
        updates: list[dict[str, Any]] = []
        sync_payload = bool(getattr(self.komga, "supports_catalogue_metadata", True))
        sync_artwork = bool(getattr(self.komga, "supports_catalogue_artwork", True))
        series_payload = self._komga_series_payload(canonical, chapters)
        for series_id in sorted(series_ids):
            updates.append(
                self._komga_update(
                    "series",
                    series_id,
                    series_payload,
                    series_metadata,
                    force=force,
                    sync_payload=sync_payload,
                    sync_artwork=sync_artwork,
                )
            )
        for chapter in chapters:
            relative = relative_by_chapter.get(chapter["id"])
            item = catalogue.get("books", {}).get(relative or "")
            if not item:
                continue
            volume = volume_by_key.get(publication_metadata_key(chapter, canonical))
            payload = self._komga_book_payload(chapter, canonical, volume)
            artwork = (
                volume
                if canonical.get("content_kind") == "comic"
                or chapter.get("chapter") in (None, "")
                else None
            )
            updates.append(
                self._komga_update(
                    "book",
                    str(item["id"]),
                    payload,
                    artwork,
                    force=force,
                    sync_payload=sync_payload,
                    sync_artwork=sync_artwork,
                )
            )

        pending = [
            update
            for update in updates
            if update.get("payload") or update.get("artwork_path")
        ]
        applied = await self.komga.apply_catalogue_metadata(pending)
        errors: list[dict[str, str]] = []
        for receipt in applied.get("receipts", []):
            if receipt.get("ok"):
                self.database.record_metadata_sync_state(
                    receipt["target_kind"],
                    receipt["target_id"],
                    payload_sha256=receipt.get("payload_sha256"),
                    artwork_sha256=receipt.get("artwork_sha256"),
                )
            else:
                error = str(receipt.get("error") or "Unknown Komga error")
                errors.append({"target_id": receipt["target_id"], "error": error})
                self.database.record_metadata_sync_state(
                    receipt["target_kind"],
                    receipt["target_id"],
                    payload_sha256=None,
                    artwork_sha256=None,
                    error=error,
                )
        missing_books = len(chapters) - (len(updates) - len(series_ids))
        fully_synced = not errors and bool(series_ids) and missing_books == 0
        if fully_synced:
            self.database.mark_series_metadata_synced(manga_id)
        elif errors:
            self.database.record_series_sync_error(manga_id, errors[0]["error"])
        return {
            "synced": fully_synced,
            "series": len(series_ids),
            "books": len(updates) - len(series_ids),
            "updated": applied.get("updated", 0),
            "artwork_uploaded": applied.get("artwork_uploaded", 0),
            "missing_books": missing_books,
            "errors": errors,
            "forced": force,
        }

    def _komga_update(
        self,
        target_kind: str,
        target_id: str,
        payload: dict[str, Any],
        artwork: dict[str, Any] | None,
        *,
        force: bool = False,
        sync_payload: bool = True,
        sync_artwork: bool = True,
    ) -> dict[str, Any]:
        payload_sha256 = _json_hash(payload) if sync_payload else None
        artwork_sha256 = (
            str((artwork or {}).get("artwork_sha256") or "") or None
            if sync_artwork
            else None
        )
        previous = self.database.get_metadata_sync_state(target_kind, target_id)
        payload_changed = bool(
            sync_payload
            and (
                force
                or not previous
                or previous.get("payload_sha256") != payload_sha256
            )
        )
        artwork_changed = bool(
            artwork_sha256
            and (
                force
                or not previous
                or previous.get("artwork_sha256") != artwork_sha256
            )
        )
        return {
            "target_kind": target_kind,
            "target_id": target_id,
            "payload": payload if payload_changed else None,
            "payload_sha256": payload_sha256,
            "artwork_path": (
                artwork.get("artwork_path") if artwork_changed and artwork else None
            ),
            "artwork_sha256": artwork_sha256,
            "artwork_media_type": (
                artwork.get("artwork_media_type")
                if artwork_changed and artwork
                else None
            ),
        }

    @staticmethod
    def _komga_series_payload(
        canonical: dict[str, Any], chapters: list[dict[str, Any]]
    ) -> dict[str, Any]:
        chapter_books = any(
            chapter.get("chapter") not in (None, "") for chapter in chapters
        )
        total_books = (
            canonical.get("chapter_count")
            if chapter_books
            else canonical.get("book_count") or canonical.get("volume_count")
        )
        display_title = str(canonical.get("display_title") or canonical["title"])
        payload: dict[str, Any] = {
            "title": display_title,
            "titleLock": True,
            "titleSort": display_title,
            "titleSortLock": True,
            "language": canonical.get("translation_language") or "en",
            "languageLock": True,
            "genres": canonical.get("genres") or [],
            "genresLock": True,
            "tags": canonical.get("tags") or [],
            "tagsLock": True,
            "links": canonical.get("links") or [],
            "linksLock": True,
            "alternateTitles": [
                {"label": "Alternative", "title": title}
                for title in canonical.get("alternate_titles") or []
            ],
            "alternateTitlesLock": True,
            # Tankarr owns this field. Sending an explicit empty value removes
            # stale scanner metadata when no matched source has a description.
            "summary": str(canonical.get("description") or ""),
            "summaryLock": True,
        }
        optional = {
            "publisher": canonical.get("publisher"),
            "readingDirection": canonical.get("reading_direction"),
            "status": {
                "ended": "ENDED",
                "completed": "ENDED",
                "ongoing": "ONGOING",
                "hiatus": "HIATUS",
                "abandoned": "ABANDONED",
                "cancelled": "ABANDONED",
            }.get(str(canonical.get("status") or "").casefold()),
            "totalBookCount": total_books,
        }
        for key, value in optional.items():
            if value not in (None, ""):
                payload[key] = value
                payload[f"{key}Lock"] = True
        return payload

    @staticmethod
    def _komga_book_payload(
        chapter: dict[str, Any],
        canonical: dict[str, Any],
        volume: dict[str, Any] | None,
    ) -> dict[str, Any]:
        volume_data = (volume or {}).get("data") or {}
        is_chapter = chapter.get("chapter") not in (None, "")
        is_comic = str(canonical.get("content_kind") or "").casefold() == "comic"
        book_data = volume_data if not is_chapter or is_comic else {}
        number = str(
            chapter.get("chapter") if is_chapter else chapter.get("volume") or "0"
        )
        title = (
            f"Issue {number}"
            if is_chapter and is_comic
            else f"Chapter {number}"
            if is_chapter
            else f"Volume {number}"
        )
        try:
            number_sort = float(number)
        except ValueError:
            number_sort = 0.0
        payload: dict[str, Any] = {
            "title": book_data.get("title") or title,
            "titleLock": True,
            "number": number,
            "numberLock": True,
            "numberSort": number_sort,
            "numberSortLock": True,
            "authors": _canonical_creator_credits(
                book_data.get("creators") or canonical.get("creators") or []
            ),
            "authorsLock": True,
            "tags": book_data.get("genres") or canonical.get("genres") or [],
            "tagsLock": True,
            "links": _book_external_links(canonical, book_data),
            "linksLock": True,
            # Do not retain a guessed or scanner-derived book description.
            "summary": str(book_data.get("description") or ""),
            "summaryLock": True,
        }
        release_date = book_data.get("release_date")
        optional = {
            "releaseDate": (
                release_date
                if re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(release_date or ""))
                else None
            ),
            "isbn": book_data.get("isbn"),
        }
        for key, value in optional.items():
            if value not in (None, ""):
                payload[key] = value
                payload[f"{key}Lock"] = True
        return payload

    def metadata_for(self, manga_id: str) -> dict[str, Any]:
        self.database.get_manga(manga_id)
        source_records = self.database.list_metadata_source_records(manga_id)
        return {
            "series": self.database.get_series_metadata(manga_id),
            "volumes": self.database.list_volume_metadata(manga_id),
            "sources": [
                {key: value for key, value in record.items() if key != "raw"}
                for record in source_records
            ],
        }

    def artwork_response(
        self, manga_id: str, volume: str | None = None
    ) -> tuple[Path, str, str | None]:
        self.database.get_manga(manga_id)
        if volume is None:
            metadata = self.database.get_series_metadata(manga_id)
        else:
            metadata = next(
                (
                    item
                    for item in self.database.list_volume_metadata(manga_id)
                    if item["volume_key"] == volume
                ),
                None,
            )
        if not metadata or not metadata.get("artwork_path"):
            raise FileNotFoundError("Metadata artwork is not available")
        path = (self.settings.data_dir / str(metadata["artwork_path"])).resolve()
        root = self.settings.data_dir.resolve()
        if root not in path.parents or not path.is_file() or path.is_symlink():
            raise FileNotFoundError("Metadata artwork is not available")
        return (
            path,
            str(metadata.get("artwork_media_type") or "image/jpeg"),
            str(metadata.get("artwork_sha256") or "") or None,
        )

    def artwork_candidates_for(self, manga_id: str) -> dict[str, Any]:
        """Return every cached, viable series cover and the durable choice."""

        self.database.get_manga(manga_id)
        metadata = self.database.get_series_metadata(manga_id)
        data = (metadata or {}).get("data") or {}
        preferred_id = self.database.get_series_artwork_preference(manga_id)
        rows = self.database.list_series_artwork_candidates(manga_id)
        automatic = next((row for row in rows if row["is_automatic"]), None)
        selected_id = str(data.get("artwork_candidate_id") or "") or None
        candidates: list[dict[str, Any]] = []
        for row in rows:
            if self._artwork_candidate_path(row) is None:
                continue
            candidate_id = str(row["candidate_id"])
            digest = str(row["artwork_sha256"])
            candidates.append(
                {
                    "candidate_id": candidate_id,
                    "source": str(row["source"]),
                    "source_url": str(row["source_url"]),
                    "image_url": (
                        f"/api/manga/{quote(manga_id, safe='')}/artwork-candidates/"
                        f"{quote(candidate_id, safe='')}?v={digest}"
                    ),
                    "artwork_sha256": digest,
                    "source_width": int(row["source_width"]),
                    "source_height": int(row["source_height"]),
                    "normalized_width": CANONICAL_ARTWORK_DIMENSIONS[0],
                    "normalized_height": CANONICAL_ARTWORK_DIMENSIONS[1],
                    "score": float(row["score"]),
                    "source_priority": int(row["source_priority"]),
                    "automatic": bool(row["is_automatic"]),
                    "selected": candidate_id == selected_id,
                }
            )
        return {
            "manga_id": manga_id,
            "selection_mode": "manual" if preferred_id else "automatic",
            "selected_candidate_id": selected_id,
            "automatic_candidate_id": (
                str(automatic["candidate_id"]) if automatic else None
            ),
            "candidates": candidates,
        }

    def artwork_candidate_response(
        self, manga_id: str, candidate_id: str
    ) -> tuple[Path, str, str]:
        self.database.get_manga(manga_id)
        candidate = next(
            (
                row
                for row in self.database.list_series_artwork_candidates(manga_id)
                if row["candidate_id"] == candidate_id
            ),
            None,
        )
        path = self._artwork_candidate_path(candidate)
        if candidate is None or path is None:
            raise FileNotFoundError("Artwork candidate is not available")
        return (
            path,
            str(candidate.get("artwork_media_type") or "image/jpeg"),
            str(candidate["artwork_sha256"]),
        )

    async def select_series_artwork(
        self, manga_id: str, candidate_id: str | None
    ) -> dict[str, Any]:
        """Apply Automatic or a cached manual cover to every published reader."""

        async with self._series_lock(manga_id):
            return await self._select_series_artwork_locked(manga_id, candidate_id)

    async def upload_series_artwork(
        self, manga_id: str, content: bytes
    ) -> dict[str, Any]:
        """Normalize, retain, and select one cover supplied by the user."""

        async with self._series_lock(manga_id):
            manga = self.database.get_manga(manga_id)
            if self.database.get_series_metadata(manga_id) is None:
                self.database.save_series_metadata(
                    manga_id,
                    self._merge_series(manga, []),
                    artwork_path=None,
                    artwork_sha256=None,
                    artwork_media_type=None,
                    source_status=[],
                )
            prepared = await asyncio.to_thread(self.artwork.prepare_bytes, content)
            digest = hashlib.sha256(bytes(prepared["content"])).hexdigest()
            candidate_id = f"upload-{digest[:24]}"
            stored = await asyncio.to_thread(
                self.artwork.store_prepared,
                manga_id,
                f"series-candidate-{candidate_id}",
                prepared,
            )
            self.database.upsert_series_artwork_candidate(
                manga_id,
                {
                    **stored,
                    "candidate_id": candidate_id,
                    "source": "upload",
                    "source_url": "user-upload",
                    "score": 0,
                    "source_priority": 0,
                },
            )
            return await self._select_series_artwork_locked(manga_id, candidate_id)

    async def _select_series_artwork_locked(
        self, manga_id: str, candidate_id: str | None
    ) -> dict[str, Any]:
        """Publish a cover while the per-series artwork lock is held."""

        self.database.get_manga(manga_id)
        metadata = self.database.get_series_metadata(manga_id)
        if metadata is None:
            raise ValueError("Refresh metadata before choosing a cover")
        candidates = self.database.list_series_artwork_candidates(manga_id)
        if not candidates:
            raise ValueError("No viable metadata covers are cached yet")
        normalized = str(candidate_id or "").strip() or None
        if normalized is None:
            selected = next((row for row in candidates if row["is_automatic"]), None)
        else:
            selected = next(
                (row for row in candidates if row["candidate_id"] == normalized),
                None,
            )
        if selected is None or self._artwork_candidate_path(selected) is None:
            raise ValueError("That cover candidate is no longer available")

        self.database.set_series_artwork_preference(manga_id, normalized)
        automatic = next((row for row in candidates if row["is_automatic"]), None)
        artwork = {
            **self._candidate_as_artwork(selected),
            "candidate_signature": (
                (metadata.get("data") or {}).get("artwork_candidate_signature")
            ),
            "candidates_evaluated": len(candidates),
            "candidate_id": str(selected["candidate_id"]),
            "automatic_candidate_id": (
                str(automatic["candidate_id"]) if automatic else None
            ),
            "selection_mode": "manual" if normalized else "automatic",
            "selection_version": ARTWORK_SELECTION_VERSION,
        }
        canonical = dict(metadata["data"])
        canonical["cover_url"] = series_artwork_url(manga_id, artwork["sha256"])
        self._apply_artwork_metadata(canonical, artwork)
        saved = self.database.save_series_metadata(
            manga_id,
            canonical,
            artwork_path=str(artwork["path"]),
            artwork_sha256=str(artwork["sha256"]),
            artwork_media_type=str(artwork["media_type"]),
            source_status=list(metadata.get("source_status") or []),
        )
        library = (
            await self.library_publisher(manga_id)
            if self.library_publisher is not None
            else {"reader_independent": True, "updated": 0, "errors": []}
        )
        komga = (
            await self.sync_to_komga(manga_id, force=True)
            if self.komga is not None
            else None
        )
        return {
            "selection": self.artwork_candidates_for(manga_id),
            "metadata": saved,
            "library": library,
            "komga": komga,
        }

    def _artwork_candidate_path(self, candidate: dict[str, Any] | None) -> Path | None:
        if not candidate or not candidate.get("artwork_path"):
            return None
        path = (self.settings.data_dir / str(candidate["artwork_path"])).resolve()
        root = self.settings.data_dir.resolve()
        if root not in path.parents or not path.is_file() or path.is_symlink():
            return None
        return path

    @staticmethod
    def _candidate_as_artwork(candidate: dict[str, Any]) -> dict[str, Any]:
        return {
            "path": str(candidate["artwork_path"]),
            "sha256": str(candidate["artwork_sha256"]),
            "media_type": str(candidate.get("artwork_media_type") or "image/jpeg"),
            "source": str(candidate["source"]),
            "source_url": str(candidate["source_url"]),
            "source_width": int(candidate["source_width"]),
            "source_height": int(candidate["source_height"]),
            "normalized_width": CANONICAL_ARTWORK_DIMENSIONS[0],
            "normalized_height": CANONICAL_ARTWORK_DIMENSIONS[1],
            "score": float(candidate["score"]),
            "source_priority": int(candidate["source_priority"]),
            "normalization_version": ARTWORK_NORMALIZATION_VERSION,
        }

    @staticmethod
    def _apply_artwork_metadata(
        target: dict[str, Any], artwork: dict[str, Any] | None
    ) -> None:
        target["artwork_source"] = artwork.get("source") if artwork else None
        target["artwork_source_url"] = artwork.get("source_url") if artwork else None
        target["artwork_source_width"] = (
            int(artwork["source_width"])
            if artwork and artwork.get("source_width") is not None
            else None
        )
        target["artwork_source_height"] = (
            int(artwork["source_height"])
            if artwork and artwork.get("source_height") is not None
            else None
        )
        target["artwork_normalized_width"] = (
            int(artwork["normalized_width"])
            if artwork and artwork.get("normalized_width") is not None
            else None
        )
        target["artwork_normalized_height"] = (
            int(artwork["normalized_height"])
            if artwork and artwork.get("normalized_height") is not None
            else None
        )
        target["artwork_score"] = (
            float(artwork["score"])
            if artwork and artwork.get("score") is not None
            else None
        )
        target["artwork_candidates_evaluated"] = (
            int(artwork["candidates_evaluated"])
            if artwork and artwork.get("candidates_evaluated") is not None
            else None
        )
        target["artwork_candidate_id"] = (
            str(artwork["candidate_id"])
            if artwork and artwork.get("candidate_id")
            else None
        )
        target["artwork_automatic_candidate_id"] = (
            str(artwork["automatic_candidate_id"])
            if artwork and artwork.get("automatic_candidate_id")
            else None
        )
        target["artwork_selection_mode"] = (
            str(artwork.get("selection_mode") or "automatic") if artwork else None
        )
        target["artwork_source_priority"] = (
            int(artwork["source_priority"])
            if artwork and artwork.get("source_priority") is not None
            else None
        )
        target["artwork_candidate_signature"] = (
            str(artwork["candidate_signature"])
            if artwork and artwork.get("candidate_signature")
            else None
        )
        target["artwork_selection_version"] = (
            int(artwork.get("selection_version") or ARTWORK_SELECTION_VERSION)
            if artwork
            else None
        )
        target["artwork_normalization_version"] = (
            int(artwork.get("normalization_version") or ARTWORK_NORMALIZATION_VERSION)
            if artwork
            else None
        )

    def status(self) -> dict[str, Any]:
        monitor_active = bool(self.task is not None and not self.task.done())
        running = bool(
            self.current_manga_id
            or self._refresh_lock.locked()
            or (self.bulk_task is not None and not self.bulk_task.done())
        )
        return {
            "enabled": self.settings.metadata_enabled,
            "running": running,
            "monitor_active": monitor_active,
            "current_manga_id": self.current_manga_id,
            "last_cycle_at": self.last_cycle_at,
            "last_cycle_error": self.last_cycle_error,
            "last_cycle_result": self.last_cycle_result,
            "last_title_migration": self.last_title_migration,
            "refresh_interval_hours": self.settings.metadata_refresh_interval_hours,
            "match_policy_version": MATCH_POLICY_VERSION,
            "match_policy_refresh_pending": self._policy_refresh_required,
            "queued_refresh": self._bulk_refresh_pending,
            "coverage": self.database.metadata_overview(),
            "sources": [source.status() for source in self.sources],
        }

    def _cached_artwork(
        self,
        manga_id: str,
        key: str,
        metadata: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        if not metadata or not (
            metadata.get("artwork_path")
            and metadata.get("artwork_sha256")
            and metadata.get("artwork_media_type")
        ):
            return None
        path = (self.settings.data_dir / str(metadata["artwork_path"])).resolve()
        root = self.settings.data_dir.resolve()
        if root not in path.parents or not path.is_file() or path.is_symlink():
            return None
        try:
            content = path.read_bytes()
            actual_sha256 = hashlib.sha256(content).hexdigest()
            if not self.artwork.is_canonical(path) or actual_sha256 != str(
                metadata["artwork_sha256"]
            ):
                stored = self.artwork.store_bytes(
                    manga_id, key, content, trusted_local=True
                )
            else:
                stored = {
                    "path": str(metadata["artwork_path"]),
                    "sha256": actual_sha256,
                    "media_type": "image/jpeg",
                    "normalized_width": CANONICAL_ARTWORK_DIMENSIONS[0],
                    "normalized_height": CANONICAL_ARTWORK_DIMENSIONS[1],
                    "normalization_version": ARTWORK_NORMALIZATION_VERSION,
                }
        except (OSError, ValueError):
            return None
        data = metadata.get("data", {})
        for field in (
            "source_width",
            "source_height",
            "source_bytes",
            "score",
            "source_priority",
            "candidate_signature",
            "candidates_evaluated",
            "selection_version",
            "candidate_id",
            "automatic_candidate_id",
            "selection_mode",
        ):
            persisted = data.get(f"artwork_{field}")
            if persisted is not None and field not in stored:
                stored[field] = persisted
        return {
            **stored,
            "source": str(data.get("artwork_source") or "cached"),
            "source_url": str(data.get("artwork_source_url") or ""),
        }

    @staticmethod
    def _artwork_candidate_signature(candidates: list[dict[str, Any]]) -> str:
        return _json_hash(
            sorted(
                (
                    {
                        "source": str(candidate.get("source") or ""),
                        "identifier": str(candidate.get("identifier") or ""),
                        "reported_width": int(candidate.get("reported_width") or 0),
                        "reported_height": int(candidate.get("reported_height") or 0),
                    }
                    for candidate in candidates
                ),
                key=lambda item: (item["source"], item["identifier"]),
            )
        )

    @classmethod
    def _artwork_quality(
        cls,
        source: str,
        prepared: dict[str, Any],
        *,
        work_type: object | None,
        entity_kind: str,
    ) -> dict[str, float | int]:
        width = max(1, int(prepared["source_width"]))
        height = max(1, int(prepared["source_height"]))
        ratio = width / height
        ratio_error = abs(math.log(ratio / CANONICAL_ARTWORK_RATIO))
        aspect_score = max(0.0, 48.0 - ratio_error * 90.0)
        orientation_penalty = 0.0
        if ratio >= 0.95 or ratio <= 0.40:
            orientation_penalty = 45.0
        elif ratio > 0.86 or ratio < 0.48:
            orientation_penalty = 15.0
        coverage = min(
            width / CANONICAL_ARTWORK_DIMENSIONS[0],
            height / CANONICAL_ARTWORK_DIMENSIONS[1],
            1.0,
        )
        resolution_score = 30.0 * math.sqrt(max(0.0, coverage))
        tiny_penalty = 0.0
        if width < 120 or height < 180:
            tiny_penalty = 20.0
        elif width < 250 or height < 375:
            tiny_penalty = 10.0
        detail = float(prepared.get("detail_stddev") or 0.0)
        detail_penalty = 30.0 if detail < 4.0 else 8.0 if detail < 10.0 else 0.0
        source_priority = cls._cover_priority(
            source, work_type=work_type, entity_kind=entity_kind
        )
        score = (
            source_priority
            + aspect_score
            + resolution_score
            - orientation_penalty
            - tiny_penalty
            - detail_penalty
        )
        return {
            "score": score,
            "source_priority": source_priority,
            "aspect_score": aspect_score,
            "resolution_score": resolution_score,
            "orientation_penalty": orientation_penalty,
            "tiny_penalty": tiny_penalty,
            "detail_penalty": detail_penalty,
        }

    @staticmethod
    def _number_sort_key(value: str) -> tuple[int, float, str]:
        try:
            return (0, float(value), value)
        except ValueError:
            return (1, 0.0, value.casefold())

    @staticmethod
    def _positive_integer(value: object) -> int | None:
        try:
            number = int(float(str(value)))
            return number if number > 0 else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _title_record_priority(
        record: dict[str, Any], work_type: object | None = None
    ) -> int:
        """Prefer catalogue titles intended to identify the work itself.

        MangaUpdates is the broadest work catalogue for manga, manhwa, manhua,
        and webtoons and generally exposes a useful English catalogue title.
        MyAnimeList and MangaDex remain verified fallbacks. Comic Vine owns
        western-comic run identity. A manual Tankarr override is applied later
        and therefore never participates in catalogue matching.
        """

        source = str(record.get("source") or "").casefold()
        is_comic = str(work_type or record.get("work_type") or "").casefold() == "comic"
        if is_comic:
            return {
                "comicvine": 140,
                "local": 100,
                "mangaupdates": 70,
                "myanimelist": 65,
                "mangadex": 60,
                "google_books": 115,
            }.get(source, 50)
        return {
            "mangabaka": 150,
            "mangaupdates": 140,
            "myanimelist": 130,
            "anilist": 128,
            "mangadex": 120,
            "google_books": 115,
            "local": 100,
            "suwayomi": 90,
            "mangapill": 80,
            "comicvine": 60,
        }.get(source, 50)

    @classmethod
    def _title_selection_key(
        cls,
        record: dict[str, Any],
        *,
        manga: dict[str, Any],
        work_type: object | None,
    ) -> tuple[int, int]:
        """Rank source authority, with explicit requested-language titles first."""

        language = str(manga.get("preferred_language") or "en").casefold()
        localized = record.get("localized_titles") or {}
        localized_title = str(localized.get(language) or "").strip()
        base = cls._title_record_priority(record, work_type=work_type)
        source = str(record.get("source") or "").casefold()
        # MAL and MangaDex label localized title fields explicitly. That is
        # stronger display-title evidence than an unlabelled romanization, but
        # does not change which catalogue owns work identity/status/counts.
        localized_bonus = (
            30
            if localized_title and source in {"mangabaka", "myanimelist", "mangadex"}
            else 0
        )
        return (base + localized_bonus, int(bool(localized_title)))

    @classmethod
    def _select_display_title(
        cls,
        manga: dict[str, Any],
        records: list[dict[str, Any]],
        record: dict[str, Any] | None,
        *,
        work_type: object | None = None,
        canonical_year: object | None = None,
    ) -> dict[str, Any]:
        """Select presentation title without changing automatic work identity.

        Explicit preferred-language catalogue fields are strongest.  When a
        catalogue only exposes an unlabelled romanized primary title, Tankarr
        may keep the managed provider's English title, but only when an
        accepted work catalogue independently lists that exact title as a
        primary title or alias.  Original and romanized names remain aliases.
        """

        language = str(manga.get("preferred_language") or "en").casefold()
        selected = record or {}
        localized = str(
            (selected.get("localized_titles") or {}).get(language) or ""
        ).strip()
        if localized:
            return {
                "title": cls._without_redundant_title_qualifiers(
                    localized, selected, canonical_year=canonical_year
                ),
                "record": selected,
                "source": str(selected.get("source") or "local"),
                "language": language,
                "localized": True,
                "strategy": "localized",
            }

        provider_title = _manga_identity_title(manga).strip()
        verified_catalogues = {
            "mangaupdates",
            "myanimelist",
            "mangadex",
            "comicvine",
            "google_books",
        }
        provider_variants = work_title_variants(provider_title)
        corroborating_records = [
            candidate
            for candidate in records
            if language == "en"
            and provider_variants
            and str(candidate.get("source") or "").casefold() in verified_catalogues
            and any(
                provider_variants & work_title_variants(value)
                for value in (
                    candidate.get("title"),
                    *(candidate.get("localized_titles") or {}).values(),
                    *(candidate.get("catalogue_alternate_titles") or []),
                )
            )
        ]
        if corroborating_records:
            corroborating_record = max(
                corroborating_records,
                key=lambda item: cls._title_selection_key(
                    item,
                    manga=manga,
                    work_type=work_type,
                ),
            )
            provider_display = cls._without_redundant_title_qualifiers(
                provider_title,
                corroborating_record,
                canonical_year=canonical_year,
            )
            catalogue_display = cls._without_redundant_title_qualifiers(
                str(selected.get("title") or "").strip(),
                selected,
                canonical_year=canonical_year,
            )
            # Prefer the catalogue's spelling and fuller form when both names
            # are lexical variants (for example Harbor/Harbour or Sarah/The
            # Legend of Mother Sarah). A dissimilar corroborated alias is the
            # translated English title of an otherwise romanized work.
            if catalogue_display and cls._lexically_related_titles(
                provider_display, catalogue_display
            ):
                return {
                    "title": catalogue_display,
                    "record": selected,
                    "source": str(selected.get("source") or "local"),
                    "language": None,
                    "localized": False,
                    "strategy": "catalogue_primary",
                }
            return {
                "title": provider_display,
                "record": corroborating_record,
                "source": str(corroborating_record.get("source") or "local"),
                "language": language,
                "localized": True,
                "strategy": "verified_provider_alias",
            }

        primary = cls._without_redundant_title_qualifiers(
            str(selected.get("title") or "").strip(),
            selected,
            canonical_year=canonical_year,
        )
        return {
            "title": primary,
            "record": selected,
            "source": str(selected.get("source") or manga.get("provider") or "local"),
            "language": None,
            "localized": False,
            "strategy": "catalogue_primary" if selected else "provider_fallback",
        }

    @staticmethod
    def _lexically_related_titles(left: object, right: object) -> bool:
        left_title = normalized_title(left)
        right_title = normalized_title(right)
        if not left_title or not right_title:
            return False
        if work_title_variants(left_title) & work_title_variants(right_title):
            return True
        left_tokens = set(left_title.split())
        right_tokens = set(right_title.split())
        containment = len(left_tokens & right_tokens) / min(
            len(left_tokens), len(right_tokens)
        )
        return (
            containment >= 0.8
            or SequenceMatcher(None, left_title, right_title).ratio() >= 0.84
        )

    @staticmethod
    def _without_redundant_title_qualifiers(
        title: str,
        record: dict[str, Any],
        *,
        canonical_year: object | None = None,
    ) -> str:
        """Remove trailing creator/year qualifiers already stored as metadata.

        Parentheticals such as ``(Colored)`` and edition names remain part of
        the title. A year is removed only when it agrees with the structured
        year selected for the work.
        """

        creators = _unique_people(
            [
                *(record.get("authors") or []),
                *(
                    creator.get("name")
                    for creator in record.get("creators") or []
                    if creator.get("name")
                ),
            ]
        )
        expected_year = str(canonical_year or record.get("year") or "").strip()
        normalized = title.strip()
        while match := re.fullmatch(
            r"(?P<title>.+?)\s*\((?P<qualifier>[^()]+)\)\s*", normalized
        ):
            qualifier = match.group("qualifier").strip()
            is_creator = any(
                creator_name_similarity(qualifier, creator) >= 0.90
                for creator in creators
            )
            is_known_year = bool(
                expected_year
                and re.fullmatch(r"\d{4}", qualifier)
                and qualifier == expected_year[:4]
            )
            if not (is_creator or is_known_year):
                break
            candidate = match.group("title").strip()
            if not candidate:
                break
            normalized = candidate
        return normalized or title

    @staticmethod
    def _record_priority(
        record: dict[str, Any], work_type: object | None = None
    ) -> int:
        source = str(record.get("source") or "")
        is_comic = str(work_type or record.get("work_type") or "").casefold() == "comic"
        if is_comic:
            return {
                "comicvine": 120,
                "google_books": 110,
                "local": 95,
                "mangaupdates": 70,
                "myanimelist": 65,
                "mangadex": 60,
            }.get(source, 50)
        return {
            # MangaUpdates owns the broadest work-level catalogue for manga,
            # manhwa, manhua and OEL/webtoon material. MAL and MangaDex remain
            # successively weaker fallbacks for status, creators and work type.
            # MangaBaka is the source of truth for descriptive metadata; the
            # catalogues it links are followers reached by ID.
            "mangabaka": 125,
            "mangaupdates": 120,
            "anilist": 116,
            "myanimelist": 115,
            "mangadex": 110,
            "google_books": 110,
            "local": 100,
            "comicvine": 60,
            "suwayomi": 55,
        }.get(source, 50)

    @classmethod
    def _standalone_publication_status(
        cls,
        manga: dict[str, Any],
        records: list[dict[str, Any]],
        *,
        work_type: object,
        issue_count: object,
    ) -> dict[str, str] | None:
        """Infer only a closed, exact standalone publication as ended.

        Google Books describes published editions, not whether an arbitrary
        series is still running. Its evidence is therefore used
        only when Tankarr manages one whole-volume file and a specialist
        catalogue independently confirms a one-issue comic, or the identity is
        explicitly a book/novel/one-shot. A numbered volume within a larger
        managed series never satisfies this rule.
        """

        if (
            cls._positive_integer(manga.get("numbered_volume_count")) != 1
            or int(manga.get("numbered_chapter_count") or 0) != 0
        ):
            return None
        exact_editions = [
            record
            for record in records
            if str(record.get("source") or "").casefold() == "google_books"
            and str(
                (record.get("publication_context") or {}).get("scope") or ""
            ).casefold()
            == "edition"
            and any(
                (record.get("publication_context") or {}).get(field)
                for field in ("edition_key", "isbn")
            )
        ]
        if not exact_editions:
            return None

        normalized_type = normalized_title(work_type).replace(" ", "_")
        edition_source = str(exact_editions[0].get("source") or "edition")
        if normalized_type in {"book", "novel", "light_novel", "one_shot"}:
            return {
                "source": edition_source,
                "reason": "exact standalone publication edition",
            }

        if normalized_type in {
            "manga",
            "manhwa",
            "manhua",
            "webtoon",
            "doujinshi",
        }:
            contradicts_standalone = any(
                _catalogue_scope(record) == "work"
                and (
                    (cls._positive_integer(record.get("volume_count")) or 0) > 1
                    or normalized_title(record.get("status"))
                    in {"ongoing", "publishing", "continuing", "releasing"}
                )
                for record in records
            )
            if not contradicts_standalone and cls._single_volume_fallback(
                manga, records
            ):
                return {
                    "source": edition_source,
                    "reason": "verified standalone manga edition",
                }

        comicvine_single_issue = any(
            str(record.get("source") or "").casefold() == "comicvine"
            and cls._positive_integer(record.get("issue_count")) == 1
            for record in records
        )
        if normalized_type in {"comic", "graphic_novel"} and (
            cls._positive_integer(issue_count) == 1 and comicvine_single_issue
        ):
            return {
                "source": f"comicvine+{edition_source}",
                "reason": "exact edition and one-issue catalogue run",
            }
        if (
            normalized_type in {"comic", "graphic_novel"}
            and not any(
                str(record.get("source") or "").casefold() == "comicvine"
                for record in records
            )
            and cls._single_volume_fallback(manga, records)
        ):
            return {
                "source": edition_source,
                "reason": "verified standalone graphic edition",
            }
        return None

    @staticmethod
    def _resolve_work_type(
        manga: dict[str, Any], records: list[dict[str, Any]]
    ) -> str | None:
        """Classify from origin and specialist catalogues.

        Comic Vine necessarily labels every result as a comic. It may not turn
        a MangaDex title, or a title corroborated by a manga catalogue, into a
        western comic.
        """

        language = str(manga.get("original_language") or "").casefold()
        if language == "ja":
            return "Manga"
        if language == "ko":
            return "Manhwa"
        if language == "zh":
            return "Manhua"
        if str(manga.get("provider") or "").casefold() == "mangadex":
            return "Manga"

        by_source = {str(record.get("source") or ""): record for record in records}
        for source in ("myanimelist", "mangaupdates", "mangadex"):
            value = str((by_source.get(source) or {}).get("work_type") or "").strip()
            if value:
                return value
        for source in (
            str(manga.get("provider") or ""),
            "comicvine",
        ):
            value = str((by_source.get(source) or {}).get("work_type") or "").strip()
            if value:
                return value
        return next(
            (str(record["work_type"]) for record in records if record.get("work_type")),
            None,
        )

    @staticmethod
    def _content_kind_for_type(work_type: object) -> str | None:
        normalized = normalized_title(work_type).replace(" ", "_")
        if normalized in {"manga", "one_shot", "doujinshi"}:
            return "manga"
        if normalized in {
            "manhwa",
            "manhua",
            "webtoon",
            "oel",
            "original_english_language",
        }:
            return "webtoon"
        if normalized in {"comic", "comics", "graphic_novel"}:
            return "comic"
        if normalized in {"book", "novel", "light_novel"}:
            return "book"
        return None

    @classmethod
    def _classify_content(
        cls, manga: dict[str, Any], records: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Classify the work family from trusted origin/catalogue evidence."""

        provider = str(manga.get("provider") or "").casefold()
        by_source = {str(record.get("source") or ""): record for record in records}
        for source in (
            "mangabaka",
            "mangaupdates",
            "anilist",
            "myanimelist",
            provider,
            "mangadex",
        ):
            record = by_source.get(source) or {}
            work_type = record.get("work_type")
            kind = cls._content_kind_for_type(work_type)
            if kind:
                return {
                    "kind": kind,
                    "subtype": str(work_type),
                    "source": source or "origin",
                    "confidence": 0.99,
                    "reason": f"{source or 'origin'} work type is {work_type}",
                }

        language = str(manga.get("original_language") or "").casefold()
        if language == "ja":
            return {
                "kind": "manga",
                "subtype": "Manga",
                "source": provider or "origin",
                "confidence": 0.96,
                "reason": "original language is Japanese",
            }
        if language in {"ko", "zh"}:
            subtype = "Manhwa" if language == "ko" else "Manhua"
            return {
                "kind": "webtoon",
                "subtype": subtype,
                "source": provider or "origin",
                "confidence": 0.92,
                "reason": f"original language is {language}",
            }

        return {
            "kind": "unknown",
            "subtype": None,
            "source": None,
            "confidence": 0.0,
            "reason": "no trusted work-family evidence",
        }

    @classmethod
    def _source_applicability(
        cls,
        manga: dict[str, Any],
        records: list[dict[str, Any]],
        source: MetadataSource,
        attempted: set[str] | None = None,
    ) -> tuple[bool, str]:
        classification = cls._classify_content(manga, records)
        kind = classification["kind"]
        if kind == "book" and source.name in {
            "mangabaka",
            "mangaupdates",
            "anilist",
            "myanimelist",
            "comicvine",
        }:
            return False, f"{source.label} is not used for confirmed books"
        if kind == "comic" and source.name in {
            "mangabaka",
            "mangaupdates",
            "anilist",
            "myanimelist",
        }:
            return False, f"{source.label} is not used for confirmed western comics"
        return True, ""

    @classmethod
    def _cover_priority(
        cls,
        source: str,
        *,
        work_type: object | None,
        entity_kind: str,
    ) -> int:
        source = source.casefold().split("/", 1)[0]
        if entity_kind == "volume":
            return {
                "comicvine": 60,
                "local": 28,
                "mangadex": 27,
                "suwayomi": 26,
                "mangapill": 24,
                "myanimelist": 20,
                "mangaupdates": 18,
                "google_books": 64,
            }.get(source, 20)
        if str(work_type or "").casefold() in {"book", "novel", "light_novel"}:
            return {
                "google_books": 42,
                "local": 26,
                "comicvine": 18,
                "myanimelist": 16,
                "mangadex": 14,
                "mangaupdates": 14,
            }.get(source, 18)
        if str(work_type or "").casefold() == "comic":
            return {
                "comicvine": 36,
                "local": 26,
                "suwayomi": 24,
                "mangapill": 22,
                "myanimelist": 14,
                "mangaupdates": 14,
                "mangadex": 14,
                "google_books": 38,
            }.get(source, 18)
        return {
            # MangaBaka's cover is the default: authority must outweigh the
            # whole quality band (aspect + resolution ≤ 78) so another
            # catalogue wins only when MangaBaka's image is unusable or the
            # operator chooses otherwise in the cover chooser.
            "mangabaka": 110,
            "myanimelist": 34,
            "mangadex": 32,
            "mangaupdates": 30,
            "anilist": 29,
            "local": 24,
            "suwayomi": 22,
            "mangapill": 20,
            "comicvine": 12,
            # Book catalogues are edition fallbacks for a confirmed manga; they
            # must not replace artwork from a specialist manga catalogue.
            "google_books": 28,
        }.get(source, 18)
