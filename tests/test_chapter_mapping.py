import pytest

from tankarr.chapter_mapping import build_chapter_index


def release(
    chapter: str,
    *,
    volume: str | None = None,
    downloaded: bool = True,
    provider: str = "mangadex",
    identifier: str | None = None,
):
    return {
        "id": identifier or f"{provider}-{volume}-{chapter}",
        "chapter": chapter,
        "volume": volume,
        "provider": provider,
        "downloaded": downloaded,
        "monitored": True,
        "version": 1,
        "pages": 20,
        "publish_at": None,
    }


def test_documented_numbered_chapters_satisfy_catalogue_extra_count():
    from tankarr.chapter_mapping import documented_extra_count

    manga = {
        "status": "ended",
        "last_chapter": "23",
        "monitor_mode": "all",
        "series_unit_override": "chapters",
        "description": "Notes: Includes chapters 14.5 and 21.5",
    }
    metadata = {
        "status": "ended",
        "chapter_count": 23,
        "provenance": {"chapter_count": "work catalogue"},
    }
    chapters = [
        {**release(str(number)), "title": f"Chapter {number}"}
        for number in range(1, 22)
    ] + [
        {**release("14.5", volume="1", provider="manual"), "title": "Vol.1 Ch.14.5"},
        {**release("21.5", volume="1", provider="manual"), "title": "Vol.1 Ch.21.5"},
    ]

    assert documented_extra_count(manga, metadata) == 2
    index = build_chapter_index(manga, metadata, chapters)
    assert index["catalogue_extra_coverage"] == {"expected": 2, "owned": 2}
    assert index["sequence_end"] == 21
    assert index["raw_missing_count"] == 0
    assert not any(slot["chapter"] in {"22", "23"} for slot in index["slots"])

    chapters[-1]["downloaded"] = False
    index = build_chapter_index(manga, metadata, chapters)
    assert index["catalogue_extra_coverage"] == {"expected": 2, "owned": 1}
    assert index["unresolved_expected_count"] == 1

    metadata["description"] = "Includes 3 bonus chapters"
    assert documented_extra_count(manga, metadata) is None
    manga["description"] = "The hero reads chapter 14.5 and chapter 21.5"
    metadata["description"] = ""
    assert documented_extra_count(manga, metadata) is None


def test_documented_word_count_accepts_a_separately_imported_named_extra():
    from tankarr.chapter_mapping import documented_extra_count

    manga = {
        "status": "ended",
        "last_chapter": "97",
        "monitor_mode": "all",
        "series_unit_override": "chapters",
        "description": "Note: Includes one extra chapter.",
    }
    metadata = {
        "status": "ended",
        "chapter_count": 97,
        "provenance": {"chapter_count": "work catalogue"},
    }
    chapters = [
        {**release(str(number)), "title": f"Chapter {number}"}
        for number in range(1, 97)
    ] + [
        {
            **release("extra1", volume="11", provider="manual"),
            "title": "Vol.11 Extra No.1",
        }
    ]

    assert documented_extra_count(manga, metadata) == 1
    index = build_chapter_index(manga, metadata, chapters)
    assert index["catalogue_extra_coverage"] == {"expected": 1, "owned": 1}
    assert index["sequence_end"] == 96
    assert index["raw_missing_count"] == 0
    assert not any(slot["chapter"] == "97" for slot in index["slots"])

    chapters[-1]["title"] = "Vol.11 Chapter 1"
    index = build_chapter_index(manga, metadata, chapters)
    assert index["catalogue_extra_coverage"] == {"expected": 1, "owned": 0}
    assert index["raw_missing_count"] == 1


def test_exact_chapter_map_already_counts_documented_decimal_extra():
    from tankarr.chapter_map import MapEntry
    from tankarr.series_summary import decorate_series_summary

    manga = {
        "status": "ended",
        "last_chapter": "5",
        "monitor_mode": "all",
        "description": "Includes one extra chapter",
    }
    metadata = {
        "status": "ended",
        "chapter_count": 5,
        "provenance": {"chapter_count": "work catalogue"},
    }
    chapters = [
        {**release(str(number), provider="suwayomi"), "title": f"Act. {number}"}
        for number in range(1, 5)
    ] + [{**release("2.5", volume="1", provider="suwayomi"), "title": "Act. 2.5"}]
    chapter_map = [MapEntry(("1",), ("2.5",), True, "operator")]

    index = build_chapter_index(manga, metadata, chapters, chapter_map=chapter_map)
    counts = decorate_series_summary(dict(manga), metadata, index)["library_count"]
    assert index["catalogue_extra_coverage"] is None
    assert index["raw_missing_count"] == 0
    assert counts["downloaded_count"] == counts["expected_count"] == 5

    chapters[-1]["downloaded"] = False
    index = build_chapter_index(manga, metadata, chapters, chapter_map=chapter_map)
    assert index["raw_missing_count"] == 1


def test_documented_volume_extras_satisfy_catalogue_cardinality_only_when_imported():
    from tankarr.series_summary import decorate_series_summary

    manga = {
        "status": "ended",
        "last_chapter": "12",
        "monitor_mode": "all",
        "series_unit_override": "chapters",
        "description": "*Note: Includes 2 extra chapters*",
    }
    metadata = {
        "status": "ended",
        "chapter_count": 12,
        "volume_count": 2,
        "provenance": {"chapter_count": "work catalogue"},
    }
    chapters = [
        {**release(str(number)), "title": f"Chapter {number}"}
        for number in range(1, 11)
    ]
    chapters += [
        {**release("11.1"), "title": "Chapter 11.1"},
        {**release("11.2"), "title": "Chapter 11.2"},
    ]
    chapters += [
        {**release("3.5", volume="1"), "title": "Vol.1 Ch.3.5"},
        {**release("8.5", volume="2"), "title": "Vol.2 Ch.8.5"},
    ]
    index = build_chapter_index(manga, metadata, chapters)
    summary = decorate_series_summary(dict(manga), metadata, index)["library_count"]
    assert index["catalogue_extra_coverage"] == {"expected": 2, "owned": 2}
    assert index["sequence_end"] == 10
    assert not any(slot["chapter"] == "12" for slot in index["slots"])
    assert summary["downloaded_count"] == summary["expected_count"] == 12
    assert summary["missing_count"] == 0

    chapters[-1]["downloaded"] = False
    index = build_chapter_index(manga, metadata, chapters)
    summary = decorate_series_summary(dict(manga), metadata, index)["library_count"]
    assert index["catalogue_extra_coverage"] == {"expected": 2, "owned": 1}
    assert index["unresolved_expected_count"] == 1
    assert summary["missing_count"] == 1
    assert summary["unindexed_missing_count"] == 1

    chapters[-1]["downloaded"] = True
    chapters[-1]["title"] = "Hiatus notice"
    index = build_chapter_index(manga, metadata, chapters)
    assert index["catalogue_extra_coverage"] == {"expected": 2, "owned": 1}
    assert index["raw_missing_count"] == 1

    manga["description"] = "A story without an explicit extra count"
    index = build_chapter_index(manga, metadata, chapters)
    assert index["catalogue_extra_coverage"] is None
    assert index["raw_missing_count"] > 0

    manga["description"] = "Note: Includes 2 extra chapters"
    chapters.append({**release("11"), "title": "Chapter 11"})
    index = build_chapter_index(manga, metadata, chapters)
    assert index["catalogue_extra_coverage"] is None


def test_decimal_extra_cannot_exceed_the_finished_works_cardinality():
    from tankarr.chapter_map import MapEntry
    from tankarr.chapter_mapping import canonical_decimal_labels

    releases = [release(str(number)) for number in range(1, 17)] + [release("16.5")]
    catalogue_map = [MapEntry(("2",), ("16.5",), True, "mangaupdates")]

    assert (
        canonical_decimal_labels(catalogue_map, releases, chapter_total=16)
        == frozenset()
    )
    assert (
        canonical_decimal_labels(
            [MapEntry(("2",), ("16.5",), True, "operator")],
            releases,
            chapter_total=16,
        )
        == frozenset()
    )


@pytest.mark.parametrize("downloaded", [True, False])
def test_mapped_optional_extra_does_not_become_a_missing_canonical_chapter(downloaded):
    from tankarr.chapter_map import MapEntry

    index = build_chapter_index(
        {"status": "ended", "monitor_mode": "all", "series_unit_override": "chapters"},
        {"status": "ended", "chapter_count": 16, "volume_count": 2},
        [release(str(number)) for number in range(1, 17)]
        + [release("16.5", downloaded=downloaded)],
        chapter_map=[MapEntry(("2",), ("16", "16.5"), True, "operator")],
    )

    extra = next(slot for slot in index["slots"] if slot["chapter"] == "16.5")
    assert extra["downloaded"] is downloaded
    assert extra["expected"] is False
    assert extra["special"] is True
    assert index["raw_missing_count"] == 0


def test_complete_read_map_counts_parts_without_inventing_parent_chapters():
    from tankarr.chapter_map import MapEntry

    labels = ["0", "1.1", "1.2", "2", "2.1"]
    index = build_chapter_index(
        {
            "status": "ended",
            "monitor_mode": "all",
            "expected_count_override": 5,
            "expected_count_unit_override": "chapter",
        },
        {"status": "ended", "chapter_count": 5, "volume_count": 2},
        [release(label, downloaded=label != "1.2") for label in labels],
        chapter_map=[
            MapEntry(("1",), ("0", "1.1", "1.2"), True, source="operator"),
            MapEntry(("2",), ("2", "2.1"), True, source="operator"),
        ],
    )
    assert {s["chapter"] for s in index["slots"] if s["expected"]} == set(labels)
    assert index["expected_count"] == 5
    assert index["sequence_end"] == 2
    assert index["raw_missing_count"] == 1
    assert index["unresolved_expected_count"] == 0
    assert index["read_chapter_count"] == 5


@pytest.mark.parametrize(
    "status,books,manual", [("continuing", 2, 5), ("ended", 3, 5), ("ended", 2, 6)]
)
def test_read_map_does_not_close_an_ongoing_partial_or_conflicting_edition(
    status, books, manual
):
    from tankarr.chapter_map import MapEntry

    index = build_chapter_index(
        {
            "status": status,
            "monitor_mode": "all",
            "expected_count_override": manual,
            "expected_count_unit_override": "chapter",
        },
        {"status": status, "chapter_count": manual, "volume_count": books},
        [release("1.1"), release("2")],
        chapter_map=[
            MapEntry(("1",), ("0", "1.1", "1.2"), True, source="operator"),
            MapEntry(("2",), ("2", "2.1"), True, source="operator"),
        ],
    )
    assert index["sequence_basis"] != "complete_read_map"


def test_final_sequence_materializes_an_unindexed_gap_and_infers_its_volume():
    from tankarr.chapter_map import entries_from_releases

    # Volume numbers that sources write in chapter names are ignored; only
    # the catalogue map assigns chapters to volumes.
    releases = [release(str(number), volume="99") for number in (42, 43, 44, 45, 47)]
    releases += [release("53.5", volume="9"), release("59.5", volume="10")]
    manga = {
        "status": "completed",
        "last_chapter": "83",
        "monitor_mode": "existing",
    }
    metadata = {
        "status": "ended",
        "chapter_count": 83,
        "provenance": {"chapter_count": "myanimelist"},
    }
    chapter_map = entries_from_releases(
        [{"volume": "8", "chapter": "42-47"}], source="ocr"
    )

    index = build_chapter_index(manga, metadata, releases, chapter_map=chapter_map)
    assert (
        next(slot for slot in index["slots"] if slot["key"] == "chapter:42")["volume"]
        == "8"
    )
    missing = next(slot for slot in index["slots"] if slot["key"] == "chapter:46")

    assert missing["volume"] == "8"
    assert missing["volume_inferred"] is False  # placed by the catalogue map
    assert missing["available"] is False
    assert missing["downloaded"] is False
    assert missing["searchable"] is True
    assert missing["evidence"] == "provider_final"
    assert index["sequence_end"] == 83
    assert index["unresolved_expected_count"] == 0
    # A .5 special never satisfies the corresponding integer main chapter.
    assert (
        next(slot for slot in index["slots"] if slot["key"] == "chapter:53")[
            "downloaded"
        ]
        is False
    )
    # Decimal extras next to their integer never reach the index at all.
    assert not any(slot["key"].startswith("special:") for slot in index["slots"])


def test_catalogue_total_wins_over_a_conflicting_legacy_final_number():
    index = build_chapter_index(
        {"status": "completed", "last_chapter": "65", "monitor_mode": "all"},
        {"status": "ended", "chapter_count": 60},
        [release(str(number)) for number in range(1, 66)],
    )

    assert index["sequence_end"] == 60
    assert index["sequence_basis"] == "catalogue_total"
    assert sum(slot["expected"] for slot in index["slots"]) == 60


def test_metadata_cardinality_does_not_invent_chapters_after_decimal_final():
    releases = [release(str(number)) for number in range(1, 91)]
    releases.extend([release("90.1"), release("90.5")])  # extras: dropped
    manga = {"status": None, "last_chapter": "90.5", "monitor_mode": "all"}
    metadata = {
        "status": "ended",
        "chapter_count": 92,
        "provenance": {"chapter_count": "myanimelist"},
    }

    index = build_chapter_index(manga, metadata, releases)

    assert index["sequence_end"] is None
    # 90 chapters indexed against a catalogue total of 92: the deficit stays
    # visible instead of being filled with numbers or with the extras.
    assert index["unresolved_expected_count"] == 2
    assert all(slot["chapter"] not in {"91", "92"} for slot in index["slots"])


def test_sparse_metadata_total_stays_unmapped_instead_of_fabricating_numbers():
    index = build_chapter_index(
        {"status": "ended", "last_chapter": None, "monitor_mode": "all"},
        {
            "status": "ended",
            "chapter_count": 84,
            "provenance": {"chapter_count": "myanimelist"},
        },
        [
            release("7", downloaded=False),
            release("20", downloaded=False),
            release("80", downloaded=False),
        ],
    )

    assert index["sequence_end"] is None
    # The work is finished: the catalogue total remains visible, while numbers
    # outside the observed 7..80 range stay unresolved rather than fabricated.
    assert index["expected_count"] == 84
    assert index["unresolved_expected_count"] == 10
    # Interior gaps can be named from the provider numbering; the remaining
    # cardinality deficit cannot safely be assigned chapter numbers.
    assert (
        next(slot for slot in index["slots"] if slot["key"] == "chapter:8")["evidence"]
        == "gap_between_observed_chapters"
    )
    assert all(slot["chapter"] != "81" for slot in index["slots"])


def test_split_parts_are_one_main_slot_and_require_every_part_on_disk():
    index = build_chapter_index(
        {"status": "ongoing", "monitor_mode": "all"},
        {},
        [release("28.1"), release("28.2", downloaded=False)],
    )

    slot = next(slot for slot in index["slots"] if slot["key"] == "chapter:28")
    assert slot["split_parts"] == ["28.1", "28.2"]
    assert slot["downloaded"] is False
    assert slot["special"] is False


def test_volume_labels_from_sources_never_split_chapter_identity():
    index = build_chapter_index(
        {"status": "completed", "last_chapter": "2", "monitor_mode": "all"},
        {"status": "ended", "chapter_count": 2},
        [
            release("1", volume="1", identifier="a"),
            release("2", volume="1", identifier="b"),
            release("1", volume="2", identifier="c"),
            release("2", volume="2", identifier="d"),
        ],
    )

    assert index["numbering_mode"] == "global"
    assert {slot["key"] for slot in index["slots"]} == {"chapter:1", "chapter:2"}
    assert index["dropped_releases"] == {"duplicate_number": 2}


def test_volume_total_materializes_missing_volumes_and_applies_ignore_override():
    releases = [
        {
            **release("", volume=str(number), identifier=f"volume-{number}"),
            "chapter": None,
        }
        for number in (1, 2)
    ]
    index = build_chapter_index(
        {"status": "completed", "monitor_mode": "none"},
        {
            "status": "ended",
            "volume_count": 3,
            "provenance": {"volume_count": "mangaupdates"},
        },
        releases,
        {"3": "ignored"},
    )

    missing = next(slot for slot in index["slots"] if slot["key"] == "volume:3")
    assert index["unit"] == "volume"
    assert index["expected_count"] == 3
    assert index["unresolved_expected_count"] == 0
    assert missing["available"] is False
    assert missing["ignored"] is True
    assert missing["monitored"] is False
    assert missing["volume_monitor_state"] == "ignored"
    assert index["raw_missing_count"] == 1
    assert index["ignored_missing_count"] == 1
    assert index["mapped_missing_count"] == 0


def test_manual_edition_total_and_manual_up_to_date_are_distinct():
    releases = [
        {
            **release("", volume="1", identifier="volume-1"),
            "chapter": None,
        }
    ]
    metadata = {
        "status": "ended",
        "volume_count": 3,
        "provenance": {"volume_count": "mangaupdates"},
    }

    collected_edition = build_chapter_index(
        {
            "status": "completed",
            "monitor_mode": "none",
            "expected_count_override": 1,
            "expected_count_unit_override": "volume",
        },
        metadata,
        releases,
    )
    manually_current = build_chapter_index(
        {
            "status": "completed",
            "monitor_mode": "all",
            "library_status_override": "up_to_date",
        },
        metadata,
        releases,
    )

    assert collected_edition["expected_count"] == 1
    assert collected_edition["expected_source"] == "manual edition override"
    assert [slot["key"] for slot in collected_edition["slots"]] == ["volume:1"]
    assert collected_edition["raw_missing_count"] == 0

    absent = [
        slot
        for slot in manually_current["slots"]
        if slot["expected"] and not slot["downloaded"]
    ]
    assert {slot["volume"] for slot in absent} == {"2", "3"}
    assert all(slot["ignored"] for slot in absent)
    assert all(slot["series_status_ignored"] for slot in absent)
    assert manually_current["raw_missing_count"] == 2
    assert manually_current["ignored_missing_count"] == 2
    assert manually_current["mapped_missing_count"] == 0


def test_owned_volumes_cover_their_chapters_through_the_map():
    from tankarr.chapter_map import entries_from_releases

    # A whole-volume file plus loose chapters, plus catalogue expectations.
    releases = [
        {**release("", volume="20", identifier="mangapill-v20"), "chapter": None},
        release("57", volume=None, provider="suwayomi"),
        release("61", volume=None, provider="suwayomi", downloaded=False),
        release("58", volume=None, provider="suwayomi", downloaded=False),
    ]
    manga = {
        "status": "completed",
        "last_chapter": "62",
        "monitor_mode": "all",
        "series_unit_override": "chapters",
    }
    metadata = {"status": "ended", "chapter_count": 62}
    chapter_map = entries_from_releases(
        [
            {"volume": "20", "chapter": "58-60"},
            {"volume": "21", "chapter": "61-63"},
        ],
        source="ocr",
    )

    index = build_chapter_index(manga, metadata, releases, chapter_map=chapter_map)
    slots = {slot["key"]: slot for slot in index["slots"]}

    # Chapter 58 is inside owned volume 20: covered, not missing, exact.
    assert slots["chapter:58"]["covered_by_volume"] == "20"
    assert slots["chapter:58"]["coverage_exact"] is True
    assert slots["chapter:58"]["downloaded"] is False
    # Chapters 59-60 were never indexed by any source but the map places
    # them in the owned volume: still covered.
    assert slots["chapter:59"]["covered_by_volume"] == "20"
    # Chapter 57 was downloaded as a loose file but volume 20 does not hold
    # it (the map says 58-60), so it is not a duplicate; a downloaded chapter
    # inside an owned volume would be.
    assert slots["chapter:57"]["duplicate_of_volume"] is None
    assert index["duplicate_count"] == 0
    # Chapter 61 belongs to volume 21, which is not owned: genuinely missing.
    assert slots["chapter:61"]["covered_by_volume"] is None
    assert slots["chapter:61"]["covered_unmapped"] is False
    assert index["covered_count"] == 3
    assert index["owned_volumes"] == ["20"]
    assert index["raw_missing_count"] == 62 - 1 - 3  # 57 downloaded, 58-60 covered

    # Without any map, owned volumes make every missing chapter "unmapped".
    blind = build_chapter_index(manga, metadata, releases)
    assert blind["covered_unmapped_count"] == 62 - 1
    assert blind["raw_missing_count"] == 0


@pytest.mark.parametrize("source", ["operator", "ocr"])
def test_partial_owned_book_map_keeps_known_edition_head_and_tail_missing(source):
    from tankarr.chapter_map import MapEntry

    index = build_chapter_index(
        {
            "status": "completed",
            "last_chapter": "12",
            "monitor_mode": "all",
            "series_unit_override": "chapters",
        },
        {"status": "ended", "chapter_count": 12},
        [_book("2"), release("1")],
        chapter_map=[MapEntry(("2",), ("4", "5", "6"), True, source)],
    )
    slots = {slot["chapter"]: slot for slot in index["slots"]}
    for number in ["2", "3", "7", "8", "9", "10", "11", "12"]:
        assert not slots[number]["covered_unmapped"]
        assert slots[number]["covered_by_volume"] is None
        assert slots[number]["expected"] and slots[number]["monitored"]
        assert not slots[number]["downloaded"]
    assert index["covered_count"] == 3
    assert index["raw_missing_count"] == 8


def test_downloaded_chapters_inside_owned_volumes_are_reported_as_duplicates():
    from tankarr.chapter_map import entries_from_releases

    releases = [
        {**release("", volume="20", identifier="pill-v20"), "chapter": None},
        release("58", volume=None, provider="suwayomi"),
        release("59", volume=None, provider="suwayomi"),
    ]
    manga = {
        "status": "completed",
        "last_chapter": "60",
        "monitor_mode": "all",
        "series_unit_override": "chapters",
    }
    metadata = {"status": "ended", "chapter_count": 60}
    chapter_map = entries_from_releases(
        [{"volume": "20", "chapter": "58-60"}], source="ocr"
    )

    index = build_chapter_index(manga, metadata, releases, chapter_map=chapter_map)
    slots = {slot["key"]: slot for slot in index["slots"]}

    assert slots["chapter:58"]["duplicate_of_volume"] == "20"
    assert slots["chapter:59"]["duplicate_of_volume"] == "20"
    assert slots["chapter:60"]["covered_by_volume"] == "20"
    assert index["duplicate_count"] == 2
    assert index["raw_missing_count"] == 60 - 3  # 58-60 satisfied, rest missing


def test_ongoing_work_uses_available_chapters_instead_of_catalogue_count():
    from tankarr.chapter_mapping import build_chapter_index

    manga = {
        "id": "m",
        "title": "Ongoing",
        "status": "ongoing",
        "monitor_mode": "all",
        "preferred_language": "en",
    }
    metadata = {
        "status": "ongoing",
        "chapter_count": None,
        "latest_release_chapter": 1191,
    }
    releases = [
        {
            "id": f"r{n}",
            "chapter": str(n),
            "volume": None,
            "language": "en",
            "provider": "suwayomi",
            "downloaded": False,
            "monitored": True,
        }
        for n in (1, 2, 3)
    ]
    index = build_chapter_index(manga, metadata, releases, {})
    # No official source mapped: use the available index, never the catalogue count.
    assert index["expected_count"] == 3
    assert index["expected_source"] == "available sources"
    assert index["reference_kind"] == "available"
    assert index["sequence_end"] is None


def test_extras_and_chapter_zero_never_reach_the_index():
    releases = [
        release("0", volume="112", downloaded=False),
        release("1", downloaded=False),
        release("1.5", downloaded=False),
        release("2", downloaded=False),
        release("2.5", downloaded=False),
    ]
    manga = {"status": "ongoing", "monitor_mode": "all"}
    index = build_chapter_index(manga, {}, releases)
    assert {slot["key"] for slot in index["slots"]} == {"chapter:1", "chapter:2"}
    assert index["special_count"] == 0
    assert index["series_unit"] == "chapters"
    assert index["dropped_releases"] == {"chapter_zero_or_negative": 1, "extra": 2}


def test_owned_optional_half_chapter_is_counted_without_becoming_wanted():
    releases = [
        release("1"),
        release("1.5"),
        release("2"),
    ]
    index = build_chapter_index(
        {"status": "ongoing", "monitor_mode": "all"}, {}, releases
    )

    assert {slot["key"] for slot in index["slots"]} == {"chapter:1", "chapter:2"}
    assert index["additional_content"] == {"prologues": 0, "extras": 1}
    assert index["special_count"] == index["special_downloaded_count"] == 1
    assert index["raw_missing_count"] == 0


def test_series_unit_never_mixes_books_and_chapters():
    releases = [
        {
            **release("", volume="1", identifier="v1"),
            "chapter": None,
            "release_unit": "volume",
        },
        {
            **release("", volume="2", identifier="v2", downloaded=False),
            "chapter": None,
            "release_unit": "volume",
        },
        release("1", downloaded=False),
        release("2", downloaded=False),
    ]
    metadata = {"status": "ended", "volume_count": 3, "chapter_count": 20}
    # Ended work with books available → volumes: chapters are not slots.
    index = build_chapter_index(
        {"status": "completed", "monitor_mode": "all"}, metadata, releases
    )
    assert index["series_unit"] == "volumes" and index["unit"] == "volume"
    assert {slot["key"] for slot in index["slots"]} == {
        "volume:1",
        "volume:2",
        "volume:3",
    }
    assert index["unit_coverage"] == {
        "chapters": {"available": 2, "expected": 20},
        "volumes": {"available": 2, "expected": 3},
    }
    # Forced to chapters: books disappear from the index, but the owned one
    # still covers whatever the map places inside it.
    forced = build_chapter_index(
        {
            "status": "completed",
            "monitor_mode": "all",
            "series_unit_override": "chapters",
        },
        metadata,
        releases,
    )
    assert (
        forced["series_unit"] == "chapters" and forced["series_unit_override"] is True
    )
    assert all(slot["chapter"] for slot in forced["slots"])
    assert forced["owned_volumes"] == ["1"]


def test_index_reports_expected_and_volume_progress_for_the_header():
    from tankarr.chapter_map import entries_from_releases
    from tankarr.series_summary import decorate_series_summary

    releases = [
        {**release("", volume="1", identifier="v1"), "chapter": None},
        release("3", downloaded=False),
        release("4", downloaded=True),
    ]
    metadata = {"status": "ongoing", "latest_release_chapter": 6, "volume_count": 3}
    chapter_map = entries_from_releases(
        [{"volume": "1", "chapter": "1-2"}], source="ocr"
    )
    # Running work with both shapes → chapters; the owned book still covers 1-2.
    index = build_chapter_index(
        {"status": "ongoing", "monitor_mode": "all"},
        metadata,
        releases,
        chapter_map=chapter_map,
    )
    assert index["series_unit"] == "chapters"
    assert index["expected_count"] == 4
    assert index["owned_volume_count"] == 1 and index["expected_volume_count"] == 3
    # 1-2 covered by the owned volume, 4 downloaded → 3 satisfied; 3 observed only.
    assert index["expected_satisfied_count"] == 3
    assert index["expected_available_count"] == 2
    counts = decorate_series_summary({"status": "ongoing"}, metadata, index)[
        "library_count"
    ]
    assert counts["downloaded_count"] == 3
    assert counts["total_count"] == counts["available_count"] == 4
    assert counts["indexed_missing_count"] == 1
    assert counts["unindexed_missing_count"] == 0


def test_running_works_are_chapters_only_until_they_end():
    from tankarr.chapter_map import entries_from_releases

    releases = [release(str(n), downloaded=False) for n in (1, 2, 3)]
    chapter_map = entries_from_releases(
        [{"volume": "1", "chapter": "1-3"}], source="ocr"
    )
    running = build_chapter_index(
        {"status": "ongoing", "monitor_mode": "all"},
        {"status": "ongoing"},
        releases,
        chapter_map=chapter_map,
    )
    assert all(slot["volume"] is None for slot in running["slots"])
    ended = build_chapter_index(
        {"status": "completed", "monitor_mode": "all"},
        {"status": "ended", "chapter_count": 3},
        releases,
        chapter_map=chapter_map,
    )
    assert {slot["volume"] for slot in ended["slots"] if slot["chapter"]} == {"1"}


def test_volumes_series_never_materialises_chapter_slots():
    releases = [
        {
            **release("", volume=str(n), identifier=f"v{n}", downloaded=n < 3),
            "chapter": None,
            "release_unit": "volume",
        }
        for n in (1, 2, 3)
    ]
    manga = {"status": "completed", "last_chapter": "103", "monitor_mode": "all"}
    metadata = {"status": "ended", "chapter_count": 103, "volume_count": 34}
    from tankarr.chapter_map import entries_from_releases

    # Owned books plus a catalogue map must not resurrect chapter slots either.
    chapter_map = entries_from_releases(
        [{"volume": "1", "chapter": "1-3"}, {"volume": "2", "chapter": "4-7"}]
    )
    index = build_chapter_index(manga, metadata, releases, chapter_map=chapter_map)
    assert index["series_unit"] == "volumes" and index["unit"] == "volume"
    assert all(slot["key"].startswith("volume:") for slot in index["slots"])
    assert len(index["slots"]) == 34 and index["sequence_end"] is None
    assert index["expected_count"] == 34 and index["raw_missing_count"] == 32


def test_a_chapter_only_aggregators_have_yet_is_not_missing():
    """One Piece 1193: posted by two aggregators, not yet on MANGA Plus.

    The official platform is the reference for a running work, so the shelf
    ends at 1192. Counting the aggregators' head start as a hole made the
    card read "1192 / 1192" and "1 missing" at the same time. The chapter
    stays listed and becomes expected again the moment the official source
    publishes it.
    """
    metadata = {
        "status": "ongoing",
        "official_links": [
            {
                "name": "MANGA Plus",
                "url": "https://mangaplus.shueisha.co.jp/titles/100020",
                "language": "en",
            }
        ],
    }
    manga = {
        "status": "ongoing",
        "monitor_mode": "all",
        "preferred_language": "en",
        "_unit_context": {"acquisition_policy": "prefer_official"},
    }
    official = [
        {
            **release(str(n), downloaded=True, provider="suwayomi", identifier=f"o{n}"),
            "source_url": "https://mangaplus.shueisha.co.jp/viewer/1",
            "source_name": "MANGA Plus by SHUEISHA",
        }
        for n in range(1, 6)
    ]
    ahead = [
        {
            **release("6", downloaded=False, provider="suwayomi", identifier="a6"),
            "source_url": "https://weebcentral.com/chapters/x",
            "source_name": "Weeb Central",
        }
    ]
    index = build_chapter_index(manga, metadata, [*official, *ahead])
    assert index["expected_count"] == 5 and index["reference_kind"] == "official"
    assert index["raw_missing_count"] == 0
    assert index["expected_available_count"] == 5
    early = next(slot for slot in index["slots"] if slot["chapter"] == "6")
    assert early["expected"] is False and early["beyond_official_frontier"] is True
    # The official source publishes it: the shelf grows and it counts again.
    published = [
        {
            **release("6", downloaded=False, provider="suwayomi", identifier="o6"),
            "source_url": "https://mangaplus.shueisha.co.jp/viewer/6",
            "source_name": "MANGA Plus by SHUEISHA",
        }
    ]
    index = build_chapter_index(manga, metadata, [*official, *ahead, *published])
    assert index["expected_count"] == 6 and index["raw_missing_count"] == 1
    # An operator who takes the first release available wants it regardless.
    first_available = {
        **manga,
        "_unit_context": {"acquisition_policy": "first_available"},
    }
    index = build_chapter_index(first_available, metadata, [*official, *ahead])
    assert index["raw_missing_count"] == 1


def test_running_work_reference_is_the_official_source_or_available_chapters():
    metadata = {
        "status": "ongoing",
        "latest_release_chapter": 877,
        "official_links": [
            {
                "name": "MANGA Plus",
                "url": "https://mangaplus.shueisha.co.jp/titles/100020",
                "language": "en",
            }
        ],
    }
    scans = [
        {
            **release(
                str(n), downloaded=False, provider="suwayomi", identifier=f"s{n}"
            ),
            "source_url": "https://weebcentral.com/c/x",
            "source_name": "Weeb Central",
        }
        for n in range(1, 11)
    ]
    manga = {"status": "ongoing", "monitor_mode": "all", "preferred_language": "en"}
    index = build_chapter_index(manga, metadata, scans)
    assert index["expected_count"] == 10 and index["reference_kind"] == "available"
    assert index["latest_chapter"] == "10"
    official = [
        {
            **release(
                str(n), downloaded=False, provider="suwayomi", identifier=f"o{n}"
            ),
            "source_url": "https://mangaplus.shueisha.co.jp/viewer/1",
            "source_name": "MANGA Plus by SHUEISHA",
        }
        for n in range(1, 9)
    ]
    index = build_chapter_index(manga, metadata, [*scans, *official])
    assert index["expected_count"] == 8 and index["reference_kind"] == "official"
    assert index["latest_chapter"] == "8"
    assert "MANGA Plus" in index["expected_source"]
    # Ended works keep the catalogue total.
    ended = build_chapter_index(
        {**manga, "status": "completed"},
        {"status": "ended", "chapter_count": 10},
        scans,
    )
    assert ended["expected_count"] == 10 and ended["reference_kind"] == "catalogue"


@pytest.mark.parametrize("status", ["ongoing", "hiatus"])
def test_continuing_available_count_ignores_catalogue_extras_and_unseen_gaps(status):
    from tankarr.series_summary import decorate_series_summary

    manga = {"status": status, "monitor_mode": "all", "preferred_language": "en"}
    metadata = {"status": status, "chapter_count": 70, "latest_release_chapter": 70}
    rows = [release(str(n)) for n in range(1, 66)]
    # A duplicate source and an optional special cannot inflate the reference.
    rows.extend([release("65", provider="suwayomi"), release("10.5")])
    index = build_chapter_index(manga, metadata, rows)
    assert index["expected_count"] == 65
    assert index["raw_missing_count"] == 0
    assert index["latest_chapter"] == "65"
    counts = decorate_series_summary(dict(manga), metadata, index)["library_count"]
    assert counts["downloaded_count"] == counts["total_count"] == 65
    assert counts["reference_unknown"] is False
    assert counts["count_note"] is None

    rows = [r for r in rows if r["chapter"] != "42"]
    rows.append(release("66", downloaded=False))
    index = build_chapter_index(manga, metadata, rows)
    assert index["expected_count"] == 65  # a count, not the last number 66
    assert index["latest_chapter"] == "66"
    assert index["raw_missing_count"] == 1
    assert index["unresolved_expected_count"] == 0
    assert not any(s["chapter"] == "42" for s in index["slots"])

    ended = build_chapter_index({**manga, "status_override": "ended"}, metadata, rows)
    assert ended["reference_kind"] == "catalogue"
    assert ended["expected_count"] == 70
    assert ended["unresolved_expected_count"] > 0


def test_latest_available_chapter_excludes_synthetic_missing_slots():
    index = build_chapter_index(
        {"status": "ended", "monitor_mode": "all", "last_chapter": "10"},
        {"status": "ended", "chapter_count": 10},
        [release(str(n)) for n in range(1, 4)],
    )
    assert index["expected_count"] == 10
    assert index["latest_chapter"] == "3"
    assert index["raw_missing_count"] == 7


def test_volumes_series_without_any_release_still_expects_the_catalogued_books():
    index = build_chapter_index(
        {
            "status": "completed",
            "monitor_mode": "all",
            "series_unit_override": "volumes",
        },
        {"status": "ended", "volume_count": 3, "chapter_count": 30},
        [],
    )
    assert index["unit"] == "volume" and [slot["key"] for slot in index["slots"]] == [
        "volume:1",
        "volume:2",
        "volume:3",
    ]
    assert index["raw_missing_count"] == 3 and index["expected_count"] == 3


def test_volumes_series_does_not_want_a_book_whose_chapters_are_on_disk():
    """Yawara: volumes 13-29 are files, chapters 1-136 are files too. Books
    mapped books are not missing; neighbours cannot prove an unmapped book's
    contents."""

    from tankarr.chapter_map import MapEntry

    chapter_map = [
        MapEntry(
            volumes=("1",), chapters=("1", "2", "3"), exact=True, source="operator"
        ),
        MapEntry(
            volumes=("2",), chapters=("4", "5", "6"), exact=True, source="operator"
        ),
        # volume 3 is not in the map
        MapEntry(
            volumes=("4",), chapters=("10", "11", "12"), exact=True, source="operator"
        ),
        MapEntry(
            volumes=("5",), chapters=("13", "14", "15"), exact=True, source="operator"
        ),
    ]
    chapters = [
        release(str(n), downloaded=True, provider="suwayomi", identifier=f"c{n}")
        for n in range(1, 10)
    ]
    book_five = {
        **release("", volume="5", downloaded=True, provider="manual", identifier="v5"),
        "chapter": None,
        "release_unit": "volume",
    }
    index = build_chapter_index(
        {
            "status": "completed",
            "monitor_mode": "all",
            "series_unit_override": "volumes",
        },
        {"status": "ended", "volume_count": 5, "chapter_count": 15},
        [*chapters, book_five],
        chapter_map=chapter_map,
    )
    by_key = {slot["key"]: slot for slot in index["slots"]}
    assert by_key["volume:1"]["covered_by_chapters"] is True
    assert by_key["volume:1"]["coverage_exact"] is True
    assert by_key["volume:2"]["covered_by_chapters"] is True
    # Volume 3 is unmapped: chapters 7-9 between its neighbours are only a hint.
    assert by_key["volume:3"]["covered_by_chapters"] is False
    assert by_key["volume:3"]["coverage_exact"] is False
    assert by_key["volume:4"]["covered_by_chapters"] is False
    assert by_key["volume:5"]["downloaded"] is True
    assert index["raw_missing_count"] == 2


def test_the_manual_edition_total_in_books_also_sets_the_edition_split():
    from tankarr.chapter_mapping import effective_edition_book_count

    assert effective_edition_book_count({}) is None
    assert effective_edition_book_count({"edition_book_count": 12}) == 12
    assert (
        effective_edition_book_count(
            {"expected_count_override": 8, "expected_count_unit_override": "volume"}
        )
        == 8
    )
    # A total expressed in chapters says nothing about books.
    assert (
        effective_edition_book_count(
            {"expected_count_override": 170, "expected_count_unit_override": "chapter"}
        )
        is None
    )
    # The explicit field still wins for series configured before the merge.
    assert (
        effective_edition_book_count(
            {
                "edition_book_count": 6,
                "expected_count_override": 8,
                "expected_count_unit_override": "volume",
            }
        )
        == 6
    )


def test_a_sparse_release_log_is_a_hint_not_a_map():
    from tankarr.chapter_map import MapEntry, downgrade_sparse_maps

    def entry(volume: str, chapters: tuple[str, ...], source: str = "mangaupdates"):
        return MapEntry(volumes=(volume,), chapters=chapters, exact=True, source=source)

    # Blade of the Phantom Master: four group labels across ten books.
    blade = [
        entry("3", ("6",)),
        entry("5", ("8", "9", "10")),
        entry("8", ("14", "15")),
        entry("10", ("17",)),
    ]
    assert all(not item.exact for item in downgrade_sparse_maps(blade))
    # Yawara: every chapter of books 1-3 accounted for, in order.
    yawara = [
        entry("1", tuple(str(n) for n in range(1, 12))),
        entry("2", tuple(str(n) for n in range(12, 24))),
        entry("3", tuple(str(n) for n in range(24, 36))),
    ]
    assert all(item.exact for item in downgrade_sparse_maps(yawara))
    # A later chapter in an earlier book is a typo, not a structure.
    twisted = [
        entry("1", ("1", "2", "3")),
        entry("2", ("4", "5", "6")),
        entry("1", ("7", "8", "9")),
    ]
    assert all(not item.exact for item in downgrade_sparse_maps(twisted))
    # The operator's map is never second-guessed.
    mine = [entry("13", ("135", "146"), source="operator")]
    assert downgrade_sparse_maps(mine)[0].exact is True


def test_two_official_platforms_the_most_advanced_is_the_reference():
    metadata = {
        "status": "ongoing",
        "official_links": [
            {
                "name": "MANGA Plus",
                "url": "https://mangaplus.shueisha.co.jp/titles/1",
                "language": "en",
            },
            {
                "name": "Comikey",
                "url": "https://comikey.com/comics/x/",
                "language": "en",
            },
        ],
    }
    manga = {"status": "ongoing", "monitor_mode": "all", "preferred_language": "en"}
    plus = [
        {
            **release(
                str(n), downloaded=False, provider="suwayomi", identifier=f"p{n}"
            ),
            "source_url": "https://mangaplus.shueisha.co.jp/viewer/1",
            "source_name": "MANGA Plus",
        }
        for n in range(1, 21)
    ]
    comikey = [
        {
            **release(
                str(n), downloaded=False, provider="suwayomi", identifier=f"c{n}"
            ),
            "source_url": "https://comikey.com/read/x",
            "source_name": "Comikey",
        }
        for n in range(1, 26)
    ]
    index = build_chapter_index(manga, metadata, [*plus, *comikey])
    assert index["expected_count"] == 25 and index["reference_kind"] == "official"
    # When the leading platform stops (rights moved), the other one becomes the reference by the same rule.
    stalled = build_chapter_index(manga, metadata, [*plus, *comikey[:10]])
    assert stalled["expected_count"] == 20


def test_summary_counts_books_for_a_volumes_series():
    from tankarr.series_summary import decorate_series_summary

    releases = [
        {
            **release("", volume="1", identifier="v1", downloaded=True),
            "chapter": None,
            "release_unit": "volume",
        }
    ]
    manga = {
        "id": "g",
        "title": "Grass",
        "status": "completed",
        "monitor_mode": "all",
        "preferred_language": "en",
        "chapter_count": 19,
        "downloaded_count": 0,
    }
    metadata = {"status": "ended", "volume_count": 1, "chapter_count": 19}
    index = build_chapter_index(manga, metadata, releases)
    summary = decorate_series_summary(dict(manga), metadata, index)
    counts = summary["library_count"]
    assert (
        counts["unit"] == "volume"
        and counts["downloaded_count"] == 1
        and counts["total_count"] == 1
    )


def test_prologue_chapter_zero_becomes_a_counted_slot():
    """When the catalogue counts the prologue, chapter 0 is a chapter.

    The series then reads 4/4 instead of 3/4 with an invisible extra."""

    manga = {
        "id": "m1",
        "preferred_language": "en",
        "status": "ended",
        "monitor_mode": "all",
    }
    releases = [
        {
            "id": f"r{index}",
            "chapter": str(index),
            "volume": None,
            "language": "en",
            "provider": "suwayomi",
            "source_name": "Source (EN)",
            "downloaded": True,
        }
        for index in range(0, 4)
    ]

    counted = build_chapter_index(manga, {"chapter_count": 4}, releases)
    ignored = build_chapter_index(manga, {"chapter_count": 3}, releases)

    assert counted["expected_available_count"] == 4
    assert counted["expected_satisfied_count"] == 4
    assert [slot["chapter"] for slot in counted["slots"] if slot["expected"]] == [
        "0",
        "1",
        "2",
        "3",
    ]
    assert ignored["expected_available_count"] == 3
    assert all(slot["chapter"] != "0" for slot in ignored["slots"])


def test_a_future_dated_episode_is_announced_not_published():
    """The official platform's reference is its publication calendar.

    An episode dated in the future is announced, not published: it is neither
    expected nor missing until its day, so a library holding every published
    episode is complete and up to date. A paid episode whose date has passed
    is published (Tapas locks every episode after the free ones for years)."""

    from datetime import UTC, datetime, timedelta

    today = datetime.now(UTC)

    manga = {
        "id": "m1",
        "preferred_language": "en",
        "status": "ongoing",
        "monitor_mode": "all",
    }
    metadata = {
        "official_links": [{"url": "https://tapas.io/series/x", "language": "en"}]
    }
    releases = [
        {
            "id": f"free{index}",
            "chapter": str(index),
            "title": f"{index}. Episode",
            "language": "en",
            "provider": "suwayomi",
            "source_name": "Tapas (EN)",
            "source_url": f"https://tapas.io/episode/{index}",
            "downloaded": True,
        }
        for index in range(1, 4)
    ] + [
        {
            "id": f"paid{index}",
            "chapter": str(index),
            "title": f"\U0001f512 {index}. Episode",
            "language": "en",
            "provider": "suwayomi",
            "source_name": "Tapas (EN)",
            "source_url": f"https://tapas.io/episode/{index}",
            "publish_at": (today + timedelta(days=7 * (index - 5))).isoformat(),
            "downloaded": False,
        }
        for index in range(4, 7)
    ]

    index = build_chapter_index(manga, metadata, releases)

    # 4 came out a week ago (paid, but published), 5 today, 6 next week.
    assert index["expected_count"] == 5
    assert index["reference_kind"] == "official"
    assert index["expected_satisfied_count"] == 3
    # Paid episodes exist but cannot be fetched: they are not "missing".
    assert index["raw_missing_count"] == 0


def test_a_finished_work_without_a_catalogue_total_uses_its_official_platform():
    """The catalogue never recorded how many chapters the work has; what the
    official platform published is the total, exactly as for a running work."""

    manga = {
        "id": "m1",
        "preferred_language": "en",
        "status": "ended",
        "monitor_mode": "all",
    }
    metadata = {
        "chapter_count": None,
        "official_links": [{"url": "https://tapas.io/series/x", "language": "en"}],
    }
    releases = [
        {
            "id": f"t{index}",
            "chapter": str(index),
            "title": f"{index}. Episode",
            "language": "en",
            "provider": "suwayomi",
            "source_name": "Tapas (EN)",
            "source_url": f"https://tapas.io/episode/{index}",
            "downloaded": index < 3,
        }
        for index in range(1, 5)
    ]

    index = build_chapter_index(manga, metadata, releases)

    assert index["expected_count"] == 4
    assert index["reference_kind"] == "official"


def test_parts_from_one_source_satisfy_a_whole_slot_nobody_else_delivered():
    # Live: chapter 45 of a webtoon sat in Wanted although MangaK had
    # delivered it as 45.1..45.6 and the audit had passed the parts together.
    whole = {
        **release("45", downloaded=False, identifier="whole-45"),
        "source_name": "Weeb",
    }
    parts = [
        {**release(f"45.{n}", identifier=f"part-{n}"), "source_name": "MangaK"}
        for n in (1, 2, 3)
    ]
    index = build_chapter_index(
        {"status": "ongoing", "monitor_mode": "all"}, {}, [whole, *parts]
    )

    slot = next(slot for slot in index["slots"] if slot["key"] == "chapter:45")
    assert slot["downloaded"] is True
    assert slot["split_parts"] == ["45.1", "45.2", "45.3"]
    assert slot["evidence"] == "observed_split_parts"


def test_parts_with_a_gap_do_not_cover_the_whole():
    whole = {
        **release("45", downloaded=False, identifier="whole-45"),
        "source_name": "Weeb",
    }
    gapped = [
        {**release("45.1", identifier="a"), "source_name": "MangaK"},
        {**release("45.3", identifier="c"), "source_name": "MangaK"},
    ]
    index = build_chapter_index(
        {"status": "ongoing", "monitor_mode": "all"}, {}, [whole, *gapped]
    )
    assert (
        next(s for s in index["slots"] if s["key"] == "chapter:45")["downloaded"]
        is False
    )


def test_a_sources_own_complete_parts_cover_the_whole_it_also_lists():
    # Live: MangaK listed chapter 45 (unreadable, blocked) AND delivered
    # 45.1..45.6; ComicK split the same chapter into seven parts nobody
    # downloaded. The six parts on disk are the chapter.
    whole_mangak = {
        **release("45", downloaded=False, identifier="mangak-45"),
        "source_name": "MangaK",
    }
    mangak_parts = [
        {**release(f"45.{n}", identifier=f"mangak-45.{n}"), "source_name": "MangaK"}
        for n in range(1, 7)
    ]
    comick_parts = [
        {
            **release(f"45.{n}", downloaded=False, identifier=f"comick-45.{n}"),
            "source_name": "ComicK",
        }
        for n in range(1, 8)
    ]
    index = build_chapter_index(
        {"status": "ongoing", "monitor_mode": "all"},
        {},
        [whole_mangak, *mangak_parts, *comick_parts],
    )
    slot = next(s for s in index["slots"] if s["key"] == "chapter:45")
    assert slot["downloaded"] is True
    assert slot["split_parts"] == [f"45.{n}" for n in range(1, 7)]


def test_a_decimal_chapter_the_map_places_in_a_volume_is_a_chapter_not_an_extra():
    # And (Mari Okazaki), live: 44 numbered chapters plus "5.5" inside volume
    # 1 are the 45 chapters the catalogues count. Tankarr dropped 5.5 as an
    # extra and materialised a chapter 45 nobody has.
    from tankarr.chapter_map import entries_from_releases

    releases = [release(str(number)) for number in range(1, 45)]
    releases.append(release("5.5", downloaded=False, identifier="five-and-a-half"))
    manga = {"status": "completed", "last_chapter": "45", "monitor_mode": "all"}
    metadata = {"status": "ended", "chapter_count": 45}
    chapter_map = entries_from_releases(
        [{"volume": "1", "chapter": "1-5"}, {"volume": "1", "chapter": "5.5"}],
        source="ocr",
    )

    index = build_chapter_index(manga, metadata, releases, chapter_map=chapter_map)

    keys = {slot["key"] for slot in index["slots"]}
    assert "chapter:5.5" in keys
    assert "chapter:45" not in keys
    half = next(slot for slot in index["slots"] if slot["key"] == "chapter:5.5")
    assert half["expected"] is True and half["special"] is False
    assert half["downloaded"] is False  # the one chapter really missing
    assert index["unresolved_expected_count"] == 0
    assert index["dropped_releases"].get("extra", 0) == 0


def test_a_side_episode_every_source_lists_explains_the_count_but_stays_an_extra():
    # The same work seen before its map is fetched: several aggregators all
    # list "5.5" (one upload mirrored), the catalogues count 45. The side
    # episode explains the count - no chapter 45 is invented - but it stays
    # an extra: specials are opt-in, and mirrors are not agreement.
    releases = [release(str(number)) for number in range(1, 45)]
    releases.append(
        {**release("5.5", downloaded=False, identifier="a-5.5"), "source_name": "A"}
    )
    releases.append(
        {**release("5.5", downloaded=False, identifier="b-5.5"), "source_name": "B"}
    )
    manga = {"status": "completed", "last_chapter": "45", "monitor_mode": "all"}
    metadata = {"status": "ended", "chapter_count": 45}

    index = build_chapter_index(manga, metadata, releases)

    keys = {slot["key"] for slot in index["slots"]}
    assert "chapter:5.5" not in keys and "chapter:45" not in keys
    assert index["unresolved_expected_count"] == 0


def _book(volume: str, *, downloaded: bool = True) -> dict:
    return {
        "id": f"torrent-{volume}",
        "chapter": None,
        "volume": volume,
        "release_unit": "volume",
        "provider": "prowlarr",
        "downloaded": downloaded,
        "monitored": True,
        "version": 1,
        "pages": 320,
        "publish_at": None,
    }


def test_declared_edition_total_cannot_prove_chapter_coverage():
    """A known edition size cannot turn ten books into 120 measured chapters."""

    releases = [_book(str(number)) for number in range(1, 11)]
    releases += [release(str(number)) for number in range(121, 145)]
    manga = {
        "status": "completed",
        "last_chapter": "144",
        "monitor_mode": "all",
        "series_unit_override": "chapters",
        "edition_book_count": 12,
    }
    metadata = {
        "status": "ended",
        "chapter_count": 144,
        "volume_count": 18,
        "provenance": {"chapter_count": "mangabaka"},
    }
    index = build_chapter_index(manga, metadata, releases)
    slots = {slot["key"]: slot for slot in index["slots"]}
    for number in (1, 12, 13, 120):
        assert slots[f"chapter:{number}"]["covered_by_volume"] is None
        assert slots[f"chapter:{number}"]["coverage_exact"] is False
    assert slots["chapter:121"]["downloaded"] is True
    assert index["expected_satisfied_count"] == 24
    assert index["edition_split"] is None


def test_without_a_declared_edition_books_of_another_edition_cover_nothing():
    releases = [_book(str(number)) for number in range(1, 11)]
    manga = {
        "status": "completed",
        "last_chapter": "144",
        "monitor_mode": "all",
        "series_unit_override": "chapters",
    }
    metadata = {"status": "ended", "chapter_count": 144, "volume_count": 18}
    index = build_chapter_index(manga, metadata, releases)
    assert index["edition_split"] is None
    slots = {slot["key"]: slot for slot in index["slots"]}
    assert slots["chapter:1"]["covered_by_volume"] is None


def test_out_of_edition_release_does_not_hide_missing_books_of_a_pinned_edition():
    index = build_chapter_index(
        {
            "status": "ended",
            "monitor_mode": "all",
            "series_unit_override": "volumes",
            "edition_book_count": 3,
        },
        {"volume_count": 3},
        [_book("1"), _book("7"), _book("10", downloaded=False)],
    )
    assert {
        slot["volume"]
        for slot in index["slots"]
        if slot["expected"] and not slot["downloaded"]
    } == {"2", "3"}
    assert index["raw_missing_count"] == 2


def test_an_edition_count_estimate_never_marks_a_chapter_file_as_duplicate():
    releases = [_book("1"), release("3")]
    manga = {
        "status": "completed",
        "last_chapter": "24",
        "monitor_mode": "all",
        "series_unit_override": "chapters",
        "edition_book_count": 2,
    }
    metadata = {"status": "ended", "chapter_count": 24}
    index = build_chapter_index(manga, metadata, releases)
    slots = {slot["key"]: slot for slot in index["slots"]}
    assert slots["chapter:3"]["duplicate_of_volume"] is None
    assert index["edition_split"] is None
    assert slots["chapter:13"]["covered_by_volume"] is None  # book 2 not owned


def test_one_mistyped_release_does_not_demote_a_whole_map():
    """Yawara: MangaUpdates logged "c74 v5" among ninety good entries that put
    chapter 74 in book 7. The mistyped entry alone becomes a hint; the book
    that legitimately holds the chapter, and the rest of the map, stay exact."""

    from tankarr.chapter_map import MapEntry, downgrade_sparse_maps

    def entry(volume: str, chapters: tuple[str, ...]):
        return MapEntry(
            volumes=(volume,), chapters=chapters, exact=True, source="mangaupdates"
        )

    good = [
        entry(str(v), tuple(str(n) for n in range(1 + 12 * (v - 1), 1 + 12 * v)))
        for v in range(1, 9)
    ]
    typo = entry("5", ("74",))
    kept = downgrade_sparse_maps([*good, typo])
    assert [
        item.exact
        for item in kept
        if item.volumes == ("5",) and item.chapters == ("74",)
    ] == [False]
    assert all(item.exact for item in kept if item is not kept[-1])


def test_a_release_logged_too_early_does_not_demote_the_chapters_after_it():
    """Yawara again, the other way round: MangaUpdates put chapter 72 in book 8
    while books 7 goes on to chapter 79. Counted against a running maximum the
    six good book-7 entries look inverted and the whole map fell back to a
    hint; only the early entry is the stray one."""

    from tankarr.chapter_map import MapEntry, downgrade_sparse_maps

    def entry(volume: str, chapters: tuple[str, ...]):
        return MapEntry(
            volumes=(volume,), chapters=chapters, exact=True, source="mangaupdates"
        )

    good = [
        entry(
            str(v),
            tuple(str(n) for n in range(1 + 12 * (v - 1), 1 + 12 * v) if n != 72),
        )
        for v in range(1, 11)
    ]
    stray = entry("8", ("72",))
    kept = downgrade_sparse_maps([*good, stray])
    verdicts = {(item.volumes, item.chapters): item.exact for item in kept}
    assert verdicts[(("8",), ("72",))] is False
    assert all(exact for key, exact in verdicts.items() if key != (("8",), ("72",)))


def test_operator_map_materializes_missing_books_chapters_and_known_fractions():
    from tankarr.chapter_map import entries_from_boundaries

    chapter_map = entries_from_boundaries(
        [{"volume": 1, "first_chapter": 0}, {"volume": 2, "first_chapter": 3}],
        last_chapter="4.5",
        known_chapters=["0.5", "3.5"],
    )
    index = build_chapter_index(
        {
            "status": "completed",
            "series_unit_override": "chapters",
            "monitor_mode": "all",
        },
        {"status": "ended"},
        [_book("1"), release("0"), release("0.5")],
        chapter_map=chapter_map,
    )
    slots = {slot["chapter"]: slot for slot in index["slots"]}
    assert set(slots) == {"0", "0.5", "1", "2", "3", "3.5", "4", "4.5"}
    assert slots["0"]["downloaded"] and slots["0"]["duplicate_of_volume"] == "1"
    assert slots["0.5"]["duplicate_of_volume"] == "1"
    assert slots["2"]["covered_by_volume"] == "1"
    for label in ("3", "3.5", "4", "4.5"):
        assert slots[label]["volume"] == "2"
        assert slots[label]["monitored"] and slots[label]["searchable"]
        assert not slots[label]["covered_unmapped"]
        assert slots[label]["covered_by_volume"] is None
    assert index["raw_missing_count"] == 4


def test_volume_mode_exposes_operator_chapters_without_changing_book_counts():
    from tankarr.chapter_map import entries_from_boundaries

    chapter_map = entries_from_boundaries(
        [{"volume": 1, "first_chapter": 0}, {"volume": 2, "first_chapter": 2}],
        last_chapter=3,
        known_chapters=["0.5", "2.5"],
    )
    manga = {
        "status": "completed",
        "series_unit_override": "volumes",
        "monitor_mode": "all",
    }
    metadata = {"status": "ended", "volume_count": 2}
    releases = [_book("1"), release("0.5"), release("2")]
    index = build_chapter_index(manga, metadata, releases, chapter_map=chapter_map)
    assert index["unit"] == "volume" and index["series_unit"] == "volumes"
    assert index["expected_count"] == 2 and index["owned_volume_count"] == 1
    assert index["raw_missing_count"] == 1
    assert [slot["key"] for slot in index["slots"]] == ["volume:1", "volume:2"]
    chapters = {slot["chapter"]: slot for slot in index["mapped_chapter_slots"]}
    assert len(chapters) == 6
    assert chapters["0"]["covered_by_volume"] == "1"
    assert chapters["0.5"]["duplicate_of_volume"] == "1"
    assert chapters["2"]["downloaded"]
    assert not chapters["2.5"]["available"] and chapters["2.5"]["searchable"]
    assert chapters["3"]["covered_by_volume"] is None


def test_reverse_coverage_requires_zero_and_every_exact_fraction():
    from tankarr.chapter_map import MapEntry

    chapter_map = [MapEntry(("1",), ("0", "0.5", "1"), True, "operator")]
    manga = {
        "status": "completed",
        "series_unit_override": "volumes",
        "monitor_mode": "all",
    }
    metadata = {"status": "ended", "volume_count": 1}
    for missing in ("0", "0.5", "1"):
        releases = [release(label) for label in ("0", "0.5", "1") if label != missing]
        index = build_chapter_index(manga, metadata, releases, chapter_map=chapter_map)
        assert not index["slots"][0]["covered_by_chapters"]
    complete = build_chapter_index(
        manga,
        metadata,
        [release(label) for label in ("0", "0.5", "1")],
        chapter_map=chapter_map,
    )
    assert complete["slots"][0]["covered_by_chapters"]
    assert complete["slots"][0]["coverage_exact"]


def test_suspect_book_keeps_ownership_but_never_covers_chapters_or_duplicates():
    from tankarr.chapter_map import MapEntry

    manga = {
        "status": "completed",
        "series_unit_override": "chapters",
        "monitor_mode": "all",
        "edition_book_count": 1,
    }
    index = build_chapter_index(
        manga,
        {"status": "ended", "chapter_count": 3},
        [_book("1"), release("1")],
        chapter_map=[MapEntry(("1",), ("1", "2", "3"), True, "operator")],
        suspect_covering_volumes={"01"},
    )
    assert index["owned_volume_count"] == 1 and index["owned_volumes"] == ["1"]
    assert index["duplicate_count"] == 0 and index["covered_count"] == 0
    assert index["covered_unmapped_count"] == 0
    assert index["raw_missing_count"] == 2
    assert index["edition_split"] is None


def test_operator_assignment_overrides_edition_estimate_and_ongoing_catalogue_hint():
    from tankarr.chapter_map import MapEntry

    chapter_map = [
        MapEntry(("1",), ("1", "2", "3", "4"), True, "catalogue"),
        MapEntry(("2",), ("3", "4"), True, "operator"),
    ]
    index = build_chapter_index(
        {
            "status": "ongoing",
            "series_unit_override": "chapters",
            "monitor_mode": "all",
            "edition_book_count": 1,
        },
        {"chapter_count": 4},
        [_book("1"), release("3", downloaded=False)],
        chapter_map=chapter_map,
    )
    slots = {slot["chapter"]: slot for slot in index["slots"]}
    assert slots["3"]["volume"] == "2"
    assert slots["3"]["covered_by_volume"] is None
    assert slots["4"]["volume"] == "2"
    assert slots["4"]["covered_by_volume"] is None


def test_a_whole_owned_hint_span_never_covers_or_duplicates_any_chapter():
    from tankarr.chapter_map import MapEntry

    index = build_chapter_index(
        {
            "status": "completed",
            "series_unit_override": "chapters",
            "monitor_mode": "all",
        },
        {"status": "ended"},
        [_book("1"), _book("2"), release("1"), release("2", downloaded=False)],
        chapter_map=[MapEntry(("1", "2"), ("1", "2"), False)],
    )
    slots = {slot["chapter"]: slot for slot in index["slots"]}
    assert slots["1"]["duplicate_of_volume"] is None
    assert slots["2"]["covered_by_volume"] is None
    assert slots["2"]["covered_unmapped"]
    assert slots["1"]["volume"] is None and slots["2"]["volume"] is None


def test_a_volume_with_a_chapter_label_does_not_satisfy_another_books_map():
    from tankarr.chapter_map import MapEntry

    index = build_chapter_index(
        {
            "status": "completed",
            "series_unit_override": "volumes",
            "monitor_mode": "all",
        },
        {"status": "ended", "volume_count": 2},
        [{**_book("2"), "chapter": "1"}],
        chapter_map=[MapEntry(("1",), ("1",), True, "operator")],
    )
    slots = {slot["volume"]: slot for slot in index["slots"]}
    assert slots["2"]["downloaded"]
    assert not slots["1"]["covered_by_chapters"]


def test_operator_prologue_accepts_numeric_zero_even_with_a_source_volume_hint():
    from tankarr.chapter_map import MapEntry

    index = build_chapter_index(
        {
            "status": "completed",
            "series_unit_override": "chapters",
            "monitor_mode": "all",
        },
        {"status": "ended"},
        [{**release("0", volume="99"), "chapter": 0}],
        chapter_map=[MapEntry(("1",), ("0",), True, "operator")],
    )
    assert index["slots"][0]["chapter"] == "0"
    assert index["slots"][0]["volume"] == "1"
    assert index["slots"][0]["downloaded"]


def test_suspect_volume_inference_uses_only_owned_books_and_canonical_labels():
    from tankarr.chapter_mapping import suspect_volume_reasons

    reasons = suspect_volume_reasons(
        [
            {**_book("01"), "provider": "manual"},
            {**_book("2"), "pages": 601},
            {**_book("3"), "pages": 600},
            {**_book("4", downloaded=False), "provider": "manual", "pages": 900},
            {**release("5", volume="5"), "pages": 900, "provider": "manual"},
            {**_book("6"), "provider": "manual", "pages": "unknown"},
            {**_book("7"), "pages": float("inf")},
        ]
    )
    assert reasons == {
        "1": "Imported by hand",
        "2": "601 pages: not verified as one book",
        "6": "Imported by hand",
    }


@pytest.mark.parametrize("source", ["operator", "catalogue"])
def test_manual_book_only_covers_missing_chapters_with_operator_boundaries(source):
    from tankarr.chapter_map import MapEntry

    chapter_map = [MapEntry(("1",), ("1", "2"), True, source)]
    manga = {
        "status": "completed",
        "series_unit_override": "chapters",
        "monitor_mode": "all",
    }
    index = build_chapter_index(
        manga,
        {"status": "ended"},
        [{**_book("1"), "provider": "manual"}, release("1")],
        chapter_map=chapter_map,
    )
    slots = {slot["chapter"]: slot for slot in index["slots"]}
    assert slots["1"]["downloaded"] and slots["1"]["duplicate_of_volume"] is None
    if source == "operator":
        assert slots["2"]["covered_by_volume"] == "1"
        assert not slots["2"]["covered_unmapped"]
    else:
        assert "2" not in slots  # a catalogue hint does not invent a chapter slot
    assert index["owned_volume_count"] == 1 and index["series_unit"] == "chapters"
    assert index["raw_missing_count"] == 0


def test_oversized_book_is_suspect_in_supplemental_volume_mode_chapters():
    from tankarr.chapter_map import MapEntry

    index = build_chapter_index(
        {
            "status": "completed",
            "series_unit_override": "volumes",
            "monitor_mode": "all",
        },
        {"status": "ended", "volume_count": 1},
        [{**_book("1"), "pages": 601}, release("1")],
        chapter_map=[MapEntry(("1",), ("1", "2"), True, "operator")],
    )
    assert index["slots"][0]["downloaded"]
    assert index["raw_missing_count"] == 0 and index["expected_count"] == 1
    assert index["series_unit"] == "volumes" and index["owned_volume_count"] == 1
    chapters = {slot["chapter"]: slot for slot in index["mapped_chapter_slots"]}
    assert chapters["1"]["duplicate_of_volume"] is None
    assert chapters["2"]["covered_by_volume"] is None
    assert not chapters["2"]["covered_unmapped"]


def test_suspect_book_does_not_satisfy_a_different_book_through_its_chapter_label():
    from tankarr.chapter_map import MapEntry

    index = build_chapter_index(
        {
            "status": "completed",
            "series_unit_override": "volumes",
            "monitor_mode": "all",
        },
        {"status": "ended", "volume_count": 2},
        [{**_book("2"), "chapter": "1", "provider": "manual", "pages": 900}],
        chapter_map=[MapEntry(("1",), ("1",), True, "operator")],
    )
    slots = {slot["volume"]: slot for slot in index["slots"]}
    assert slots["2"]["downloaded"] and not slots["1"]["covered_by_chapters"]
    assert index["raw_missing_count"] == 1 and index["owned_volume_count"] == 1


def test_explicit_empty_suspect_set_overrides_inference_and_keeps_exact_coverage():
    from tankarr.chapter_map import MapEntry

    index = build_chapter_index(
        {
            "status": "completed",
            "series_unit_override": "chapters",
            "monitor_mode": "all",
        },
        {"status": "ended"},
        [{**_book("1"), "provider": "manual", "pages": 900}, release("1")],
        chapter_map=[MapEntry(("1",), ("1", "2"), True, "operator")],
        suspect_covering_volumes=set(),
    )
    slots = {slot["chapter"]: slot for slot in index["slots"]}
    assert slots["1"]["duplicate_of_volume"] == "1"
    assert slots["2"]["covered_by_volume"] == "1"
    assert index["duplicate_count"] == 1 and index["covered_count"] == 1


def test_unverified_assembly_metadata_cannot_bypass_suspect_inference():
    from tankarr.chapter_mapping import suspect_volume_reasons

    assert "1" in suspect_volume_reasons(
        [{**_book("1"), "provider": "manual", "assembled_from": ["claimed-chapter"]}]
    )


def test_a_persisted_estimate_cannot_establish_owned_book_coverage():
    """Legacy estimates cannot prove chapter ownership or duplicate files."""

    from tankarr.chapter_map import MapEntry

    manga = {
        "id": "blade",
        "title": "Blade",
        "status": "ended",
        "monitor_mode": "all",
        "series_unit_override": "chapters",
        "expected_count_override": 9,
    }

    def chapter_release(identifier, number, **extra):
        return {
            "id": identifier,
            "manga_id": "blade",
            "volume": None,
            "chapter": number,
            "release_unit": "chapter",
            "downloaded": False,
            "provider": "suwayomi",
            "language": "en",
            **extra,
        }

    releases = [
        {
            "id": "b1",
            "manga_id": "blade",
            "volume": "1",
            "chapter": None,
            "release_unit": "volume",
            "downloaded": True,
            "provider": "manual",
            "language": "en",
            "library_path": "/library/b1.cbz",
            "pages": 200,
        },
        {
            "id": "b2",
            "manga_id": "blade",
            "volume": "2",
            "chapter": None,
            "release_unit": "volume",
            "downloaded": True,
            "provider": "manual",
            "language": "en",
            "library_path": "/library/b2.cbz",
            "pages": 200,
        },
        *[chapter_release(f"c{n}", str(n)) for n in range(1, 10)],
        chapter_release("c40.5", "40.5"),
        chapter_release("c40.5b", "40.5", source_key="other", source_name="Other"),
    ]
    entries = [
        MapEntry(
            volumes=("1",), chapters=("1", "2", "3", "4"), exact=True, source="estimate"
        ),
        MapEntry(
            volumes=("2",), chapters=("5", "6", "7", "8"), exact=True, source="estimate"
        ),
    ]
    index = build_chapter_index(
        manga,
        None,
        releases,
        {},
        chapter_map=entries,
        suspect_covering_volumes={"1", "2"},
    )
    by = {
        slot["chapter"]: slot for slot in index["slots"] if slot["chapter"] is not None
    }
    assert all(not by[str(n)]["covered_by_volume"] for n in range(1, 10))
    assert all(not by[str(n)]["coverage_exact"] for n in range(1, 10))
    assert not any(slot.get("duplicate_of_volume") for slot in by.values())


def test_a_number_listed_only_as_a_stub_is_observed_but_not_expected():
    from tankarr.chapter_mapping import build_chapter_index

    releases = [
        {
            "id": "a",
            "chapter": "202",
            "volume": None,
            "release_unit": "chapter",
            "downloaded": 1,
            "pages": 37,
            "source_name": "MangaDex (EN)",
            "language": "en",
        },
        {
            "id": "b",
            "chapter": "203",
            "volume": None,
            "release_unit": "chapter",
            "downloaded": 1,
            "pages": 36,
            "source_name": "MangaDex (EN)",
            "language": "en",
        },
        {
            "id": "c",
            "chapter": "204",
            "volume": None,
            "release_unit": "chapter",
            "downloaded": 0,
            "pages": 1,
            "source_name": "Manga Ball (EN)",
            "language": "en",
        },
        {
            "id": "d",
            "chapter": "205",
            "volume": None,
            "release_unit": "chapter",
            "downloaded": 0,
            "pages": 1,
            "source_name": "Manga Ball (EN)",
            "language": "en",
        },
    ]
    manga = {
        "id": "g",
        "title": "Guyver",
        "status": "hiatus",
        "monitor_mode": "all",
        "preferred_language": "en",
    }
    index = build_chapter_index(manga, {"status": "hiatus"}, releases, {}, [])
    by_chapter = {slot.get("chapter"): slot for slot in index["slots"]}

    assert by_chapter["202"]["expected"] is True
    assert by_chapter["204"]["expected"] is False
    assert by_chapter["205"]["expected"] is False
    assert index["raw_missing_count"] == 0
    # ...and a stub past the last real chapter opens no gap up to it.
    del releases[1]  # 203 gone: 202 real, 204/205 stubs
    index = build_chapter_index(manga, {"status": "hiatus"}, releases, {}, [])
    assert [slot.get("chapter") for slot in index["slots"] if slot.get("expected")] == [
        "202"
    ]


def test_a_book_far_off_the_editions_size_is_suspect_with_its_reason():
    from tankarr.chapter_mapping import suspect_volume_reasons

    def book(volume, pages, provider="prowlarr"):
        return {
            "volume": str(volume),
            "chapter": None,
            "release_unit": "volume",
            "downloaded": 1,
            "pages": pages,
            "provider": provider,
        }

    releases = [
        book(n, p) for n, p in ((1, 324), (2, 319), (3, 319), (4, 356), (5, 300))
    ]
    releases += [book(11, 434, "manual"), book(12, 218, "manual")]

    suspects = suspect_volume_reasons(releases)

    assert set(suspects) == {"11", "12"}
    assert "another edition or a wrong split" in suspects["11"]
    assert "218 pages against an edition of about 319" in suspects["12"]
    # too few books to know the edition's size: nothing is judged by pages
    assert suspect_volume_reasons([book(1, 324), book(2, 120)]) == {}


def test_a_book_numbered_past_the_edition_is_observed_but_not_expected():
    from tankarr.chapter_mapping import build_chapter_index

    releases = [
        {
            "id": f"v{n}",
            "chapter": None,
            "volume": str(n),
            "release_unit": "volume",
            "downloaded": 1 if n <= 12 else 0,
            "pages": 320,
            "provider": "prowlarr",
            "language": "en",
        }
        for n in (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15)
    ]
    manga = {
        "id": "mk",
        "title": "Master Keaton",
        "status": "ended",
        "monitor_mode": "all",
        "preferred_language": "en",
        "edition_book_count": 12,
    }
    index = build_chapter_index(
        manga, {"status": "ended", "volume_count": 18}, releases, {}, []
    )

    assert index["expected_volume_count"] == 12
    beyond = [
        slot for slot in index["slots"] if slot.get("volume") in {"13", "14", "15"}
    ]
    assert beyond and all(slot["expected"] is False for slot in beyond)
    assert index["raw_missing_count"] == 0


def test_numbers_past_a_manual_chapter_total_are_not_expected():
    from tankarr.chapter_mapping import build_chapter_index

    def chapter(n, downloaded, pages, source):
        return {
            "id": f"c{n}-{source}",
            "chapter": str(n),
            "volume": None,
            "release_unit": "chapter",
            "downloaded": downloaded,
            "pages": pages,
            "source_name": source,
            "language": "en",
        }

    releases = [chapter(n, 1, 28, "MangaDex") for n in range(1, 201)]
    releases += [chapter(202, 0, 37, "Manga Ball"), chapter(203, 0, 36, "Manga Ball")]
    manga = {
        "id": "g",
        "title": "Guyver",
        "status": "hiatus",
        "monitor_mode": "all",
        "preferred_language": "en",
        "expected_count_override": 200,
        "expected_count_unit_override": "chapter",
    }
    index = build_chapter_index(
        manga, {"status": "hiatus", "chapter_count": 201}, releases, {}, []
    )

    by = {slot.get("chapter"): slot for slot in index["slots"]}
    assert by["200"]["expected"] is True
    assert by["202"]["expected"] is False and by["203"]["expected"] is False
    assert index["raw_missing_count"] == 0
    assert index["expected_count"] == 200


def test_ended_catalogue_count_including_prologue_ends_sequence_at_numbering():
    from tankarr.chapter_mapping import build_chapter_index

    def chapter(n, downloaded, pages, source):
        return {
            "id": f"c{n}-{source}",
            "chapter": str(n),
            "volume": None,
            "release_unit": "chapter",
            "downloaded": downloaded,
            "pages": pages,
            "source_name": source,
            "language": "en",
        }

    releases = [chapter(n, 1, 28, "MangaDex") for n in range(1, 201)]
    releases += [chapter("0", 0, -1, "ComicK"), chapter(202, 0, 37, "Manga Ball")]
    manga = {
        "id": "g",
        "title": "Guyver",
        "status": "ended",
        "monitor_mode": "all",
        "preferred_language": "en",
    }
    index = build_chapter_index(
        manga,
        {
            "status": "ended",
            "chapter_count": 201,
            "provenance": {"chapter_count": "consensus:anilist,kitsu"},
        },
        releases,
        {},
        [],
    )

    assert index["expected_count"] == 200
    assert index["raw_missing_count"] == 0
    assert all(
        slot["expected"] is False
        for slot in index["slots"]
        if slot.get("chapter") == "202"
    )


def test_a_hand_built_book_off_the_editions_size_asks_for_the_real_one():
    from tankarr.chapter_mapping import build_chapter_index

    def book(volume, pages, provider="prowlarr"):
        return {
            "id": f"v{volume}-{provider}",
            "chapter": None,
            "volume": str(volume),
            "release_unit": "volume",
            "downloaded": 1,
            "pages": pages,
            "provider": provider,
            "language": "en",
        }

    releases = [
        book(n, p) for n, p in ((1, 324), (2, 319), (3, 319), (4, 356), (5, 300))
    ]
    releases += [book(6, 434, "manual")]
    manga = {
        "id": "mk",
        "title": "Master Keaton",
        "status": "ended",
        "monitor_mode": "all",
        "preferred_language": "en",
        "edition_book_count": 6,
    }
    index = build_chapter_index(
        manga, {"status": "ended", "volume_count": 6}, releases, {}, []
    )

    six = next(slot for slot in index["slots"] if slot.get("volume") == "6")
    assert six["downloaded"] is True
    assert "another edition or a wrong split" in six["replaceable"]
    assert all(
        "replaceable" not in slot
        for slot in index["slots"]
        if slot.get("volume") != "6"
    )


def _listed(label: str, source: str, *, downloaded: bool = True) -> dict:
    return {
        "id": f"{source}-{label}",
        "chapter": label,
        "volume": None,
        "release_unit": "chapter",
        "downloaded": downloaded,
        "monitored": True,
        "pages": 20,
        "source_name": source,
        "language": "en",
    }


def test_a_source_list_that_matches_the_count_ends_the_numbering_at_its_decimals():
    """SPRIGGAN: the catalogue counts 62 chapters and the sources that carry
    the work whole end "…59, 60, 60.5, 60.6" - 62 entries. Those decimals are
    the last two chapters, bound in the last book; 61 and 62 were never
    written and must not sit in Wanted forever."""

    from tankarr.chapter_map import entries_from_releases

    releases = [
        _listed(str(number), source)
        for source in ("Atsumaru", "Weeb Central")
        for number in range(1, 61)
    ]
    releases += [
        _listed(label, source, downloaded=False)
        for source in ("Atsumaru", "Weeb Central")
        for label in ("60.5", "60.6")
    ]
    manga = {
        "id": "s",
        "title": "SPRIGGAN",
        "status": "completed",
        "last_chapter": "62",
        "monitor_mode": "all",
        "preferred_language": "en",
    }
    metadata = {"status": "ended", "chapter_count": 62, "volume_count": 11}
    chapter_map = entries_from_releases([{"volume": "11", "chapter": "57-60"}])

    index = build_chapter_index(manga, metadata, releases, chapter_map=chapter_map)

    keys = {slot["key"] for slot in index["slots"]}
    assert "chapter:61" not in keys and "chapter:62" not in keys
    tail = [slot for slot in index["slots"] if slot["chapter"] in {"60.5", "60.6"}]
    assert len(tail) == 2
    assert all(slot["expected"] and slot["monitored"] for slot in tail)
    assert {slot["volume"] for slot in tail} == {None}
    assert {slot["evidence"] for slot in tail} == {"counted_tail"}
    assert index["sequence_end"] == 60
    assert index["expected_count"] == 62
    assert index["raw_missing_count"] == 2


def test_a_lone_list_matching_the_count_does_not_turn_its_omake_into_a_chapter():
    # One aggregator lists 44 chapters plus a "44.5" nobody else carries: 45
    # entries, the catalogue's count. One list is not evidence - the omake
    # stays an extra and the last chapter stays expected.
    releases = [_listed(str(number), "A") for number in range(1, 45)]
    releases.append(_listed("44.5", "A", downloaded=False))
    manga = {
        "id": "a",
        "status": "completed",
        "last_chapter": "45",
        "monitor_mode": "all",
        "preferred_language": "en",
    }

    index = build_chapter_index(
        manga, {"status": "ended", "chapter_count": 45}, releases
    )

    keys = {slot["key"] for slot in index["slots"]}
    assert "chapter:45" in keys and "chapter:44.5" not in keys
    assert index["sequence_end"] == 45


def test_complete_source_lists_count_internal_extras_without_phantom_tail():
    rows = [
        {**release(str(n), provider=source, downloaded=n <= 8), "source_name": source}
        for source in ("first", "second")
        for n in range(1, 9)
    ]
    rows += [
        {**release(n, provider=source, downloaded=False), "source_name": source}
        for source in ("first", "second")
        for n in ("2.5", "5.5")
    ]
    index = build_chapter_index(
        {"status": "ended", "last_chapter": "10", "monitor_mode": "all"},
        {"status": "ended", "chapter_count": 10},
        rows,
    )
    missing = [
        slot["chapter"]
        for slot in index["slots"]
        if slot["expected"] and not slot["downloaded"]
    ]
    assert set(missing) == {"2.5", "5.5"}
    assert index["raw_missing_count"] == 2
    assert index["expected_count"] == 10
    assert index["expected_satisfied_count"] == 8
    assert not any(slot["chapter"] in {"9", "10"} for slot in index["slots"])


def test_numbered_prologue_file_does_not_satisfy_normal_chapter():
    rows = [release(str(n)) for n in range(1, 7)]
    rows[1]["downloaded"] = False
    rows.append({**release("2", identifier="prelude"), "title": "Prologue 2"})
    index = build_chapter_index(
        {"status": "ongoing", "monitor_mode": "all"},
        {},
        rows,
    )
    slot = next(slot for slot in index["slots"] if slot["chapter"] == "2")
    assert not slot["downloaded"]
    assert index["raw_missing_count"] == 1


@pytest.mark.parametrize("next_chapter", [False, True])
def test_verified_publication_number_is_not_a_final_edition_limit(next_chapter):
    manga = {
        "status": "ongoing",
        "monitor_mode": "all",
        "series_unit_override": "chapters",
        "verified_chapter_count": 3,
        "verified_chapter_source": "https://publisher.example/series",
    }
    releases = [release(str(n)) for n in range(1, 4)]
    if next_chapter:
        releases.append(release("4", downloaded=False))
    index = build_chapter_index(manga, {}, releases)
    assert index["expected_count"] == (4 if next_chapter else 3)
    assert index["reference_kind"] == "verified"
    from tankarr.series_summary import decorate_series_summary

    summary = decorate_series_summary(dict(manga), {}, index)
    assert summary["library_count"]["reference_unknown"] is False
    assert summary["library_count"]["source"] == manga["verified_chapter_source"]
    assert index["raw_missing_count"] == int(next_chapter)
    if next_chapter:
        slot = next(s for s in index["slots"] if s["chapter"] == "4")
        assert slot["expected"] and not slot["special"]


def test_verified_publication_number_materialises_unindexed_gaps():
    index = build_chapter_index(
        {
            "status": "ongoing",
            "monitor_mode": "all",
            "series_unit_override": "chapters",
            "verified_chapter_count": 3,
            "verified_chapter_source": "publisher",
        },
        {},
        [release("1")],
    )
    assert index["expected_count"] == 3
    assert index["raw_missing_count"] == 2


def test_explicit_edition_limit_takes_precedence_over_verified_publication():
    index = build_chapter_index(
        {
            "status": "hiatus",
            "monitor_mode": "all",
            "series_unit_override": "chapters",
            "verified_chapter_count": 4,
            "verified_chapter_source": "publisher",
            "expected_count_override": 3,
            "expected_count_unit_override": "chapter",
        },
        {},
        [release(str(n)) for n in range(1, 4)] + [release("4", downloaded=False)],
    )
    assert index["expected_count"] == 3
    assert index["reference_kind"] == "manual"
    assert index["raw_missing_count"] == 0


def test_owned_prologues_are_visible_without_inflating_numbered_chapters():
    chapters = [release(str(n)) for n in range(1, 4)]
    prologues = [
        {
            **release("0", identifier=f"prologue-{n}"),
            "chapter": None,
            "title": f"Prologue {n}",
        }
        for n in range(1, 3)
    ]
    index = build_chapter_index(
        {
            "status": "ongoing",
            "monitor_mode": "all",
            "series_unit_override": "chapters",
            "verified_chapter_count": 3,
            "verified_chapter_source": "publisher",
        },
        {},
        chapters + prologues + [{**prologues[0], "id": "alternate-scan"}],
    )
    assert index["expected_count"] == 3
    assert index["expected_satisfied_count"] == 3
    assert index["raw_missing_count"] == 0
    assert index["additional_content"] == {"prologues": 2, "extras": 0}


def test_catalogue_counted_content_is_not_added_twice():
    from tankarr.chapter_mapping import owned_additional_content

    prologue = {"id": "prelude", "title": "Prologue 1", "downloaded": True}
    extra = {"id": "extra", "title": "Omake", "downloaded": True}
    missing = {"id": "absent", "title": "Prologue 2", "downloaded": False}
    slots = [{"expected": True, "releases": [prologue]}]
    assert owned_additional_content([prologue, extra, missing], slots) == {
        "prologues": 0,
        "extras": 1,
    }
