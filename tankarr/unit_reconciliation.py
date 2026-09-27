"""Detect sources that number whole volumes as if they were chapters.

Aggregators frequently expose a volume-based edition as "Chapter 1…34".
Trusting that numbering makes Tankarr believe it owns chapters 1-34 and go
hunting for 35+ elsewhere, downloading content it already has. The evidence
that a source counts volumes is convergent and cheap:

* the source lists exactly as many items as the catalogue has volumes;
* the catalogue (or the chapter map) knows far more chapters than that;
* the downloaded files are volume-sized (hundreds of pages).

Matching counts alone can describe an incomplete chapter translation. Positive
page evidence is required and belongs to the individual source, not every
extension exposed by the same provider. A source is never reclassified twice.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from statistics import median
from typing import Any

from tankarr.release_kind import CHAPTER_MAX_PAGES
from tankarr.series_form import _is_webtoon
from tankarr.torrent_utils import release_number_hints

VOLUME_PAGE_THRESHOLD = 120
CHAPTER_RATIO = 2.0


@dataclass(frozen=True)
class UnitVerdict:
    provider: str
    numbered: int
    volume_count: int
    chapter_total: int
    median_pages: float | None
    reason: str
    release_ids: tuple[str, ...] = ()


def release_source(release: dict[str, Any]) -> str:
    """Keep independent extensions behind one provider separate."""
    provider = str(release.get("provider") or "")
    source = str(release.get("source_key") or release.get("source_name") or "")
    return f"{provider}/{source}" if source else provider


def misclassified_explicit_chapters(
    manga: dict[str, Any],
    metadata: dict[str, Any] | None,
    releases: Iterable[dict[str, Any]],
) -> dict[str, str]:
    """Return volume rows whose own titles explicitly identify chapters.

    Long-scroll episodes can contain more than 120 image tiles. Page count
    alone used to turn those files into books during reconciliation. Printed
    works can have the same problem when an aggregator publishes one unusually
    long chapter and mentions its containing volume in the title. An explicit
    chapter number matching the source identity wins over that page heuristic;
    short releases with no volume marker also repair old count-only guesses.
    Actual book releases still keep their explicit volume marker.
    """

    if str(manga.get("series_unit_override") or "").casefold() in {
        "volume",
        "volumes",
    }:
        return {}
    webtoon = _is_webtoon(metadata)
    result: dict[str, str] = {}
    for release in releases:
        if str(release.get("release_unit") or "chapter") != "volume":
            continue
        volume, chapter = release_number_hints(str(release.get("title") or ""))
        if chapter is None or not release.get("id"):
            continue
        source = release.get("source_chapter", release.get("chapter"))
        try:
            explicit_source_chapter = source is not None and Decimal(
                str(source)
            ) == Decimal(str(chapter))
        except (InvalidOperation, ValueError):
            explicit_source_chapter = False
        restored_chapter = str(chapter)
        if webtoon:
            # Season-relative titles such as ``S3 - Chapter 1`` can sit on a
            # source whose stable chapter identity is 418.  The title proves
            # that this is an episode rather than a book, but must not replace
            # that global identity with the season-local display number.
            if source is not None:
                try:
                    restored_chapter = str(Decimal(str(source)).normalize())
                except (InvalidOperation, ValueError):
                    pass
        else:
            if not explicit_source_chapter:
                continue
            pages = _integer(release.get("pages"))
            if volume is None and pages is not None and pages <= CHAPTER_MAX_PAGES:
                result[str(release["id"])] = restored_chapter
                continue
            # A printed source can legitimately call each tankobon "Chapter
            # 1", "Chapter 2", and so on. Without positive short-page evidence,
            # require a title naming a *different* containing volume, as in
            # "Chapter 21 - Volume 2 Part 1".  That mismatch proves the source
            # chapter was incorrectly copied into the volume field.
            if volume is None:
                continue
            try:
                if Decimal(str(volume)) == Decimal(str(chapter)):
                    continue
            except (InvalidOperation, ValueError):
                continue
        result[str(release["id"])] = restored_chapter
    return result


def _integer(value: object) -> int | None:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if number != number.to_integral_value() or number <= 0:
        return None
    return int(number)


def volume_numbered_providers(
    releases: Iterable[dict[str, Any]],
    *,
    volume_count: int | None,
    chapter_total: int | None,
    page_counts: dict[str, list[int]] | None = None,
) -> list[UnitVerdict]:
    """Return the providers whose "chapter" numbers are really volume numbers.

    ``page_counts`` maps ``release_source`` to counts of its downloaded files,
    when Tankarr could inspect them.
    """

    if not volume_count or not chapter_total:
        return []
    if chapter_total < volume_count * CHAPTER_RATIO:
        return []
    by_provider: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for release in releases:
        if str(release.get("release_unit") or "chapter") != "chapter":
            continue
        if str(release.get("volume") or "").strip():
            continue
        if _integer(release.get("chapter")) is None:
            continue
        by_provider[release_source(release)].append(release)

    verdicts: list[UnitVerdict] = []
    for source, items in by_provider.items():
        numbers = {_integer(item.get("chapter")) for item in items}
        numbers.discard(None)
        numbered = len(numbers)
        if numbered < 3 or abs(numbered - volume_count) > 1:
            continue
        if max(numbers) > volume_count + 1:  # type: ignore[arg-type]
            continue
        pages = [count for count in (page_counts or {}).get(source, []) if count > 0]
        median_pages = float(median(pages)) if pages else None
        if median_pages is None or median_pages < VOLUME_PAGE_THRESHOLD:
            continue
        evidence = (
            f"{numbered} numbered items match the catalogue's {volume_count} volumes "
            f"while the work has {chapter_total} chapters"
        )
        if median_pages is not None:
            evidence += f"; downloaded files hold ~{int(median_pages)} pages each"
        verdicts.append(
            UnitVerdict(
                provider=str(items[0].get("provider") or ""),
                numbered=numbered,
                volume_count=volume_count,
                chapter_total=chapter_total,
                median_pages=median_pages,
                reason=evidence,
                release_ids=tuple(str(item["id"]) for item in items if item.get("id")),
            )
        )
    return verdicts
