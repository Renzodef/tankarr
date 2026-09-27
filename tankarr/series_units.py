"""Read-only mixed-series groups, built from one bulk database snapshot."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from decimal import Decimal
from typing import Any

from tankarr.catalogue_consensus import managed_volume_count
from tankarr.chapter_map import (
    MapEntry,
    canonical_label,
    chapters_by_volume,
    edition_entries,
    effective_entries,
)
from tankarr.chapter_mapping import (
    MAX_CANONICAL_SEQUENCE,
    build_chapter_index,
    canonical_decimal_labels,
    counted_chapter_total,
    effective_edition_book_count,
    suspect_volume_reasons,
)
from tankarr.database import Database
from tankarr.series_unit import _context, is_volume_release
from tankarr.source_numbering import is_unpublished

UNMAPPED_NOTICE = "Which chapters each book contains is not known: chapters are kept and no book is counted as covered."


def _is_book(release: dict[str, Any]) -> bool:
    unit = release.get("release_unit")
    if unit in {"chapter", "volume"}:
        return unit == "volume"
    return is_volume_release(release)


def _positive(value: object) -> int | None:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _sort_number(value: str) -> tuple[int, Decimal | str]:
    label = canonical_label(value)
    return (0, Decimal(label)) if label is not None else (1, value.casefold())


def _pages(value: object) -> int | None:
    try:
        pages = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return None
    return pages if pages >= 0 else None


def _file_rows(releases: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    files = []
    for release in sorted(
        releases,
        key=lambda item: (
            str(item.get("provider") or "") == "manual",
            -(_pages(item.get("version")) or 0),
            str(item.get("id") or ""),
        ),
    ):
        if not release.get("downloaded"):
            continue
        files.append(
            {
                "id": str(release["id"]),
                "title": str(release.get("title") or ""),
                "provider": str(release.get("provider") or ""),
                "source_name": release.get("source_name"),
                "language": str(release.get("language") or ""),
                "pages": _pages(release.get("pages")),
                "library_path": release.get("library_path"),
            }
        )
    return files


def _open_file(files: list[dict[str, Any]]) -> dict[str, Any] | None:
    return next((item for item in files if item["library_path"]), None)


def _new_slot(
    label: str | None, key: str, monitored: bool, *, expected: bool
) -> dict[str, Any]:
    return {
        "key": key,
        "chapter": label,
        "volume": None,
        "expected": expected,
        "special": not expected,
        "evidence": "exact_map" if expected else "owned_file",
        "volume_inferred": False,
        "split_parts": [],
        "releases": [],
        "available": False,
        "downloaded": False,
        "monitored": monitored,
        "volume_monitor_state": "automatic",
        "ignored": False,
        "queue_status": None,
        "searchable": expected and label is not None,
    }


def build_series_units(
    manga: dict[str, Any],
    metadata: dict[str, Any] | None,
    releases: Iterable[dict[str, Any]],
    chapter_map: Iterable[MapEntry],
    volume_monitor_overrides: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Group exact canonical numbers; never inspect or infer file contents.

    The optional embedded unit context is loaded by load_series_units. Keeping
    it explicit here makes this builder independent of the application's global
    context callback and prevents database reads while grouping individual books.
    """
    manga = {**manga, "_unit_context": dict(manga.get("_unit_context") or {})}
    rows = [dict(release) for release in releases]
    for release in rows:
        if release.get("chapter") == 0 and not isinstance(release.get("chapter"), bool):
            release["chapter"] = "0"
    from tankarr.series_form import _is_webtoon

    webtoon = _is_webtoon(metadata)
    entries = effective_entries(edition_entries(chapter_map, metadata))
    edition_limit = effective_edition_book_count(manga) or managed_volume_count(
        metadata
    )
    catalogue_volume_count = (metadata or {}).get("volume_count")
    try:
        catalogue_volume_count = int(catalogue_volume_count)
    except (TypeError, ValueError):
        catalogue_volume_count = None
    if (
        edition_limit
        and catalogue_volume_count
        and edition_limit != catalogue_volume_count
    ):
        # Catalogue release logs describe the original binding.  When the
        # managed edition has a different number of books, truncating that
        # map assigns every later chapter to no book at all.  Only boundaries
        # read from this edition or set by its operator survive.
        entries = [entry for entry in entries if entry.source in {"ocr", "operator"}]
    chapter_total = counted_chapter_total(manga, metadata)
    canonical_decimals = canonical_decimal_labels(
        entries,
        rows,
        chapter_total=chapter_total,
        strict_cardinality=(
            str(manga.get("expected_count_unit_override") or "").casefold() == "chapter"
            and manga.get("expected_count_override") is not None
        ),
    )
    if chapter_total is None and canonical_label(manga.get("last_chapter")):
        # A legacy final-chapter field is an endpoint, not a cardinality.  It
        # cannot make a catalogue omake such as 7.5 one of chapters 1..16.
        # Keep decimals only when this edition was actually read or the
        # operator explicitly mapped them.
        canonical_decimals = frozenset(
            label
            for entry in entries
            if entry.exact and entry.source in {"ocr", "operator"}
            for label in entry.chapters
            if (number := canonical_label(label)) is not None
            and Decimal(number) != Decimal(number).to_integral_value()
        )
    entries = [
        MapEntry(
            entry.volumes,
            tuple(
                label
                for label in entry.chapters
                if (number := canonical_label(label)) is None
                or Decimal(number) == Decimal(number).to_integral_value()
                or number in canonical_decimals
                or entry.source in {"operator", "ocr"}
            ),
            entry.exact,
            entry.source,
            entry.release_date,
        )
        for entry in entries
    ]
    entries = [entry for entry in entries if entry.chapters]
    exact_map = chapters_by_volume(entries)
    assignments: dict[str, set[str]] = defaultdict(set)
    for volume, labels in exact_map.items():
        for label in labels:
            if canonical_label(label) is not None:
                assignments[label].add(volume)
    exact_map = {
        volume: {label for label in labels if canonical_label(label) is not None}
        for volume, labels in exact_map.items()
    }
    exact_map = {volume: labels for volume, labels in exact_map.items() if labels}
    # The edition on disk numbers the books. A catalogue that counts another
    # edition (18 tankobon against 12 Viz books) must not add rows beyond it.
    if edition_limit:
        exact_map = {
            volume: labels
            for volume, labels in exact_map.items()
            if (label := canonical_label(volume)) is None
            or Decimal(label) <= edition_limit
        }
        assignments = defaultdict(
            set,
            {
                label: {v for v in volumes if v in exact_map}
                for label, volumes in assignments.items()
            },
        )
        assignments = defaultdict(
            set, {label: v for label, v in assignments.items() if v}
        )
    grouped = bool(exact_map)
    suspects = suspect_volume_reasons(
        (release for release in rows if _is_book(release)), chapter_map=entries
    )
    overrides = {
        canonical_label(volume) or volume: str(state).casefold()
        for volume, state in (volume_monitor_overrides or {}).items()
        if str(state).casefold() in {"monitored", "ignored"}
    }
    index = build_chapter_index(
        manga,
        metadata,
        [
            {**release, "volume": None}
            if not _is_book(release) and canonical_label(release.get("chapter")) is None
            else release
            for release in rows
        ],
        overrides,
        chapter_map=entries,
        suspect_covering_volumes=set(suspects),
    )
    default_monitored = str(manga.get("monitor_mode") or "none") in {"all", "existing"}
    marked_current = manga.get("library_status_override") == "up_to_date"
    chapter_slots: dict[str, dict[str, Any]] = {}
    book_slots: dict[str, dict[str, Any]] = {}
    release_slots: dict[str, str] = {}
    for slot in [*index["slots"], *index["mapped_chapter_slots"]]:
        if slot["chapter"] is None:
            if slot.get("volume") is not None:
                book_slots[str(slot["volume"])] = slot
            continue
        copied = {**slot, "releases": []}
        chapter_slots[str(slot["chapter"])] = copied
        for release in slot["releases"]:
            release_slots[str(release["id"])] = str(slot["chapter"])

    # The index counts a chapter assembled from parts once. The book view
    # displays those mapped part files; do not add a second empty parent row.
    mapped_part_parents: set[str] = set()
    for label, slot in list(chapter_slots.items()):
        parts = slot.get("split_parts") or []
        if (
            parts
            and label not in assignments
            and all(part in assignments for part in parts)
        ):
            del chapter_slots[label]
            mapped_part_parents.add(label)
            for release in rows:
                if release_slots.get(str(release["id"])) == label:
                    part = canonical_label(release.get("chapter"))
                    if part in parts:
                        release_slots[str(release["id"])] = part

    book_releases: dict[str, list[dict[str, Any]]] = defaultdict(list)
    official_head_label = canonical_label(index.get("official_frontier"))
    official_head = (
        Decimal(official_head_label) if official_head_label is not None else None
    )
    loose_slots: list[dict[str, Any]] = []
    for release in rows:
        if _is_book(release):
            volume = canonical_label(release.get("volume")) or str(
                release.get("volume") or "Unnumbered book"
            )
            book_releases[volume].append(release)
            continue
        label = release_slots.get(str(release["id"]))
        if (
            label is None
            and str(release.get("numbering_status") or "mapped") == "mapped"
        ):
            label = canonical_label(release.get("chapter"))
        if label is None:
            if release.get("downloaded"):
                slot = _new_slot(
                    None,
                    f"release:{release['id']}",
                    bool(release.get("monitored")),
                    expected=False,
                )
                slot["releases"] = [release]
                loose_slots.append(slot)
            continue
        if label in mapped_part_parents and not release.get("downloaded"):
            continue
        if label not in chapter_slots:
            number = Decimal(label)
            expected = label in canonical_decimals or (
                number > 0
                and number == number.to_integral_value()
                and (release.get("downloaded") or not is_unpublished(release))
            )
            if not expected and not release.get("downloaded"):
                continue
            # The index drops the chapters the official edition has not
            # reached; a scanlation of one must not put the slot back as a
            # hole. Lookism's WEBTOON English run ends at 613 while the
            # aggregators already number 624, and those eleven showed as
            # missing under "613 / 613". A file on disk still keeps its row.
            if (
                official_head is not None
                and not release.get("downloaded")
                and number > official_head
            ):
                continue
            # Where the index ended the numbering, a release numbered past it
            # is an aggregator's error, not a chapter: Adekan runs to 83 and
            # one source listed a "Chapter 487", which sat in a phantom book
            # 21 as a missing chapter. The map path below already refuses
            # those numbers; a release must not put them back. A file on disk
            # still keeps its row.
            end = index.get("sequence_end")
            if end is not None and not release.get("downloaded") and number > end:
                continue
            chapter_slots[label] = _new_slot(
                label, f"chapter:{label}", default_monitored, expected=expected
            )
        chapter_slots[label]["releases"].append(release)
    # A book's estimate spreads the catalogue's count over the volumes, so it
    # names numbers the work never used: SPRIGGAN's last book "holds 57-62"
    # where the numbering ends at 60 and the last two chapters are 60.5 and
    # 60.6. Where the index ended the sequence, the estimate does not reopen it.
    sequence_end = index.get("sequence_end")
    # A complete read map can finish with decimal extras, including an
    # entire final book. Its integer sequence end is not their upper bound.
    read_map_labels = (
        {label for label, slot in chapter_slots.items() if slot.get("expected")}
        if index.get("sequence_basis") == "complete_read_map"
        else set()
    )
    for label in assignments:
        if label in chapter_slots:
            continue
        number = canonical_label(label)
        if (
            sequence_end is not None
            and number is not None
            and Decimal(number) > sequence_end
            and label not in read_map_labels
        ):
            continue
        chapter_slots[label] = _new_slot(
            label, f"chapter:{label}", default_monitored, expected=True
        )

    edition_count = effective_edition_book_count(manga)
    chapter_cardinality = counted_chapter_total(manga, metadata)
    # Some catalogues count only named stories while describing a larger
    # collected edition (Raika is 15 books but one catalogue reports 8
    # chapters).  A complete, contiguous set of explicit book files on disk
    # is stronger evidence for the edition size than that incompatible story
    # count.  Partial sets still take the conservative path below.
    owned_book_numbers = {
        int(number)
        for volume, alternatives in book_releases.items()
        if (number := canonical_label(volume)) is not None
        and Decimal(number) == Decimal(number).to_integral_value()
        and any(release.get("downloaded") for release in alternatives)
    }
    complete_owned_edition = bool(
        edition_count and set(range(1, edition_count + 1)).issubset(owned_book_numbers)
    )
    if (
        edition_count
        and chapter_cardinality
        and edition_count > chapter_cardinality
        and not complete_owned_edition
    ):
        edition_count = None
    expected_books = edition_count or index.get("expected_volume_count")
    if (
        expected_books
        and chapter_cardinality
        and expected_books > chapter_cardinality
        and not complete_owned_edition
    ):
        expected_books = managed_volume_count(metadata)
    volumes = (
        set(book_releases)
        | set(exact_map)
        | {
            volume
            for volume in book_slots
            if not edition_count
            or (
                (label := canonical_label(volume)) is not None
                and Decimal(label) <= edition_count
            )
        }
    )
    if expected_books and expected_books <= MAX_CANONICAL_SEQUENCE:
        volumes.update(str(number) for number in range(1, expected_books + 1))
    owned_volumes = {
        volume
        for volume, alternatives in book_releases.items()
        if any(release.get("downloaded") for release in alternatives)
    }
    covering_volumes = owned_volumes - set(suspects)
    children: dict[str, list[dict[str, Any]]] = defaultdict(list)
    unassigned = []
    for slot in [
        *(chapter_slots[label] for label in sorted(chapter_slots, key=_sort_number)),
        *loose_slots,
    ]:
        label = slot["chapter"]
        candidates = assignments.get(label, set())
        volume = next(iter(candidates)) if len(candidates) == 1 else None
        if volume is None and slot.get("evidence") == "counted_tail":
            # A chapter the sources number past the numbering has no row of
            # its own in the map; the index bound it to the book its base
            # chapter closes, and that placement is as good as a mapped one.
            volume = slot.get("volume")
            if volume is not None and label is not None:
                assignments[label].add(volume)
        files = _file_rows(slot["releases"])
        readable = _open_file(files)
        monitor_state = overrides.get(volume or "", "automatic")
        downloaded = (
            bool(slot.get("downloaded")) if slot.get("split_parts") else bool(files)
        )
        monitored = bool(slot.get("monitored"))
        if slot["releases"]:
            monitored = any(
                bool(release.get("monitored")) for release in slot["releases"]
            )
        ignored = monitor_state == "ignored" or (marked_current and not downloaded)
        if monitor_state == "monitored":
            monitored = True
        if ignored:
            monitored = False
        # The index already lets explicit boundaries cover a chapter in a
        # hand-imported book (Moonlight Mile: 24 books built by hand, an
        # operator map, every chapter "missing" all the same). That verdict
        # holds here too; a suspect book still covers nothing on its own.
        covered = bool(
            volume
            and not downloaded
            and (
                volume in covering_volumes
                or (
                    slot.get("covered_by_volume") == volume
                    and slot.get("coverage_exact")
                )
            )
        )
        unmapped = bool(
            volume is None
            and covering_volumes
            and not downloaded
            # Episodes of a webtoon are published on their own: an owned bundle
            # is not a printed book that can quietly hold a missing one.
            and not webtoon
            and slot["expected"]
            and (
                not grouped
                or slot.get("covered_unmapped")
                or (slot.get("covered_by_volume") and not slot.get("coverage_exact"))
            )
        )
        duplicate = (
            volume if volume in covering_volumes and downloaded and files else None
        )
        slot.update(
            volume=volume,
            volume_inferred=False,
            files=files,
            open_release_id=readable["id"] if readable else None,
            pages=(readable or (files[0] if files else {})).get("pages"),
            available=any(
                release.get("downloaded") or not is_unpublished(release)
                for release in slot["releases"]
            ),
            downloaded=downloaded,
            monitored=monitored,
            ignored=ignored,
            volume_monitor_state=monitor_state,
            covered_by_volume=volume if covered else None,
            covered_by_chapters=False,
            coverage_exact=covered or bool(duplicate),
            covered_unmapped=unmapped,
            duplicate_of_volume=duplicate,
            missing=bool(
                slot["expected"]
                and not downloaded
                and not covered
                and not unmapped
                and not ignored
            ),
        )
        # Source feeds often label unowned decimal fragments as specials. They
        # are neither Wanted nor part of the managed book's contents until a
        # file (or an exactly mapped owned book) actually provides them.
        if (
            slot["special"]
            and not slot["expected"]
            and not slot["downloaded"]
            and not slot["covered_by_volume"]
        ):
            continue
        if volume is None:
            unassigned.append(slot)
        else:
            children[volume].append(slot)

    books = []
    for volume in sorted(volumes, key=_sort_number):
        files = _file_rows(book_releases[volume])
        readable = _open_file(files)
        owned = bool(files)
        chapters = children[volume]
        # The book holds what the slots say it holds: the estimate's numbers
        # past the end of the numbering are not among them, and a chapter
        # numbered past it ("60.6") is.
        visible_labels = {
            str(slot["chapter"]) for slot in chapters if slot.get("chapter") is not None
        }
        labels = {
            label
            for label in exact_map.get(volume, set())
            if label in visible_labels
            and (
                sequence_end is None
                or label in chapter_slots
                or label in read_map_labels
                or (number := canonical_label(label)) is None
                or Decimal(number) <= sequence_end
            )
        } | {
            str(slot["chapter"])
            for slot in children.get(volume, ())
            if slot.get("evidence") == "counted_tail" and slot.get("chapter")
        }
        exact = bool(labels) and all(assignments[label] == {volume} for label in labels)
        # A decimal extra a source lists inside the book ("74.22") is not one
        # of its chapters: every numbered chapter on disk is the book from
        # chapters, extras or not (Iryū 10 and 18, Touch 23).
        counted = [
            slot
            for slot in chapters
            if slot.get("expected", True) and not slot.get("special")
        ] or chapters
        covered = bool(
            exact and counted and all(slot["downloaded"] for slot in counted)
        )
        duplicate_ids = [
            file["id"]
            for slot in chapters
            if slot["duplicate_of_volume"] == volume
            for file in slot["files"]
        ]
        monitor_state = overrides.get(volume, "automatic")
        monitored = bool(book_slots.get(volume, {}).get("monitored", default_monitored))
        if monitor_state == "monitored":
            monitored = True
        ignored = monitor_state == "ignored" or (marked_current and not owned)
        if ignored:
            monitored = False
        number = Decimal(volume) if canonical_label(volume) is not None else None
        expected = (
            bool(
                number is not None
                and number == number.to_integral_value()
                and 1 <= number <= expected_books
            )
            if expected_books
            else bool(labels or book_slots.get(volume, {}).get("expected"))
        )
        books.append(
            {
                "key": f"volume:{volume}",
                "volume": volume,
                "expected": expected,
                "owned": owned,
                "monitored": monitored,
                "ignored": ignored,
                "volume_monitor_state": monitor_state,
                "status": "owned"
                if owned
                else "covered_by_chapters"
                if covered
                else "missing"
                if expected
                else "unmapped",
                "pages": (readable or (files[0] if files else {})).get("pages"),
                "files": files,
                "open_release_id": readable["id"] if readable else None,
                "suspect": volume in suspects,
                "retirement_note": (
                    f"{suspects[volume]} · not used to retire chapters"
                    if volume in suspects
                    else None
                ),
                "exact": exact,
                "map_sources": sorted(
                    {
                        entry.source
                        for entry in entries
                        if entry.exact and volume in entry.volumes
                    }
                ),
                "chapter_range": {
                    "first": min(labels, key=Decimal),
                    "last": max(labels, key=Decimal),
                }
                if labels
                else None,
                "chapters": chapters,
                "chapter_count": len(labels),
                "downloaded_chapter_count": sum(
                    bool(slot["downloaded"]) for slot in chapters
                ),
                "missing_chapter_count": sum(slot["missing"] for slot in chapters),
                "duplicate_file_count": len(duplicate_ids),
                "duplicate_release_ids": duplicate_ids,
                "covered_by_chapters": covered and not owned,
                "can_assemble": bool(
                    not owned
                    and covered
                    and all(
                        any(file["library_path"] for file in slot["files"])
                        for slot in chapters
                    )
                ),
                "collapsed": owned,
            }
        )
    return {
        "manga_id": str(manga["id"]),
        "mode": "grouped" if grouped else "flat",
        "series_unit": index["series_unit"],
        "edition_book_count": edition_count,
        "expected_book_count": expected_books,
        # Acquisition can follow whole books while the page still needs the
        # work's chapter cardinality to lay those books out.  The chapter
        # index deliberately counts volumes in that mode, so its
        # ``expected_count`` is the number of books, not the number of
        # chapters or stories inside them.  Keep the two facts
        # separate or a persisted estimate feeds itself forever as v1=c1.
        "expected_chapter_count": index.get("read_chapter_count")
        or (
            index.get("expected_count")
            if index.get("unit") == "chapter"
            else counted_chapter_total(manga, metadata)
        ),
        # What the catalogue counts, even for a running work whose total is
        # still moving: never evidence, only a last resort for laying out a
        # shelf of books nobody has mapped (Moonlight Mile).
        "catalogue_chapter_count": _positive(
            (metadata or {}).get("chapter_count") or manga.get("chapter_count")
        ),
        "chapter_sequence_end": index.get("sequence_end"),
        "exact_sources": sorted(
            {entry.source for entry in entries if entry.exact and entry.source}
        ),
        "warning": None if grouped else UNMAPPED_NOTICE,
        "hints": [
            {
                "volumes": list(entry.volumes),
                "chapters": list(entry.chapters),
                "source": entry.source,
            }
            for entry in entries
            if not entry.exact
        ][:100],
        "books": books,
        "unassigned_chapters": unassigned,
    }


def _acquisition_frontier(
    database: Database, manga: dict[str, Any], releases: list[dict[str, Any]]
) -> Decimal | None:
    """The chapter the official edition has reached, as acquisition sees it.

    Under "prefer official" the hunt refuses every release past this line
    ("Beyond the confirmed official edition frontier"), so a shelf that
    counts those chapters as missing asks for what will never be fetched:
    Lookism 614-625 under an edition at 613. The page reads the same line.
    """

    from tankarr.series_summary import publication_summary
    from tankarr.source_ranking import official_frontier, official_hosts

    ranking = getattr(database, "source_ranking", None)
    if getattr(ranking, "acquisition_policy", None) != "prefer_official":
        return None
    metadata = manga.get("metadata") or {}
    try:
        return official_frontier(
            releases,
            official_hosts(
                metadata.get("official_links"),
                language=str(manga.get("preferred_language") or ""),
            ),
            metadata=metadata,
            status=publication_summary(manga, metadata)["status"],
            chapter_total=(
                manga.get("expected_count_override")
                if str(manga.get("expected_count_unit_override") or "").casefold()
                == "chapter"
                else None
            ),
        )
    except Exception:  # noqa: BLE001 - the page never fails for a frontier
        return None


def load_series_units(
    database: Database,
    manga_id: str,
    preference: str = "volumes",
    *,
    sync: bool = False,
    specials_outside_books: bool = False,
) -> dict[str, Any]:
    """Six bulk SELECTs, plus the application's fixed unit-context lookups.

    The grouped payload is then read as one form, books or chapters, with a
    completion plan per book (``series_form``)."""
    from tankarr.series_form import apply_series_form

    with database.read_snapshot():
        manga = database.get_manga(manga_id, include_logical_counts=False)
        manga["_unit_context"] = _context(manga)
        releases = database.list_chapters(manga_id, manga["preferred_language"])
        stored_entries = database.chapter_map(manga_id)
        # Old statistical maps have no evidential value. sync_books removes
        # the legacy rows; effective_entries excludes them for every reader.
        entries = [entry for entry in stored_entries if entry.source != "estimate"]
        overrides = {
            row["volume_key"]: row["state"]
            for row in database.list_volume_monitor_overrides(manga_id)
        }
        units = build_series_units(
            manga, manga.get("metadata"), releases, entries, overrides
        )
        # What the pages said about each chapter file and the books.
        content = database.content_alignment(manga_id)
        frontier = _acquisition_frontier(database, manga, releases)
    from tankarr.series_form import sync_books

    units = apply_series_form(
        units,
        manga,
        manga.get("metadata"),
        preference=preference,
        exact_sources=units.get("exact_sources") or [],
        specials_outside_books=specials_outside_books,
        content=content,
        frontier=frontier,
    )
    if sync:
        try:
            units["sync"] = sync_books(database, manga_id, units)
        except Exception:  # noqa: BLE001 - the page never fails for bookkeeping
            units["sync"] = {"map_entries": 0, "chapters_renumbered": 0}
    return units
