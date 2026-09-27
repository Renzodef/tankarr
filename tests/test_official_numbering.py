from __future__ import annotations

from tankarr.official_numbering import (
    MIN_OFFICIAL_ANCHORS,
    official_chapter_frontier,
    surplus_downloads,
)

WEBTOON = frozenset({"webtoons.com"})


def official(chapter: int | str, **extra):
    return {
        "id": f"webtoon-{chapter}",
        "chapter": str(chapter),
        "provider": "suwayomi",
        "source_name": "Webtoons.com (EN)",
        "source_url": f"https://www.webtoons.com/en/x/y/ep/viewer?episode_no={chapter}",
        "release_unit": "chapter",
        **extra,
    }


def mirror(chapter: int | str, **extra):
    return {
        "id": f"mirror-{chapter}",
        "chapter": str(chapter),
        "provider": "suwayomi",
        "source_name": "Weeb Central (EN)",
        "source_url": f"https://weebcentral.com/chapters/{chapter}",
        "release_unit": "chapter",
        **extra,
    }


def webtoon_run(last: int, first: int = 1):
    return [official(number) for number in range(first, last + 1)]


def test_lookism_is_placed_at_the_english_head_not_the_korean_one():
    # WEBTOON English is at 611; the scanlators number from NAVER and are at 622.
    releases = [
        *webtoon_run(611),
        *[mirror(number, downloaded=True) for number in range(605, 623)],
    ]

    verdict = official_chapter_frontier(releases, WEBTOON)

    assert verdict.enforceable is True
    assert verdict.label == "611"
    surplus = surplus_downloads(releases, verdict)
    assert [item["chapter"] for item in surplus] == [str(n) for n in range(612, 623)]


def test_a_prologue_numbered_zero_does_not_break_the_coverage_check():
    releases = [official(0), *webtoon_run(650)]

    verdict = official_chapter_frontier(releases, WEBTOON)

    assert verdict.enforceable is True
    assert verdict.label == "650"


def test_split_parts_past_the_head_are_surplus_too():
    # comickfan chops one episode into 311.1 … 311.9.
    releases = [
        *webtoon_run(308),
        *[mirror(f"311.{part}", downloaded=True) for part in range(1, 10)],
    ]

    surplus = surplus_downloads(releases, official_chapter_frontier(releases, WEBTOON))

    assert len(surplus) == 9


def test_a_thinly_mapped_official_source_may_not_condemn_anything():
    releases = [
        *webtoon_run(MIN_OFFICIAL_ANCHORS - 1),
        mirror(500, downloaded=True),
    ]

    verdict = official_chapter_frontier(releases, WEBTOON)

    assert verdict.enforceable is False
    assert "fewer than" in verdict.reason
    assert surplus_downloads(releases, verdict) == []


def test_holes_just_below_the_head_refuse_a_verdict():
    releases = [*webtoon_run(600), official(650), mirror(700, downloaded=True)]

    verdict = official_chapter_frontier(releases, WEBTOON)

    assert verdict.enforceable is False
    assert "holes just below its head" in verdict.reason


def test_a_source_exposing_only_recent_episodes_may_not_place_the_head():
    # Internally perfect - 250 to 300 with no holes - and still no evidence of
    # where the edition ends. Deleting 301-400 against it would be a guess.
    releases = [*webtoon_run(300, first=250), mirror(400, downloaded=True)]

    verdict = official_chapter_frontier(releases, WEBTOON)

    assert verdict.enforceable is False
    assert "starts at chapter 250" in verdict.reason
    assert surplus_downloads(releases, verdict) == []


def test_a_gappy_official_source_is_refused():
    releases = [
        # Starts at 1 and is dense at the head so both other guards pass;
        # what is left is a backlog full of holes.
        official(1),
        *[official(number) for number in range(1, 200) if number % 4 == 0],
        *webtoon_run(300, first=200),
        mirror(400, downloaded=True),
    ]

    verdict = official_chapter_frontier(releases, WEBTOON)

    assert verdict.enforceable is False
    assert "too partial" in verdict.reason
    assert surplus_downloads(releases, verdict) == []


def test_a_wildly_numbered_release_is_not_running_ahead():
    # 9999 is a mis-parse or a special: the frontier has nothing to say and
    # must not be the reason a readable file disappears.
    releases = [*webtoon_run(611), mirror(9999, downloaded=True)]

    surplus = surplus_downloads(releases, official_chapter_frontier(releases, WEBTOON))

    assert surplus == []


def test_the_official_source_is_never_surplus_against_itself():
    releases = [*webtoon_run(611)]
    for release in releases:
        release["downloaded"] = True

    surplus = surplus_downloads(releases, official_chapter_frontier(releases, WEBTOON))

    assert surplus == []


def test_a_work_without_an_official_platform_is_left_alone():
    releases = [mirror(number, downloaded=True) for number in range(1, 100)]

    verdict = official_chapter_frontier(releases, frozenset())

    assert verdict.enforceable is False
    assert surplus_downloads(releases, verdict) == []


def test_books_are_never_surplus_on_a_chapter_frontier():
    releases = [
        *webtoon_run(611),
        {
            "id": "book-20",
            "chapter": None,
            "volume": "20",
            "release_unit": "volume",
            "provider": "prowlarr",
            "downloaded": True,
        },
    ]

    surplus = surplus_downloads(releases, official_chapter_frontier(releases, WEBTOON))

    assert surplus == []


def test_only_downloaded_releases_are_reported_as_surplus():
    releases = [*webtoon_run(611), mirror(612), mirror(613, downloaded=True)]

    surplus = surplus_downloads(releases, official_chapter_frontier(releases, WEBTOON))

    assert [item["chapter"] for item in surplus] == ["613"]


def test_a_sample_of_the_opening_chapters_is_not_the_end_of_the_edition():
    """A publisher's free opening sample starts at chapter 1, has no holes and
    covers its range: every test for a complete edition passes. What tells
    them apart is the work continuing unbroken past it."""

    hosts = frozenset({"official.example"})

    def release(number: int, official: bool) -> dict:
        host = "official.example" if official else "scan.example"
        return {"chapter": str(number), "source_url": f"https://{host}/{number}"}

    sample = [release(n, True) for n in range(1, 51)]
    sample += [release(n, False) for n in range(1, 545)]
    verdict = official_chapter_frontier(sample, hosts)
    assert verdict.enforceable is False
    assert "window onto the work" in verdict.reason

    # the case the alignment exists for: the official edition is whole and the
    # scanlators simply run a little ahead of it
    edition = [release(n, True) for n in range(1, 301)]
    edition += [release(n, False) for n in range(1, 313)]
    ahead = official_chapter_frontier(edition, hosts)
    assert ahead.enforceable is True
