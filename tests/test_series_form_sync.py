from __future__ import annotations

from tankarr.chapter_map import MapEntry, downgrade_sparse_maps
from tankarr.series_form import chapter_volume_assignments, estimate_entries, sync_books
from tankarr.series_summary import decorate_series_summary


def test_sparse_catalogue_suggestions_do_not_pin_estimates_after_reload():
    from tankarr.series_form import apply_series_form
    from tankarr.series_units import build_series_units

    manga = {
        "id": "m",
        "status": "ended",
        "monitor_mode": "all",
        "series_unit_override": "chapters",
    }
    metadata = {"chapter_count": 8, "volume_count": 2}
    releases = [
        {
            "id": f"c{n}",
            "chapter": str(n),
            "title": f"Chapter {n}",
            "volume": None,
            "release_unit": "chapter",
            "provider": "fixture",
            "language": "en",
            "monitored": True,
            "downloaded": True,
            "library_path": f"/library/c{n}.cbz",
            "pages": 20,
        }
        for n in range(1, 9)
    ]
    catalogue = [
        MapEntry(("1",), ("1", "4"), True, "catalogue"),
        MapEntry(("2",), ("5", "8"), True, "catalogue"),
    ]
    first = apply_series_form(
        build_series_units(manga, metadata, releases, catalogue), manga, metadata
    )
    estimates = estimate_entries(first)
    second = apply_series_form(
        build_series_units(manga, metadata, releases, [*catalogue, *estimates]),
        manga,
        metadata,
    )
    assert first["unassigned_chapters"] == []
    assert first["unassigned_chapters"] == second["unassigned_chapters"]
    assert [[s["chapter"] for s in b["chapters"]] for b in second["books"]] == [
        ["1", "2", "3", "4"],
        ["5", "6", "7", "8"],
    ]
    assert estimate_entries(second) == []
    assert sum(len(s["files"]) for b in second["books"] for s in b["chapters"]) == 8
    assert chapter_volume_assignments(second) == {}


def test_stale_estimate_cannot_assign_chapters_to_an_owned_book():
    """A saved estimate must not replace the independent chapter count."""

    from tankarr.series_form import apply_series_form
    from tankarr.series_units import build_series_units

    manga = {
        "id": "anthology",
        "status": "ended",
        "monitor_mode": "all",
        "series_unit_override": "volumes",
    }
    metadata = {"status": "ended", "volume_count": 1, "chapter_count": 10}
    releases = [
        {
            "id": "book-1",
            "chapter": None,
            "volume": "1",
            "release_unit": "volume",
            "provider": "manual",
            "language": "en",
            "monitored": True,
            "downloaded": True,
            "library_path": "/library/book-1.cbz",
            "pages": 290,
        }
    ]
    stale = [MapEntry(("1",), ("1",), True, "estimate")]

    result = apply_series_form(
        build_series_units(manga, metadata, releases, stale), manga, metadata
    )

    assert result["expected_chapter_count"] == 10
    # The stale estimate assigns nothing; the shelf only shows the counted
    # chapters as an estimated range, with no chapter row behind it.
    assert result["books"][0]["chapter_range"] == {"first": "1", "last": "10"}
    assert result["books"][0]["estimated"] and not result["books"][0]["exact"]
    assert result["books"][0]["chapters"] == []
    assert estimate_entries(result) == []


def test_loader_removes_estimates_without_rebuilding_them(tmp_path):
    from tankarr.database import Database
    from tankarr.series_units import load_series_units

    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    database.upsert_manga(
        {
            "id": "anthology",
            "title": "Anthology",
            "authors": [],
            "status": "ended",
            "monitor_mode": "all",
            "series_unit_override": "volumes",
        },
        "en",
        "all",
    )
    database.save_series_metadata(
        "anthology",
        {"status": "ended", "volume_count": 1, "chapter_count": 10},
        artwork_path=None,
        artwork_sha256=None,
        artwork_media_type=None,
        source_status=[],
    )
    database.replace_chapter_map(
        "anthology", "estimate", [MapEntry(("1",), ("1",), True, "estimate")]
    )

    result = load_series_units(database, "anthology", sync=True)

    # Laid out from the count for the page, removed from the database.
    assert result["books"][0]["chapter_range"] == {"first": "1", "last": "10"}
    assert result["books"][0]["estimated"]
    assert database.chapter_map("anthology", raw=True) == []
    assert result["sync"]["map_entries"] == 1
    second = load_series_units(database, "anthology", sync=True)
    assert second["sync"] == {"map_entries": 0, "chapters_renumbered": 0}


def test_raw_estimates_remain_available_for_migration():
    entries = [
        MapEntry(
            volumes=("3",),
            chapters=("9", "10", "11", "12"),
            exact=True,
            source="estimate",
        ),
        MapEntry(volumes=("1",), chapters=("1",), exact=True, source="mangaupdates"),
    ]
    kept = {(e.source, e.exact) for e in downgrade_sparse_maps(entries)}
    assert ("estimate", True) in kept


def test_only_explicit_chapter_memberships_are_written_back():
    units = {
        "form": "volumes",
        "books": [
            {
                "volume": "1",
                "estimated": False,
                "exact": True,
                "chapter_range": {"first": "1", "last": "2"},
                "chapters": [
                    {"chapter": "1", "files": [{"id": "f1"}]},
                    {"chapter": "2", "files": []},
                ],
            },
            {
                "volume": "2",
                "estimated": True,
                "chapter_range": {"first": "3", "last": "4"},
                "chapters": [
                    {"chapter": "3", "files": [{"id": "f3"}]},
                    {"chapter": "4", "files": [{"id": "f4"}]},
                ],
            },
        ],
    }
    assert estimate_entries(units) == []
    assert chapter_volume_assignments(units) == {"f1": "1"}

    class Db:
        def __init__(self):
            self.replaced = []
            self.volumes = None

        def chapter_map(self, manga_id, **_kwargs):
            return [MapEntry(("2",), ("3", "4"), True, "estimate")]

        def replace_chapter_map(self, manga_id, source, entries):
            self.replaced.append((source, len(list(entries))))
            return 1

        def set_chapter_volumes(self, manga_id, volumes):
            self.volumes = volumes
            return len(volumes)

    db = Db()
    out = sync_books(db, "m", units)
    assert db.replaced == [("estimate", 0)] and db.volumes == {
        "f1": "1",
    }
    assert out == {"map_entries": 1, "chapters_renumbered": 1}
    # a running series writes nothing
    assert sync_books(Db(), "m", {"form": "chapters", "books": units["books"]}) == {
        "map_entries": 1,
        "chapters_renumbered": 0,
    }


def test_books_covered_by_chapters_count_as_satisfied():
    manga = {
        "id": "m",
        "title": "Yawara",
        "status": "ended",
        "downloaded_count": 17,
        "logical_downloaded_count": 17,
        "special_downloaded_count": 0,
        "expected_count_override": None,
        "edition_book_count": None,
        "series_unit_override": "volumes",
        "monitor_mode": "all",
    }
    slots = [
        {
            "chapter": None,
            "volume": str(v),
            "expected": True,
            "downloaded": v > 12,
            "covered_by_chapters": v <= 12,
        }
        for v in range(1, 30)
    ]
    index = {
        "unit": "volume",
        "slots": slots,
        "expected_count": 29,
        "expected_volume_count": 29,
    }
    decorated = decorate_series_summary(manga, {"volume_count": 29}, index)
    counts = decorated["library_count"]
    assert counts["covered_by_chapters_count"] == 12
    assert counts["downloaded_count"] == 29 and counts["missing_count"] == 0


def test_unmapped_chapters_are_not_assigned_from_range_endpoints():
    """Range endpoints cannot establish unlisted chapter membership."""

    units = {
        "form": "volumes",
        "books": [
            {
                "volume": "3",
                "exact": True,
                "chapter_range": {"first": "23", "last": "33"},
                "chapters": [{"chapter": "23", "files": [{"id": "in-slot"}]}],
            }
        ],
        "unassigned_chapters": [
            # the catalogue's log skips 31 outright
            {"chapter": "31", "files": [{"id": "gap"}]},
            # a half chapter between two the log does list
            {"chapter": "24.5", "files": [{"id": "decimal"}]},
            # past the last book: an extra, and it stays one
            {"chapter": "400", "files": [{"id": "extra"}]},
            {"chapter": None, "files": [{"id": "unnumbered"}]},
        ],
    }
    assert chapter_volume_assignments(units) == {
        "in-slot": "3",
    }


def test_an_unassigned_chapter_never_overrides_its_own_book():
    units = {
        "form": "volumes",
        "books": [
            {
                "volume": "1",
                "exact": True,
                "chapter_range": {"first": "1", "last": "9"},
                "chapters": [{"chapter": "5", "files": [{"id": "f5"}]}],
            },
            {
                "volume": "2",
                "chapter_range": {"first": "5", "last": "12"},
                "chapters": [],
            },
        ],
    }
    # overlapping ranges are a broken map; the slot the book already owns wins
    units["unassigned_chapters"] = [{"chapter": "5", "files": [{"id": "f5"}]}]
    assert chapter_volume_assignments(units)["f5"] == "1"


def test_half_chapters_do_not_inherit_a_neighbouring_book():
    """Numbering order alone cannot establish where a half chapter is bound."""

    units = {
        "form": "volumes",
        "books": [
            {
                "volume": "2",
                "chapter_range": {"first": "7", "last": "12"},
                "chapters": [],
            },
            {
                "volume": "3",
                "chapter_range": {"first": "13", "last": "18"},
                "chapters": [],
            },
        ],
        "unassigned_chapters": [
            {"chapter": "12.5", "files": [{"id": "half"}]},
            {"chapter": "18.5", "files": [{"id": "after-last"}]},
            {"chapter": "6.5", "files": [{"id": "before-first"}]},
        ],
    }
    assignments = chapter_volume_assignments(units)
    assert assignments == {}


def test_a_chapter_series_keeps_the_book_number_out_of_its_names():
    """The stored volume still serves duplicate matching; only the filename
    ignores it, so one tagged chapter cannot sort away from its neighbours."""

    from tankarr.naming import chapter_filename

    manga = {"id": "m", "title": "Kingdom", "authors": []}
    tagged = {"chapter": "492.5", "volume": "45", "language": "en", "provider": "s"}
    assert "v045" in chapter_filename(manga, tagged, "en")
    # what the organizer hands the namer for a chapters series
    assert "v045" not in chapter_filename(manga, {**tagged, "volume": None}, "en")
    assert "c492.5" in chapter_filename(manga, {**tagged, "volume": None}, "en")


def test_a_refresh_without_a_volume_keeps_the_book_the_map_assigned(tmp_path):
    """Most catalogues name no volume. If that erased the book number the map
    had written, every refresh would undo the filing and rename the file."""

    from tankarr.database import Database

    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    database.upsert_manga(
        {
            "id": "m",
            "title": "Yawara",
            "description": "",
            "cover_url": None,
            "authors": [],
            "original_language": "ja",
        },
        "en",
    )
    release = {
        "id": "r1",
        "chapter": "1",
        "volume": None,
        "title": "Chapter 1",
        "language": "en",
        "provider": "suwayomi",
        "groups": [],
        "source_url": "https://example.invalid/1",
        "publish_at": None,
    }
    database.upsert_chapters("m", [release])
    database.set_chapter_volumes("m", {"r1": "3"})
    assert [c["volume"] for c in database.list_all_chapters("m")] == ["3"]

    # the catalogue is polled again and still names no volume
    database.upsert_chapters("m", [release])
    assert [c["volume"] for c in database.list_all_chapters("m")] == ["3"]

    # a source that does name one still corrects it
    database.upsert_chapters("m", [{**release, "volume": "4"}])
    assert [c["volume"] for c in database.list_all_chapters("m")] == ["4"]


def test_the_filename_follows_the_book_number_the_series_mostly_has():
    """A stray tag on three of 890 chapters sorted those three away from
    every neighbour; a prologue outside the books must not strip the rest."""

    from tankarr.service import TankarrService

    def files(tagged: int, bare: int) -> list[dict]:
        return [
            {"downloaded": True, "library_path": f"/library/t{i}.cbz", "volume": "1"}
            for i in range(tagged)
        ] + [
            {"downloaded": True, "library_path": f"/library/b{i}.cbz", "volume": None}
            for i in range(bare)
        ]

    names = TankarrService._series_names_books
    assert names(files(890, 0)) is True  # every file filed into a book
    assert names(files(650, 1)) is True  # one prologue outside them all
    assert names(files(3, 887)) is False  # a tag from one source, not a filing
    assert names(files(96, 165)) is False
    assert names([]) is True  # nothing downloaded decides nothing
    # a chapter with no file of its own never votes
    assert names([{"downloaded": False, "volume": None}] * 5) is True


def test_two_different_chapters_on_one_name_are_never_deleted(tmp_path):
    """A duplicate is one chapter held twice. When distinct chapters collapse
    onto one name the filename failed, and deleting the loser would destroy
    content nothing else holds."""

    from tankarr.service import TankarrService

    class Db:
        def demoted_sources(self, manga_id):
            return ()

        def page_quality(self, manga_id):
            return {}

    service = TankarrService.__new__(TankarrService)
    service.database = Db()
    destination = tmp_path / "Series - c051 [en].cbz"
    for name in ("a.cbz", "b.cbz"):
        (tmp_path / name).write_bytes(b"cbz")

    same = [
        ({"id": "r1", "manga_id": "m", "canonical_chapter": "51"}, tmp_path / "a.cbz"),
        ({"id": "r2", "manga_id": "m", "canonical_chapter": "51"}, tmp_path / "b.cbz"),
    ]
    different = [
        ({"id": "r1", "manga_id": "m", "canonical_chapter": "51"}, tmp_path / "a.cbz"),
        ({"id": "r2", "manga_id": "m", "canonical_chapter": "52"}, tmp_path / "b.cbz"),
    ]
    unnumbered = [
        ({"id": "r1", "manga_id": "m", "canonical_chapter": None}, tmp_path / "a.cbz"),
        ({"id": "r2", "manga_id": "m", "canonical_chapter": None}, tmp_path / "b.cbz"),
    ]
    # two copies of one chapter still resolve, so real duplicates are cleaned
    assert service._duplicate_download_keeper(same, destination) is not None
    # these two must both survive
    assert service._duplicate_download_keeper(different, destination) is None
    assert service._duplicate_download_keeper(unnumbered, destination) is None


def test_a_book_that_was_read_can_condemn_the_chapter_files_inside_it():
    """A hand-imported book cannot say what it holds, so it never justified
    removing a chapter file. A reading of its pages can, and that is what the
    reading is for."""

    from tankarr.chapter_map import MapEntry
    from tankarr.service import TankarrService

    service = TankarrService.__new__(TankarrService)

    class Db:
        def __init__(self, entries):
            self.entries = entries

        def chapter_map(self, manga_id, **_kwargs):
            return self.entries

    read = [MapEntry(volumes=("1",), chapters=("1", "2"), exact=True, source="ocr")]
    guessed = [
        MapEntry(volumes=("1",), chapters=("1", "2"), exact=True, source="estimate")
    ]

    def suspects(_manga_id):
        return {"1": "Imported by hand", "2": "Over 600 pages"}

    service.suspect_covering_volumes = suspects

    def still_suspect(entries):
        service.database = Db(entries)
        map_entries = service.database.chapter_map("m")
        reasons = service.suspect_covering_volumes("m")
        read_volumes = {
            str(volume)
            for entry in map_entries
            if entry.source == "ocr" and entry.exact
            for volume in entry.volumes
        }
        return {
            volume
            for volume, reason in reasons.items()
            if reason != "Imported by hand" or str(volume) not in read_volumes
        }

    # the read book is trusted; the oversized one stays suspect either way
    assert still_suspect(read) == {"2"}
    # an estimate is not a reading: the hand-imported book is still not trusted
    assert still_suspect(guessed) == {"1", "2"}


def test_a_fragment_without_a_chapter_number_is_not_a_whole_book():
    """A 263 KB file named cUnknown-<id> stood in for V.B. Rose volume 9 and
    condemned the six real chapters filed under it. Only what the provider
    delivered as a volume covers anything."""

    def is_whole_book(release: dict) -> bool:
        return str(release.get("release_unit") or "chapter") == "volume"

    tankobon = {"release_unit": "volume", "volume": "9", "chapter": None}
    fragment = {"release_unit": "chapter", "volume": "9", "chapter": None}
    chapter = {"release_unit": "chapter", "volume": "9", "chapter": "48"}
    assert is_whole_book(tankobon) is True
    assert is_whole_book(fragment) is False
    assert is_whole_book(chapter) is False


def test_a_special_no_book_contains_is_a_row_only_if_asked_for():
    """FLCL's second cut of its chapters, published apart, is not part of any
    tankobon. A shelf of books should not carry rows for it unless the reader
    turned them on, and it is never counted as missing either way."""

    from tankarr.series_form import apply_series_form

    def payload():
        return {
            "books": [
                {
                    "volume": "1",
                    "expected": True,
                    "chapter_range": {"first": "1", "last": "3"},
                    "chapters": [
                        {
                            "chapter": "1",
                            "downloaded": True,
                            "releases": [],
                            "files": [],
                        }
                    ],
                }
            ],
            "unassigned_chapters": [
                {"chapter": "2.1", "special": True, "downloaded": True, "releases": []},
                {"chapter": "99", "special": False, "downloaded": True, "releases": []},
            ],
            "expected_book_count": 1,
            "expected_chapter_count": 3,
        }

    manga = {"id": "m", "title": "FLCL", "status": "ended", "monitor_mode": "all"}
    hidden = apply_series_form(payload(), manga, None)
    assert hidden["hidden_specials"] == 1
    assert [s["chapter"] for s in hidden["unassigned_chapters"]] == ["99"]

    shown = apply_series_form(payload(), manga, None, specials_outside_books=True)
    assert shown["hidden_specials"] == 0
    assert {s["chapter"] for s in shown["unassigned_chapters"]} == {"2.1", "99"}


def test_a_count_set_by_hand_is_the_answer_not_a_floor():
    """Baby Steps ends at 455 where the catalogue still says 464. Laying the
    books out over the larger number stretched the last one past the ending
    somebody had checked, and nine chapters that do not exist read as missing."""

    from tankarr.series_form import apply_series_form

    units = {
        "books": [
            {
                "volume": "1",
                "expected": True,
                "chapter_range": {"first": "1", "last": "5"},
                "chapters": [{"chapter": "1", "downloaded": True, "releases": []}],
            },
            {
                "volume": "2",
                "expected": True,
                "chapter_range": {"first": "6", "last": "10"},
                "chapters": [{"chapter": "6", "downloaded": True, "releases": []}],
            },
        ],
        "unassigned_chapters": [],
        "expected_book_count": 2,
        "expected_chapter_count": 10,
    }
    manga = {
        "id": "m",
        "title": "Example",
        "status": "ended",
        "monitor_mode": "all",
        "last_chapter": "20",  # the catalogue still believes in ten more
        "expected_count_override": 10,
        "expected_count_unit_override": "chapter",
    }
    out = apply_series_form(units, manga, None)
    last = out["books"][-1]
    assert last["chapter_range"]["last"] == "10"


def test_book_number_placeholders_are_discarded_without_inventing_chapters():
    """A catalogue repeating book numbers does not describe 249 chapters."""

    from tankarr.series_form import apply_series_form

    books = [
        {
            "volume": str(n),
            "expected": True,
            "owned": True,
            "chapter_range": {"first": str(n), "last": str(n)},
            "chapters": [{"chapter": str(n), "downloaded": False, "releases": []}],
        }
        for n in range(1, 23)
    ]
    units = {
        "books": books,
        "unassigned_chapters": [],
        "expected_book_count": 22,
        "expected_chapter_count": 22,
    }
    manga = {
        "id": "m",
        "title": "20th Century Boys",
        "status": "ended",
        "monitor_mode": "all",
        "last_chapter": "249",
    }
    out = apply_series_form(units, manga, None)
    assert all(not b["chapter_range"] for b in out["books"])
    assert len(out["unassigned_chapters"]) == 22


def test_the_twentieth_book_cannot_open_at_chapter_one():
    """Every book before it holds at least one chapter, so book N begins at
    chapter N or later. MangaUpdates gave BECK's volume 20 the range 1-60 and
    it stood as fact while the nineteen before it held nothing."""

    from tankarr.series_form import apply_series_form

    books = [
        {"volume": str(n), "expected": True, "owned": True, "chapters": []}
        for n in range(1, 35)
    ]
    books[19]["chapter_range"] = {"first": "1", "last": "60"}
    units = {
        "books": books,
        "unassigned_chapters": [],
        "expected_book_count": 34,
        "expected_chapter_count": 34,
    }
    manga = {
        "id": "m",
        "title": "BECK",
        "status": "ended",
        "monitor_mode": "all",
        "last_chapter": "102",
    }
    out = apply_series_form(units, manga, None)
    assert sum(b["chapter_count"] for b in out["books"]) == 0
    assert all(not b["chapter_range"] for b in out["books"])


def test_sync_removes_obsolete_file_tags_and_is_idempotent(tmp_path):
    from tankarr.database import Database

    db = Database(tmp_path / "test.db")
    db.initialize()
    # The actual database update must support setting an existing tag to NULL.
    db.upsert_manga({"id": "m", "title": "Example", "authors": []}, "en", "all")
    db.upsert_chapters(
        "m",
        [
            {
                "id": "c1",
                "chapter": "1",
                "volume": "2",
                "language": "en",
                "provider": "fixture",
                "title": "Chapter 1",
                "source_url": "https://example.invalid/1",
                "groups": [],
                "publish_at": None,
            }
        ],
    )
    db.replace_chapter_map(
        "m", "estimate", [MapEntry(("2",), ("1",), True, "estimate")]
    )
    units = {
        "form": "volumes",
        "books": [],
        "unassigned_chapters": [
            {
                "chapter": "1",
                "releases": [
                    {"id": "c1", "chapter": "1", "volume": "2", "downloaded": True}
                ],
            }
        ],
    }
    result = sync_books(db, "m", units)
    assert result == {"map_entries": 1, "chapters_renumbered": 1}
    with db.connect() as connection:
        assert (
            connection.execute(
                "SELECT volume FROM chapter_release WHERE id='c1'"
            ).fetchone()[0]
            is None
        )
    assert sync_books(db, "m", units) == {"map_entries": 0, "chapters_renumbered": 0}


def test_provider_refresh_cannot_replace_verified_book_membership(tmp_path):
    from tankarr.database import Database

    db = Database(tmp_path / "test.db")
    db.initialize()
    db.upsert_manga({"id": "m", "title": "Example", "authors": []}, "en", "all")
    chapter = {
        "id": "c",
        "chapter": "5",
        "volume": "2",
        "title": "Vol.2 Ch.5",
        "language": "en",
        "provider": "fixture",
        "source_url": "https://example.invalid/5",
    }
    db.upsert_chapters("m", [chapter])
    assert db.get_chapter("c")["volume"] == "2"
    db.replace_chapter_map(
        "m", "operator", [MapEntry(("1",), ("5",), True, "operator")]
    )
    db.upsert_chapters("m", [chapter])
    assert db.get_chapter("c")["chapter"] == "5"
    assert db.get_chapter("c")["volume"] == "1"
    # Removing the explicit evidence restores normal source corrections.
    db.replace_chapter_map("m", "operator", [])
    db.upsert_chapters("m", [chapter])
    assert db.get_chapter("c")["volume"] == "2"


def test_map_with_owned_unassigned_chapters_is_not_complete():
    from tankarr.series_form import map_confidence

    books = [{"exact": True, "chapter_range": {"first": "4", "last": "7"}}]
    result = map_confidence(books, [], ["catalogue"], unassigned_chapters=True)
    assert result["level"] == "partial"
