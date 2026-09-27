from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock

import pytest

from tankarr.chapter_map import MapEntry
from tankarr.database import Database
from tankarr.series_units import UNMAPPED_NOTICE, build_series_units, load_series_units


def manga(**changes):
    return {
        "id": "series",
        "title": "Fixture",
        "status": "completed",
        "monitor_mode": "all",
        "series_unit_override": "volumes",
        **changes,
    }


def chapter(number, *, identifier=None, downloaded=True, **changes):
    identifier = identifier or f"chapter-{number}"
    return {
        "id": identifier,
        "chapter": str(number) if number is not None else None,
        "volume": "99",
        "release_unit": "chapter",
        "provider": "fixture",
        "source_name": "Source",
        "language": "en",
        "title": f"Chapter {number}",
        "groups": [],
        "publish_at": None,
        "source_url": "https://nas.local/release",
        "version": 1,
        "pages": 20,
        "downloaded": downloaded,
        "monitored": True,
        "library_path": f"/srv/library/{identifier}.cbz" if downloaded else None,
        **changes,
    }


def book(volume, **changes):
    return chapter(
        None,
        identifier=f"book-{volume}",
        **{"volume": str(volume), "release_unit": "volume", "pages": 180, **changes},
    )


@pytest.mark.parametrize("last_present", [True, False])
def test_complete_read_map_keeps_a_final_book_of_decimal_extras(last_present):
    result = build_series_units(
        manga(series_unit_override="chapters"),
        {"status": "ended", "volume_count": 2, "chapter_count": 4},
        [
            chapter(n, volume=None)
            for n in ("1", "2", "2.1", "2.2")
            if n != "2.2" or last_present
        ],
        [
            MapEntry(("1",), ("1", "2"), True, "operator"),
            MapEntry(("2",), ("2.1", "2.2"), True, "operator"),
        ],
    )
    assert result["chapter_sequence_end"] == 2
    assert result["expected_chapter_count"] == 4
    last = result["books"][1]
    assert last["exact"] is True
    assert last["chapter_range"] == {"first": "2.1", "last": "2.2"}
    assert last["chapter_count"] == len(last["chapters"]) == 2
    assert last["covered_by_chapters"] is last_present
    assert last["chapters"][-1]["missing"] is not last_present
    assert not result["unassigned_chapters"]


def test_exact_mixed_groups_keep_sources_and_compute_safe_duplicate_files():
    result = build_series_units(
        manga(),
        {"status": "ended", "volume_count": 2},
        [
            book(1),
            chapter(0),
            chapter(0, identifier="another-zero", provider="other"),
            chapter("0.5", downloaded=False),
            chapter(1, downloaded=False),
            chapter(2),
            chapter("2.5"),
            chapter(3),
        ],
        [
            MapEntry(("1",), ("0", "0.5", "1"), True, "operator"),
            MapEntry(("2",), ("2", "2.5"), True, "operator"),
        ],
    )
    assert result["mode"] == "grouped" and result["warning"] is None
    first, second = result["books"]
    assert first["owned"] and first["collapsed"] and first["pages"] == 180
    assert first["open_release_id"] == "book-1"
    assert [row["chapter"] for row in first["chapters"]] == ["0", "0.5", "1"]
    assert first["duplicate_file_count"] == 2
    assert set(first["duplicate_release_ids"]) == {"chapter-0", "another-zero"}
    assert (
        first["missing_chapter_count"] == 0 and first["downloaded_chapter_count"] == 1
    )
    assert first["chapters"][1]["covered_by_volume"] == "1"
    assert not first["can_assemble"]
    assert not second["owned"] and not second["collapsed"]
    assert second["status"] == "covered_by_chapters" and second["can_assemble"]
    assert second["downloaded_chapter_count"] == second["chapter_count"] == 2
    assert [row["chapter"] for row in result["unassigned_chapters"]] == ["3"]
    assert result["unassigned_chapters"][0]["volume"] is None


def test_absent_optional_specials_do_not_enter_book_counts_but_owned_ones_do():
    work = manga(
        series_unit_override="chapters",
        expected_count_override=2,
        expected_count_unit_override="chapter",
    )
    metadata = {"status": "ended", "chapter_count": 2, "volume_count": 1}
    mapping = [MapEntry(("1",), ("1", "1.5", "2"), True, "operator")]
    main = [chapter("1", volume=None), chapter("2", volume=None)]
    absent = build_series_units(
        work, metadata, [*main, chapter("1.5", volume=None, downloaded=False)], mapping
    )["books"][0]
    assert [row["chapter"] for row in absent["chapters"]] == ["1", "2"]
    assert absent["chapter_count"] == absent["downloaded_chapter_count"] == 2
    assert absent["chapter_range"] == {"first": "1", "last": "2"}
    assert absent["covered_by_chapters"] and absent["can_assemble"]

    owned = build_series_units(
        work, metadata, [*main, chapter("1.5", volume=None)], mapping
    )["books"][0]
    assert [row["chapter"] for row in owned["chapters"]] == ["1", "1.5", "2"]
    assert owned["chapter_count"] == owned["downloaded_chapter_count"] == 3
    assert owned["chapters"][1]["special"] and owned["chapters"][1]["downloaded"]


def test_hints_only_use_flat_books_and_chapters_without_covering_or_badges():
    result = build_series_units(
        manga(),
        {},
        [book(1), chapter(1), chapter(2, downloaded=False)],
        [MapEntry(("1",), ("1", "2"), False, "catalogue")],
    )
    assert result["mode"] == "flat" and result["warning"] == UNMAPPED_NOTICE
    assert result["hints"] == []  # catalogue suggestions stay in the boundary editor
    assert result["books"][0]["chapters"] == []
    assert not result["books"][0]["covered_by_chapters"]
    assert result["books"][0]["duplicate_file_count"] == 0
    rows = result["unassigned_chapters"]
    assert [row["chapter"] for row in rows] == ["1", "2"]
    assert all(
        row["volume"] is None and row["duplicate_of_volume"] is None for row in rows
    )
    assert rows[1]["covered_unmapped"] and not rows[1]["missing"]


def test_partial_exact_map_uses_operator_precedence_and_keeps_other_files_unassigned():
    result = build_series_units(
        manga(),
        {},
        [book(1), chapter(1), chapter(2), chapter(3)],
        [
            MapEntry(("1",), ("1", "2", "3"), True, "catalogue"),
            MapEntry(("1",), ("1",), True, "operator"),
        ],
    )
    assert [row["chapter"] for row in result["books"][0]["chapters"]] == ["1"]
    assert [row["chapter"] for row in result["unassigned_chapters"]] == ["2", "3"]
    assert result["books"][0]["duplicate_release_ids"] == ["chapter-1"]


@pytest.mark.parametrize(
    ("changes", "covered"), [({"provider": "manual"}, True), ({"pages": 601}, False)]
)
def test_suspect_books_remain_owned_but_never_offer_duplicate_retirement(
    changes, covered
):
    """A suspect book retires nothing. Whether it covers a missing chapter
    is a different question: explicit boundaries say what a hand-imported
    book holds (Moonlight Mile: 24 books built by hand, an operator map,
    every chapter "missing" all the same), while a book too big to be one
    book proves nothing about its contents."""

    result = build_series_units(
        manga(),
        {"volume_count": 1},
        [book(1, **changes), chapter(1), chapter(2, downloaded=False)],
        [MapEntry(("1",), ("1", "2"), True, "operator")],
    )
    group = result["books"][0]
    assert group["owned"] and group["suspect"]
    assert group["retirement_note"].endswith("not used to retire chapters")
    assert group["duplicate_file_count"] == 0 and group["duplicate_release_ids"] == []
    assert group["missing_chapter_count"] == (0 if covered else 1)
    assert bool(group["chapters"][1]["covered_by_volume"]) is covered


def test_declared_edition_count_limits_expected_books_but_preserves_extra_owned_books():
    result = build_series_units(
        manga(edition_book_count=12), {"volume_count": 18}, [book(1), book(16)], []
    )
    assert result["expected_book_count"] == result["edition_book_count"] == 12
    assert [row["volume"] for row in result["books"]] == [
        str(n) for n in range(1, 13)
    ] + ["16"]
    assert not result["books"][-1]["expected"] and result["books"][-1]["owned"]
    assert sum(row["owned"] for row in result["books"]) == 2


def test_managed_edition_does_not_truncate_the_original_editions_chapter_map():
    result = build_series_units(
        manga(edition_book_count=2),
        {"status": "ended", "volume_count": 4, "chapter_count": 20},
        [book(1), book(2)],
        [
            MapEntry(("1",), tuple(str(n) for n in range(1, 6)), True, "mangaupdates"),
            MapEntry(("2",), tuple(str(n) for n in range(6, 11)), True, "mangaupdates"),
            MapEntry(
                ("3",), tuple(str(n) for n in range(11, 16)), True, "mangaupdates"
            ),
            MapEntry(
                ("4",), tuple(str(n) for n in range(16, 21)), True, "mangaupdates"
            ),
        ],
    )

    assert result["expected_book_count"] == 2
    assert all(book["chapter_range"] is None for book in result["books"])
    assert result["exact_sources"] == []


def test_volume_acquisition_keeps_the_finished_works_chapter_cardinality():
    """The volume index counts books; the book map still counts chapters."""

    result = build_series_units(
        manga(),
        {"status": "ended", "volume_count": 1, "chapter_count": 10},
        [book(1)],
        [MapEntry(("1",), ("1",), True, "estimate")],
    )

    assert result["series_unit"] == "volumes"
    assert result["expected_book_count"] == 1
    assert result["expected_chapter_count"] == 10


def test_complete_owned_edition_beats_incompatible_catalogue_story_count():
    result = build_series_units(
        manga(edition_book_count=15),
        {"status": "ended", "volume_count": 15, "chapter_count": 8},
        [book(number) for number in range(1, 16)],
        [],
    )

    assert result["expected_book_count"] == 15
    assert len(result["books"]) == 15
    assert all(row["expected"] and row["owned"] for row in result["books"])


def test_partial_owned_edition_does_not_override_incompatible_story_count():
    result = build_series_units(
        manga(edition_book_count=15),
        {"status": "ended", "volume_count": 15, "chapter_count": 8},
        [book(number) for number in range(1, 15)],
        [],
    )

    assert result["edition_book_count"] is None


def test_catalogue_decimal_extra_does_not_overfill_a_complete_book_map():
    """A catalogue may place an omake in a book without counting it as one
    of the work's chapters.  The book map must use the same cardinality rule
    as the chapter index or the detail page reports 17 chapters out of 16."""

    rows = [chapter(number, volume=None) for number in range(1, 17)] + [
        chapter("16.5", volume=None)
    ]
    result = build_series_units(
        manga(series_unit_override="chapters", last_chapter="16"),
        {"status": "ended", "volume_count": 2, "chapter_count": 16},
        rows,
        [
            MapEntry(("1",), tuple(map(str, range(1, 9))), True, "mangaupdates"),
            MapEntry(
                ("2",),
                (*tuple(map(str, range(9, 17))), "16.5"),
                True,
                "mangaupdates",
            ),
        ],
    )

    from tankarr.series_form import apply_series_form

    result = apply_series_form(result, manga(), None)
    assert sum(book["chapter_count"] for book in result["books"]) == 16
    assert "16.5" not in {
        row["chapter"] for book in result["books"] for row in book["chapters"]
    }


def test_legacy_final_number_does_not_count_a_catalogue_decimal_extra():
    from tankarr.series_form import apply_series_form

    item = manga(series_unit_override="chapters", last_chapter="16")
    metadata = {"status": "ended", "volume_count": 4, "chapter_count": None}
    result = build_series_units(
        item,
        metadata,
        [chapter(number, volume=None) for number in (1, 2, 3, 4, 5, 6, 7, "7.5")],
        [
            MapEntry(("1",), tuple(map(str, range(1, 6))), True, "mangaupdates"),
            MapEntry(("2",), ("6", "7", "7.5"), True, "mangaupdates"),
        ],
    )
    result = apply_series_form(result, item, metadata)

    # The catalogue boundaries are suggestions. Sixteen numbered chapters
    # are split four per book; the uncounted decimal stays outside.
    assert sum(book["downloaded_chapter_count"] for book in result["books"]) == 7
    assert sum(book["chapter_count"] for book in result["books"][:2]) == 8
    assert "7.5" not in {
        row["chapter"] for book in result["books"] for row in book["chapters"]
    }


def test_monitoring_filters_distinguish_missing_ignored_and_downloaded_chapters():
    result = build_series_units(
        manga(),
        {"volume_count": 2},
        [chapter(1), chapter(2, downloaded=False), chapter(3, downloaded=False)],
        [
            MapEntry(("1",), ("1", "2"), True, "operator"),
            MapEntry(("2",), ("3",), True, "operator"),
        ],
        {"1": "ignored", "2": "monitored"},
    )
    first, second = result["books"]
    assert first["ignored"] and not first["monitored"]
    assert all(row["ignored"] and not row["monitored"] for row in first["chapters"])
    assert first["chapters"][0]["downloaded"] and first["missing_chapter_count"] == 0
    assert (
        second["monitored"]
        and not second["ignored"]
        and second["missing_chapter_count"] == 1
    )


def test_conflicting_exact_sources_do_not_assign_a_chapter_to_either_book():
    result = build_series_units(
        manga(),
        {},
        [book(1), chapter(1)],
        [
            MapEntry(("1",), ("1",), True, "source-a"),
            MapEntry(("2",), ("1",), True, "source-b"),
        ],
    )
    assert all(not row["exact"] and not row["can_assemble"] for row in result["books"])
    assert all(row["duplicate_file_count"] == 0 for row in result["books"])
    assert result["unassigned_chapters"][0]["volume"] is None
    assert result["unassigned_chapters"][0]["files"][0]["id"] == "chapter-1"


def test_unmapped_downloaded_files_are_preserved_but_pending_prologues_stay_hidden():
    result = build_series_units(
        manga(),
        {},
        [
            chapter(None, identifier="unknown", numbering_status="unmapped"),
            chapter("7.5"),
            chapter(0, downloaded=False),
        ],
        [],
    )
    assert {
        file["id"] for row in result["unassigned_chapters"] for file in row["files"]
    } == {"unknown", "chapter-7.5"}
    assert len(result["unassigned_chapters"]) == 2
    assert all(not row["missing"] for row in result["unassigned_chapters"])


def test_missing_path_or_fraction_prevents_assembly_without_opening_files():
    result = build_series_units(
        manga(),
        {},
        [chapter(1, library_path=None)],
        [MapEntry(("1",), ("1", "1.5"), True, "operator")],
    )
    assert not result["books"][0]["can_assemble"]
    assert result["books"][0]["missing_chapter_count"] == 1


def test_builder_never_calls_the_application_unit_context(monkeypatch):
    callback = Mock(
        side_effect=AssertionError("pure builder consulted application state")
    )
    monkeypatch.setattr("tankarr.series_unit.unit_context", callback)
    build_series_units(manga(), {}, [], [])
    callback.assert_not_called()


def test_bulk_loader_uses_constant_queries_for_four_hundred_chapters(
    tmp_path, monkeypatch
):
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    for name, count in (("small", 10), ("large", 400)):
        database.upsert_manga({**manga(id=name), "authors": []}, "en", "all")
        database.update_manga(name, {"series_unit_override": "volumes"})
        database.upsert_chapters(
            name,
            [
                chapter(n, identifier=f"{name}-{n}", downloaded=False)
                for n in range(1, count + 1)
            ],
        )
        database.replace_chapter_map(
            name,
            "operator",
            [
                MapEntry(
                    (str(n // 10 + 1),),
                    tuple(str(c) for c in range(n + 1, n + 11)),
                    True,
                    "operator",
                )
                for n in range(0, count, 10)
            ],
        )
    monkeypatch.setattr("tankarr.series_units._context", lambda _manga: {})
    statements = []
    original = database.connect

    @contextmanager
    def traced():
        with original() as connection:
            connection.set_trace_callback(statements.append)
            yield connection

    monkeypatch.setattr(database, "connect", traced)
    monkeypatch.setattr(
        Path,
        "open",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("read model opened a file")
        ),
    )
    import tankarr.series_units as module

    actual_builder = module.build_chapter_index
    builder = Mock(wraps=actual_builder)
    monkeypatch.setattr(module, "build_chapter_index", builder)
    small = load_series_units(database, "small")
    small_queries = sum(sql.lstrip().upper().startswith("SELECT") for sql in statements)
    statements.clear()
    large = load_series_units(database, "large")
    large_queries = sum(sql.lstrip().upper().startswith("SELECT") for sql in statements)
    assert small_queries == large_queries == 6  # + the content verdicts
    assert len(small["books"]) == 1 and len(large["books"]) == 40
    assert sum(row["chapter_count"] for row in large["books"]) == 400
    assert builder.call_count == 2  # exactly one index build per request


def test_loader_rejects_unknown_series(tmp_path):
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    with pytest.raises(KeyError):
        load_series_units(database, "absent")


@pytest.mark.parametrize("complete", [False, True])
def test_split_chapter_keeps_canonical_completeness_and_all_owned_part_files(complete):
    result = build_series_units(
        manga(series_unit_override="chapters"),
        {},
        [chapter("1.1"), chapter("1.2", downloaded=complete)],
        [MapEntry(("1",), ("1",), True, "operator")],
    )
    group = result["books"][0]
    assert len(group["chapters"]) == 1
    row = group["chapters"][0]
    assert row["chapter"] == "1" and row["split_parts"] == ["1.1", "1.2"]
    assert row["downloaded"] is complete
    assert len(row["files"]) == (2 if complete else 1)
    assert group["can_assemble"] is complete
    assert group["missing_chapter_count"] == (0 if complete else 1)


def test_the_edition_on_disk_drops_catalogue_books_beyond_it():
    """Master Keaton: 12 Viz books on disk, a catalogue map in 18 tankobon.
    With the edition set to 12 the tankobon 13-18 are not rows; their
    chapters follow the operator's Viz boundaries instead."""

    entries = [
        MapEntry(volumes=("11",), chapters=("121",), exact=True, source="operator"),
        MapEntry(
            volumes=("16",), chapters=("121", "122"), exact=True, source="mangaupdates"
        ),
        MapEntry(volumes=("18",), chapters=("140",), exact=True, source="mangaupdates"),
    ]
    units = build_series_units(
        manga(edition_book_count=12),
        None,
        [
            book("11"),
            chapter("121", identifier="c121"),
            chapter("140", identifier="c140"),
        ],
        entries,
    )
    volumes = [item["volume"] for item in units["books"]]
    assert "16" not in volumes and "18" not in volumes
    by = {item["volume"]: item for item in units["books"]}
    assert [c["chapter"] for c in by["11"]["chapters"]] == ["121"]


def test_the_last_book_holds_the_decimals_the_count_named_not_phantom_numbers():
    """SPRIGGAN: the catalogue counts 62 chapters and the estimate spread them
    over eleven books, so the last one "holds 57-62". The sources that carry
    the work whole end at 60 plus 60.5 and 60.6: those two are the book's last
    chapters, and 61 and 62 were never written."""

    from tankarr.series_form import apply_series_form

    releases = [
        chapter(number, identifier=f"{source}-{number}", source_name=source)
        for source in ("Atsumaru", "Weeb Central")
        for number in range(1, 61)
    ]
    releases += [
        chapter(
            label, identifier=f"{source}-{label}", source_name=source, downloaded=False
        )
        for source in ("Atsumaru", "Weeb Central")
        for label in ("60.5", "60.6")
    ]
    chapter_map = [
        MapEntry(
            volumes=("11",),
            chapters=("57", "58", "59", "60", "61", "62"),
            exact=True,
            source="mangaupdates",
        ),
    ]

    result = build_series_units(
        manga(status="ended", series_unit_override="chapters", last_chapter="62"),
        {"status": "ended", "chapter_count": 62, "volume_count": 11},
        releases,
        chapter_map,
    )

    result = apply_series_form(result, manga(), None)
    last = next(book for book in result["books"] if book["volume"] == "11")
    labels = [item["chapter"] for item in last["chapters"]]
    assert "61" not in labels and "62" not in labels
    assert labels[-2:] == ["60.5", "60.6"]
    tail = [item for item in last["chapters"] if "." in item["chapter"]]
    assert [item["chapter"] for item in tail] == ["60.5", "60.6"]
    assert all(item["missing"] and item["monitored"] for item in tail)
    assert last["chapter_range"] == {"first": "58", "last": "60.6"}
    assert last["chapter_count"] == 5
    assert last["estimated"] and not last["exact"]

    # All chapters are present, but the estimated book cannot be assembled.
    whole = build_series_units(
        manga(status="ended", series_unit_override="chapters", last_chapter="62"),
        {"status": "ended", "chapter_count": 62, "volume_count": 11},
        [{**release, "downloaded": True} for release in releases],
        chapter_map,
    )
    whole = apply_series_form(whole, manga(), None)
    last = next(book for book in whole["books"] if book["volume"] == "11")
    assert last["status"] == "covered_by_chapters"
    assert last["missing_chapter_count"] == 0
    assert not last["can_assemble"]


@pytest.mark.parametrize("manual", [False, True])
def test_book_estimates_keep_the_numbered_end_when_the_count_includes_a_decimal(manual):
    from tankarr.series_form import apply_series_form

    item = manga(status="ended", series_unit_override="chapters", last_chapter="45")
    if manual:
        item.update(expected_count_override=45, expected_count_unit_override="chapter")
    metadata = {"status": "ended", "chapter_count": 45, "volume_count": 2}
    rows = [chapter(n) for n in range(1, 45)] + [chapter("5.5")]
    entries = [
        MapEntry(
            volumes=("1",),
            chapters=tuple(map(str, range(1, 23))) + ("5.5",),
            exact=True,
            source="ocr",
        ),
        MapEntry(
            volumes=("2",),
            chapters=tuple(map(str, range(23, 45))),
            exact=True,
            source="ocr",
        ),
    ]
    units = build_series_units(item, metadata, rows, entries)
    assert units["expected_chapter_count"] == 45
    assert units["chapter_sequence_end"] == 44
    rendered = apply_series_form(units, item, metadata)
    chapters = [c for book in rendered["books"] for c in book["chapters"]]
    assert len(chapters) == 45
    assert "5.5" in {c["chapter"] for c in chapters}
    assert "45" not in {c["chapter"] for c in chapters}
    assert not any(c["missing"] for c in chapters)
    assert rendered["books"][-1]["chapter_range"]["last"] == "44"


def test_mapped_side_stories_keep_their_books_without_inflating_chapter_count():
    from tankarr.chapter_mapping import build_chapter_index
    from tankarr.series_form import apply_series_form

    item = manga(
        status="ended",
        series_unit_override="chapters",
        edition_book_count=2,
        expected_count_override=2,
        expected_count_unit_override="chapter",
    )
    rows = [chapter(n, volume=None) for n in (1, 2, "2.1", "2.2")]
    entries = [
        MapEntry(("1",), ("1", "2", "2.1"), True, "operator"),
        MapEntry(("2",), ("2.2",), True, "operator"),
    ]
    metadata = {"status": "ended", "chapter_count": 8, "volume_count": 3}
    index = build_chapter_index(item, metadata, rows, chapter_map=entries)
    assert index["read_chapter_count"] == 2
    assert index["expected_available_count"] == 2
    assert index["additional_content"] == {"prologues": 0, "extras": 2}
    result = apply_series_form(
        build_series_units(item, metadata, rows, entries), item, metadata
    )
    assert result["expected_chapter_count"] == 2
    assert result["map_confidence"]["level"] == "exact"
    assert [[s["chapter"] for s in b["chapters"]] for b in result["books"]] == [
        ["1", "2", "2.1"],
        ["2.2"],
    ]
    extras = [s for b in result["books"] for s in b["chapters"] if s["special"]]
    assert len(extras) == 2 and all(not s["expected"] for s in extras)
    assert not result["unassigned_chapters"]


def test_catalogue_totals_lay_out_books_without_generating_boundaries():
    from tankarr.series_form import apply_series_form, chapter_volume_assignments

    item = manga(status="ended", series_unit_override="chapters")
    metadata = {"status": "ended", "chapter_count": 12, "volume_count": 3}
    rows = [chapter(n, volume=None) for n in range(1, 13)]
    # Include an old persisted layout to exercise the real legacy data path.
    entries = [MapEntry(("1",), tuple(map(str, range(1, 5))), True, "estimate")]
    result = apply_series_form(
        build_series_units(item, metadata, rows, entries), item, metadata
    )
    # The shelf shows every book with an equal share of the run, but the
    # layout is estimated only: nothing is exact and nothing reaches the
    # database as a membership.
    assert result["map_confidence"]["level"] == "estimated"
    assert result["map_confidence"]["unknown_books"] == 0
    assert [b["chapter_count"] for b in result["books"]] == [4, 4, 4]
    assert all(b["estimated"] and not b["exact"] for b in result["books"])
    assert all(not b["covered_by_chapters"] for b in result["books"])
    assert result["unassigned_chapters"] == []
    assert chapter_volume_assignments(result) == {}


def test_mapped_parts_count_once_and_do_not_create_empty_unassigned_parents():
    from tankarr.chapter_mapping import build_chapter_index

    item = manga(
        status="ended",
        series_unit_override="chapters",
        expected_count_override=5,
        expected_count_unit_override="chapter",
    )
    metadata = {"status": "ended", "volume_count": 2, "chapter_count": 5}
    # Four ordinary chapters, one canonical .5, with chapter 3 split in two.
    labels = ["1", "1.5", "2", "3.1", "3.2", "4"]
    rows = [chapter(n, volume=None) for n in labels]
    rows += [
        chapter(n, identifier=f"alternative-{n}", volume=None, downloaded=False)
        for n in labels
    ]
    entries = [
        MapEntry(("1",), tuple(labels[:3]), True, "operator"),
        MapEntry(("2",), tuple(labels[3:]), True, "operator"),
    ]
    index = build_chapter_index(item, metadata, rows, chapter_map=entries)
    assert index["read_chapter_count"] == 5
    assert index["expected_available_count"] == 5
    assert index["additional_content"] == {"prologues": 0, "extras": 0}
    result = build_series_units(item, metadata, rows, entries)
    assert result["expected_chapter_count"] == 5
    assert result["unassigned_chapters"] == []
    files = [
        f["id"] for b in result["books"] for s in b["chapters"] for f in s["files"]
    ]
    assert len(files) == len(set(files)) == 6


def test_webtoon_season_cannot_cover_a_missing_episode_as_a_printed_book():
    result = build_series_units(
        manga(),
        {"format": "webtoon", "chapter_count": 2},
        [book(1), chapter(1, volume="1"), chapter(2, volume="1", downloaded=False)],
        [MapEntry(("1",), ("1", "2"), True, "catalogue")],
    )
    assert all(not b.get("covered_by_chapters") for b in result["books"])
    missing = next(c for c in result["unassigned_chapters"] if c["chapter"] == "2")
    assert missing["missing"]


def _units_with_end(*, owned_past: bool):
    """Five chapters, a catalogue that says five, and a stray "487"."""

    rows = [chapter(n) for n in range(1, 6)] + [
        chapter(487, identifier="chapter-487", downloaded=owned_past)
    ]
    return build_series_units(
        manga(series_unit_override="chapters", status="ended"),
        {"status": "ended", "chapter_count": 5},
        rows,
        [],
    )


def _numbers(result):
    return {
        str(slot["chapter"])
        for slot in result["unassigned_chapters"]
        if slot.get("chapter")
    } | {
        str(slot["chapter"])
        for book_row in result["books"]
        for slot in (book_row.get("chapters") or [])
        if slot.get("chapter")
    }


def test_a_release_numbered_past_the_end_is_not_a_chapter():
    """Adekan runs to 83 and one source listed a "Chapter 487": it became an
    expected chapter in a phantom book 21, counted as missing. The map path
    already refused such numbers; the release path did not."""

    result = _units_with_end(owned_past=False)

    assert result["chapter_sequence_end"] == 5
    assert "487" not in _numbers(result)


def test_a_file_on_disk_past_the_end_still_keeps_its_row():
    """The guard drops an aggregator's error, never a file you own."""

    assert "487" in _numbers(_units_with_end(owned_past=True))
