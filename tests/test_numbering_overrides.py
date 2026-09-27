from __future__ import annotations

import pytest

from tankarr.chapter_map import MapEntry
from tankarr.database import Database


@pytest.fixture
def database(tmp_path):
    db = Database(tmp_path / "library.db")
    db.initialize()
    db.upsert_manga({"id": "series", "title": "Example", "authors": []}, "en", "all")
    db.upsert_chapters("series", [release("mirror-a"), release("mirror-b")])
    return db


def release(ident, number="5"):
    return {
        "id": ident,
        "chapter": number,
        "volume": "2",
        "title": f"Chapter {number}",
        "provider": ident,
        "language": "en",
        "source_url": f"https://example.invalid/{ident}/{number}",
    }


def test_correspondence_is_source_scoped_and_survives_refresh(database):
    database.replace_chapter_map(
        "series",
        "operator",
        [
            MapEntry(("1",), ("5",), True, "operator"),
            MapEntry(("2",), ("11",), True, "operator"),
        ],
    )
    database.replace_numbering_overrides(
        "series", {"mirror-a": "11"}, evidence="Printed contents: part 5 = item 11"
    )
    database.upsert_chapters("series", [release("mirror-a"), release("mirror-b")])
    a, b = database.get_chapter("mirror-a"), database.get_chapter("mirror-b")
    assert (a["source_chapter"], a["chapter"], a["volume"]) == ("5", "11", "2")
    assert (b["source_chapter"], b["chapter"], b["volume"]) == ("5", "5", "1")
    assert a["numbering_method"] == "operator_correspondence"
    assert a["numbering_evidence"]["evidence"].startswith("Printed contents")


def test_withdrawal_and_provider_renumbering_do_not_reuse_old_evidence(database):
    database.replace_numbering_overrides("series", {"mirror-a": "11"}, evidence="TOC")
    database.replace_numbering_overrides("series", {}, evidence="")
    assert database.get_chapter("mirror-a")["chapter"] == "5"
    database.replace_numbering_overrides("series", {"mirror-a": "11"}, evidence="TOC")
    database.upsert_chapters("series", [release("mirror-a", "6")])
    assert database.get_chapter("mirror-a")["chapter"] == "6"
    assert (
        database.get_chapter("mirror-a")["numbering_method"]
        != "operator_correspondence"
    )


def test_invalid_batch_does_not_erase_previous_correspondences(database):
    database.replace_numbering_overrides("series", {"mirror-a": "11"}, evidence="TOC")
    for assignments, evidence in [
        ({"mirror-a": "NaN"}, "TOC"),
        ({"unknown": "1"}, "TOC"),
        ({"mirror-a": "7"}, ""),
    ]:
        with pytest.raises(ValueError):
            database.replace_numbering_overrides(
                "series", assignments, evidence=evidence
            )
    database.upsert_chapters("series", [release("mirror-a")])
    assert database.get_chapter("mirror-a")["chapter"] == "11"


def test_reviewed_unknown_book_does_not_reuse_rejected_catalogue_boundaries(database):
    database.replace_chapter_map(
        "series",
        "catalogue",
        [
            MapEntry(("1",), ("1", "2", "3", "4", "5"), True, "catalogue"),
        ],
    )
    database.replace_chapter_map(
        "series",
        "operator",
        [
            MapEntry(("1",), (), True, "operator"),
        ],
    )
    entries = database.chapter_map("series")
    assert entries == [MapEntry(("1",), (), True, "operator")]
    from tankarr.series_units import load_series_units

    units = load_series_units(database, "series")
    assert all(book["chapter_range"] is None for book in units["books"])


@pytest.mark.parametrize("chapters", [(), ("1", "2", "3", "4")])
def test_source_refresh_cannot_restore_rejected_reviewed_book_membership(
    database, chapters
):
    database.replace_chapter_map(
        "series", "operator", [MapEntry(("2",), chapters, True, "operator")]
    )
    database.upsert_chapters("series", [release("mirror-a")])
    row = database.get_chapter("mirror-a")
    assert row["chapter"] == "5"
    assert row["volume"] is None


def test_reviewed_book_does_not_clear_whole_volume_or_unreviewed_book(database):
    database.replace_chapter_map(
        "series", "operator", [MapEntry(("2",), (), True, "operator")]
    )
    database.upsert_chapters(
        "series",
        [
            {**release("book"), "release_unit": "volume", "chapter": None},
            {**release("outside"), "volume": "3"},
        ],
    )
    assert database.get_chapter("book")["volume"] == "2"
    assert database.get_chapter("outside")["volume"] == "3"


def test_webtoon_seasons_are_not_printed_books_and_refresh_cannot_restore_them(
    database,
):
    database.save_series_metadata(
        "series",
        {"format": "webtoon"},
        artwork_path=None,
        artwork_sha256=None,
        artwork_media_type=None,
        source_status=[],
    )
    season = MapEntry(("2",), ("5",), True, "catalogue")
    database.replace_chapter_map("series", "catalogue", [season])
    database.upsert_chapters("series", [release("mirror-a")])
    assert database.chapter_map("series", raw=True) == [season]
    assert database.chapter_map("series") == []
    assert database.get_chapter("mirror-a")["volume"] is None
    verified = MapEntry(("1",), ("5",), True, "operator")
    database.replace_chapter_map("series", "operator", [verified])
    database.upsert_chapters("series", [release("mirror-a")])
    assert database.chapter_map("series") == [verified]
    assert database.get_chapter("mirror-a")["volume"] == "1"
