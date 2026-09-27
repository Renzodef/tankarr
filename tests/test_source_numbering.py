from __future__ import annotations

import copy
from decimal import Decimal

import pytest

from tankarr.source_numbering import (
    is_locked_title,
    is_special_title,
    renumber_by_rank,
    renumber_from_titles,
    title_number,
)


def episodes(offset: int, count: int, *, locked_from: int | None = None) -> list[dict]:
    """A webtoon source whose episode index runs ahead of the chapter number."""

    result = []
    for index in range(1, count + 1):
        chapter = index + offset
        lock = "\U0001f512 " if locked_from is not None and index >= locked_from else ""
        result.append(
            {
                "chapter": str(chapter),
                "title": f"{lock}{index}. Episode {index}",
            }
        )
    return result


@pytest.mark.parametrize("reverse", [False, True])
def test_restarted_arcs_are_not_conflated_with_ordinary_chapters(reverse):
    rows = episodes(0, 7) + episodes(0, 11) + episodes(0, 20)
    if reverse:
        rows.reverse()
    source_numbers = [r["chapter"] for r in rows]
    result = renumber_from_titles(rows)
    assert [r["source_chapter"] for r in result] == source_numbers
    assert all(r["chapter"] is None for r in result)
    assert all(r["numbering_status"] == "ambiguous" for r in result)
    assert all(r["numbering_method"] == "restarted_sequences" for r in result)


@pytest.mark.parametrize("kind", ["groups", "volumes", "interleaved", "duplicate"])
def test_repeated_release_numbers_do_not_alone_establish_restarted_arcs(kind):
    first, second = episodes(0, 8), episodes(0, 8)
    if kind == "groups":
        for row in first:
            row["groups"] = ["Group A"]
        for row in second:
            row["groups"] = ["Group B"]
        rows = first + second
    elif kind == "volumes":
        for row in first:
            row["volume"] = "1"
        for row in second:
            row["volume"] = "2"
        rows = first + second
    elif kind == "interleaved":
        rows = [item for pair in zip(first, second) for item in pair]
    else:
        rows = first + [copy.deepcopy(first[-1])]
    assert all(r["chapter"] is not None for r in renumber_from_titles(rows))


def test_titles_replace_an_episode_index_that_counts_specials():
    """Tapas numbers episodes; the title carries the real chapter number."""

    renumbered = renumber_from_titles(episodes(offset=43, count=10))

    assert [item["chapter"] for item in renumbered] == [str(n) for n in range(1, 11)]
    assert [item["source_chapter"] for item in renumbered] == [
        str(n) for n in range(44, 54)
    ]


def test_numbering_is_left_alone_without_agreement():
    """Only a source that consistently titles its chapters is overridden."""

    partial = episodes(offset=43, count=10)
    for item in partial[3:]:
        item["title"] = "Untitled episode"
    assert [item["chapter"] for item in renumber_from_titles(partial)] == [
        str(n) for n in range(44, 54)
    ]

    duplicated = episodes(offset=43, count=6)
    duplicated[2]["title"] = "1. Episode 1"
    assert renumber_from_titles(duplicated)[2]["chapter"] == "46"

    aligned = episodes(offset=0, count=6)
    assert [item["chapter"] for item in renumber_from_titles(aligned)] == [
        str(n) for n in range(1, 7)
    ]


def test_locked_episodes_are_recognised():
    assert is_locked_title("\U0001f512 250. Cylrit's Sword") is True
    assert is_locked_title("250. Free chapter") is False
    assert title_number("\U0001f512 250. Cylrit's Sword") == 250
    assert title_number("Vol.3 Ch.20") == 20


def test_specials_and_a_teaser_do_not_break_the_run():
    """A platform posts announcements between episodes and the odd teaser
    out of order. The numbered run is the chapter numbering; everything else
    keeps its title and no chapter number, so it cannot collide."""

    chapters = episodes(offset=43, count=20)
    chapters.insert(5, {"chapter": "48.5", "title": "Fan Art Contest Winners!"})
    chapters.insert(0, {"chapter": "0", "title": "\U0001f512 21. New Message"})

    renumbered = renumber_from_titles(chapters)
    numbered = [item["chapter"] for item in renumbered if item["chapter"]]
    specials = [item for item in renumbered if not item["chapter"]]

    assert numbered == [str(n) for n in range(1, 21)]
    assert [item["title"] for item in specials] == [
        "\U0001f512 21. New Message",
        "Fan Art Contest Winners!",
    ]


def test_webtoon_titles_carry_the_chapter_at_the_end():
    assert title_number("[Season 3] Ep. 235 (Season 3 Finale) (ch. 650)") == 650
    assert title_number("Side Story - Rei (1) (ch. 155.01)") == Decimal("155.01")
    assert title_number("Ep. 611: Gapryong Kim (2) (ch. 611) ♫") == 611


def test_unnumbered_episodes_are_ranked_without_the_announcements():
    """Tapas titles are names, not numbers, and the episode index counts
    recaps and notices; the real episodes are numbered in publication order
    and the notices become extras."""

    chapters = [
        {"chapter": "1", "title": "Prologue"},
        {"chapter": "2", "title": "The Purpose of Life"},
        {"chapter": "3", "title": "Season 1 Recap"},
        {"chapter": "4", "title": "Opportunity"},
        {"chapter": "5", "title": "Editor's Note - End Of Season 1"},
        {"chapter": "6", "title": "Return"},
    ]

    renumbered = renumber_by_rank(chapters)

    assert [item["chapter"] for item in renumbered] == ["1", "2", None, "3", None, "4"]


def test_aggregator_titles_name_the_chapter_and_drop_the_point_zero_duplicate():
    """MangaK keeps a running index that drifts from the chapter numbers in its
    own titles ("Chapter 45" filed as 46 after a "Chapter 45.0" duplicate)."""

    assert title_number("Chapter 45") == 45
    assert title_number("Chapter-151: Episode 148") == 151
    assert title_number("Chapter 45.0") is None
    assert title_number("Chapter 44.3") == Decimal("44.3")

    chapters = [
        {"chapter": "44", "title": "Chapter 44"},
        {"chapter": "45", "title": "Chapter 45.0"},
        {"chapter": "46", "title": "Chapter 45"},
        {"chapter": "47", "title": "Chapter 46"},
        {"chapter": "48", "title": "Chapter 47"},
        {"chapter": "49", "title": "Chapter 48"},
    ]
    renumbered = renumber_from_titles(chapters)

    assert [item["chapter"] for item in renumbered] == [
        "44",
        None,
        "45",
        "46",
        "47",
        "48",
    ]


def test_dual_number_title_uses_the_work_number_not_the_mirror_index():
    assert title_number("Chapter 280 - 237") == 237

    chapters = [
        {"chapter": str(source), "title": f"Chapter {source} - {canonical}"}
        for source, canonical in zip(range(280, 286), range(237, 243))
    ]
    renumbered = renumber_from_titles(chapters)

    assert [item["source_chapter"] for item in renumbered] == [
        str(number) for number in range(280, 286)
    ]
    assert [item["chapter"] for item in renumbered] == [
        str(number) for number in range(237, 243)
    ]


def test_volume_prefixed_chapter_titles_do_not_trigger_rank_renumbering():
    chapters = [
        {
            "chapter": str(number),
            "title": f"Vol.{(number - 1) // 10 + 1} Chapter {number}: Title",
        }
        for number in range(1, 11)
    ]
    chapters.insert(
        2,
        {"chapter": "1", "title": "Vol.54 Kingdom Guidebook Omake 1"},
    )

    renumbered = renumber_from_titles(chapters)

    assert [
        item["chapter"] for item in renumbered if item["numbering_status"] == "mapped"
    ] == [str(number) for number in range(1, 11)]
    assert renumbered[2]["chapter"] is None
    assert renumbered[2]["numbering_method"] == "special_title"
    assert is_special_title("Vol.19 Chapter 202: Announcement") is False
    assert is_special_title("Season 2 announcement") is True


def test_a_future_dated_episode_is_not_yet_released_even_without_a_lock():
    from datetime import UTC, datetime

    from tankarr.source_numbering import is_not_yet_released

    now = datetime(2026, 9, 5, 20, 0, tzinfo=UTC)
    assert is_not_yet_released(
        {"title": "252. Turning Point", "publish_at": "2026-09-10T16:00:00+00:00"},
        now=now,
    )
    assert is_not_yet_released(
        {"title": "🔒 252. Turning Point", "publish_at": None}, now=now
    )
    assert not is_not_yet_released(
        {"title": "251. New Message", "publish_at": "2026-09-03T16:00:00+00:00"},
        now=now,
    )
    assert not is_not_yet_released(
        {"title": "Chapter 12", "publish_at": "garbage"}, now=now
    )


def test_a_one_or_two_page_listing_is_a_placeholder_not_a_chapter():
    from tankarr.source_numbering import is_stub, is_unpublished

    stub = {"chapter": "204", "pages": 1, "publish_at": "2016-01-01T00:00:00+00:00"}
    real = {"chapter": "203", "pages": 36, "publish_at": "2016-01-01T00:00:00+00:00"}
    unknown = {"chapter": "199", "pages": -1, "publish_at": "2016-01-01T00:00:00+00:00"}
    kept = {"chapter": "10", "pages": 2, "downloaded": 1}

    assert is_stub(stub) and is_unpublished(stub)
    assert not is_stub(real) and not is_unpublished(real)
    assert not is_stub(unknown) and not is_unpublished(unknown)
    assert not is_stub(kept)


def test_title_volume_reads_the_source_book_tag():
    from tankarr.source_numbering import title_volume

    assert title_volume("Vol.21 Chapter 81") == 21
    assert title_volume("Volume 3") == 3
    assert title_volume("Vol. 9 Chapter 0 : Naoki's Story") == 9
    assert title_volume("Chapter 84") is None
    assert title_volume("") is None


def test_a_chapter_zero_filed_under_a_later_book_is_a_side_story():
    from tankarr.source_numbering import is_side_story

    assert is_side_story("Vol.9 Chapter 0 : Naoki's Story", "0")
    assert not is_side_story("Vol.1 Chapter 0 : Prologue", "0")
    assert not is_side_story("Chapter 0", "0")
    assert not is_side_story("Vol.9 Chapter 1", "1")
