from tankarr.completeness import assess_chapter_completeness


def manga(*, status: str = "completed", last_chapter: str = "5") -> dict:
    return {
        "id": "manga-1",
        "provider": "mangadex",
        "title": "Example Story",
        "status": status,
        "last_chapter": last_chapter,
    }


def chapters(*numbers: object) -> list[dict]:
    return [
        {"id": f"chapter-{index}", "chapter": number}
        for index, number in enumerate(numbers, start=1)
    ]


def matched_source(
    *,
    name: str = "myanimelist",
    label: str = "MyAnimeList",
    chapter_count: int | None = 5,
    status: str = "ended",
) -> dict:
    return {
        "name": name,
        "label": label,
        "state": "matched",
        "confidence": 0.99,
        "reason": "primary exact, creator",
        "record": {
            "source": name,
            "chapter_count": chapter_count,
            "status": status,
        },
    }


def assess(
    values: list[dict],
    *,
    source_checks: list[dict] | None = None,
    available_count: int | None = None,
    remote_manga: dict | None = None,
) -> dict:
    return assess_chapter_completeness(
        remote_manga or manga(),
        values,
        language="en",
        available_chapter_count=(
            len(values) if available_count is None else available_count
        ),
        source_checks=([matched_source()] if source_checks is None else source_checks),
    )


def test_complete_requires_metadata_total_and_full_numbered_coverage():
    result = assess(chapters(1, 2, 3, 4, 5))

    assert result["status"] == "complete"
    assert result["expected_chapter_count"] == 5
    assert result["covered_chapter_count"] == 5
    assert result["missing_chapter_count"] == 0
    assert "MyAnimeList confirms that the work has ended" in result["message"]


def test_same_latest_chapter_does_not_hide_numbering_gaps():
    result = assess(chapters(1, 3, 4, 5), available_count=4)

    assert result["status"] == "incomplete"
    assert result["covered_chapter_count"] == 4
    assert result["missing_chapter_count"] == 1
    assert result["missing_chapter_ranges"] == ["2"]


def test_split_releases_collectively_cover_one_canonical_chapter():
    result = assess(chapters(1, 2, "3.1", "3.2", 4, 5), available_count=6)

    assert result["status"] == "complete"
    assert result["covered_chapter_count"] == 5
    assert result["missing_chapter_count"] == 0


def test_single_decimal_special_does_not_replace_a_missing_main_chapter():
    result = assess(chapters(1, 2, "3.5", 4, 5), available_count=5)

    assert result["status"] == "incomplete"
    assert result["missing_chapter_ranges"] == ["3"]


def test_metadata_total_counts_numbered_specials_instead_of_assuming_last_number():
    result = assess(
        chapters(1, 2, 3, 4, "4.5"),
        remote_manga=manga(status="unknown", last_chapter=""),
    )

    assert result["status"] == "complete"
    assert result["expected_chapter_count"] == 5
    assert result["covered_chapter_count"] == 5
    assert result["missing_chapter_count"] == 0
    assert result["missing_chapter_ranges"] == []


def test_metadata_cardinality_deficit_does_not_invent_missing_number_ranges():
    result = assess(
        chapters(1, 2, "2.5", 3),
        remote_manga=manga(status="unknown", last_chapter=""),
    )

    assert result["status"] == "incomplete"
    assert result["covered_chapter_count"] == 4
    assert result["missing_chapter_count"] == 1
    assert result["missing_chapter_ranges"] == []


def test_provider_final_chapter_alone_remains_unconfirmed():
    result = assess(chapters(1, 2, 3, 4, 5), source_checks=[])

    assert result["status"] == "unknown"
    assert result["expected_chapter_count"] is None
    assert result["provider_claimed_chapter_count"] == 5
    assert "not independently confirmed" in result["message"]


def test_conflicting_catalogue_totals_are_visible_and_fail_closed():
    result = assess(
        chapters(1, 2, 3, 4, 5),
        remote_manga=manga(status="unknown", last_chapter=""),
        source_checks=[
            matched_source(),
            matched_source(
                name="mangaupdates",
                label="MangaUpdates",
                chapter_count=6,
            ),
        ],
    )

    assert result["status"] == "conflict"
    assert "MyAnimeList: 5" in result["message"]
    assert "MangaUpdates: 6" in result["message"]


def test_provider_numbering_wins_when_terminal_catalogue_segmentation_differs():
    result = assess(
        chapters(*range(1, 66)),
        available_count=65,
        remote_manga=manga(status="completed", last_chapter="65"),
        source_checks=[
            matched_source(
                name="mangaupdates",
                label="MangaUpdates",
                chapter_count=None,
            ),
            matched_source(chapter_count=60),
        ],
    )

    assert result["status"] == "complete"
    assert result["expected_chapter_count"] == 65
    assert result["verification_basis"] == "provider_final"
    assert result["numbering_disagreement"] is True
    assert "Catalogue segmentation differs" in result["message"]


def test_equal_raw_count_with_unnumbered_releases_is_not_declared_complete():
    result = assess(
        chapters("Special A", "Special B", "Special C", "Special D", "Special E")
    )

    assert result["status"] == "unknown"
    assert "too many are unnumbered" in result["message"]


def test_zero_based_numbering_can_still_prove_complete_coverage():
    result = assess(chapters(0, 1, 2, 3, 4))

    assert result["status"] == "complete"
    assert result["missing_chapter_ranges"] == []
