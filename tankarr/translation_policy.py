"""Language and slot rules shared by automatic and manual translation fallback."""

from __future__ import annotations

from urllib.parse import urlsplit

from tankarr.chapter_mapping import canonical_number
from tankarr.languages import SUPPORTED_LANGUAGE_CODES, normalize_language_code


def normalize_fallback_languages(raw: str) -> str:
    # Retained for old API clients and saved settings; no longer a search filter.
    if raw.strip().casefold() == "auto":
        return "auto"
    values = [
        "original"
        if item.strip().casefold() == "original"
        else normalize_language_code(item)
        for item in raw.split(",")
        if item.strip()
    ]
    if not values or len(values) > 12:
        raise ValueError("Choose between 1 and 12 fallback languages")
    return ",".join(dict.fromkeys(values))


def fallback_languages(manga: dict, default: str = "auto") -> list[str]:
    """All known source languages, ranked internally, never an operator list.

    ``default`` and the old per-series field are accepted for compatibility but
    cannot exclude a usable edition. Discovery rotates through this order in
    bounded batches; known candidates are considered in every pass.
    """
    original = str(manga.get("original_language") or "").casefold()
    available = set(manga.get("available_languages") or [])
    return sorted(
        SUPPORTED_LANGUAGE_CODES - {manga["preferred_language"]},
        key=lambda language: (
            language != original,
            language not in available,
            language,
        ),
    )


def processor_languages(manga: dict, settings) -> list[str]:
    languages = fallback_languages(manga)
    if not settings.translation_processor_url:
        from tankarr.local_translation import OCR_LANGUAGES

        languages = [language for language in languages if language in OCR_LANGUAGES]
    return languages


def source_rank(source: dict, manga: dict) -> tuple:
    """Prefer an owned source, original language, then availability and cost."""
    return (
        not bool(source.get("downloaded") and source.get("library_path")),
        source.get("language") != manga.get("original_language"),
        -float(source.get("match_score") or 0),
        -int(source.get("seeders") or 0),
        int(source.get("size_bytes") or 0),
        str(source.get("id") or ""),
    )


def validate_translation_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "Translation URLs must use HTTP(S), without credentials, query or fragment"
        )
    return value.rstrip("/")


def slot_key(release: dict) -> str:
    chapter = canonical_number(release.get("chapter"))
    volume = canonical_number(release.get("volume"))
    if chapter is not None:
        # Numbering reconciliation has already mapped source numbers to work numbers.
        return f"chapter:{chapter}"
    if volume is not None:
        return f"volume:{volume}"
    raise ValueError("Translation fallback needs an explicit chapter or book number")


def eligible_source(source: dict, languages: list[str]) -> bool:
    return bool(
        source.get("language") in languages
        and source.get("provider") not in {"expected", "translated", "assembled"}
        and source.get("numbering_status", "mapped") == "mapped"
        and not source.get("blocked")
        and (
            canonical_number(source.get("chapter")) is not None
            or canonical_number(source.get("volume")) is not None
        )
    )


def matches_slot(release: dict, key: str) -> bool:
    try:
        return slot_key(release) == key
    except ValueError:
        return False
