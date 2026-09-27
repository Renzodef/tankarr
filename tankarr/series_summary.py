from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

TERMINAL_STATUSES = frozenset(
    {
        "ended",
        "completed",
        "complete",
        "finished",
        "abandoned",
        "dropped",
        "cancelled",
        "canceled",
        "discontinued",
    }
)
# A paused work is not a finished one: chapters may still come, and the
# official platform stays the reference for how many exist.
CONTINUING_STATUSES = frozenset(
    {"ongoing", "continuing", "publishing", "releasing", "hiatus", "on_hiatus"}
)
# A hiatus is still "continuing" for every decision (frontier, monitoring,
# counts) but the library shows it apart: the catalogue says the author
# stopped, so no calendar date and no expectation of new chapters for now.
PAUSED_STATUSES = frozenset({"hiatus", "on_hiatus", "paused"})


def automatic_updates_paused(manga: dict[str, Any]) -> bool:
    """A manually closed collection stays frozen until Automatic is restored.

    Do not change the saved monitoring preference: removing the override must
    resume the user's previous schedule, including future-release monitoring.
    Explicit searches and refreshes remain available.
    """
    return str(manga.get("status_override") or "").strip().casefold() == "ended"


def _positive_integer(value: object) -> int | None:
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    if number <= 0 or number != number.to_integral_value():
        return None
    return int(number)


def publication_summary(
    manga: dict[str, Any], metadata: dict[str, Any] | None = None
) -> dict[str, str | bool | None]:
    """Resolve the reversible manual status without mutating source metadata."""

    override = str(manga.get("status_override") or "").strip().casefold()
    if override in {"continuing", "ended", "hiatus"}:
        return {
            "status": "continuing" if override == "hiatus" else override,
            "source": "manual",
            "overridden": True,
            "raw_status": override,
            "paused": override == "hiatus",
            "pause_sources": ["manual"] if override == "hiatus" else [],
        }

    metadata_status = str((metadata or {}).get("status") or "").strip().casefold()
    provider_status = str(manga.get("status") or "").strip().casefold()
    # A pause is announced by whoever notices first: the catalogues that
    # track it (MangaBaka, MyAnimeList, MangaUpdates) or the publisher's own
    # platform (Webtoons said "On hiatus" for unORDINARY while every
    # catalogue still said ongoing). Any one of them is enough; none of them
    # can end a work. AniList is not consulted: it calls NANA "releasing".
    signals = {
        str(key): str(value or "").strip().casefold()
        for key, value in (manga.get("publication_signals") or {}).items()
    }
    official_status = str(manga.get("official_status") or "").strip().casefold()
    if official_status and "official" not in signals:
        signals["official"] = official_status
    raw_status = metadata_status or provider_status
    source = "metadata" if metadata_status else "provider" if provider_status else None
    if raw_status in TERMINAL_STATUSES:
        status = "ended"
    elif raw_status in CONTINUING_STATUSES:
        status = "continuing"
    else:
        status = "unknown"
    pause_sources = [key for key, value in signals.items() if value in PAUSED_STATUSES]
    # The merged catalogue status is one of those signals restated; name the
    # generic "metadata"/"provider" only when no specific reporter is known.
    if raw_status in PAUSED_STATUSES and source and not pause_sources:
        pause_sources.append(source)
    paused = status != "ended" and bool(pause_sources)
    if paused and not raw_status:
        status, source, raw_status = "continuing", pause_sources[0], "hiatus"
    return {
        "status": status,
        "source": source,
        "overridden": False,
        "raw_status": raw_status or None,
        "paused": paused,
        "pause_sources": pause_sources if paused else [],
    }


def library_count_summary(
    manga: dict[str, Any], metadata: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Compare managed files with a canonical work total when units align.

    Download-provider indexes describe what Tankarr can fetch now. Catalogue
    totals describe the work itself. Keeping both counts prevents a three-item
    translated feed from being presented as a three-chapter work.
    """

    metadata = metadata or {}
    classification = metadata.get("classification") or {}
    content_kind = str(
        classification.get("kind") or metadata.get("content_kind") or "unknown"
    ).casefold()
    numbered_chapters = int(manga.get("numbered_chapter_count") or 0)
    numbered_volumes = int(manga.get("numbered_volume_count") or 0)

    if numbered_chapters > 0:
        unit = "chapter"
        metadata_field = "chapter_count"
        provider_field = "last_chapter"
    elif numbered_volumes > 0:
        unit = "issue" if content_kind == "comic" else "volume"
        metadata_field = "issue_count" if unit == "issue" else "volume_count"
        provider_field = "last_volume"
    elif content_kind == "comic" and _positive_integer(metadata.get("issue_count")):
        unit = "issue"
        metadata_field = "issue_count"
        provider_field = "last_volume"
    elif _positive_integer(metadata.get("chapter_count")):
        unit = "chapter"
        metadata_field = "chapter_count"
        provider_field = "last_chapter"
    elif _positive_integer(metadata.get("volume_count")):
        unit = "volume"
        metadata_field = "volume_count"
        provider_field = "last_volume"
    else:
        unit = "book"
        metadata_field = "book_count"
        provider_field = "last_volume"

    raw_available = max(0, int(manga.get("chapter_count") or 0))
    raw_downloaded = max(0, int(manga.get("downloaded_count") or 0))
    has_logical_counts = unit == "chapter" and "logical_chapter_count" in manga
    main_available = (
        max(0, int(manga.get("logical_chapter_count") or 0))
        if has_logical_counts
        else raw_available
    )
    main_downloaded = (
        max(0, int(manga.get("logical_downloaded_count") or 0))
        if has_logical_counts
        else raw_downloaded
    )
    special_available = (
        max(0, int(manga.get("special_chapter_count") or 0))
        if has_logical_counts
        else 0
    )
    special_downloaded = (
        max(0, int(manga.get("special_downloaded_count") or 0))
        if has_logical_counts
        else 0
    )
    combined_available = main_available + special_available
    combined_downloaded = main_downloaded + special_downloaded
    catalogue_expected = _positive_integer(metadata.get(metadata_field))
    provenance = metadata.get("provenance") or {}
    catalogue_source = str(provenance.get(metadata_field) or "").strip() or None
    expected = catalogue_expected
    source = catalogue_source
    segmentation_differs = False
    count_note: str | None = None

    publication = publication_summary(manga, metadata)
    continuing_chapters = unit == "chapter" and publication["status"] == "continuing"
    if continuing_chapters:
        expected = None
        source = None
    if expected is None and publication["status"] == "ended":
        expected = _positive_integer(manga.get(provider_field))
        if expected is not None:
            source = str(manga.get("provider") or "provider")

    # An integral final chapter defines an exact sequence (1..N), so decimal
    # specials cannot replace a missing main chapter.  A metadata-only chapter
    # total is instead a cardinality: catalogues often include selected extras
    # even when provider numbering ends at values such as 90.5.  In that case
    # main chapters plus distinct specials may satisfy the total, while split
    # releases have already been collapsed by logical_chapter_coverage().
    provider_final = (
        _positive_integer(manga.get("last_chapter"))
        if publication["status"] == "ended"
        else None
    )
    if unit == "chapter" and provider_final is not None:
        expected = provider_final
        source = str(manga.get("provider") or "provider")
        if catalogue_expected is not None and catalogue_expected != provider_final:
            segmentation_differs = True
            count_note = (
                f"The download source ends at chapter {provider_final}; "
                f"{catalogue_source or 'the work catalogue'} reports "
                f"{catalogue_expected} original-work chapters."
            )
    automatic_expected = expected
    automatic_source = source
    manual_expected = _positive_integer(manga.get("expected_count_override"))
    manual_unit = (
        str(manga.get("expected_count_unit_override") or "").strip().casefold()
    )
    expected_overridden = manual_expected is not None and manual_unit == unit
    if expected_overridden:
        if automatic_expected != manual_expected:
            segmentation_differs = True
            if automatic_expected is None:
                count_note = (
                    f"The managed edition is manually set to {manual_expected} "
                    f"{unit}s; no reliable automatic total is available."
                )
            else:
                count_note = (
                    f"The managed edition is manually set to {manual_expected} "
                    f"{unit}s; {automatic_source or 'the work catalogue'} reports "
                    f"{automatic_expected} original-work {unit}s."
                )
        expected = manual_expected
        source = "manual edition"
    cardinality_mode = (
        unit == "chapter"
        and expected is not None
        and provider_final is None
        and main_available < expected
    )
    if cardinality_mode:
        known_missing = max(0, combined_available - combined_downloaded)
        unindexed_missing = max(0, expected - combined_available)
        missing = min(expected, known_missing + unindexed_missing)
        return {
            "unit": unit,
            "available_count": min(expected, combined_available),
            "downloaded_count": max(0, expected - missing),
            "expected_count": expected,
            "total_count": expected,
            "missing_count": missing,
            "indexed_missing_count": min(missing, known_missing),
            "unindexed_missing_count": min(missing, unindexed_missing),
            "source": source,
            "metadata_field": metadata_field,
            "count_conflict": False,
            "catalogue_expected_count": catalogue_expected,
            "catalogue_source": catalogue_source,
            "segmentation_differs": segmentation_differs,
            "count_note": count_note,
            "expected_count_overridden": expected_overridden,
            "automatic_expected_count": automatic_expected,
            "automatic_expected_source": automatic_source,
        }

    if unit == "chapter":
        numbered_reference = expected is not None or continuing_chapters
        available = main_available if numbered_reference else combined_available
        downloaded = main_downloaded if numbered_reference else combined_downloaded
    else:
        available = raw_available
        downloaded = raw_downloaded

    observed = max(available, downloaded)
    if expected is not None and expected < observed:
        # A library can legitimately contain a translated/collected edition
        # split into more units than the original work catalogue. Preserve the
        # observed edition as the operational total and expose the scope
        # difference instead of reporting impossible negative missing counts.
        segmentation_differs = True
        count_note = count_note or (
            f"The managed edition contains {observed} {unit}s; "
            f"{catalogue_source or 'the work catalogue'} reports "
            f"{expected} original-edition {unit}s."
        )
        expected = observed
        source = str(manga.get("provider") or "library")
    total = max(observed, expected or 0)
    missing = max(0, total - downloaded)
    indexed_missing = min(missing, max(0, available - downloaded))
    unindexed_missing = max(0, missing - indexed_missing)

    return {
        "unit": unit,
        "available_count": available,
        "downloaded_count": downloaded,
        "expected_count": expected,
        "total_count": total,
        "missing_count": missing,
        "indexed_missing_count": indexed_missing,
        "unindexed_missing_count": unindexed_missing,
        "source": source,
        "metadata_field": metadata_field,
        "count_conflict": False,
        "catalogue_expected_count": catalogue_expected,
        "catalogue_source": catalogue_source,
        "segmentation_differs": segmentation_differs,
        "count_note": count_note,
        "expected_count_overridden": expected_overridden,
        "automatic_expected_count": automatic_expected,
        "automatic_expected_source": automatic_source,
    }


def decorate_series_summary(
    manga: dict[str, Any],
    metadata: dict[str, Any] | None = None,
    chapter_index: dict[str, Any] | None = None,
) -> dict[str, Any]:
    manga["publication"] = publication_summary(manga, metadata)
    count_input = manga
    if chapter_index is not None and chapter_index.get("unit") == "volume":
        books = [
            slot
            for slot in chapter_index.get("slots") or []
            if slot.get("expected") and not slot.get("chapter")
        ]
        # Raw source rows may still contain chapters after acquisition switches
        # to books. Diagnostics and overrides must use the same unit as progress.
        count_input = {
            **manga,
            "numbered_chapter_count": 0,
            "numbered_volume_count": max(1, len(books)),
            "chapter_count": sum(bool(slot.get("available")) for slot in books),
            "downloaded_count": sum(
                bool(slot.get("downloaded") or slot.get("covered_by_chapters"))
                for slot in books
            ),
        }
    counts = library_count_summary(count_input, metadata)
    if chapter_index is not None:
        counts["additional_content"] = chapter_index.get("additional_content", {})
    if (
        chapter_index is not None
        and str(chapter_index.get("unit") or "chapter") == "chapter"
    ):
        # The canonical index is the truth for chapter units: one slot per
        # integer chapter, specials aside, volumes covering their chapters.
        expected_total = chapter_index.get("expected_count")
        observed = int(chapter_index.get("expected_available_count") or 0)
        satisfied = int(chapter_index.get("expected_satisfied_count") or 0)
        total = (
            int(expected_total)
            if expected_total
            else max(
                observed, int(chapter_index.get("raw_missing_count") or 0) + satisfied
            )
        )
        counts["unit"] = "chapter"
        counts["expected_count"] = int(expected_total) if expected_total else None
        counts["reference_kind"] = str(chapter_index.get("reference_kind") or "none")
        counts["latest_chapter"] = chapter_index.get("latest_chapter")
        if chapter_index.get("expected_source"):
            counts["source"] = chapter_index["expected_source"]
        counts["reference_unknown"] = (
            not expected_total
            and counts["reference_kind"] != "available"
            and manga["publication"]["status"] != "ended"
        )
        if counts["reference_kind"] == "available":
            counts["count_note"] = None
            counts["segmentation_differs"] = False
            counts["automatic_expected_count"] = total
            counts["automatic_expected_source"] = "available sources"
            # The available reference includes owned book coverage as well
            # as source rows. Otherwise covered chapters make an actually
            # downloadable missing chapter look unindexed.
            observed = total
        reported = _positive_integer((metadata or {}).get("latest_release_chapter"))
        if counts["reference_unknown"] and reported:
            counts["count_note"] = (
                f"Catalogue reports {reported} chapters so far. This can include "
                "prologues or extras and is not the final chapter number."
            )
        counts["total_count"] = max(total, satisfied)
        counts["available_count"] = observed
        counts["downloaded_count"] = satisfied
        counts["missing_count"] = int(chapter_index.get("raw_missing_count") or 0)
        counts["indexed_missing_count"] = max(0, observed - satisfied)
        counts["unindexed_missing_count"] = max(
            0, counts["missing_count"] - counts["indexed_missing_count"]
        )
        counts["special_count"] = int(chapter_index.get("special_count") or 0)
        counts["special_downloaded_count"] = int(
            chapter_index.get("special_downloaded_count") or 0
        )
        if extra_coverage := chapter_index.get("catalogue_extra_coverage"):
            counts["catalogue_extra_coverage"] = extra_coverage
            counts["count_note"] = (
                f"The catalogue total includes {extra_coverage['expected']} extra "
                f"chapters; {extra_coverage['owned']} distinct volume extras "
                "are imported."
            )
        expected_volumes = chapter_index.get("expected_volume_count")
        counts["volume_progress"] = (
            {
                "owned": int(chapter_index.get("owned_volume_count") or 0),
                "expected": int(expected_volumes),
            }
            if expected_volumes
            else None
        )
        # A work the catalogues agree is 24 books cannot be finished at 24
        # indexed chapters: a book holds several of them. The total stays
        # unknown - one catalogue's 206 is not corroboration - but "up to
        # date" is a claim the books already disprove (Moonlight Mile).
        counts["short_of_books"] = bool(
            counts["reference_unknown"]
            and expected_volumes
            and observed <= int(expected_volumes)
        )
    if (
        chapter_index is not None
        and str(chapter_index.get("unit") or "chapter") == "volume"
    ):
        # A volumes series counts books: expected from the catalogue, owned
        # from the index; the catalogue's chapter total is irrelevant here.
        slots = [
            slot
            for slot in chapter_index.get("slots") or []
            if slot.get("expected") and not slot.get("chapter")
        ]
        owned = sum(1 for slot in slots if slot.get("downloaded"))
        # A book whose chapters are all on disk is a book the reader can open,
        # chapter by chapter: it counts as satisfied (29/29); the page says
        # which rows are books and which are chapters.
        covered = sum(
            1
            for slot in slots
            if not slot.get("downloaded") and slot.get("covered_by_chapters")
        )
        satisfied = owned + covered
        expected_volumes = chapter_index.get("expected_volume_count") or len(slots)
        counts["unit"] = "volume"
        counts["expected_count"] = int(expected_volumes) if expected_volumes else None
        # The automatic and catalogue totals must count the same unit as the
        # series: a 12-book edition of an 18-volume work is compared with 18
        # volumes, not with the 121 chapters the chapter heuristic picked.
        catalogue_volumes = _positive_integer(
            (metadata or {}).get("book_count")
        ) or _positive_integer((metadata or {}).get("volume_count"))
        counts["catalogue_expected_count"] = catalogue_volumes
        counts["automatic_expected_count"] = catalogue_volumes
        counts["automatic_expected_source"] = "catalogue" if catalogue_volumes else None
        counts["reference_kind"] = str(
            chapter_index.get("reference_kind") or "catalogue"
        )
        counts["reference_unknown"] = False
        counts["total_count"] = max(int(expected_volumes or 0), len(slots), satisfied)
        counts["available_count"] = sum(1 for slot in slots if slot.get("available"))
        counts["downloaded_count"] = satisfied
        counts["covered_by_chapters_count"] = covered
        counts["missing_count"] = int(chapter_index.get("raw_missing_count") or 0)
        counts["indexed_missing_count"] = max(0, counts["available_count"] - satisfied)
        counts["unindexed_missing_count"] = max(
            0, counts["missing_count"] - counts["indexed_missing_count"]
        )
        # The other unit, for the card: how large the work is in chapters,
        # beside how many of its books are on the shelf.
        catalogue_chapters = _positive_integer((metadata or {}).get("chapter_count"))
        counts["chapter_progress"] = (
            {
                "owned": sum(
                    1
                    for slot in chapter_index.get("mapped_chapter_slots") or []
                    if slot.get("downloaded")
                ),
                "expected": int(catalogue_chapters),
            }
            if catalogue_chapters
            else None
        )
    raw_missing = int(counts["missing_count"])
    counts["raw_missing_count"] = raw_missing
    counts["ignored_missing_count"] = 0
    if chapter_index is not None:
        index_unit = str(chapter_index.get("unit") or "chapter")
        summary_unit = str(counts.get("unit") or "chapter")
        compatible_units = index_unit == summary_unit or {
            index_unit,
            summary_unit,
        } == {"volume", "issue"}
        if compatible_units:
            ignored_slots = [
                slot
                for slot in chapter_index.get("slots") or []
                if slot.get("expected")
                and not slot.get("downloaded")
                and slot.get("ignored")
            ]
            ignored = min(raw_missing, len(ignored_slots))
            ignored_indexed = min(
                int(counts.get("indexed_missing_count") or 0),
                sum(1 for slot in ignored_slots if slot.get("available")),
            )
            ignored_unindexed = min(
                int(counts.get("unindexed_missing_count") or 0),
                ignored - ignored_indexed,
            )
            counts["ignored_missing_count"] = ignored
            counts["missing_count"] = raw_missing - ignored
            counts["indexed_missing_count"] = max(
                0, int(counts.get("indexed_missing_count") or 0) - ignored_indexed
            )
            counts["unindexed_missing_count"] = max(
                0,
                int(counts.get("unindexed_missing_count") or 0) - ignored_unindexed,
            )
    library_status_override = (
        str(manga.get("library_status_override") or "").strip().casefold()
    )
    counts["library_status_overridden"] = library_status_override == "up_to_date"
    if counts["library_status_overridden"]:
        counts["ignored_missing_count"] = raw_missing
        counts["missing_count"] = 0
        counts["indexed_missing_count"] = 0
        counts["unindexed_missing_count"] = 0
    manga["library_count"] = counts
    return manga
