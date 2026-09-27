import pytest

from tankarr.chapter_map import MapEntry
from tankarr.operator_map import OperatorChapterMap
from tankarr.series_form import apply_series_form, chapter_volume_assignments
from tankarr.series_units import build_series_units, load_series_units
from tests.test_continuing_acquisition import chapter, prepare
from tests.test_queue_management import build_service
from tests.test_series_form import book, slot, units
from tests.test_series_units import chapter as unit_chapter
from tests.test_series_units import manga


def ranges(result):
    return [item["chapter_range"] for item in result["books"]]


@pytest.mark.parametrize("status", ["ongoing", "hiatus", "ended"])
@pytest.mark.parametrize("source", ["mangadex", "mangaupdates", "catalogue"])
def test_dense_catalogue_maps_do_not_override_the_even_division(status, source):
    item = manga(status=status, series_unit_override="chapters")
    metadata = {"status": status, "chapter_count": 12, "volume_count": 2}
    payload = build_series_units(
        item,
        metadata,
        [unit_chapter(n) for n in range(1, 13)],
        [
            MapEntry(("1",), ("1", "2"), True, source),
            MapEntry(("2",), tuple(str(n) for n in range(3, 13)), True, source),
        ],
    )
    result = apply_series_form(payload, item, metadata)
    assert ranges(result) == [
        {"first": "1", "last": "6"},
        {"first": "7", "last": "12"},
    ]
    assert all(
        item["estimated"] and not item["can_assemble"] for item in result["books"]
    )
    assert chapter_volume_assignments(result) == {}


def test_reused_estimates_redistribute_on_new_chapters_and_books():
    item = {"status": "ongoing"}
    result = apply_series_form(
        units(
            [book(str(n)) for n in range(1, 4)],
            [slot(str(n), downloaded=True) for n in range(1, 10)],
            expected_chapters=9,
        ),
        item,
        None,
    )
    result["unassigned_chapters"].extend(
        slot(str(n), downloaded=True) for n in range(10, 13)
    )
    result["expected_chapter_count"] = 12
    result = apply_series_form(result, item, None)
    assert ranges(result) == [
        {"first": "1", "last": "4"},
        {"first": "5", "last": "8"},
        {"first": "9", "last": "12"},
    ]
    result["books"].append(book("4"))
    result["expected_book_count"] = 4
    result = apply_series_form(result, item, None)
    assert ranges(result) == [
        {"first": "1", "last": "3"},
        {"first": "4", "last": "6"},
        {"first": "7", "last": "9"},
        {"first": "10", "last": "12"},
    ]
    stable = ranges(result)
    assert ranges(apply_series_form(result, item, None)) == stable
    assert len([s for b in result["books"] for s in b["chapters"]]) == 12


def test_recalculation_keeps_manual_intervals_fixed():
    anchor = book(
        "1",
        [slot("1", downloaded=True), slot("2", downloaded=True)],
        rng={"first": "1", "last": "2"},
    )
    anchor["map_sources"] = ["operator"]
    result = apply_series_form(
        units(
            [anchor, book("2"), book("3")],
            [slot(str(n), downloaded=True) for n in range(3, 10)],
            expected_chapters=9,
        ),
        {"status": "ended"},
        None,
    )
    result["unassigned_chapters"].extend(
        slot(str(n), downloaded=True) for n in range(10, 13)
    )
    result["expected_chapter_count"] = 12
    result = apply_series_form(result, {"status": "ended"}, None)
    assert ranges(result) == [
        {"first": "1", "last": "2"},
        {"first": "3", "last": "7"},
        {"first": "8", "last": "12"},
    ]
    assert result["books"][0]["exact"]
    assert not result["books"][0]["estimated"]


def test_count_only_books_recalculate_without_inventing_chapter_slots():
    item = {"status": "ongoing"}
    payload = units([book(str(n), owned=True) for n in range(1, 4)])
    payload["catalogue_chapter_count"] = 9
    result = apply_series_form(payload, item, None)
    result["catalogue_chapter_count"] = 12
    result = apply_series_form(result, item, None)
    assert ranges(result) == [
        {"first": "1", "last": "4"},
        {"first": "5", "last": "8"},
        {"first": "9", "last": "12"},
    ]
    assert all(not item["chapters"] for item in result["books"])


def test_refresh_uses_new_releases_and_preserves_catalogue_suggestions(tmp_path):
    database = prepare(tmp_path, "ongoing", mapped=False)
    entries = [MapEntry(("1",), ("1", "2"), True, "mangadex")]
    database.replace_chapter_map("manga-1", "mangadex", entries)
    database.upsert_chapters("manga-1", [chapter(n) for n in range(1, 7)])
    before = load_series_units(database, "manga-1")
    assert ranges(before) == [{"first": "1", "last": "3"}, {"first": "4", "last": "6"}]
    database.upsert_chapters("manga-1", [chapter(7), chapter(8)])
    after = load_series_units(database, "manga-1", sync=True)
    assert ranges(after) == [{"first": "1", "last": "4"}, {"first": "5", "last": "8"}]
    assert database.chapter_map("manga-1") == []
    assert database.chapter_map("manga-1", raw=True) == entries
    editor = OperatorChapterMap(database, build_service(tmp_path, database))
    assert editor.read("manga-1")["suggestions"] == [
        {"volume": "1", "first_chapter": "1"}
    ]
