from __future__ import annotations

import re
import unicodedata
from decimal import Decimal, InvalidOperation
from pathlib import Path

from tankarr.source_numbering import numbered_prologue
from tankarr.torrent_sources import EXTERNAL_IMPORT_PROVIDERS

WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

UNKNOWN_AUTHOR = "Unknown Author"
UNKNOWN_CHAPTER = "Unknown"
UNKNOWN_SERIES = "Unknown Series"
UNKNOWN_VOLUME = "Unknown"
UNKNOWN_LANGUAGE = "und"
NUMBER_WIDTH = 3
LIBRARY_NAMING_VERSION = 2
LIBRARY_NAMING_FORMAT = (
    "{Series} ({Authors})/{Series} - v{Volume} c{Chapter} [{Language}].cbz"
)

# Keep the complete filename below the 255-byte component limit of common
# filesystems, wherever the library storage lives.
# Each semantic field has its own budget so truncation can never remove the
# chapter, volume, or language tokens from the end of the filename.
SERIES_DIRECTORY_BYTES = 112
AUTHOR_DIRECTORY_BYTES = 80
SERIES_FILENAME_BYTES = 120
VOLUME_TOKEN_BYTES = 20
CHAPTER_TOKEN_BYTES = 44
LANGUAGE_TOKEN_BYTES = 16


def _truncate_utf8(value: str, max_bytes: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def safe_component(
    value: object,
    fallback: str = "Unknown",
    max_length: int = 150,
    max_bytes: int = 240,
) -> str:
    value = unicodedata.normalize("NFKC", str(value or ""))
    value = re.sub(r"[\x00-\x1f<>:\"/\\|?*]", " ", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    if not value:
        value = fallback
    if value.upper() in WINDOWS_RESERVED:
        value = f"_{value}"
    value = value[:max_length].rstrip(" .")
    value = _truncate_utf8(value, max_bytes).rstrip(" .")
    return value or fallback


def _sortable_number(value: object, *, fallback: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return fallback
    number_range = re.fullmatch(r"(-?\d+(?:\.\d+)?)\s*[-–]\s*(-?\d+(?:\.\d+)?)", raw)
    if number_range is not None:
        start = _sortable_number(number_range.group(1), fallback=fallback)
        end = _sortable_number(number_range.group(2), fallback=fallback)
        return f"{start}-{end}"
    try:
        number = Decimal(raw)
    except (InvalidOperation, ValueError):
        return safe_component(raw, fallback=fallback, max_length=44, max_bytes=44)
    if not number.is_finite():
        return safe_component(raw, fallback=fallback, max_length=44, max_bytes=44)

    normalized = format(number, "f")
    if "." in normalized:
        normalized = normalized.rstrip("0").rstrip(".")
    sign = ""
    if normalized.startswith("-"):
        sign, normalized = "-", normalized[1:]
    integer, separator, fraction = normalized.partition(".")
    padded = integer.zfill(NUMBER_WIDTH)
    return f"{sign}{padded}{separator}{fraction}" if separator else f"{sign}{padded}"


def author_label(manga: dict) -> str:
    authors = [
        safe_component(author, fallback="", max_length=80, max_bytes=80)
        for author in manga.get("authors", [])
        if str(author or "").strip()
    ]
    return ", ".join(author for author in authors if author) or UNKNOWN_AUTHOR


def series_label(manga: dict) -> str:
    return safe_component(
        manga.get("title"),
        fallback=UNKNOWN_SERIES,
        max_length=150,
        max_bytes=SERIES_DIRECTORY_BYTES,
    )


def series_directory_name(manga: dict) -> str:
    series = series_label(manga)
    authors = safe_component(
        author_label(manga),
        fallback=UNKNOWN_AUTHOR,
        max_length=100,
        max_bytes=AUTHOR_DIRECTORY_BYTES,
    )
    return safe_component(
        f"{series} ({authors})",
        fallback=f"{UNKNOWN_SERIES} ({UNKNOWN_AUTHOR})",
        max_length=240,
        max_bytes=240,
    )


def volume_token(chapter: dict) -> str:
    return safe_component(
        _sortable_number(chapter.get("volume"), fallback=UNKNOWN_VOLUME),
        fallback=UNKNOWN_VOLUME,
        max_length=VOLUME_TOKEN_BYTES,
        max_bytes=VOLUME_TOKEN_BYTES,
    )


def chapter_token(chapter: dict) -> str:
    prologue = numbered_prologue(chapter.get("title"))
    if prologue is not None:
        return f"000-prologue-{prologue:03d}"
    raw_number = chapter.get("chapter")
    if raw_number is not None and str(raw_number).strip():
        value = _sortable_number(raw_number, fallback=UNKNOWN_CHAPTER)
    else:
        stable_id = safe_component(
            chapter.get("id"), fallback="", max_length=36, max_bytes=36
        )
        value = f"{UNKNOWN_CHAPTER}-{stable_id}" if stable_id else UNKNOWN_CHAPTER
    return safe_component(
        value,
        fallback=UNKNOWN_CHAPTER,
        max_length=CHAPTER_TOKEN_BYTES,
        max_bytes=CHAPTER_TOKEN_BYTES,
    )


def language_token(chapter: dict, language: str | None = None) -> str:
    return safe_component(
        language or chapter.get("language"),
        fallback=UNKNOWN_LANGUAGE,
        max_length=LANGUAGE_TOKEN_BYTES,
        max_bytes=LANGUAGE_TOKEN_BYTES,
    ).lower()


def chapter_filename(manga: dict, chapter: dict, language: str | None = None) -> str:
    """Naming v2: `{Series} - v{Volume} c{Chapter} [{lang}].cbz`.

    The author lives in the series directory name only. Tokens without a
    value are dropped: a local volume archive becomes `Series - v027 [en]`,
    while chapterless provider releases keep their stable-id chapter token so
    two parallel releases in the same volume can never collide on disk.
    """

    series = safe_component(
        series_label(manga),
        fallback=UNKNOWN_SERIES,
        max_length=120,
        max_bytes=SERIES_FILENAME_BYTES,
    )
    has_volume = bool(str(chapter.get("volume") or "").strip())
    has_number = bool(str(chapter.get("chapter") or "").strip())
    tokens: list[str] = []
    if has_volume:
        tokens.append(f"v{volume_token(chapter)}")
    keep_chapter_token = has_number or not (
        has_volume
        and (
            chapter.get("provider") == "local"
            or chapter.get("provider") in EXTERNAL_IMPORT_PROVIDERS
            or chapter.get("release_unit") == "volume"
        )
    )
    if keep_chapter_token:
        tokens.append(f"c{chapter_token(chapter)}")
    middle = " ".join(tokens)
    return f"{series} - {middle} [{language_token(chapter, language)}].cbz"


def final_library_path(library_dir: Path, manga: dict, chapter: dict) -> Path:
    return (
        library_dir
        / series_directory_name(manga)
        / chapter_filename(manga, chapter, chapter.get("language"))
    )
