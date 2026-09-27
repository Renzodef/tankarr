from __future__ import annotations

import pytest
import respx
from httpx import Response

from tankarr.catalogue_consensus import (
    AGREED,
    CONFLICT,
    LONE,
    UNCONFIRMED,
    corroborate,
    count_consensus,
    count_is_reliable,
    english_edition,
    managed_volume_count,
    status_consensus,
)
from tankarr.config import Settings
from tankarr.metadata.kitsu import KitsuMetadataSource, normalize_kitsu_manga
from tankarr.metadata.myanimelist import (
    MyAnimeListMetadataSource,
    normalize_mal_manga,
)


def record(source: str, **fields):
    return {"source": source, "external_id": "1", **fields}


def test_a_count_only_mangabaka_claims_is_a_lone_claim():
    # Hansel & Gretel: MangaBaka alone said "1 chapter" and that digit closed
    # the frontier of a 22-chapter series.
    verdict = count_consensus([record("mangabaka", chapter_count=1)], "chapter_count")

    assert verdict["confidence"] == LONE
    assert verdict["value"] == 1


def test_two_catalogues_within_tolerance_agree():
    verdict = count_consensus(
        [
            record("mangabaka", chapter_count=76),
            record("anilist", chapter_count=76),
            record("kitsu", chapter_count=77),
        ],
        "chapter_count",
    )

    assert verdict["confidence"] == AGREED
    assert verdict["value"] == 76
    assert set(verdict["agreeing"]) == {"mangabaka", "anilist", "kitsu"}


def test_mangabaka_outvoted_takes_the_agreeing_clusters_value():
    verdict = count_consensus(
        [
            record("mangabaka", chapter_count=1),
            record("anilist", chapter_count=22),
            record("myanimelist", chapter_count=22),
        ],
        "chapter_count",
    )

    assert verdict["confidence"] == AGREED
    assert verdict["value"] == 22
    assert "mangabaka" not in verdict["agreeing"]


def test_catalogues_that_all_disagree_are_a_conflict_kept_on_the_spine():
    verdict = count_consensus(
        [
            record("mangabaka", volume_count=4),
            record("anilist", volume_count=9),
            record("kitsu", volume_count=15),
        ],
        "volume_count",
    )

    assert verdict["confidence"] == CONFLICT
    assert verdict["value"] == 4


def test_nobody_knowing_is_unconfirmed():
    assert count_consensus([record("mangabaka")], "chapter_count")["confidence"] == (
        UNCONFIRMED
    )


def test_status_is_decided_by_majority():
    verdict = status_consensus(
        [
            record("mangabaka", status="ended"),
            record("anilist", status="ended"),
            record("kitsu", status="ongoing"),
        ]
    )

    assert verdict == {
        "value": "ended",
        "confidence": AGREED,
        "votes": {"mangabaka": "ended", "anilist": "ended", "kitsu": "ongoing"},
    }


def test_the_english_edition_comes_from_mangabakas_publisher_note():
    # Queen Emeraldas: four Japanese volumes, two Kodansha omnibuses.
    edition = english_edition(
        [
            record(
                "mangabaka",
                volume_count=4,
                publishers=[
                    {"name": "Kodansha", "type": "Original", "note": ""},
                    {
                        "name": "Kodansha Manga",
                        "type": "English",
                        "note": "2 Vols - Complete",
                    },
                ],
            )
        ]
    )

    assert edition == {
        "publisher": "Kodansha Manga",
        "volume_count": 2,
        "omnibus": 1,
        "complete": True,
        "note": "2 Vols - Complete",
        "source": "mangabaka",
    }
    assert managed_volume_count({"managed_edition": edition}) == 2


def test_only_a_corroborated_count_may_set_a_frontier():
    assert count_is_reliable({"count_confidence": {"chapter": AGREED}}, "chapter")
    assert not count_is_reliable({"count_confidence": {"chapter": LONE}}, "chapter")
    assert not count_is_reliable({"count_confidence": {"chapter": CONFLICT}}, "chapter")
    # A record from before corroboration existed keeps its old standing.
    assert count_is_reliable({}, "chapter")


def test_corroborate_bundles_every_axis():
    result = corroborate(
        [
            record("mangabaka", chapter_count=76, volume_count=18, status="ended"),
            record("anilist", chapter_count=76, volume_count=18, status="ended"),
        ]
    )

    assert result["chapter_count"]["confidence"] == AGREED
    assert result["volume_count"]["confidence"] == AGREED
    assert result["status"]["value"] == "ended"
    assert result["english_edition"] is None


KITSU_PAYLOAD = {
    "data": {
        "id": "3185",
        "attributes": {
            "slug": "tenshi-nanka-ja-nai",
            "canonicalTitle": "Tenshi Nanka ja Nai",
            "titles": {"en_jp": "Tenshi Nanka ja Nai", "en_us": "I'm No Angel"},
            "abbreviatedTitles": ["TenNai"],
            "synopsis": "Midori.",
            "startDate": "1991-09-01",
            "endDate": "1995-01-01",
            "status": "finished",
            "mangaType": "manga",
            "chapterCount": 40,
            "volumeCount": 8,
            "posterImage": {"original": "https://media.kitsu.io/x.jpg"},
        },
    }
}


def test_kitsu_normalizes_counts_status_and_titles():
    item = normalize_kitsu_manga(KITSU_PAYLOAD)

    assert item["source"] == "kitsu"
    assert item["external_id"] == "3185"
    assert item["title"] == "Tenshi Nanka ja Nai"
    assert "I'm No Angel" in item["alternate_titles"]
    assert item["chapter_count"] == 40
    assert item["volume_count"] == 8
    assert item["status"] == "ended"
    assert item["year"] == 1991
    assert item["work_type"] == "Manga"


@pytest.mark.asyncio
@respx.mock
async def test_kitsu_is_read_by_id_and_never_searched():
    route = respx.get("https://kitsu.io/api/edge/manga/3185").mock(
        return_value=Response(200, json=KITSU_PAYLOAD)
    )
    source = KitsuMetadataSource(Settings())
    try:
        assert source.automatic_matching is False
        assert await source.search_series("Tenshi Nanka ja Nai") == []
        item = await source.get_series("3185")
    finally:
        await source.aclose()

    assert route.called
    assert item["volume_count"] == 8


MAL_PAYLOAD = {
    "data": {
        "mal_id": 132214,
        "url": "https://myanimelist.net/manga/132214",
        "title": "Jeonjijeok Dokja Sijeom",
        "title_english": "Omniscient Reader",
        "titles": [{"type": "Default", "title": "Jeonjijeok Dokja Sijeom"}],
        "type": "Manhwa",
        "chapters": None,
        "volumes": None,
        "status": "Publishing",
        "published": {"from": "2020-05-26T00:00:00+00:00", "to": None},
        "authors": [{"name": "Sing, Shong"}, {"name": "Sleepy-C"}],
        "genres": [{"name": "Action"}],
        "synopsis": "Kim Dokja.",
        "images": {"jpg": {"large_image_url": "https://cdn.myanimelist.net/x.jpg"}},
    }
}


def test_mal_normalizes_status_authors_and_titles():
    item = normalize_mal_manga(MAL_PAYLOAD)

    assert item["source"] == "myanimelist"
    assert item["external_id"] == "132214"
    assert item["status"] == "ongoing"
    assert item["work_type"] == "Manhwa"
    assert item["authors"] == ["Shong Sing", "Sleepy-C"]
    assert "Omniscient Reader" in item["alternate_titles"]
    assert item["chapter_count"] is None  # a running work has no total yet
    assert item["year"] == 2020


@pytest.mark.asyncio
@respx.mock
async def test_mal_outage_is_one_missing_vote_not_a_failure_of_the_series():
    respx.get("https://api.jikan.moe/v4/manga/132214").mock(
        return_value=Response(
            504, json={"status": 504, "message": "Jikan failed to connect"}
        )
    )
    source = MyAnimeListMetadataSource(Settings())
    try:
        with pytest.raises(Exception):
            await source.get_series("132214")
    finally:
        await source.aclose()


def test_the_origin_record_cannot_second_mangabaka():
    # The series' own "catalogue" record is MangaBaka's data under another
    # label. Measured live, it appeared as a voter on every series; on Hansel
    # & Gretel it would have turned the lone "1 chapter" into an agreement.
    verdict = count_consensus(
        [
            record("mangabaka", chapter_count=1),
            record("catalogue", chapter_count=1),
            record("local", chapter_count=1),
        ],
        "chapter_count",
    )

    assert verdict["confidence"] == LONE
    assert verdict["votes"] == {"mangabaka": 1}


def test_a_partial_english_edition_is_not_the_works_extent():
    # Kana's Billy Bat: one volume out, "Ongoing". Taking it as the target
    # declared a 20-volume work complete after one book.
    partial = english_edition(
        [
            record(
                "mangabaka",
                volume_count=20,
                publishers=[
                    {"name": "Kodansha", "type": "Original", "note": ""},
                    {
                        "name": "Kana (US)",
                        "type": "English",
                        "note": "1 Volume; Ongoing",
                    },
                ],
            )
        ]
    )

    assert partial["volume_count"] == 1
    assert partial["complete"] is False
    assert managed_volume_count({"managed_edition": partial}) is None


def test_an_omnibus_english_edition_keeps_its_book_count_but_is_not_the_extent():
    # Seven Seas' Yokohama Kaidashi Kikou: "5 3-in-1 Volumes - Complete".
    # Read naively the "1" of "3-in-1" made it a one-book work.
    edition = english_edition(
        [
            record(
                "mangabaka",
                volume_count=14,
                publishers=[
                    {"name": "Kodansha", "type": "Original", "note": ""},
                    {
                        "name": "Seven Seas Entertainment",
                        "type": "English",
                        "note": "5 3-in-1 Volumes - Complete",
                    },
                ],
            )
        ]
    )

    assert edition["volume_count"] == 5
    assert edition["omnibus"] == 3
    assert edition["complete"] is True
    assert managed_volume_count({"managed_edition": edition}) is None


def test_a_plain_complete_english_edition_still_sets_the_extent():
    # Kodansha's Mars: "15 Vols, Digital - Complete - 2-in-1 Print Ed. Upcoming"
    # names a print omnibus as an aside; the digital edition is the fifteen.
    edition = english_edition(
        [
            record(
                "mangabaka",
                volume_count=15,
                publishers=[
                    {
                        "name": "Kodansha Manga",
                        "type": "English",
                        "note": "15 Vols, Digital - Complete - 2-in-1 Print Ed. Upcoming",
                    },
                ],
            )
        ]
    )

    assert edition["volume_count"] == 15
    assert edition["omnibus"] == 1
    assert managed_volume_count({"managed_edition": edition}) == 15


def test_a_qualifier_between_the_count_and_volumes_still_reads_the_count():
    edition = english_edition(
        [
            record(
                "mangabaka",
                volume_count=18,
                publishers=[
                    {
                        "name": "VIZ Media",
                        "type": "English",
                        "note": "12 Physical Volumes; Complete",
                    },
                ],
            )
        ]
    )

    assert edition["volume_count"] == 12
    assert managed_volume_count({"managed_edition": edition}) == 12
