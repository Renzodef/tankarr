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


def test_official_season_zero_episode_with_repeated_title_keeps_its_own_chapter():
    official = [
        release(
            "season-one-finale",
            "Webtoons.com (EN)",
            79,
            title="[Season 1] Ep. 78 (ch. 78)",
            host="webtoons.com",
        ),
        release(
            "season-two-zero",
            "Webtoons.com (EN)",
            80,
            title="[Season 2] Ep. 0 (ch. 78)",
            host="webtoons.com",
        ),
        release(
            "season-two-one",
            "Webtoons.com (EN)",
            81,
            title="[Season 2] Ep. 1 (ch. 79)",
            host="webtoons.com",
        ),
    ]

    decisions = reconcile_numbering(official)

    assert decisions["season-one-finale"]["canonical_chapter"] == "78"
    assert decisions["season-two-zero"]["canonical_chapter"] == "78.5"
    assert decisions["season-two-zero"]["numbering_method"] == "title_season_opening"
    assert decisions["season-two-one"]["canonical_chapter"] == "79"


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

    decisions = reconcile_numbering([*official, *aligned_reference, *drifted])

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

    decision = reconcile_numbering([corrected])["corrected"]

    assert decision["source_chapter"] == "412"
    assert decision["canonical_chapter"] == "393"
    assert decision["numbering_method"] == "explicit_title"
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

    decision = reconcile_numbering([future])["official-future"]

    assert decision["source_chapter"] == "0"
    assert decision["canonical_chapter"] == "251"
    assert decision["numbering_status"] == "mapped"
    assert decision["numbering_method"] == "explicit_title"
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


def test_each_configured_source_can_extend_a_slower_publisher_feed():
    publisher = [release(f"publisher-{n}", "Publisher", n) for n in range(1, 6)]
    for source in ("Mirror", "Community", "Publisher"):
        newest = release("newest", source, 8)
        result = reconcile_numbering([*publisher, newest])["newest"]
        assert result["canonical_chapter"] == "8"
        assert result["numbering_status"] == "mapped"


def test_retired_official_pending_state_is_rebuilt_from_the_source():
    stale = release("newest", "Source", 630)
    stale.update(
        canonical_chapter=None,
        chapter=None,
        numbering_status="pending_evidence",
        numbering_method="awaiting_secondary_official_evidence",
        numbering_version=6,
    )
    result = reconcile_numbering([stale])["newest"]
    assert result["canonical_chapter"] == "630"
    assert result["numbering_status"] == "mapped"


def test_specials_do_not_become_chapters_when_official_gating_is_removed():
    newest = release("special", "Source", 8, title="Omake 8")
    result = reconcile_numbering([newest])["special"]
    assert result["canonical_chapter"] is None
    assert result["numbering_status"] == "unmapped"


def test_uniform_page_counts_cannot_prove_a_numbering_offset():
    reference = [
        release(f"owned-{n}", "Owned", n, title="Episode", pages=20)
        for n in range(1, 8)
    ]
    for item in reference:
        item["downloaded"] = True
    other = [
        release(f"new-{n}", "Other", n, title="Episode", pages=20) for n in range(8, 15)
    ]
    decisions = reconcile_numbering([*reference, *other])
    assert decisions["new-14"]["canonical_chapter"] == "14"


def test_owned_source_is_the_content_reference_without_publisher_roles():
    owned = [
        release(f"owned-{n}", "Community", n, title="Episode", pages=20 + n)
        for n in range(1, 13)
    ]
    for item in owned:
        item["downloaded"] = True
    other = [
        release(f"other-{n + 10}", "Publisher", n + 10, title="Episode", pages=20 + n)
        for n in range(1, 13)
    ]
    result = reconcile_numbering([*owned, *other])
    assert result["other-22"]["canonical_chapter"] == "12"
    assert result["other-22"]["numbering_method"] == "content_sequence_alignment"
