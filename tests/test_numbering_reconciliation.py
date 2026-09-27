from __future__ import annotations

from tankarr.numbering_reconciliation import reconcile_numbering


def release(
    release_id: str,
    source: str,
    number: int,
    *,
    title: str | None = None,
    pages: int = 20,
    host: str = "mirror.example",
    method: str = "source_identity",
    canonical: int | None = None,
) -> dict:
    return {
        "id": release_id,
        "provider": "suwayomi",
        "source_key": f"suwayomi:{source}",
        "source_name": source,
        "source_url": f"https://{host}/{release_id}",
        "release_unit": "chapter",
        "source_chapter": str(number),
        "canonical_chapter": str(canonical if canonical is not None else number),
        "chapter": str(canonical if canonical is not None else number),
        "numbering_status": "mapped",
        "numbering_method": method,
        "numbering_confidence": 0.6,
        "numbering_evidence": {},
        "title": title or f"Chapter {number}",
        "pages": pages,
    }


def test_release_ahead_of_the_primary_official_head_waits_for_evidence():
    official = [
        release(f"official-{number}", "Webtoons.com (EN)", number, host="webtoons.com")
        for number in range(1, 6)
    ]
    mirror = [release("mirror-6", "MangaK (EN)", 6)]

    decisions = reconcile_numbering(
        [*official, *mirror], official_hosts=frozenset({"webtoons.com"})
    )

    assert decisions["mirror-6"]["canonical_chapter"] is None
    assert decisions["mirror-6"]["numbering_status"] == "pending_evidence"
    assert decisions["mirror-6"]["numbering_method"] == (
        "awaiting_secondary_official_evidence"
    )


def test_finished_catalogue_maps_matching_explicit_title_inside_its_boundary():
    pending = release("mirror-45", "Mirror", 45, title="Chapter 45")
    pending.update(
        canonical_chapter=None,
        chapter=None,
        numbering_status="pending_evidence",
        numbering_method="awaiting_secondary_official_evidence",
    )

    official = [
        release(f"official-{number}", "Webtoons.com (EN)", number, host="webtoons.com")
        for number in range(1, 6)
    ]
    decision = reconcile_numbering(
        [*official, pending],
        official_hosts=frozenset({"webtoons.com"}),
        canonical_end=55,
    )["mirror-45"]

    assert decision["canonical_chapter"] == "45"
    assert decision["numbering_status"] == "mapped"
    assert decision["numbering_method"] == "catalogue_bounded_title"


def test_finished_catalogue_does_not_map_title_past_its_boundary():
    pending = release("mirror-56", "Mirror", 56, title="Chapter 56")
    pending.update(
        canonical_chapter=None,
        chapter=None,
        numbering_status="pending_evidence",
        numbering_method="awaiting_secondary_official_evidence",
    )

    decision = reconcile_numbering([pending], canonical_end=55)["mirror-56"]

    assert decision["canonical_chapter"] is None
    assert decision["numbering_status"] == "pending_evidence"


def test_secondary_official_edition_can_validate_an_early_release():
    official = [
        release(f"official-{number}", "Webtoons.com (EN)", number, host="webtoons.com")
        for number in range(1, 6)
    ]
    secondary = [
        release(f"secondary-{number}", "NAVER", number, host="comic.naver.com")
        for number in range(1, 9)
    ]
    mirror = [
        release(f"mirror-{number}", "Weeb Central (EN)", number)
        for number in range(1, 9)
    ]

    decisions = reconcile_numbering(
        [*official, *secondary, *mirror],
        source_roles={
            "webtoons.com": "primary_official",
            "comic.naver.com": "secondary_official",
        },
    )

    assert decisions["mirror-6"]["canonical_chapter"] == "6"
    assert decisions["mirror-8"]["canonical_chapter"] == "8"
    assert decisions["mirror-8"]["numbering_method"] == ("secondary_official_bridge")


def test_secondary_official_metadata_can_validate_an_early_release():
    official = [
        release(f"official-{number}", "Webtoons.com (EN)", number, host="webtoons.com")
        for number in range(1, 6)
    ]
    mirror = [
        release(f"mirror-{number}", "MangaDex (EN)", number) for number in range(1, 9)
    ]
    evidence = [
        {"host": "comic.naver.com", "edition_chapter": str(number)}
        for number in range(1, 9)
    ]

    decisions = reconcile_numbering(
        [*official, *mirror],
        source_roles={
            "webtoons.com": "primary_official",
            "comic.naver.com": "secondary_official",
        },
        secondary_evidence=evidence,
    )

    assert decisions["mirror-8"]["canonical_chapter"] == "8"
    assert decisions["mirror-8"]["numbering_method"] == "secondary_official_bridge"


def test_verified_mirror_identity_can_continue_a_short_primary_prefix():
    official = [
        release(f"official-{number}", "Webtoons.com (EN)", number, host="webtoons.com")
        for number in range(1, 10)
    ]
    mirror = [
        release(f"mirror-{number}", "Weeb Central (EN)", number)
        for number in range(1, 101)
    ]

    decisions = reconcile_numbering(
        [*official, *mirror], official_hosts=frozenset({"webtoons.com"})
    )

    assert decisions["mirror-100"]["canonical_chapter"] == "100"
    assert decisions["mirror-100"]["numbering_status"] == "mapped"
    assert decisions["mirror-100"]["numbering_method"] == (
        "verified_identity_continuation"
    )


def test_special_release_cannot_fill_a_continuation_gap():
    official = [
        release(f"official-{number}", "Webtoons.com (EN)", number, host="webtoons.com")
        for number in range(1, 6)
    ]
    mirror = [
        release(f"mirror-{number}", "Weeb Central (EN)", number)
        for number in range(1, 6)
    ] + [
        release(
            "mirror-6-special",
            "Weeb Central (EN)",
            6,
            title="Season 2 announcement",
        ),
        release("mirror-7", "Weeb Central (EN)", 7, title="Chapter 7"),
    ]

    decisions = reconcile_numbering(
        [*official, *mirror],
        official_hosts=frozenset({"webtoons.com"}),
    )

    assert decisions["mirror-6-special"]["numbering_method"] == "special_title"
    assert decisions["mirror-7"]["canonical_chapter"] is None
    assert decisions["mirror-7"]["numbering_method"] == (
        "awaiting_secondary_official_evidence"
    )


def test_special_releases_cannot_supply_identity_anchors():
    official = [
        release(f"official-{number}", "Webtoons.com (EN)", number, host="webtoons.com")
        for number in range(1, 6)
    ]
    mirror = [
        release("mirror-1", "MangaFire (EN)", 1),
        release(
            "mirror-special-2",
            "MangaFire (EN)",
            2,
            title="Season 2 announcement",
        ),
        *[
            release(f"mirror-{number}", "MangaFire (EN)", number)
            for number in range(3, 7)
        ],
    ]

    decisions = reconcile_numbering(
        [*official, *mirror],
        official_hosts=frozenset({"webtoons.com"}),
    )

    assert decisions["mirror-6"]["canonical_chapter"] is None
    assert decisions["mirror-6"]["numbering_evidence"]["identity_anchors"] == 4
    assert decisions["mirror-6"]["numbering_evidence"]["drift_detected"] is True


def test_verified_identity_does_not_jump_a_gap_after_the_official_head():
    official = [
        release(f"official-{number}", "Webtoons.com (EN)", number, host="webtoons.com")
        for number in range(1, 6)
    ]
    mirror = [
        release(f"mirror-{number}", "Weeb Central (EN)", number)
        for number in [1, 2, 3, 4, 5, 7, 8]
    ]

    decisions = reconcile_numbering(
        [*official, *mirror], official_hosts=frozenset({"webtoons.com"})
    )

    assert decisions["mirror-7"]["canonical_chapter"] is None
    assert decisions["mirror-8"]["canonical_chapter"] is None


def test_known_numbering_drift_prevents_identity_continuation():
    official = [
        release(f"official-{number}", "Webtoons.com (EN)", number, host="webtoons.com")
        for number in range(1, 6)
    ]
    drifted = [
        release(
            f"mirror-{number}",
            "MangaK (EN)",
            number,
            title=f"Chapter {number + 10}",
        )
        for number in range(1, 6)
    ] + [release("mirror-6", "MangaK (EN)", 6, title="Episode")]

    decisions = reconcile_numbering(
        [*official, *drifted], official_hosts=frozenset({"webtoons.com"})
    )

    assert decisions["mirror-5"]["numbering_method"] == "title_sequence"
    assert decisions["mirror-6"]["canonical_chapter"] is None
    assert decisions["mirror-6"]["numbering_evidence"]["drift_detected"] is True


def test_secondary_metadata_bridge_rejects_a_non_contiguous_outlier():
    official = [
        release(f"official-{number}", "Webtoons.com (EN)", number, host="webtoons.com")
        for number in range(1, 6)
    ]
    mirror = [
        release(f"mirror-{number}", "MangaDex (EN)", number)
        for number in [1, 2, 3, 6, 999]
    ]
    evidence = [
        {"host": "comic.naver.com", "edition_chapter": str(number)}
        for number in [1, 2, 3, 6, 999]
    ]

    decisions = reconcile_numbering(
        [*official, *mirror],
        source_roles={
            "webtoons.com": "primary_official",
            "comic.naver.com": "secondary_official",
        },
        secondary_evidence=evidence,
    )

    assert decisions["mirror-6"]["canonical_chapter"] == "6"
    assert decisions["mirror-6"]["numbering_method"] == "secondary_official_bridge"
    assert decisions["mirror-999"]["canonical_chapter"] is None


def test_secondary_metadata_special_cannot_fill_a_bridge_gap():
    official = [
        release(f"official-{number}", "Webtoons.com (EN)", number, host="webtoons.com")
        for number in range(1, 6)
    ]
    mirror = [
        release(f"mirror-{number}", "MangaDex (EN)", number)
        for number in [1, 2, 3, 4, 5, 7]
    ]
    evidence = [
        {
            "host": "comic.naver.com",
            "edition_chapter": str(number),
            "title": "Season 2 announcement" if number == 6 else f"Chapter {number}",
        }
        for number in range(1, 8)
    ]

    decisions = reconcile_numbering(
        [*official, *mirror],
        source_roles={
            "webtoons.com": "primary_official",
            "comic.naver.com": "secondary_official",
        },
        secondary_evidence=evidence,
    )

    assert decisions["mirror-7"]["canonical_chapter"] is None
    assert decisions["mirror-7"]["numbering_method"] == (
        "awaiting_secondary_official_evidence"
    )


def test_page_sequence_maps_only_the_supported_offset_segment():
    page_counts = list(range(30, 44))
    official = [
        release(
            f"official-{canonical}",
            "Webtoons.com (EN)",
            canonical,
            pages=-1,
            host="webtoons.com",
        )
        for canonical in range(380, 394)
    ]
    aligned_reference = [
        release(
            f"reference-{canonical}",
            "MangaPill (EN)",
            canonical,
            pages=pages,
        )
        for canonical, pages in zip(range(380, 394), page_counts)
    ]
    drifted = [
        release(
            f"drifted-{source}",
            "MangaK (EN)",
            source,
            title="Episode",
            pages=pages,
        )
        for source, pages in zip(range(394, 408), page_counts)
    ]

    decisions = reconcile_numbering(
        [*official, *aligned_reference, *drifted],
        official_hosts=frozenset({"webtoons.com"}),
    )

    assert decisions["drifted-394"]["canonical_chapter"] == "380"
    assert decisions["drifted-407"]["canonical_chapter"] == "393"
    assert decisions["drifted-407"]["numbering_method"] == (
        "content_sequence_alignment"
    )


def test_legacy_manual_mapping_is_reconciled_from_primary_official_title():
    corrected = release(
        "corrected",
        "Webtoons.com (EN)",
        412,
        title="Episode 393",
        host="webtoons.com",
        method="manual",
        canonical=393,
    )

    decision = reconcile_numbering(
        [corrected], official_hosts=frozenset({"webtoons.com"})
    )["corrected"]

    assert decision["source_chapter"] == "412"
    assert decision["canonical_chapter"] == "393"
    assert decision["numbering_method"] == "official_title"
    assert decision["edition_chapter"] == "393"
    assert decision["primary_chapter"] == "393"


def test_explicit_official_title_survives_a_reset_provider_index():
    future = release(
        "official-future",
        "Tapas (EN)",
        0,
        title="\U0001f512 251. New Message",
        host="tapas.io",
    )

    decision = reconcile_numbering([future], official_hosts=frozenset({"tapas.io"}))[
        "official-future"
    ]

    assert decision["source_chapter"] == "0"
    assert decision["canonical_chapter"] == "251"
    assert decision["numbering_status"] == "mapped"
    assert decision["numbering_method"] == "official_title"
    assert decision["numbering_evidence"]["title_chapter"] == "251"


def test_valid_title_sequence_is_not_rejected_by_unrelated_outliers():
    outliers = [
        release("outlier-a", "MangaK (EN)", 175, title="Chapter 175-16"),
        release("outlier-b", "MangaK (EN)", 188, title="Chapter 188-1"),
    ]
    sequence = [
        release(
            f"sequence-{source}",
            "MangaK (EN)",
            source,
            title=f"Chapter {source} - {canonical}",
        )
        for source, canonical in zip(range(266, 281), range(223, 238))
    ]

    decisions = reconcile_numbering([*outliers, *sequence])

    assert decisions["sequence-266"]["canonical_chapter"] == "223"
    assert decisions["sequence-280"]["canonical_chapter"] == "237"
    assert decisions["sequence-280"]["numbering_method"] == "title_sequence"
    assert decisions["outlier-a"]["numbering_method"] != "title_sequence"


def test_explicit_titles_repair_a_stale_rank_based_mapping():
    stale = [
        release(
            f"chapter-{number}",
            "MangaK (EN)",
            number,
            title=f"Vol.1 Chapter {number}: Title",
            method="rank_after_specials",
            canonical=number + 5,
        )
        for number in range(1, 7)
    ]
    for item in stale:
        item["numbering_status"] = "provisional"

    decisions = reconcile_numbering(stale)

    assert [
        decisions[f"chapter-{number}"]["canonical_chapter"] for number in range(1, 7)
    ] == [str(number) for number in range(1, 7)]
    assert all(
        decisions[f"chapter-{number}"]["numbering_status"] == "mapped"
        for number in range(1, 7)
    )


def test_numbered_prologues_keep_provider_identity_without_colliding():
    rows = [release(str(n), "Source", n) for n in range(1, 9)]
    rows += [release(f"p{n}", "Source", n, title=f"Prologue {n}") for n in range(1, 4)]
    decisions = reconcile_numbering(rows)
    for n in range(1, 4):
        assert decisions[f"p{n}"]["source_chapter"] == str(n)
        assert decisions[f"p{n}"]["canonical_chapter"] is None
        assert decisions[str(n)]["canonical_chapter"] == str(n)
    ordinary = release("named", "Source", 9, title="Chapter 9: Prologue 2")
    assert reconcile_numbering([ordinary])["named"]["canonical_chapter"] == "9"


def test_a_chapter_zero_tagged_with_a_later_book_is_not_an_expected_chapter():
    """Nana: "Vol.9 Chapter 0 : Naoki's Story" is an extra printed in book
    nine, not a prologue the shelf should count or hunt for."""

    side = release("side-0", "mirror", 0, title="Vol.9 Chapter 0 : Naoki's Story")
    prologue = release("pro-0", "mirror", 0, title="Vol.1 Chapter 0 : Prologue")
    decisions = reconcile_numbering(
        [side, prologue] + [release(f"c{n}", "mirror", n) for n in range(1, 6)]
    )
    assert decisions["side-0"]["numbering_status"] == "unmapped"
    assert decisions["side-0"]["numbering_method"] == "special_title"
    assert decisions["pro-0"]["numbering_status"] == "mapped"
