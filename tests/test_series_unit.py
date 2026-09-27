from __future__ import annotations

import pytest

from tankarr.series_unit import (
    coverage_for_units,
    normalize_chapter_releases,
    resolve_series_unit,
    select_releases,
    unit_coverage,
)


def test_uncorroborated_scan_frontier_cannot_complete_a_multivolume_work():
    metadata = {
        "status": "ended",
        "volume_count": 5,
        "chapter_count": 1,
        "latest_release_chapter": 1,
        "count_confidence": {"chapter": "lone", "volume": "agreed"},
    }
    releases = [ch("1", downloaded=True)]
    coverage = coverage_for_units({}, metadata, releases)
    assert coverage["chapters"]["expected"] is None
    assert coverage["chapters"]["complete"] is False
    assert resolve_series_unit({}, metadata, releases)["unit"] == "volumes"
    assert (
        resolve_series_unit({"series_unit_override": "chapters"}, metadata, releases)[
            "unit"
        ]
        == "chapters"
    )


def ch(chapter, provider="a", **extra):
    return {
        "id": f"{provider}-{chapter}",
        "chapter": chapter,
        "volume": None,
        "provider": provider,
        "release_unit": "chapter",
        **extra,
    }


def vol(volume, provider="books"):
    return {
        "id": f"{provider}-v{volume}",
        "chapter": None,
        "volume": volume,
        "provider": provider,
        "release_unit": "volume",
    }


def test_unit_resolution_matches_the_library_cases():
    # Bakune Young: three books from the local source only.
    assert (
        resolve_series_unit(
            {}, {"volume_count": 3, "chapter_count": 30}, [vol("1"), vol("2"), vol("3")]
        )["unit"]
        == "volumes"
    )
    # One Piece: running, chapters from several sources.
    assert (
        resolve_series_unit(
            {"status": "ongoing"},
            {"status": "ongoing", "volume_count": 115},
            [ch("1"), ch("2")],
        )["unit"]
        == "chapters"
    )
    # BECK: ended, 34 volumes, both books and chapters offered → volumes.
    beck = resolve_series_unit(
        {},
        {"status": "ended", "volume_count": 34, "chapter_count": 103},
        [vol("1"), ch("1"), ch("2")],
    )
    assert beck["unit"] == "volumes" and beck["override"] is False
    # Same series, running → chapters.
    assert (
        resolve_series_unit(
            {}, {"status": "ongoing", "volume_count": 34}, [vol("1"), ch("1")]
        )["unit"]
        == "chapters"
    )
    # Ended with no book source → chapters, the user may still force volumes.
    assert (
        resolve_series_unit(
            {}, {"status": "ended", "volume_count": 5, "chapter_count": 48}, [ch("1")]
        )["unit"]
        == "chapters"
    )
    # A finished one-shot / short work is a book even when only episodes are offered (Grass).
    assert (
        resolve_series_unit(
            {},
            {"status": "ended", "volume_count": 1, "chapter_count": 19},
            [ch("1"), ch("2")],
        )["unit"]
        == "volumes"
    )
    forced = resolve_series_unit(
        {"series_unit_override": "volumes"}, {"status": "ended"}, [ch("1")]
    )
    assert forced == {
        "unit": "volumes",
        "reason": "chosen for this series",
        "override": True,
    }
    # Nothing mapped yet: short works default to volumes, the rest to chapters.
    assert (
        resolve_series_unit({}, {"volume_count": 1, "chapter_count": 10}, [])["unit"]
        == "volumes"
    )
    # Nothing mapped and not a short work: the global preference (volumes by default).
    assert (
        resolve_series_unit({}, {"volume_count": 20, "chapter_count": 200}, [])["unit"]
        == "volumes"
    )


def test_duplicate_integers_in_one_source_keep_the_best_release():
    releases = [
        ch("32", pages=18, id="a-32-bonus"),
        ch("32", pages=20, id="a-32"),
        ch("32", provider="b"),
    ]
    kept, dropped = normalize_chapter_releases(releases)
    assert sorted(r["id"] for r in kept) == ["a-32", "b-32"]
    assert dropped == {"duplicate_number": 1}


def test_split_parts_without_the_integer_are_kept_and_extras_are_dropped():
    releases = [
        ch("101.1"),
        ch("101.2"),
        ch("101.3"),
        ch("101.4"),  # MangaK-style split of 101
        ch("102"),
        ch("102.1"),
        ch("102.2"),  # parts next to the integer: extras
        ch("97"),
        ch("97.5"),  # fan art
        ch("0"),
        ch("-1"),  # preview / bonus
        ch("0.1"),
        ch("0.2"),  # prologue parts: extras, never chapter 0
        ch("101", provider="b"),
        ch("101.5", provider="b"),  # 101.5 next to 101: extra
    ]
    kept, dropped = normalize_chapter_releases(releases)
    assert sorted(r["chapter"] for r in kept if r["provider"] == "a") == [
        "101.1",
        "101.2",
        "101.3",
        "101.4",
        "102",
        "97",
    ]
    assert [r["chapter"] for r in kept if r["provider"] == "b"] == ["101"]
    assert dropped == {"extra": 6, "chapter_zero_or_negative": 2}


def test_lone_decimal_is_renamed_when_another_source_numbers_it_as_integer():
    releases = [ch("12.5"), ch("12", provider="b"), ch("13", provider="b")]
    kept, _ = normalize_chapter_releases(releases)
    renamed = next(r for r in kept if r["provider"] == "a")
    assert renamed["chapter"] == "12" and renamed["chapter_label_source"] == "12.5"


def test_select_releases_never_mixes_units_and_reports_coverage():
    metadata = {"status": "ended", "volume_count": 34, "chapter_count": 103}
    releases = [
        vol("1"),
        vol("2"),
        ch("1"),
        ch("2"),
        ch("2.5"),
        {"id": "loose", "chapter": None, "volume": None, "provider": "x"},
    ]
    info, selected = select_releases({}, metadata, releases)
    assert info["unit"] == "volumes" and [r["volume"] for r in selected] == ["1", "2"]
    assert info["coverage"] == {
        "chapters": {"available": 2, "expected": 103},
        "volumes": {"available": 2, "expected": 34},
    }
    info, selected = select_releases(
        {"series_unit_override": "chapters"}, metadata, releases
    )
    assert info["unit"] == "chapters" and sorted(r["chapter"] for r in selected) == [
        "1",
        "2",
    ]
    assert info["dropped"] == {"not_a_chapter": 3, "extra": 1}
    assert unit_coverage({}, [], {})["volumes"] == {"available": 0, "expected": None}


def test_numbers_only_one_source_claims_beyond_the_consensus_are_unconfirmed():
    from tankarr.series_unit import confirm_chapter_numbers

    def src(name, numbers, host="scan.test"):
        return [
            {
                **ch(str(n), provider="suwayomi", source_name=name, id=f"{name}-{n}"),
                "source_url": f"https://{host}/c/{n}",
            }
            for n in numbers
        ]

    # Two sources agree up to 250; MangaK alone continues 251..253 then jumps to 260..280.
    releases = (
        src("MangaFire", range(1, 251))
        + src("WeebCentral", range(1, 251))
        + src("MangaK", list(range(1, 254)) + list(range(260, 281)))
    )
    kept, dropped = confirm_chapter_numbers(releases)
    numbers = sorted({int(r["chapter"]) for r in kept})
    assert (
        numbers[-1] == 253 and dropped == 21
    )  # 251-253 continue the sequence, 260-280 do not
    # The official platform confirms on its own.
    official = src("Tapas", range(1, 281), host="tapas.io")
    kept, dropped = confirm_chapter_numbers(
        releases + official, official_hosts={"tapas.io"}
    )
    assert dropped == 0
    # A single source has no consensus to compare against: everything stays.
    kept, dropped = confirm_chapter_numbers(src("Only", range(1, 20)))
    assert dropped == 0 and len(kept) == 19


def test_unit_follows_what_the_sources_can_complete():
    import tankarr.series_unit as su

    ended = {"status": "ended", "volume_count": 3, "chapter_count": 30}
    all_chapters = [ch(str(n)) for n in range(1, 31)]
    two_books = [vol("1"), vol("2")]
    # Chapters can complete it, books cannot → chapters, even with preference volumes.
    assert (
        resolve_series_unit(
            {}, {**ended, "volume_count": 34}, all_chapters + two_books
        )["unit"]
        == "chapters"
    )
    # Both complete → the preference decides (default volumes).
    both = all_chapters + two_books + [vol("3")]
    assert resolve_series_unit({}, ended, both)["unit"] == "volumes"
    su.unit_context = lambda mid: {"preferred": "chapters", "indexer_volumes": set()}
    try:
        assert resolve_series_unit({"id": "x"}, ended, both)["unit"] == "chapters"
        # Indexer offers count as available books: 2 mapped + volume 3 on Usenet → books complete.
        su.unit_context = lambda mid: {"preferred": "volumes", "indexer_volumes": {3}}
        assert (
            resolve_series_unit(
                {"id": "x"}, {**ended, "chapter_count": 60}, all_chapters + two_books
            )["unit"]
            == "volumes"
        )
    finally:
        su.unit_context = None
    # Books already in the library keep the series on books (hand import of
    # two volumes) - unless the books cannot finish the ended work and the
    # chapters can: Kana's lone Billy Bat volume must not keep the 165
    # offered chapters out. The unit that completes the work wins.
    owned = [{**vol("1"), "downloaded": True}, {**vol("2"), "downloaded": True}]
    assert (
        resolve_series_unit({}, {**ended, "volume_count": 34}, owned + all_chapters)[
            "unit"
        ]
        == "chapters"
    )
    assert (
        resolve_series_unit(
            {}, {**ended, "volume_count": 34}, owned + all_chapters[:20]
        )["unit"]
        == "volumes"
    )
    assert (
        resolve_series_unit({}, {**ended, "volume_count": 2}, owned + all_chapters)[
            "unit"
        ]
        == "volumes"
    )
    # Running work: chapters, unless only books exist.
    assert (
        resolve_series_unit(
            {}, {"status": "ongoing", "volume_count": 22}, [vol("1"), vol("2")]
        )["unit"]
        == "volumes"
    )
    assert (
        resolve_series_unit(
            {}, {"status": "ongoing", "volume_count": 22}, [vol("1"), ch("1")]
        )["unit"]
        == "chapters"
    )


def test_books_on_disk_yield_when_no_source_can_extend_the_shelf():
    """Nana and Ouke no Monshou: 22 and 69 tankobon on disk and not one volume
    offered anywhere, beside 86 and 874 chapters. Paused rather than ended,
    so the Billy Bat escape never applied and the hunt would have asked for
    books nobody carries the day the work resumed."""

    owned = [{**vol(str(n)), "downloaded": True} for n in (1, 2)]
    all_chapters = [ch(str(n)) for n in range(1, 31)]
    paused = {"status": "hiatus", "volume_count": 34, "chapter_count": 30}
    assert resolve_series_unit({}, paused, owned + all_chapters)["unit"] == "chapters"
    # A continuing work follows available chapters even when more books
    # are offered. Owned books keep their coverage separately.
    running = {"status": "ongoing", "volume_count": 34, "chapter_count": 30}
    assert (
        resolve_series_unit({}, running, owned + all_chapters + [vol("3")])["unit"]
        == "chapters"
    )


@pytest.mark.parametrize("status", ["ongoing", "hiatus", "on_hiatus", "paused"])
def test_continuing_books_only_series_can_follow_new_chapters(status):
    metadata = {"status": status, "volume_count": 2, "chapter_count": 21}
    releases = [{**vol(str(n)), "downloaded": True} for n in (1, 2)]
    assert resolve_series_unit({}, metadata, releases)["unit"] == "volumes"
    releases.append(ch("21"))
    info, selected = select_releases({}, metadata, releases)
    assert info["unit"] == "chapters"
    assert [row["chapter"] for row in selected] == ["21"]
    assert (
        resolve_series_unit({"series_unit_override": "volumes"}, metadata, releases)[
            "unit"
        ]
        == "volumes"
    )


def test_manual_continuing_status_takes_precedence_over_finished_catalogue():
    assert (
        resolve_series_unit(
            {"status_override": "continuing"},
            {"status": "ended", "volume_count": 1, "chapter_count": 2},
            [{**vol("1"), "downloaded": True}, ch("2")],
        )["unit"]
        == "chapters"
    )


def test_chapter_zero_counts_when_the_catalogue_counts_the_prologue():
    """A webtoon prologue is chapter 0.

    It is an extra when the catalogue ignores it and the first chapter when
    the catalogue counts it, so the catalogue total decides."""

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
    manga = {"preferred_language": "en", "status": "ended"}

    counted = select_releases(manga, {"chapter_count": 4}, releases)[1]
    ignored = select_releases(manga, {"chapter_count": 3}, releases)[1]

    assert sorted(item["chapter"] for item in counted) == ["0", "1", "2", "3"]
    assert sorted(item["chapter"] for item in ignored) == ["1", "2", "3"]


def test_locked_episodes_are_not_download_candidates():
    """A paid episode cannot be fetched, so it is neither wanted nor counted.

    One already imported before it was locked stays part of the library."""

    releases = [
        {
            "id": "free",
            "chapter": "1",
            "title": "1. Free",
            "language": "en",
            "provider": "suwayomi",
            "source_name": "Tapas (EN)",
            "downloaded": False,
        },
        {
            "id": "locked",
            "chapter": "2",
            "title": "\U0001f512 2. Paid",
            "language": "en",
            "provider": "suwayomi",
            "source_name": "Tapas (EN)",
            "downloaded": False,
        },
        {
            "id": "owned",
            "chapter": "3",
            "title": "\U0001f512 3. Paid but imported earlier",
            "language": "en",
            "provider": "suwayomi",
            "source_name": "Tapas (EN)",
            "downloaded": True,
        },
    ]

    kept, dropped = normalize_chapter_releases(releases)

    assert sorted(item["id"] for item in kept) == ["free", "owned"]
    assert dropped["locked"] == 1


WEBTOON_LINKS = {
    "official_links": [
        {
            "url": "https://www.webtoons.com/en/fantasy/tower-of-god/list?title_no=95",
            "language": "en",
        }
    ]
}


def webtoon(chapter, **extra):
    return ch(
        chapter,
        provider="suwayomi",
        source_name="Webtoons.com (EN)",
        source_url=(
            "https://www.webtoons.com/en/fantasy/tower-of-god/ep/viewer"
            f"?title_no=95&episode_no={chapter}"
        ),
        **extra,
    )


def test_the_official_platform_decides_whether_the_prologue_is_a_chapter():
    # Tower of God: WEBTOON English lists "[Season 1] Ep. 0 (ch. 0)". Dropping
    # it leaves the whole library one chapter behind the official numbering.
    releases = [webtoon(str(number)) for number in range(0, 30)]

    _info, selected = select_releases(
        {"preferred_language": "en"}, WEBTOON_LINKS, releases
    )

    assert "0" in {item["chapter"] for item in selected}
    assert len(selected) == 30


def test_a_prologue_no_official_source_publishes_stays_an_extra():
    # Only a mirror has a chapter 0, and the catalogue does not count it.
    releases = [ch("0", provider="mirror")] + [
        webtoon(str(number)) for number in range(1, 30)
    ]

    info, selected = select_releases(
        {"preferred_language": "en"}, WEBTOON_LINKS, releases
    )

    assert "0" not in {item["chapter"] for item in selected}
    assert info["dropped"]["chapter_zero_or_negative"] == 1


def test_a_chapter_the_official_edition_has_not_reached_is_not_a_slot():
    # Lookism: WEBTOON English stops at 611, the scanlators are at 622.
    releases = [webtoon(str(number)) for number in range(1, 612)] + [
        ch(str(number), provider="mirror") for number in range(605, 623)
    ]

    info, selected = select_releases(
        {"preferred_language": "en"}, WEBTOON_LINKS, releases
    )

    numbers = {item["chapter"] for item in selected}
    assert max(int(number) for number in numbers) == 611
    assert info["dropped"]["beyond_official_edition"] == 11


def test_without_an_official_source_nothing_is_cut_off():
    releases = [ch(str(number), provider="mirror") for number in range(1, 40)]

    info, selected = select_releases({"preferred_language": "en"}, {}, releases)

    assert len(selected) == 39
    assert "beyond_official_edition" not in info["dropped"]


def test_an_offer_the_hunt_refused_does_not_keep_a_series_on_books():
    # Hansel & Gretel: catalogue says 1 volume / 1 chapter; the sources carry
    # 22 chapters; the indexers list a "Hansel and Gretel" torrent for v1 that
    # the hunt keeps refusing (another author's book). While that offer counts
    # as a book, the chapters are never asked for.
    releases = [ch(str(n), provider="mirror") for n in range(1, 23)]
    metadata = {"status": "ended", "volume_count": 1, "chapter_count": 1}
    manga = {
        "status": "ended",
        "_unit_context": {
            "preferred": "volumes",
            "indexer_volumes": {1},
            "unobtainable_volumes": set(),
        },
    }
    assert resolve_series_unit(manga, metadata, releases)["unit"] == "volumes"

    manga["_unit_context"]["unobtainable_volumes"] = {1}

    resolved = resolve_series_unit(manga, metadata, releases)
    assert resolved["unit"] == "chapters"


def test_a_finished_work_does_not_count_instalments_past_its_total():
    # Blade of the Phantom Master: 76 chapters in the books, numbered to 284
    # by aggregators counting magazine instalments.
    releases = [
        ch(str(n), provider="mirror", downloaded=n <= 76) for n in range(1, 285)
    ]
    metadata = {"status": "ended", "chapter_count": 76}

    info, selected = select_releases({"preferred_language": "en"}, metadata, releases)

    assert len(selected) == 76
    assert info["dropped"]["beyond_catalogue_total"] == 208


def test_a_file_on_disk_past_the_catalogue_total_is_kept():
    # A catalogue that is off by one must never hide a chapter the operator has.
    releases = [ch(str(n), provider="mirror", downloaded=True) for n in range(1, 78)]
    metadata = {"status": "ended", "chapter_count": 76}

    info, selected = select_releases({"preferred_language": "en"}, metadata, releases)

    assert len(selected) == 77
    assert "beyond_catalogue_total" not in info["dropped"]


def test_a_running_work_is_never_capped_by_the_catalogue_total():
    releases = [ch(str(n), provider="mirror") for n in range(1, 100)]
    metadata = {"status": "ongoing", "chapter_count": 50}

    info, selected = select_releases({"preferred_language": "en"}, metadata, releases)

    assert len(selected) == 99


def test_the_managed_edition_sets_the_books_a_series_expects():
    # Queen Emeraldas: four Japanese volumes, two Kodansha omnibuses. The
    # library can own two books, not four.
    metadata = {
        "status": "ended",
        "volume_count": 4,
        "managed_edition": {
            "publisher": "Kodansha Manga",
            "volume_count": 2,
            "complete": True,
        },
    }

    coverage = unit_coverage({}, [vol("1"), vol("2")], metadata)

    assert coverage["volumes"]["expected"] == 2
    assert coverage["volumes"]["available"] == 2


def test_a_lone_catalogue_total_does_not_hide_chapters_of_an_ended_work():
    releases = [ch(str(n), provider="mirror") for n in range(1, 23)]
    metadata = {
        "status": "ended",
        "chapter_count": 1,
        "count_confidence": {"chapter": "lone"},
    }

    info, selected = select_releases({"preferred_language": "en"}, metadata, releases)

    assert len(selected) == 22
    assert "beyond_catalogue_total" not in info["dropped"]


def test_unconfirmed_numbers_are_kept_when_the_file_is_on_disk():
    """A lone source past the consensus is dropped, unless the chapter is already ours."""

    from tankarr.series_unit import confirm_chapter_numbers

    def rel(chapter: str, source: str, *, downloaded: bool = False) -> dict:
        return {
            "id": f"{source}-{chapter}",
            "chapter": chapter,
            "provider": "suwayomi",
            "source_key": f"suwayomi:{source}",
            "source_url": f"https://{source}.test/{chapter}",
            "downloaded": downloaded,
        }

    releases = [rel("41", "a"), rel("41", "b"), rel("42", "a"), rel("42", "b")]
    releases += [rel("121", "c"), rel("122", "c", downloaded=True)]
    kept, dropped = confirm_chapter_numbers(releases)
    numbers = sorted(int(r["chapter"]) for r in kept)
    assert numbers == [41, 41, 42, 42, 122]
    assert dropped == 1


def test_books_already_on_their_way_keep_an_ended_work_on_volumes():
    """Ten of fourteen books in the download client are coverage, whatever
    the indexer ledger says; 142 of 144 chapters must not flip the unit."""

    from tankarr.series_unit import resolve_series_unit

    manga = {
        "id": "ykk",
        "status": "ended",
        "preferred_language": "en",
        "_unit_context": {
            "preferred": "volumes",
            "indexer_volumes": set(range(1, 14)),
            "unobtainable_volumes": {14},
            "pending_volumes": set(range(5, 15)),
        },
    }
    metadata = {"status": "ended", "volume_count": 14, "chapter_count": 144}
    releases = [
        {
            "id": f"c{n}",
            "chapter": str(n),
            "volume": None,
            "release_unit": "chapter",
            "downloaded": 0,
        }
        for n in range(1, 143)
    ]

    assert resolve_series_unit(manga, metadata, releases)["unit"] == "volumes"
    manga["_unit_context"]["pending_volumes"] = set()
    assert resolve_series_unit(manga, metadata, releases)["unit"] == "chapters"


def test_a_webtoon_is_followed_in_episodes_even_when_it_has_ended():
    """Books over a webtoon are a publisher's line, not the work's shape.

    Tower of God arrived with sixteen "books" that were episodes 418-420 read
    as volume numbers, and Omniscient Reader wanted five books no source has
    ever carried: in books that series could never complete. Both are marked
    LEFT_TO_RIGHT, so the reading direction says nothing; the catalogue states
    the kind outright.
    """

    webtoon = {"content_kind": "webtoon", "volume_count": 19, "chapter_count": 660}
    decision = resolve_series_unit({"id": "x", "status": "ended"}, webtoon, [])
    assert decision["unit"] == "chapters"
    assert decision["override"] is False

    # The same work under the other spelling the catalogue uses.
    nested = {"classification": {"kind": "webtoon"}, "volume_count": 20}
    assert resolve_series_unit({"id": "x", "status": "ended"}, nested, [])["unit"] == (
        "chapters"
    )

    # A manga that has ended still takes books when they can complete it.
    manga = {"content_kind": "manga", "volume_count": 15, "chapter_count": 90}
    assert resolve_series_unit({"id": "x", "status": "ended"}, manga, [])["unit"] == (
        "volumes"
    )

    # An operator who asks for books on a webtoon still gets them.
    forced = {"id": "x", "status": "ended", "series_unit_override": "volumes"}
    assert resolve_series_unit(forced, webtoon, [])["unit"] == "volumes"
