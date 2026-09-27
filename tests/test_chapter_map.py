from __future__ import annotations

import pytest

from tankarr.chapter_map import (
    MapEntry,
    chapters_by_volume,
    coverage_for_chapter,
    effective_entries,
    entries_from_boundaries,
    entries_from_releases,
    expand_numbers,
    integer_chapter_total,
)


def test_expand_numbers_handles_ranges_decimals_and_decorations():
    assert expand_numbers("12") == ["12"]
    assert expand_numbers("012") == ["12"]
    assert expand_numbers("12.5") == ["12.5"]
    assert expand_numbers("99-100") == ["99", "100"]
    assert expand_numbers("101-102 (end)") == ["101", "102"]
    assert expand_numbers("73-75 HQ") == ["73", "74", "75"]
    assert expand_numbers("73 LQ") == ["73"]
    assert expand_numbers("12.1-12.4") == ["12.1", "12.4"]
    assert expand_numbers("Extra") == []
    assert expand_numbers("") == []
    assert expand_numbers("1-9999") == []


def test_entries_from_mangaupdates_releases_distinguish_exact_from_coarse():
    entries = entries_from_releases(
        [
            {"volume": "34", "chapter": "101-102 (end)", "release_date": "2009-04-27"},
            {"volume": "20", "chapter": "58", "release_date": "2008-01-01"},
            {"volume": "20", "chapter": "58"},  # duplicate release, different group
            {"volume": "1-20", "chapter": "1-60", "release_date": "2007-01-01"},
            {"volume": None, "chapter": "3"},  # no volume: no map evidence
            {"volume": "5", "chapter": "Extra"},
        ]
    )
    assert entries == [
        MapEntry(("34",), ("101", "102"), True, "mangaupdates", "2009-04-27"),
        MapEntry(("20",), ("58",), True, "mangaupdates", "2008-01-01"),
        MapEntry(
            tuple(str(n) for n in range(1, 21)),
            tuple(str(n) for n in range(1, 61)),
            False,
            "mangaupdates",
            "2007-01-01",
        ),
    ]
    assert chapters_by_volume(entries) == {"34": {"101", "102"}, "20": {"58"}}
    assert integer_chapter_total(entries) == 102


def test_coverage_uses_exact_entries_before_coarse_spans():
    entries = entries_from_releases(
        [
            {"volume": "34", "chapter": "101-102"},
            {"volume": "1-20", "chapter": "1-60"},
        ]
    )
    owned = {"34", "1", "2"}

    exact = coverage_for_chapter("101", entries, owned)
    assert exact.covered and exact.volume == "34" and exact.exact

    known_but_missing = coverage_for_chapter("102", entries, {"1"})
    assert not known_but_missing.covered and not known_but_missing.unmapped
    assert known_but_missing.candidates == ("34",)

    # Only part of the coarse span is owned: honest "covered, unmapped".
    partial = coverage_for_chapter("30", entries, owned)
    assert partial.unmapped and not partial.covered

    whole_span = coverage_for_chapter("30", entries, {str(n) for n in range(1, 21)})
    assert whole_span.unmapped and not whole_span.covered and whole_span.volume is None

    assert not coverage_for_chapter("200", entries, owned).covered
    assert not coverage_for_chapter("5", entries, set()).covered


def test_operator_boundaries_are_half_open_and_include_only_known_fractions():
    entries = entries_from_boundaries(
        [
            {"volume": "12", "first_chapter": "55"},
            {"volume": "13", "first_chapter": "60.5"},
        ],
        last_chapter="63.5",
        known_chapters=["54.5", "55.5", "56.125", "64", "Extra"],
    )
    assert entries == [
        MapEntry(
            ("12",),
            ("55", "55.5", "56", "56.125", "57", "58", "59", "60"),
            True,
            "operator",
        ),
        MapEntry(("13",), ("60.5", "61", "62", "63", "63.5"), True, "operator"),
    ]
    assert "60.5" not in entries[0].chapters
    assert "55.1" not in entries[0].chapters


def test_zero_and_decimal_numbers_keep_exact_numeric_identity():
    entries = entries_from_boundaries(
        [
            {"volume": "01.00", "first_chapter": 0},
            {"volume": "2.5", "first_chapter": "0.50"},
        ],
        last_chapter=1,
        known_chapters=["0.25"],
    )
    assert entries[0].chapters == ("0", "0.25")
    assert entries[1].volumes == ("2.5",)
    assert entries[1].chapters == ("0.5", "1")
    assert coverage_for_chapter(0, entries, {"1"}).covered
    assert coverage_for_chapter("0.500", entries, {"2.5"}).covered
    assert expand_numbers(0) == ["0"]


@pytest.mark.parametrize(
    "boundaries,last",
    [
        ([{"volume": 0, "first_chapter": 1}], 5),
        ([{"volume": "NaN", "first_chapter": 1}], 5),
        ([{"volume": 1, "first_chapter": "Infinity"}], 5),
        ([{"volume": 1, "first_chapter": -1}], 5),
        ([{"volume": 1, "first_chapter": 6}], 5),
        ([{"volume": 1, "first_chapter": 1}], "NaN"),
        ([{"volume": 2, "first_chapter": 1}, {"volume": 1, "first_chapter": 2}], 5),
        ([{"volume": 1, "first_chapter": 1}, {"volume": 1, "first_chapter": 2}], 5),
        ([{"volume": 1, "first_chapter": 2}, {"volume": 2, "first_chapter": 1}], 5),
        ([{"volume": 1, "first_chapter": 1}, {"volume": 2, "first_chapter": 1}], 5),
        ([{"volume": 1, "first_chapter": 0}], 10000),
        ([{"volume": "1e99999", "first_chapter": 1}], 5),
    ],
)
def test_operator_boundaries_reject_invalid_or_unbounded_intervals(boundaries, last):
    with pytest.raises(ValueError):
        entries_from_boundaries(boundaries, last_chapter=last)


def test_operator_map_hides_conflicting_volume_and_chapter_assignments_without_mutation():
    original = [
        MapEntry(("1",), ("1", "2", "3"), True, "catalogue"),
        MapEntry(("2",), ("2.00", "3", "4"), True, "official"),
        MapEntry(("1", "3"), ("2", "5"), False, "hint"),
        MapEntry(("01",), ("1.00", "2"), True, "operator"),
    ]
    effective = effective_entries(original)
    assert chapters_by_volume(effective) == {"1": {"1", "2"}, "2": {"3", "4"}}
    assert coverage_for_chapter("2", original, {"2"}).covered is False
    assert coverage_for_chapter("2", original, {"1"}).volume == "1"
    assert coverage_for_chapter("3", original, {"1"}).covered is False
    assert original[0].chapters == ("1", "2", "3")
    assert original[1].chapters == ("2.00", "3", "4")
    # Removing only the operator rows restores the source evidence unchanged.
    assert coverage_for_chapter("2", original[:-1], {"2"}).unmapped
    assert effective_entries(effective) == effective


def test_saved_operator_interval_does_not_grow_with_later_catalogue_rows():
    operator = entries_from_boundaries(
        [{"volume": 1, "first_chapter": 1}], last_chapter=3
    )
    refreshed = [MapEntry(("1",), ("1", "2", "3", "4", "4.5"), True, "catalogue")]
    assert chapters_by_volume([*refreshed, *operator]) == {"1": {"1", "2", "3"}}
    assert not coverage_for_chapter("4", [*refreshed, *operator], {"1"}).covered


def test_bulk_coverage_matches_single_chapter_verdicts_after_operator_resolution():
    from tankarr.chapter_map import Coverage, coverage_by_chapter

    entries = [
        MapEntry(("1",), ("0", "0.5", "1", "2"), True, "catalogue"),
        MapEntry(("2",), ("2", "3"), True, "operator"),
        MapEntry(("3", "4"), ("4", "5"), False, "catalogue"),
    ]
    for owned in (set(), {"1"}, {"2"}, {"1", "2", "3", "4"}):
        bulk = coverage_by_chapter(entries, owned)
        for chapter in ("0", "0.5", "1", "2", "3", "4", "5", "6"):
            assert bulk.get(chapter, Coverage()) == coverage_for_chapter(
                chapter, entries, owned
            )


def test_a_reading_of_the_books_owns_them_below_the_operator():
    """The "ocr" source (what a reader found printed in the books) overrides a
    catalogue's entry for the books it read, the operator still overrides it,
    and neither is ever demoted as a sparse log."""

    from tankarr.chapter_map import MapEntry, downgrade_sparse_maps, effective_entries

    entries = [
        MapEntry(
            volumes=("1",), chapters=("1", "2"), exact=True, source="mangaupdates"
        ),
        MapEntry(volumes=("1",), chapters=("1", "2", "3"), exact=True, source="ocr"),
        MapEntry(volumes=("2",), chapters=("4", "5"), exact=True, source="ocr"),
        MapEntry(
            volumes=("2",), chapters=("4", "5", "6"), exact=True, source="operator"
        ),
    ]
    resolved = {(e.volumes, e.source): e.chapters for e in effective_entries(entries)}
    assert resolved[(("1",), "ocr")] == ("1", "2", "3")
    assert (("1",), "mangaupdates") not in resolved
    assert resolved[(("2",), "operator")] == ("4", "5", "6")
    assert (("2",), "ocr") not in resolved
    kept = {(e.source, e.exact) for e in downgrade_sparse_maps(entries)}
    assert ("ocr", True) in kept and ("operator", True) in kept


def test_one_catalogues_conflicting_ranges_do_not_invent_a_boundary():

    from tankarr.chapter_map import MapEntry, chapters_by_volume

    grouped = chapters_by_volume(
        [
            MapEntry(("25",), ("155", "156", "161", "162"), True, "mangaupdates"),
            MapEntry(("26",), ("161", "162", "163"), True, "mangaupdates"),
        ]
    )

    assert grouped["25"] == {"155", "156", "161", "162"}
    assert grouped["26"] == {"161", "162", "163"}


def test_two_catalogues_naming_different_books_still_place_nothing():
    from tankarr.chapter_map import MapEntry, chapters_by_volume

    grouped = chapters_by_volume(
        [
            MapEntry(("1",), ("7",), True, "source-a"),
            MapEntry(("2",), ("7",), True, "source-b"),
        ]
    )

    assert grouped["1"] == {"7"} and grouped["2"] == {"7"}


def test_conflicting_claims_never_certify_chapter_coverage():
    from tankarr.chapter_map import coverage_by_chapter

    entries = [
        MapEntry(("16",), ("61",), True, "catalogue"),
        MapEntry(("17",), ("61",), True, "catalogue"),
    ]
    for owned in ({"16"}, {"17"}, {"16", "17"}):
        single = coverage_for_chapter("61", entries, owned)
        assert single == coverage_by_chapter(entries, owned)["61"]
        assert single.unmapped and not single.exact and not single.covered


def test_new_catalogue_evidence_overrides_a_stale_estimate_without_duplicate_owners():
    entries = [
        MapEntry(("1",), ("1", "2", "3"), True, "mangaupdates"),
        MapEntry(("1",), ("1", "2"), True, "estimate"),
        MapEntry(("2",), ("3", "4", "5"), True, "estimate"),
    ]
    effective = effective_entries(entries)
    grouped = chapters_by_volume(effective)
    assert grouped == {"1": {"1", "2", "3"}}
    assert effective_entries(effective) == effective
    assert entries[2].chapters == ("3", "4", "5")  # stored evidence is unchanged
