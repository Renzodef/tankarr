"""Series-level metadata findings for the library audit.

The file audit looks at archives. These checks look at what a reader sees on
the series page: a series no metadata source knows, creator names that do not
lead to an author page, and a title left in another language than the one the
series is kept in.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from tankarr.translation_strip_audit import (
    StripScanStore,
    library_files,
    strip_finding,
    strip_findings_by_series,
)

# Short function words that identify a title's language. Only words that are
# rare as English title words are listed, so an English title is not flagged
# because of a borrowed name.
_TITLE_WORDS: dict[str, frozenset[str]] = {
    "it": frozenset(
        {
            "il",
            "lo",
            "gli",
            "del",
            "della",
            "dello",
            "dei",
            "degli",
            "delle",
            "nel",
            "nella",
            "sul",
            "sulla",
            "una",
            "uno",
            "che",
            "di",
            "da",
            "e",
        }
    ),
    "fr": frozenset(
        {
            "le",
            "la",
            "les",
            "du",
            "des",
            "une",
            "un",
            "et",
            "au",
            "aux",
            "dans",
            "sur",
            "de",
        }
    ),
    "es": frozenset(
        {"el", "los", "las", "del", "una", "uno", "y", "en", "con", "de", "la"}
    ),
    "de": frozenset(
        {"der", "die", "das", "dem", "den", "des", "und", "ein", "eine", "im", "vom"}
    ),
    "pt": frozenset({"o", "os", "as", "do", "da", "dos", "das", "um", "uma", "e"}),
}
_ENGLISH_WORDS = frozenset(
    {
        "the",
        "of",
        "and",
        "a",
        "an",
        "in",
        "to",
        "my",
        "is",
        "on",
        "for",
        "with",
        "from",
        "at",
        "by",
        "your",
        "his",
        "her",
        "who",
        "what",
    }
)


def _words(title: str) -> list[str]:
    folded = unicodedata.normalize("NFKD", title.casefold())
    folded = "".join(char for char in folded if not unicodedata.combining(char))
    return re.findall(r"[a-z]+", folded)


def title_language_hint(title: str) -> str | None:
    """Best guess of a Latin-script title's language, or None when unsure.

    Returns "en" only through the absence of a guess: a title with any common
    English function word is never attributed to another language.
    """

    words = _words(title)
    if not words or any(word in _ENGLISH_WORDS for word in words):
        return None
    scores = {
        language: len({word for word in words if word in vocabulary})
        for language, vocabulary in _TITLE_WORDS.items()
    }
    best = max(scores.values())
    if best < 2:
        return None
    leaders = [language for language, score in scores.items() if score == best]
    return leaders[0] if len(leaders) == 1 else None


def series_metadata_findings(
    manga: dict[str, Any],
    author_links: list[dict[str, Any]],
    *,
    has_external_metadata: bool,
    strip_volumes: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    if not has_external_metadata:
        findings.append(
            {
                "code": "no_external_metadata",
                "label": "No metadata source is linked to this series",
            }
        )
    if [name for name in manga.get("authors") or [] if str(name).strip()] and not (
        author_links
    ):
        findings.append(
            {
                "code": "authors_not_linked",
                "label": "Creator names do not open an author page",
            }
        )
    preferred = str(manga.get("preferred_language") or "").strip().casefold()
    detected = title_language_hint(str(manga.get("title") or ""))
    if detected and preferred and detected != preferred:
        findings.append(
            {
                "code": "title_language_mismatch",
                "label": "Title looks like another language than the series",
                "detected_language": detected,
                "series_language": preferred,
            }
        )
    if strip_volumes:
        findings.append(strip_finding(strip_volumes))
    return findings


def library_metadata_findings(
    database: Any, strip_store: StripScanStore | None = None
) -> dict[str, Any]:
    """Every series with at least one metadata finding."""

    strips = (
        strip_findings_by_series(library_files(database), strip_store)
        if strip_store is not None
        else {}
    )

    external = database.series_with_external_metadata_ids()
    links = database.manga_authors_by_series()
    series = []
    counts: dict[str, int] = {}
    for manga in database.list_manga(with_logical_counts=False):
        manga_id = str(manga["id"])
        findings = series_metadata_findings(
            manga,
            links.get(manga_id, []),
            has_external_metadata=manga_id in external,
            strip_volumes=strips.get(manga_id),
        )
        if not findings:
            continue
        for finding in findings:
            counts[finding["code"]] = counts.get(finding["code"], 0) + 1
        series.append(
            {
                "manga_id": manga_id,
                "title": manga.get("title"),
                "authors": list(manga.get("authors") or []),
                "preferred_language": manga.get("preferred_language"),
                "findings": findings,
            }
        )
    series.sort(key=lambda item: str(item["title"] or "").casefold())
    return {"counts": counts, "series": series}
