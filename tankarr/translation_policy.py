"""Language and slot rules shared by automatic and manual translation fallback."""

from __future__ import annotations

from urllib.parse import urlsplit

from tankarr.chapter_mapping import canonical_number
from tankarr.languages import normalize_language_code


def normalize_fallback_languages(raw: str) -> str:
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


def fallback_languages(manga: dict, default: str) -> list[str]:
    result = []
    for value in normalize_fallback_languages(
        manga.get("translation_source_languages") or default
    ).split(","):
        if value == "original":
            value = manga.get("original_language") or ""
        try:
            value = normalize_language_code(value)
        except ValueError:
            continue
        if value != manga["preferred_language"] and value not in result:
            result.append(value)
    return result


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
