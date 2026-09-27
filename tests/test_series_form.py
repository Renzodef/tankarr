from __future__ import annotations

import pytest

from tankarr.series_form import (
    PLAN_ASSEMBLE,
    PLAN_BOOK,
    PLAN_MISSING,
    PLAN_PARTIAL,
    apply_series_form,
    series_form,
    uniform_kind,
)


def slot(number: str, *, downloaded: bool) -> dict:
    return {
        "key": f"chapter:{number}",
        "chapter": number,
        "volume": None,
        "expected": True,
        "downloaded": downloaded,
        "missing": not downloaded,
        "files": [{"id": f"f{number}"}] if downloaded else [],
        "releases": [],
    }


def book(
    volume: str, chapters: list[dict] | None = None, *, owned: bool = False, rng=None
) -> dict:
    chapters = chapters or []
    return {
        "key": f"volume:{volume}",
        "volume": volume,
        "expected": True,
        "owned": owned,
        "status": "owned" if owned else "missing",
        "pages": 200 if owned else None,
        "chapter_range": rng,
        "chapters": chapters,
        "chapter_count": len(chapters),
        "downloaded_chapter_count": sum(bool(c["downloaded"]) for c in chapters),
        "missing_chapter_count": sum(bool(c["missing"]) for c in chapters),
        "exact": bool(rng),
        "covered_by_chapters": False,
    }


def units(books, unassigned=(), *, expected_books=None, expected_chapters=None):
    return {
        "manga_id": "m",
        "mode": "grouped",
        "books": books,
        "unassigned_chapters": list(unassigned),
        "expected_book_count": expected_books or len(books),
        "expected_chapter_count": expected_chapters,
        "hints": [],
    }


def test_running_and_webtoon_series_read_as_chapters():
    assert series_form({"status": "ongoing"}, None, 12)[0] == "chapters"
    assert (
        series_form({"status": "ended"}, {"reading_direction": "WEBTOON"}, 5)[0]
        == "chapters"
    )
    assert series_form({"status": "hiatus"}, None, None)[0] == "chapters"
    assert series_form({"status": "hiatus"}, None, 9)[0] == "volumes"
    # "on_hiatus" is what the official sources spell it: it is the same pause,
    # and must not be announced as a finished series.
    assert series_form({"status": "on_hiatus"}, None, 9) == (
        "volumes",
        "paused series with 9 known books",
    )
    assert series_form({"status": "on_hiatus"}, None, None)[0] == "chapters"
    assert series_form({"status": "ended"}, None, 44)[0] == "volumes"
    # a status override wins over the catalogue
    assert (
        series_form({"status": "ongoing", "status_override": "ended"}, None, 3)[0]
        == "volumes"
    )
    # a unit stored by hand decides nothing: the work and the shelf do. A
    # running series pinned to books is still read as a list of chapters.
    assert series_form(
        {"status": "ongoing", "series_unit_override": "volumes"}, None, 21
    ) == ("chapters", "running series: chapters until it ends")
    # and a webtoon pinned to books is still read by episode
    assert (
        series_form(
            {"status": "ended", "series_unit_override": "volumes"},
            {"reading_direction": "WEBTOON"},
            5,
        )[0]
        == "chapters"
    )
    # the pin towards chapters is ignored in the same way: a finished work
    # with a known number of books is a shelf of books
    assert (
        series_form({"status": "ended", "series_unit_override": "chapters"}, None, 44)[
            0
        ]
        == "volumes"
    )


def test_an_unfinished_shelf_of_chapters_is_not_divided_into_books():
    # Guyver: 200 chapters on disk, not one tankobon, and a catalogue map
    # covering every book. While the work is only paused that map describes
    # an edition nobody here owns, so the shelf stays the chapters it holds.
    def shelf():
        chapters = [slot(str(number), downloaded=True) for number in range(1, 13)]
        books = [
            book("1", chapters[:6], rng={"first": "1", "last": "6"}),
            book("2", chapters[6:], rng={"first": "7", "last": "12"}),
        ]
        for entry in books:
            entry["map_sources"] = ["operator"]
        return units(books, expected_books=2)

    paused = apply_series_form(shelf(), {"status": "hiatus"}, None)
    assert paused["form"] == "chapters"
    # the very same shelf, once the work has ended, is read as its books:
    # that is where the division into volumes is made.
    assert apply_series_form(shelf(), {"status": "ended"}, None)["form"] == "volumes"


@pytest.mark.parametrize(
    ("status", "complete", "form"),
    [
        ("ended", True, "volumes"),
        ("ended", False, "chapters"),
        ("ongoing", True, "chapters"),
    ],
)
def test_verified_printed_edition_overrides_webtoon_family(status, complete, form):
    books = [
        book(
            "1",
            [
                slot("0", downloaded=True),
                slot("1.1", downloaded=True),
                slot("1.2", downloaded=True),
            ],
            rng={"first": "0", "last": "1.2"},
        ),
        book(
            "2",
            [slot("2.1", downloaded=True), slot("2.2", downloaded=True)],
            rng={"first": "2.1", "last": "2.2"},
        ),
    ]
    for item in books:
        item["map_sources"] = ["operator"]
    if not complete:
        books[1]["map_sources"] = ["catalogue_hint"]
    result = apply_series_form(
        units(books, expected_books=2, expected_chapters=5),
        {"status": status, "monitor_mode": "all"},
        {
            "content_kind": "webtoon",
            "classification": {"subtype": "Manhwa"},
            "official_links": [{"url": "https://www.webtoons.com/en/example/1"}],
        },
        exact_sources=["operator"],
    )
    assert result["form"] == form
    assert not result["unassigned_chapters"]
    assert [s["chapter"] for b in result["books"] for s in b["chapters"]] == [
        "0",
        "1.1",
        "1.2",
        "2.1",
        "2.2",
    ]
    if form == "volumes":
        assert result["map_confidence"]["level"] == "exact"
        assert all(not b["estimated"] for b in result["books"])


def test_printed_manhwa_is_not_assumed_to_be_a_webtoon():
    metadata = {
        "content_kind": "webtoon",
        "classification": {"kind": "webtoon", "subtype": "Manhwa"},
        "reading_direction": "LEFT_TO_RIGHT",
        "official_links": [
            {"url": "https://drawnandquarterly.com/grass", "type": "publisher"}
        ],
    }
    assert series_form({"status": "ended"}, metadata, 1)[0] == "volumes"
    metadata["official_links"].append(
        {"url": "https://comic.naver.com/webtoon/list?titleId=1"}
    )
    assert series_form({"status": "ended"}, metadata, 1)[0] == "chapters"


def test_plan_states_and_uniform_kind():
    complete = [slot(str(n), downloaded=True) for n in range(1, 5)]
    half = [slot("5", downloaded=True), slot("6", downloaded=False)]
    books = [
        book("1", complete, rng={"first": "1", "last": "4"}),
        book("2", half, rng={"first": "5", "last": "6"}),
        book("3", owned=True, rng={"first": "7", "last": "9"}),
        book("4", rng={"first": "10", "last": "12"}),
    ]
    out = apply_series_form(
        units(books, expected_chapters=12), {"status": "ended"}, None
    )
    states = {b["volume"]: b["plan_state"] for b in out["books"]}
    assert states == {
        "1": PLAN_ASSEMBLE,
        "2": PLAN_PARTIAL,
        "3": PLAN_BOOK,
        "4": PLAN_MISSING,
    }
    assert out["form"] == "volumes" and out["uniform"] == "mixed"
    assert out["map_confidence"]["level"] == "exact"  # every book had a range
    assert "Search the book" in out["books"][3]["plan_text"]
    chapters_first = apply_series_form(
        units([dict(b) for b in books], expected_chapters=12),
        {"status": "ended"},
        None,
        preference="chapters",
    )
    assert chapters_first["books"][0]["plan_text"] == "Chapters 1–4 complete"
    assert "search chapters" in chapters_first["books"][1]["plan_text"]


def test_a_series_complete_from_chapters_makes_a_stray_book_redundant():
    """Last Quarter: one book on disk, every book's chapters complete."""

    books = [
        book(
            "1",
            [slot("1", downloaded=True), slot("2", downloaded=True)],
            owned=True,
            rng={"first": "1", "last": "2"},
        ),
        book("2", [slot("3", downloaded=True)], rng={"first": "3", "last": "3"}),
        book("3", [slot("4", downloaded=True)], rng={"first": "4", "last": "4"}),
    ]
    assert uniform_kind(books) == "chapters"
    out = apply_series_form(
        units(books, expected_chapters=4), {"status": "completed"}, None
    )
    assert out["uniform"] == "chapters"
    assert out["books"][0]["redundant_book"] is True
    assert "redundant" in out["books"][0]["plan_text"]


def test_every_book_on_disk_is_the_books_form_and_running_series_keep_chapters():
    books = [book("1", owned=True), book("2", owned=True)]
    out = apply_series_form(
        units(books, expected_chapters=20), {"status": "ended"}, None
    )
    assert out["uniform"] == "books" and out["form"] == "volumes"
    # A book count establishes no membership: the counted chapters are laid
    # out as an estimate, never as an exact range.
    assert [b["chapter_range"] for b in out["books"]] == [
        {"first": "1", "last": "10"},
        {"first": "11", "last": "20"},
    ]
    assert all(b["estimated"] and not b["exact"] for b in out["books"])
    running = apply_series_form(
        units([book("1")], [slot("7", downloaded=True)]), {"status": "ongoing"}, None
    )
    assert running["form"] == "chapters"
    assert [c["chapter"] for c in running["unassigned_chapters"]] == ["7"]


def test_a_release_outside_the_edition_is_not_a_row():
    """Master Keaton: retired tankobon 16-18 and a "vol 01-18" pack release
    still exist as releases, none on disk; the 12-book edition shows 12 rows."""

    books = [book(str(k), owned=True) for k in range(1, 4)]
    stray = [book("16", rng=None), book("1-18", rng=None)]
    for item in stray:
        item["expected"] = False
    out = apply_series_form(
        units([*books, *stray], expected_books=3, expected_chapters=24),
        {"status": "ended"},
        None,
    )
    assert [b["volume"] for b in out["books"]] == ["1", "2", "3"]
    assert out["uniform"] == "books"


def test_owned_release_outside_the_edition_does_not_reduce_map_confidence():
    expected = [
        book("1", [slot("1", downloaded=True)], rng={"first": "1", "last": "1"}),
        book("2", [slot("2", downloaded=True)], rng={"first": "2", "last": "2"}),
    ]
    extra = book("21", owned=True, rng={"first": "3", "last": "3"})
    extra["expected"] = False
    extra["estimated"] = True

    out = apply_series_form(
        units([*expected, extra], expected_books=2, expected_chapters=2),
        {"status": "ended"},
        None,
    )

    assert [row["volume"] for row in out["books"]] == ["1", "2", "21"]
    assert out["map_confidence"] == {
        "level": "exact",
        "estimated_books": 0,
        "unknown_books": 0,
        "sources": [],
        "hint_sources": [],
    }


def test_mixed_catalogue_and_saved_estimate_remain_labelled_estimated():
    books = [
        book(
            "1",
            [slot(str(n), downloaded=True) for n in range(1, 5)],
            rng={"first": "1", "last": "4"},
        )
    ]
    books[0]["map_sources"] = ["mangaupdates", "estimate"]
    result = apply_series_form(
        units(books, expected_chapters=4), {"status": "ended"}, None
    )
    assert result["books"][0]["estimated"]


def test_empty_books_between_described_neighbours_share_the_loose_chapters():
    from tankarr.series_form import chapter_volume_assignments

    books = [
        book(
            "1",
            [slot(str(n), downloaded=True) for n in range(1, 6)],
            rng={"first": "1", "last": "5"},
        ),
        book("2"),
        book(
            "3",
            [slot(str(n), downloaded=True) for n in range(11, 16)],
            rng={"first": "11", "last": "15"},
        ),
        book("4"),
        book("5"),
    ]
    loose = [
        slot(str(n), downloaded=n != 20)
        for n in list(range(6, 11)) + list(range(16, 23))
    ]
    result = apply_series_form(
        units(books, loose, expected_chapters=22), {"status": "ended"}, None
    )
    by_volume = {b["volume"]: b for b in result["books"]}
    assert by_volume["2"]["chapter_range"] == {"first": "6", "last": "10"}
    assert [s["chapter"] for s in by_volume["4"]["chapters"]] == [
        "16",
        "17",
        "18",
        "19",
    ]
    assert [s["chapter"] for s in by_volume["5"]["chapters"]] == ["20", "21", "22"]
    for volume in ("2", "4", "5"):
        assert by_volume[volume]["estimated"] is True
        assert by_volume[volume]["exact"] is False
        assert by_volume[volume]["covered_by_chapters"] is False
        assert by_volume[volume]["can_assemble"] is False
        assert "estimate" in by_volume[volume]["map_sources"]
    assert by_volume["2"]["status"] == "covered_by_chapters"
    assert by_volume["5"]["status"] == "missing"
    assert by_volume["5"]["missing_chapter_count"] == 1
    assert result["unassigned_chapters"] == []
    # Only the described books hand explicit memberships to the database.
    assert set(chapter_volume_assignments(result).values()) == {"1", "3"}
    assert result["map_confidence"]["level"] == "partial"


def test_without_any_described_book_the_run_is_divided_equally():
    books = [book("1"), book("2"), book("3")]
    loose = [slot(str(n), downloaded=True) for n in range(1, 8)]
    result = apply_series_form(
        units(books, loose, expected_chapters=7), {"status": "ended"}, None
    )
    counts = [b["chapter_count"] for b in result["books"]]
    assert counts == [3, 2, 2]
    assert all(b["estimated"] and not b["exact"] for b in result["books"])
    assert result["map_confidence"]["level"] == "estimated"
    assert result["uniform"] == "chapters"


def test_layout_estimate_skips_owned_books_specials_and_in_range_strays():
    books = [
        book(
            "1",
            [slot("1", downloaded=True), slot("3", downloaded=True)],
            rng={"first": "1", "last": "3"},
        ),
        book("2", owned=True),
        book("3"),
    ]
    special = slot("3.5", downloaded=True)
    special["special"] = True
    stray = slot("2", downloaded=True)  # inside book 1's range but not in its map
    loose = [stray, special, slot("4", downloaded=True), slot("5", downloaded=False)]
    result = apply_series_form(
        units(books, loose, expected_chapters=5),
        {"status": "ended"},
        None,
        specials_outside_books=True,
    )
    by_volume = {b["volume"]: b for b in result["books"]}
    # An owned book with unknown contents takes its share too, and a chapter
    # laid into a book you own is in the book, not missing.
    assert by_volume["2"]["owned"] and by_volume["2"]["estimated"]
    assert [s["chapter"] for s in by_volume["2"]["chapters"]] == ["4"]
    assert by_volume["2"]["chapters"][0]["missing"] is False
    assert by_volume["2"]["plan_state"] == "book"
    assert [s["chapter"] for s in by_volume["3"]["chapters"]] == ["5"]
    assert by_volume["3"]["status"] == "missing"
    assert {s["chapter"] for s in result["unassigned_chapters"]} == {"2", "3.5"}


def test_books_owned_without_any_chapter_file_share_the_counted_chapters():
    books = [book("1", owned=True), book("2", owned=True)]
    units_payload = units(books, expected_chapters=7)
    units_payload["warning"] = "Which chapters each book contains is not known"
    result = apply_series_form(units_payload, {"status": "ended"}, None)
    first, second = result["books"]
    assert first["chapter_range"] == {"first": "1", "last": "4"}
    assert second["chapter_range"] == {"first": "5", "last": "7"}
    for item in (first, second):
        assert item["estimated"] and not item["exact"]
        assert item["chapters"] == [] and item["chapter_count"] in (3, 4)
        assert item["plan_state"] == "book"
        assert not item["covered_by_chapters"] and not item.get("can_assemble")
    assert result["warning"] is None
    assert result["uniform"] == "books"
    assert result["map_confidence"]["level"] == "estimated"


def test_books_only_shelf_without_a_count_carries_no_unmapped_notice():
    books = [book("1", owned=True)]
    units_payload = units(books, expected_chapters=None)
    units_payload["warning"] = "Which chapters each book contains is not known"
    result = apply_series_form(units_payload, {"status": "ended"}, None)
    (only,) = result["books"]
    assert only["chapter_range"] is None and not only["estimated"]
    assert result["warning"] is None


def test_a_count_below_the_book_count_lays_nothing_out():
    books = [book(str(n), owned=True) for n in range(1, 4)]
    units_payload = units(books, expected_chapters=2)
    units_payload["warning"] = "Which chapters each book contains is not known"
    result = apply_series_form(units_payload, {"status": "ended"}, None)
    assert all(item["chapter_range"] is None for item in result["books"])
    assert result["warning"] is None


def test_counted_chapters_fill_only_the_books_between_described_neighbours():
    books = [
        book(
            "1",
            [slot(str(n), downloaded=True) for n in range(1, 6)],
            rng={"first": "1", "last": "5"},
        ),
        book("2", owned=True),
        book("3"),
    ]
    result = apply_series_form(
        units(books, expected_chapters=12), {"status": "ended"}, None
    )
    by_volume = {b["volume"]: b for b in result["books"]}
    assert by_volume["1"]["exact"] and not by_volume["1"]["estimated"]
    assert by_volume["2"]["chapter_range"] == {"first": "6", "last": "9"}
    assert by_volume["3"]["chapter_range"] == {"first": "10", "last": "12"}
    assert (
        by_volume["3"]["status"] == "missing"
        and by_volume["3"]["plan_state"] == "missing"
    )
    assert result["warning"] is None


def _tagged(number: str, tag: int, *, downloaded: bool = False) -> dict:
    item = slot(number, downloaded=downloaded)
    item["releases"] = [{"id": f"r{number}", "title": f"Vol.{tag} Chapter {number}"}]
    return item


def _phantom(number: str) -> dict:
    item = slot(number, downloaded=False)
    item["releases"] = []
    return item


def test_source_tags_never_replace_operator_boundaries():
    """Nana: the map said vol 1 = 0.1–0.3 and vol 2 = 1–4 while every
    source files 1–2 under "Vol.1" and 3–4 under "Vol.2"."""

    books = [
        book(
            "1",
            [_phantom("0.1"), _phantom("0.2"), _phantom("0.3")],
            owned=True,
            rng={"first": "0.1", "last": "0.3"},
        ),
        book(
            "2",
            [_tagged("1", 1), _tagged("2", 1), _tagged("3", 2), _tagged("4", 2)],
            owned=True,
            rng={"first": "1", "last": "4"},
        ),
        book(
            "3",
            [_tagged(str(n), 3) for n in range(5, 9)],
            owned=True,
            rng={"first": "5", "last": "8"},
        ),
        book(
            "4",
            [_tagged(str(n), 4) for n in range(9, 13)],
            owned=True,
            rng={"first": "9", "last": "12"},
        ),
    ]
    for item in books:
        item["map_sources"] = ["operator"]
    result = apply_series_form(
        units(books, expected_chapters=12), {"status": "hiatus"}, None
    )
    by_volume = {b["volume"]: b for b in result["books"]}
    assert [s["chapter"] for s in by_volume["1"]["chapters"]] == ["0.1", "0.2", "0.3"]
    assert by_volume["1"]["chapter_range"] == {"first": "0.1", "last": "0.3"}
    assert [s["chapter"] for s in by_volume["2"]["chapters"]] == ["1", "2", "3", "4"]
    assert by_volume["2"]["exact"] and not by_volume["2"]["estimated"]
    assert by_volume["3"]["exact"] and not by_volume["3"]["estimated"]
    assert result["hints"] == []


def test_source_tags_that_disagree_with_the_map_throughout_move_nothing():
    books = [
        book(
            "1",
            [_phantom("0.1"), _phantom("0.2")],
            owned=True,
            rng={"first": "0.1", "last": "0.2"},
        ),
        book(
            "2",
            [_tagged("1", 1), _tagged("2", 1), _tagged("3", 1), _tagged("4", 1)],
            owned=True,
            rng={"first": "1", "last": "4"},
        ),
        book(
            "3",
            [_tagged(str(n), 2) for n in range(5, 9)],
            owned=True,
            rng={"first": "5", "last": "8"},
        ),
    ]
    for item in books:
        item["map_sources"] = ["operator"]
    result = apply_series_form(
        units(books, expected_chapters=8), {"status": "ended"}, None
    )
    by_volume = {b["volume"]: b for b in result["books"]}
    assert [s["chapter"] for s in by_volume["1"]["chapters"]] == ["0.1", "0.2"]
    assert [s["chapter"] for s in by_volume["2"]["chapters"]] == ["1", "2", "3", "4"]
    assert by_volume["2"]["exact"]
    assert not [h for h in result["hints"] if h["source"] == "source_tags"]


def test_content_verdicts_do_not_replace_operator_boundaries():
    """Nana: the map files 79–80 in book 21 and leaves 81 loose; the pages
    of the 80 file are not in the book, those of the 81 file are."""

    inside = slot("81", downloaded=True)
    books = [
        book(
            "21",
            [slot("79", downloaded=False), slot("80", downloaded=True)],
            owned=True,
            rng={"first": "79", "last": "80"},
        ),
    ]
    books[0]["map_sources"] = ["operator"]
    content = {
        "f80": {"chapter_id": "f80", "verdict": "outside", "volume": ""},
        "f81": {"chapter_id": "f81", "verdict": "inside", "volume": "21"},
    }
    result = apply_series_form(
        units(books, [inside], expected_chapters=81),
        {"status": "hiatus"},
        None,
        content=content,
    )
    (only,) = result["books"]
    assert [s["chapter"] for s in only["chapters"]] == ["79", "80"]
    assert only["map_sources"] == ["operator"]
    assert [s["chapter"] for s in result["unassigned_chapters"]] == ["81"]
    assert result["content_notes"] == []


def test_without_verdicts_the_shelf_carries_no_content_notes():
    result = apply_series_form(
        units([book("1", owned=True)]), {"status": "ended"}, None
    )
    assert result["content_notes"] == []


def test_a_whole_chapter_whose_parts_the_books_hold_is_not_hunted():
    """Violence Jack: one source numbers the story 2, the map files 2.1 and
    2.2; the whole "2" is the same pages under another count."""

    books = [
        book(
            "1",
            [slot("1", downloaded=True), slot("2.1", downloaded=True)],
            owned=True,
            rng={"first": "1", "last": "2.1"},
        ),
        book(
            "2",
            [slot("2.2", downloaded=True), slot("3", downloaded=True)],
            owned=True,
            rng={"first": "2.2", "last": "3"},
        ),
    ]
    for item in books:
        item["map_sources"] = ["operator"]
    ghost = slot("2", downloaded=False)
    real = slot("4", downloaded=False)
    result = apply_series_form(
        units(books, [ghost, real], expected_chapters=4), {"status": "hiatus"}, None
    )
    assert [s["chapter"] for s in result["unassigned_chapters"]] == ["4"]
    assert result["hidden_specials"] == 1


def test_finished_books_do_not_extend_operator_boundaries_to_absorb_a_tail():
    """BECK: 34 books on the shelf, the map ends at 102, chapter 103 hung
    loose and would be fetched only to be retired again."""

    books = [
        book(
            "33",
            [slot(str(n), downloaded=False) for n in range(97, 100)],
            owned=True,
            rng={"first": "97", "last": "99"},
        ),
        book(
            "34",
            [slot(str(n), downloaded=False) for n in range(100, 103)],
            owned=True,
            rng={"first": "100", "last": "102"},
        ),
    ]
    for item in books:
        item["map_sources"] = ["operator"]
    tail = slot("103", downloaded=False)
    gap = slot("99.5", downloaded=False)
    result = apply_series_form(
        units(books, [tail, gap], expected_chapters=103), {"status": "completed"}, None
    )
    by_volume = {b["volume"]: b for b in result["books"]}
    assert [s["chapter"] for s in by_volume["34"]["chapters"]] == [
        "100",
        "101",
        "102",
    ]
    assert not by_volume["34"]["estimated"] and by_volume["34"]["status"] == "owned"
    assert by_volume["34"]["chapter_range"] == {"first": "100", "last": "102"}
    assert [s["chapter"] for s in by_volume["33"]["chapters"]] == [
        "97",
        "98",
        "99",
    ]
    assert result["unassigned_chapters"] == [tail, gap]
    assert not any(s["missing"] for b in result["books"] for s in b["chapters"])


def test_a_paused_work_keeps_its_loose_tail():
    """Nana: on hiatus, 81–84 were never collected; they stay loose."""

    books = [
        book(
            "21",
            [slot("78", downloaded=False)],
            owned=True,
            rng={"first": "78", "last": "80"},
        )
    ]
    books[0]["map_sources"] = ["operator"]
    tail = slot("81", downloaded=False)
    result = apply_series_form(
        units(books, [tail], expected_chapters=84), {"status": "hiatus"}, None
    )
    assert [s["chapter"] for s in result["unassigned_chapters"]] == ["81"]


def test_a_decimal_extra_does_not_keep_a_book_from_being_complete_from_chapters():
    """Iryū 10: every numbered chapter on disk, a "74.22" extra listed by a
    source is not one of the book's chapters."""

    extra = slot("74.22", downloaded=False)
    extra["expected"] = False
    extra["special"] = True
    extra["missing"] = False
    chapters = [slot(str(n), downloaded=True) for n in range(74, 82)] + [extra]
    result = apply_series_form(
        units([book("10", chapters, rng={"first": "74", "last": "81"})]),
        {"status": "ended"},
        None,
    )
    (only,) = result["books"]
    assert only["plan_state"] == PLAN_ASSEMBLE
    assert result["uniform"] == "chapters"


def test_an_owned_book_laid_out_from_loose_chapters_stays_owned():
    books = [
        book(
            "1",
            [slot("1", downloaded=True)],
            owned=True,
            rng={"first": "1", "last": "1"},
        ),
        book("2", owned=True),
    ]
    books[0]["map_sources"] = ["operator"]
    result = apply_series_form(
        units(books, [slot("2", downloaded=False)], expected_chapters=2),
        {"status": "hiatus"},
        None,
    )
    by_volume = {b["volume"]: b for b in result["books"]}
    assert by_volume["2"]["status"] == "owned" and by_volume["2"]["chapter_range"] == {
        "first": "2",
        "last": "2",
    }


def test_a_running_work_owned_as_books_is_read_as_books():
    """Moonlight Mile: 24 tankobon on disk, no chapter file at all, and the
    page showed a chapter list of 24 rows with not one book on it."""

    books = [book(str(n), owned=True) for n in range(1, 4)]
    result = apply_series_form(units(books), {"status": "ongoing"}, None)
    assert result["form"] == "volumes"
    assert result["form_reason"] == "the shelf holds books, not chapters"
    assert result["uniform"] == "books"
    assert all(b["plan_state"] == PLAN_BOOK for b in result["books"])


def test_a_running_shelf_with_chapter_files_stays_on_chapters():
    """Adekan: 13 books and 73 chapter files — still a chapter shelf."""

    owned = book(
        "1", [slot("1", downloaded=True)], owned=True, rng={"first": "1", "last": "1"}
    )
    result = apply_series_form(
        units([owned], [slot("2", downloaded=True)]), {"status": "ongoing"}, None
    )
    assert result["form"] == "chapters"


def test_a_webtoon_bundle_does_not_turn_the_page_into_books():
    books = [book(str(n), owned=True) for n in range(1, 4)]
    result = apply_series_form(
        units(books), {"status": "ongoing"}, {"content_kind": "webtoon"}
    )
    assert result["form"] == "chapters"


def test_a_finished_shelf_without_a_map_splits_the_count_evenly():
    """Captain Harlock: three omnibus books, 55 counted chapters and no map.
    Book one read "ch. 1–55" because the finished-shelf pass ran before the
    even split and every chapter fell into the first book by default."""

    books = [book(str(n), owned=True) for n in (1, 2, 3)]
    loose = [slot(str(n), downloaded=False) for n in range(1, 56)]
    result = apply_series_form(
        units(books, loose, expected_chapters=55), {"status": "ended"}, None
    )
    by_volume = {b["volume"]: b for b in result["books"]}
    assert by_volume["1"]["chapter_range"] == {"first": "1", "last": "19"}
    assert by_volume["2"]["chapter_range"] == {"first": "20", "last": "37"}
    assert by_volume["3"]["chapter_range"] == {"first": "38", "last": "55"}
    assert all(b["estimated"] and b["status"] == "owned" for b in result["books"])
    assert result["unassigned_chapters"] == []
    assert result["warning"] is None


def test_the_books_only_rule_leaves_a_verified_division_alone():
    """A shelf whose division is verified keeps that reason, not the
    generic one: the page shows why the boundaries are trusted."""

    books = [
        book(
            "1",
            [slot("1", downloaded=False)],
            owned=True,
            rng={"first": "1", "last": "1"},
        ),
        book(
            "2",
            [slot("2", downloaded=False)],
            owned=True,
            rng={"first": "2", "last": "2"},
        ),
    ]
    for item in books:
        item["map_sources"] = ["operator"]
        item["exact"] = True
    result = apply_series_form(
        units(books, expected_books=2), {"status": "ended"}, None
    )
    assert result["form"] == "volumes"
    assert result["form_reason"] == "saved division into 2 books"


def test_a_chapter_shelf_carries_no_unmapped_notice():
    """ONE PIECE, HUNTER×HUNTER and fourteen others showed "which chapters
    each book contains is not known" above a page with no book on it."""

    units_payload = units([], [slot("1", downloaded=True)])
    units_payload["warning"] = "Which chapters each book contains is not known"
    result = apply_series_form(units_payload, {"status": "ongoing"}, None)
    assert result["form"] == "chapters"
    assert result["warning"] is None


def test_a_running_shelf_of_books_is_laid_out_with_the_catalogue_count():
    """Moonlight Mile: 24 tankobon on disk, a running work whose total is
    still moving, and books 4-24 showed nothing at all. The count lays them
    out as a division on the shelf, never as evidence."""

    books = [
        book(
            "1",
            [slot(str(n), downloaded=False) for n in range(1, 9)],
            owned=True,
            rng={"first": "1", "last": "8"},
        ),
        book("2", owned=True),
        book("3", owned=True),
    ]
    books[0]["map_sources"] = ["operator"]
    payload = units(books, expected_chapters=None)
    payload["catalogue_chapter_count"] = 24
    result = apply_series_form(payload, {"status": "ongoing"}, None)
    by_volume = {b["volume"]: b for b in result["books"]}
    assert by_volume["1"]["chapter_range"] == {"first": "1", "last": "8"}
    assert by_volume["2"]["chapter_range"] == {"first": "9", "last": "16"}
    assert by_volume["3"]["chapter_range"] == {"first": "17", "last": "24"}
    assert by_volume["2"]["estimated"] and by_volume["2"]["status"] == "owned"


def test_a_one_shot_of_a_single_chapter_shows_it():
    """A lone book holding "1 chapter" is not a placeholder: sixteen
    single-volume works showed no contents at all."""

    result = apply_series_form(
        units([book("1", owned=True)], expected_chapters=1), {"status": "ended"}, None
    )
    (only,) = result["books"]
    assert only["chapter_range"] == {"first": "1", "last": "1"}
    assert only["estimated"] and only["status"] == "owned"


def test_a_count_below_a_multi_book_shelf_still_lays_nothing_out():
    books = [book(str(n), owned=True) for n in (1, 2, 3)]
    result = apply_series_form(
        units(books, expected_chapters=2), {"status": "ended"}, None
    )
    assert all(b["chapter_range"] is None for b in result["books"])


def test_a_running_chapter_shelf_still_lays_its_books_out():
    """redEyes: 27 books, 79 chapters on disk and every book blank; Ate Ya:
    chapters 9-14 orphaned between two described books. A running work keeps
    the chapter form, but the shelf still shows what each book holds."""

    from tankarr.series_form import chapter_volume_assignments

    books = [book(str(n)) for n in range(1, 4)]
    loose = [slot(str(n), downloaded=True) for n in range(1, 10)]
    result = apply_series_form(
        units(books, loose, expected_chapters=9), {"status": "ongoing"}, None
    )

    assert result["form"] == "chapters"
    by_volume = {b["volume"]: b for b in result["books"]}
    assert by_volume["1"]["chapter_range"] == {"first": "1", "last": "3"}
    assert by_volume["2"]["chapter_range"] == {"first": "4", "last": "6"}
    assert by_volume["3"]["chapter_range"] == {"first": "7", "last": "9"}
    assert all(b["estimated"] and not b["exact"] for b in result["books"])
    assert result["unassigned_chapters"] == []
    # Layout only: a chapter shelf hands no membership to the database.
    assert chapter_volume_assignments(result) == {}


@pytest.mark.parametrize("status", ["ongoing", "hiatus", "ended"])
def test_even_layout_is_recomputed_when_new_chapters_arrive(status):
    from tankarr.series_form import chapter_volume_assignments

    def layout(count):
        return apply_series_form(
            units(
                [book(str(n)) for n in range(1, 4)],
                [slot(str(n), downloaded=True) for n in range(1, count + 1)],
                expected_chapters=count,
            ),
            {"status": status},
            None,
        )

    before, after = layout(9), layout(12)
    assert [item["chapter_range"] for item in before["books"]] == [
        {"first": "1", "last": "3"},
        {"first": "4", "last": "6"},
        {"first": "7", "last": "9"},
    ]
    assert [item["chapter_range"] for item in after["books"]] == [
        {"first": "1", "last": "4"},
        {"first": "5", "last": "8"},
        {"first": "9", "last": "12"},
    ]
    assert all(item["estimated"] and not item["exact"] for item in after["books"])
    assert chapter_volume_assignments(after) == {}


def test_a_lone_book_with_a_stray_chapter_is_left_alone():
    """With one book and one loose chapter there is nothing to divide: an
    invented range would say less than an honest blank."""

    result = apply_series_form(
        units([book("1")], [slot("7", downloaded=True)]), {"status": "ongoing"}, None
    )

    assert result["form"] == "chapters"
    assert [c["chapter"] for c in result["unassigned_chapters"]] == ["7"]
    assert result["books"][0]["chapter_range"] is None


def test_estimated_layout_does_not_claim_edition_files_are_present():
    first = slot("1", downloaded=True)
    result = apply_series_form(
        units([book("1", [first])], expected_books=1),
        {"status": "ended"},
        {"volume_count": 1},
    )
    summary = result["completeness"]
    assert summary["indexed_chapters_on_disk"] == 1
    assert summary["owned_books"] == 0
    assert summary["edition_files_complete"] is False
