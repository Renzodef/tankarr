from tankarr.database import logical_chapter_coverage
from tankarr.series_summary import (
    decorate_series_summary,
    library_count_summary,
    publication_summary,
)


def test_continuing_summary_does_not_turn_catalogue_extras_into_missing():
    summary = library_count_summary(
        {
            "status": "hiatus",
            "chapter_count": 70,
            "downloaded_count": 70,
            "numbered_chapter_count": 65,
            "logical_chapter_count": 65,
            "logical_downloaded_count": 65,
            "special_chapter_count": 5,
            "special_downloaded_count": 5,
        },
        {"status": "hiatus", "chapter_count": 70},
    )
    assert summary["downloaded_count"] == summary["total_count"] == 65
    assert summary["missing_count"] == 0
    assert summary["catalogue_expected_count"] == 70


def test_volume_progress_and_diagnostics_ignore_inactive_chapter_override():
    manga = {
        "status": "ended",
        "numbered_chapter_count": 30,
        "numbered_volume_count": 3,
        "chapter_count": 33,
        "downloaded_count": 3,
        "expected_count_override": 27,
        "expected_count_unit_override": "chapter",
    }
    metadata = {
        "status": "ended",
        "chapter_count": 30,
        "volume_count": 3,
        "provenance": {
            "chapter_count": "chapters catalogue",
            "volume_count": "books catalogue",
        },
    }
    index = {
        "unit": "volume",
        "expected_volume_count": 3,
        "slots": [
            {"expected": True, "available": True, "downloaded": True, "chapter": None}
            for _ in range(3)
        ],
    }
    summary = decorate_series_summary(manga, metadata, index)["library_count"]
    assert summary["downloaded_count"] == summary["total_count"] == 3
    assert summary["missing_count"] == 0
    assert summary["metadata_field"] == "volume_count"
    assert summary["source"] == summary["catalogue_source"] == "books catalogue"
    assert summary["expected_count_overridden"] is False
    assert summary["segmentation_differs"] is False
    assert summary["count_note"] is None
    assert manga["expected_count_override"] == 27  # retained for switching back

    # A real volume override remains meaningful and keeps its own explanation.
    manga.update(expected_count_override=3, expected_count_unit_override="volume")
    summary = decorate_series_summary(manga, {**metadata, "volume_count": 4}, index)[
        "library_count"
    ]
    assert summary["expected_count_overridden"] is True
    assert "3 volumes" in summary["count_note"]
    assert "4 original-work volumes" in summary["count_note"]


def test_logical_chapter_coverage_collapses_split_parts_and_keeps_specials():
    coverage = logical_chapter_coverage(
        [
            {"chapter": "1", "downloaded": True},
            {"chapter": "2", "downloaded": True},
            {"chapter": "3.1", "downloaded": True},
            {"chapter": "3.2", "downloaded": True},
            {"chapter": "4", "downloaded": True},
            {"chapter": "5", "downloaded": True},
            {"chapter": "5.5", "downloaded": True},
        ]
    )

    assert coverage == {
        "logical_chapter_count": 5,
        "logical_downloaded_count": 5,
        "special_chapter_count": 1,
        "special_downloaded_count": 1,
    }


def test_split_chapter_is_downloaded_only_when_every_known_part_is_present():
    coverage = logical_chapter_coverage(
        [
            {"chapter": "1", "downloaded": True},
            {"chapter": "2.1", "downloaded": True},
            {"chapter": "2.2", "downloaded": False},
        ]
    )

    assert coverage["logical_chapter_count"] == 2
    assert coverage["logical_downloaded_count"] == 1


def test_catalogue_count_uses_logical_chapters_instead_of_physical_parts():
    summary = library_count_summary(
        {
            "provider": "mangadex",
            "status": "completed",
            "chapter_count": 7,
            "downloaded_count": 7,
            "logical_chapter_count": 5,
            "logical_downloaded_count": 5,
            "special_chapter_count": 1,
            "special_downloaded_count": 1,
            "numbered_chapter_count": 7,
            "numbered_volume_count": 0,
        },
        {
            "status": "ended",
            "chapter_count": 5,
            "provenance": {"chapter_count": "myanimelist"},
        },
    )

    assert summary["available_count"] == 5
    assert summary["downloaded_count"] == 5
    assert summary["total_count"] == 5
    assert summary["missing_count"] == 0
    assert summary["count_conflict"] is False


def test_metadata_cardinality_can_be_satisfied_by_numbered_specials():
    summary = library_count_summary(
        {
            "provider": "local",
            "status": None,
            "chapter_count": 100,
            "downloaded_count": 100,
            "logical_chapter_count": 90,
            "logical_downloaded_count": 90,
            "special_chapter_count": 10,
            "special_downloaded_count": 10,
            "numbered_chapter_count": 100,
            "numbered_volume_count": 0,
        },
        {
            "status": "ended",
            "chapter_count": 92,
            "provenance": {"chapter_count": "myanimelist"},
        },
    )

    assert summary["available_count"] == 92
    assert summary["downloaded_count"] == 92
    assert summary["missing_count"] == 0
    assert summary["count_conflict"] is False


def test_integral_provider_final_keeps_exact_sequence_semantics():
    summary = library_count_summary(
        {
            "provider": "mangadex",
            "status": "completed",
            "last_chapter": "83",
            "chapter_count": 86,
            "downloaded_count": 86,
            "logical_chapter_count": 82,
            "logical_downloaded_count": 82,
            "special_chapter_count": 4,
            "special_downloaded_count": 4,
            "numbered_chapter_count": 86,
            "numbered_volume_count": 0,
        },
        {
            "status": "ended",
            "chapter_count": 83,
            "provenance": {"chapter_count": "myanimelist"},
        },
    )

    assert summary["available_count"] == 82
    assert summary["downloaded_count"] == 82
    assert summary["missing_count"] == 1
    assert summary["unindexed_missing_count"] == 1


def test_cardinality_still_reports_indexed_and_unindexed_deficits():
    summary = library_count_summary(
        {
            "provider": "local",
            "status": None,
            "chapter_count": 5,
            "downloaded_count": 4,
            "logical_chapter_count": 4,
            "logical_downloaded_count": 3,
            "special_chapter_count": 1,
            "special_downloaded_count": 1,
            "numbered_chapter_count": 5,
            "numbered_volume_count": 0,
        },
        {
            "status": "ended",
            "chapter_count": 7,
            "provenance": {"chapter_count": "myanimelist"},
        },
    )

    assert summary["available_count"] == 5
    assert summary["downloaded_count"] == 4
    assert summary["missing_count"] == 3
    assert summary["indexed_missing_count"] == 1
    assert summary["unindexed_missing_count"] == 2


def test_catalogue_total_exposes_unindexed_missing_chapters_for_ended_work():
    manga = {
        "provider": "mangadex",
        "status": "ended",
        "chapter_count": 3,
        "downloaded_count": 0,
        "numbered_chapter_count": 3,
        "numbered_volume_count": 0,
    }
    metadata = {
        "status": "ended",
        "chapter_count": 84,
        "volume_count": 21,
        "provenance": {
            "chapter_count": "myanimelist",
            "volume_count": "myanimelist",
        },
        "classification": {"kind": "manga"},
    }

    summary = library_count_summary(manga, metadata)

    assert summary == {
        "unit": "chapter",
        "available_count": 3,
        "downloaded_count": 0,
        "expected_count": 84,
        "total_count": 84,
        "missing_count": 84,
        "indexed_missing_count": 3,
        "unindexed_missing_count": 81,
        "source": "myanimelist",
        "metadata_field": "chapter_count",
        "count_conflict": False,
        "catalogue_expected_count": 84,
        "catalogue_source": "myanimelist",
        "segmentation_differs": False,
        "count_note": None,
        "expected_count_overridden": False,
        "automatic_expected_count": 84,
        "automatic_expected_source": "myanimelist",
    }


def test_volume_archives_compare_with_volume_metadata_not_chapter_metadata():
    summary = library_count_summary(
        {
            "provider": "local",
            "status": "completed",
            "last_volume": "21",
            "chapter_count": 2,
            "downloaded_count": 2,
            "numbered_chapter_count": 0,
            "numbered_volume_count": 2,
        },
        {
            "status": "ended",
            "chapter_count": 84,
            "volume_count": 21,
            "provenance": {
                "chapter_count": "myanimelist",
                "volume_count": "myanimelist",
            },
            "classification": {"kind": "manga"},
        },
    )

    assert summary["unit"] == "volume"
    assert summary["expected_count"] == 21
    assert summary["missing_count"] == 19
    assert summary["unindexed_missing_count"] == 19


def test_observed_entries_are_never_hidden_by_a_lower_catalogue_total():
    summary = library_count_summary(
        {
            "provider": "mangadex",
            "status": "completed",
            "chapter_count": 6,
            "downloaded_count": 6,
            "numbered_chapter_count": 6,
            "numbered_volume_count": 0,
        },
        {
            "status": "ended",
            "chapter_count": 5,
            "provenance": {"chapter_count": "myanimelist"},
        },
    )

    assert summary["expected_count"] == 6
    assert summary["total_count"] == 6
    assert summary["missing_count"] == 0
    assert summary["count_conflict"] is False
    assert summary["segmentation_differs"] is True
    assert summary["catalogue_expected_count"] == 5


def test_ended_translation_uses_provider_final_and_retains_catalogue_scope():
    summary = library_count_summary(
        {
            "provider": "mangadex",
            "status": "completed",
            "last_chapter": "65",
            "chapter_count": 65,
            "downloaded_count": 65,
            "logical_chapter_count": 65,
            "logical_downloaded_count": 65,
            "special_chapter_count": 0,
            "special_downloaded_count": 0,
            "numbered_chapter_count": 65,
            "numbered_volume_count": 0,
        },
        {
            "status": "ended",
            "chapter_count": 60,
            "provenance": {"chapter_count": "myanimelist"},
        },
    )

    assert summary["downloaded_count"] == 65
    assert summary["expected_count"] == 65
    assert summary["missing_count"] == 0
    assert summary["catalogue_expected_count"] == 60
    assert summary["segmentation_differs"] is True


def test_local_volume_edition_can_have_more_splits_than_original_work():
    summary = library_count_summary(
        {
            "provider": "local",
            "status": "completed",
            "chapter_count": 3,
            "downloaded_count": 3,
            "numbered_chapter_count": 0,
            "numbered_volume_count": 3,
        },
        {
            "status": "ended",
            "volume_count": 2,
            "provenance": {"volume_count": "myanimelist"},
            "classification": {"kind": "manga"},
        },
    )

    assert summary["unit"] == "volume"
    assert summary["expected_count"] == 3
    assert summary["missing_count"] == 0
    assert summary["catalogue_expected_count"] == 2
    assert summary["segmentation_differs"] is True


def test_manual_publication_status_is_reversible_and_wins_only_in_summary():
    manga = {"status": "unknown", "status_override": "ended"}
    metadata = {"status": "ongoing"}

    manual = publication_summary(manga, metadata)
    automatic = publication_summary({**manga, "status_override": None}, metadata)

    assert manual == {
        "status": "ended",
        "source": "manual",
        "overridden": True,
        "raw_status": "ended",
        "paused": False,
        "pause_sources": [],
    }
    assert automatic == {
        "status": "continuing",
        "source": "metadata",
        "overridden": False,
        "raw_status": "ongoing",
        "paused": False,
        "pause_sources": [],
    }


def test_hiatus_is_a_pause_not_an_end():
    """A paused work may resume: the official platform stays its reference and
    monitoring keeps watching. The raw status is kept for display."""

    summary = publication_summary({"status": "hiatus"})

    assert summary == {
        "status": "continuing",
        "source": "provider",
        "overridden": False,
        "raw_status": "hiatus",
        "paused": True,
        "pause_sources": ["provider"],
    }
    assert publication_summary({"status": "ongoing"})["paused"] is False


def test_the_official_platform_pause_wins_over_a_catalogue_that_says_ongoing():
    """unORDINARY, 2026-09-05: Webtoons said "On hiatus", MangaBaka said
    ongoing, and the calendar kept projecting a Thursday that never came."""

    summary = publication_summary(
        {"status": "ongoing", "official_status": "hiatus"}, {"status": "ongoing"}
    )
    assert summary["status"] == "continuing" and summary["paused"] is True
    # A finished work is finished whatever the platform's flag says.
    ended = publication_summary({"status": "completed", "official_status": "hiatus"})
    assert ended["status"] == "ended" and ended["paused"] is False
    # Without any catalogue status the platform's word is the status.
    only = publication_summary({"official_status": "hiatus"})
    assert only == {
        "status": "continuing",
        "source": "official",
        "overridden": False,
        "raw_status": "hiatus",
        "paused": True,
        "pause_sources": ["official"],
    }


def test_any_catalogue_that_tracks_pauses_can_declare_one():
    """MyAnimeList says NANA is on hiatus; if MangaBaka lagged behind and
    said ongoing, MAL alone is enough. AniList is never asked: it says
    releasing. Who reported the pause is kept for the badge."""

    summary = publication_summary(
        {
            "status": "ongoing",
            "publication_signals": {
                "mangabaka": "ongoing",
                "myanimelist": "hiatus",
                "anilist": "hiatus",
                "official": "ongoing",
            },
        },
        {"status": "ongoing"},
    )
    assert summary["paused"] is True
    assert summary["pause_sources"] == ["myanimelist", "anilist"] or summary[
        "pause_sources"
    ] == ["myanimelist"]
    resumed = publication_summary(
        {
            "status": "ongoing",
            "publication_signals": {"mangabaka": "ongoing", "myanimelist": "ongoing"},
        },
        {"status": "ongoing"},
    )
    assert resumed["paused"] is False and resumed["pause_sources"] == []


def test_hiatus_can_be_set_by_hand_and_stays_a_continuing_work():
    """The operator knows a work paused before the catalogue does. The manual
    value is shown as a pause; every decision still treats it as continuing."""

    summary = publication_summary({"status": "ongoing", "status_override": "hiatus"})

    assert summary["status"] == "continuing"
    assert summary["paused"] is True
    assert summary["overridden"] is True
    assert summary["raw_status"] == "hiatus"


def test_manual_edition_total_and_library_status_keep_real_file_counts_visible():
    manga = {
        "provider": "local",
        "status": "completed",
        "chapter_count": 1,
        "downloaded_count": 1,
        "numbered_chapter_count": 0,
        "numbered_volume_count": 1,
        "expected_count_override": 1,
        "expected_count_unit_override": "volume",
    }
    metadata = {
        "status": "ended",
        "volume_count": 2,
        "provenance": {"volume_count": "mangaupdates"},
        "classification": {"kind": "manga"},
    }

    edition = library_count_summary(manga, metadata)
    manually_current = decorate_series_summary(
        {
            **manga,
            "expected_count_override": None,
            "expected_count_unit_override": None,
            "library_status_override": "up_to_date",
        },
        {**metadata, "volume_count": 3},
    )["library_count"]

    assert edition["downloaded_count"] == 1
    assert edition["expected_count"] == 1
    assert edition["missing_count"] == 0
    assert edition["catalogue_expected_count"] == 2
    assert edition["expected_count_overridden"] is True
    assert edition["segmentation_differs"] is True
    assert edition["count_conflict"] is False

    assert manually_current["downloaded_count"] == 1
    assert manually_current["total_count"] == 3
    assert manually_current["raw_missing_count"] == 2
    assert manually_current["missing_count"] == 0
    assert manually_current["ignored_missing_count"] == 2
    assert manually_current["library_status_overridden"] is True
