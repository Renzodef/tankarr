from __future__ import annotations

import pytest
import respx
from httpx import Response

from tankarr.config import Settings
from tankarr.metadata.anilist import AniListMetadataSource
from tankarr.metadata.correlations import correlation_url, normalize_correlation
from tankarr.metadata.mangabaka import (
    MangaBakaMetadataSource,
    plain_description,
)

MANGABAKA_BECK = {
    "id": 10341,
    "title": "BECK",
    "native_title": "BECK",
    "romanized_title": "BECK",
    "titles": [
        {
            "language": "ko",
            "title": "벡(BECK)",
            "is_primary": True,
            "traits": ["official"],
        },
        {"language": "en", "title": "Beck: Mongolian Chop Squad", "is_primary": False},
    ],
    "secondary_titles": {"ru": [{"title": "Бек", "type": "unknown"}]},
    "type": "manga",
    "status": "completed",
    "year": 2000,
    "published": {"start_date": "2000-02-17", "end_date": "2008-06-05"},
    "total_chapters": "103",
    "final_volume": "34",
    "authors": ["Harold Sakuishi"],
    "artists": ["Harold Sakuishi"],
    "publishers": [
        {"name": "Kodansha", "type": "Original"},
        {"name": "Kodansha Manga", "type": "English", "note": "34 Volumes"},
    ],
    "genres": ["music", "slice_of_life"],
    "tags": ["Band"],
    "rating": 85.69,
    "content_rating": "safe",
    "cover": {
        "raw": {"url": "https://images.example/beck.jpg", "width": 720, "height": 1024}
    },
    "links_v2": [
        {
            "url": "https://kodansha.us/series/beck/",
            "name": "kodansha.us",
            "name_display": "Kodansha",
        }
    ],
    "source": {
        "anilist": {"id": 30145, "rating_normalized": None},
        "anime_planet": {"id": "beck-mongolian-chop-squad"},
        "kitsu": {"id": 354},
        "manga_updates": {"id": "q9z31ex"},
        "my_anime_list": {"id": 145},
        "shikimori": {"id": 145},
    },
    "merged_with": None,
}

ANILIST_BECK = {
    "id": 30145,
    "idMal": 145,
    "title": {"romaji": "BECK", "english": "BECK", "native": "BECK"},
    "synonyms": ["ベック", "Beck: Mongolian Chop Squad"],
    "format": "MANGA",
    "status": "FINISHED",
    "chapters": 103,
    "volumes": 34,
    "countryOfOrigin": "JP",
    "isAdult": False,
    "startDate": {"year": 2000, "month": 2, "day": 17},
    "endDate": {"year": 2008, "month": 6, "day": None},
    "description": "Koyuki is a <i>bored</i> teen.<br>Then he meets Ryusuke.",
    "genres": ["Music", "Drama"],
    "tags": [{"name": "Band"}],
    "averageScore": 86,
    "coverImage": {
        "extraLarge": "https://img.example/beck-xl.jpg",
        "large": "https://img.example/beck.jpg",
    },
    "siteUrl": "https://anilist.co/manga/30145",
    "externalLinks": [
        {
            "site": "Kodansha USA",
            "url": "https://kodansha.us/series/beck/",
            "type": "INFO",
        }
    ],
    "staff": {
        "edges": [
            {"role": "Story & Art", "node": {"name": {"full": "Harold Sakuishi"}}}
        ]
    },
    "relations": {
        "edges": [
            {
                "relationType": "ADAPTATION",
                "node": {"id": 57, "title": {"romaji": "BECK"}, "format": "TV"},
            }
        ]
    },
}


@pytest.mark.asyncio
@respx.mock
async def test_mangabaka_search_normalizes_identity_and_external_ids():
    respx.get("https://api.mangabaka.org/v1/series/search").mock(
        return_value=Response(200, json={"status": 200, "data": [MANGABAKA_BECK]})
    )
    source = MangaBakaMetadataSource(Settings())
    try:
        (record,) = await source.search_series("beck")
    finally:
        await source.aclose()

    assert record["source"] == "mangabaka"
    assert record["external_id"] == "10341"
    assert record["title"] == "BECK"
    assert record["work_type"] == "Manga"
    assert record["original_language"] == "ja"
    assert record["status"] == "ended"
    assert record["year"] == 2000
    assert record["publication_year"] == 2000
    assert record["volume_count"] == 34
    assert record["chapter_count"] == 103
    assert record["authors"] == ["Harold Sakuishi"]
    assert {c["role"] for c in record["creators"]} == {"writer", "artist"}
    assert "Beck: Mongolian Chop Squad" in record["alternate_titles"]
    assert "Бек" in record["alternate_titles"]
    assert record["localized_titles"] == {"ko": "벡(BECK)", "en": "BECK"}
    assert record["publisher"] == "Kodansha"
    assert record["published_start"] == "2000-02-17"
    assert record["cover"]["url"] == "https://images.example/beck.jpg"
    assert record["links"][0] == {
        "label": "MangaBaka",
        "url": "https://mangabaka.org/10341",
    }
    # Linked catalogues become header chips with each community's score.
    assert [(site["label"], site["rating"]) for site in record["external_sources"]][
        :3
    ] == [
        ("MangaBaka", 8.6),
        ("MangaUpdates", None),
        ("AniList", None),
    ]
    assert (
        record["external_sources"][1]["url"]
        == "https://www.mangaupdates.com/series/q9z31ex"
    )
    assert record["localized_titles"]["en"] == "BECK"
    # Every identifier is in the form the corresponding Tankarr source uses:
    # MangaUpdates' base-36 slug becomes the decimal series ID.
    assert record["external_ids"] == {
        "mangaupdates": "57199464681",
        "myanimelist": "145",
        "anilist": "30145",
        "kitsu": "354",
        "animeplanet": "beck-mongolian-chop-squad",
    }


@pytest.mark.asyncio
@respx.mock
async def test_mangabaka_running_work_reports_progress_not_a_total():
    running = {
        **MANGABAKA_BECK,
        "id": 8805,
        "title": "unOrdinary",
        "type": "oel",
        "status": "releasing",
        "total_chapters": "397",
        "final_volume": None,
        "source": {"manga_updates": {"id": "fby045h"}, "kitsu": {"id": 38359}},
    }
    respx.get("https://api.mangabaka.org/v1/series/8805").mock(
        return_value=Response(200, json={"status": 200, "data": running})
    )
    source = MangaBakaMetadataSource(Settings())
    try:
        record = await source.get_series("8805")
    finally:
        await source.aclose()

    assert record["work_type"] == "OEL"
    assert record["original_language"] == "en"
    assert record["status"] == "ongoing"
    assert record["chapter_count"] is None
    assert record["latest_release_chapter"] == 397
    assert record["volume_count"] is None
    assert record["external_ids"] == {"mangaupdates": "33373975301", "kitsu": "38359"}


@pytest.mark.asyncio
@respx.mock
async def test_mangabaka_follows_a_merged_duplicate_to_the_surviving_record():
    respx.get("https://api.mangabaka.org/v1/series/999").mock(
        return_value=Response(
            200, json={"status": 200, "data": {"id": 999, "merged_with": 10341}}
        )
    )
    respx.get("https://api.mangabaka.org/v1/series/10341").mock(
        return_value=Response(200, json={"status": 200, "data": MANGABAKA_BECK})
    )
    source = MangaBakaMetadataSource(Settings())
    try:
        record = await source.get_series("999")
    finally:
        await source.aclose()
    assert record["external_id"] == "10341"


@pytest.mark.asyncio
@respx.mock
async def test_anilist_detail_normalizes_counts_roles_and_relations():
    route = respx.post("https://graphql.anilist.co").mock(
        return_value=Response(200, json={"data": {"Media": ANILIST_BECK}})
    )
    source = AniListMetadataSource(Settings())
    try:
        record = await source.get_series("30145")
    finally:
        await source.aclose()

    assert route.calls[0].request.headers["content-type"].startswith("application/json")
    assert record["source"] == "anilist"
    assert record["external_id"] == "30145"
    assert record["title"] == "BECK"
    assert record["work_type"] == "Manga"
    assert record["original_language"] == "ja"
    assert record["status"] == "ended"
    assert record["chapter_count"] == 103
    assert record["volume_count"] == 34
    assert record["year"] == 2000
    assert record["published_start"] == "2000-02-17"
    assert record["published_end"] == "2008-06-01"
    assert record["description"] == "Koyuki is a bored teen. Then he meets Ryusuke."
    assert record["authors"] == ["Harold Sakuishi"]
    assert {c["role"] for c in record["creators"]} == {"writer", "artist"}
    assert "Beck: Mongolian Chop Squad" in record["alternate_titles"]
    assert record["external_ids"] == {"myanimelist": "145"}
    assert record["related_works"] == [
        {"relation": "adaptation", "external_id": "57", "title": "BECK", "format": "TV"}
    ]
    assert record["cover"] == {"url": "https://img.example/beck-xl.jpg"}


@pytest.mark.asyncio
@respx.mock
async def test_anilist_classifies_by_country_and_surfaces_graphql_errors():
    manhwa = {
        **ANILIST_BECK,
        "id": 105398,
        "countryOfOrigin": "KR",
        "title": {
            "romaji": "Na Honjaman Level Up",
            "english": "Solo Leveling",
            "native": "나 혼자만 레벨업",
        },
    }
    oel = {**ANILIST_BECK, "id": 1, "countryOfOrigin": "US"}
    respx.post("https://graphql.anilist.co").mock(
        side_effect=[
            Response(200, json={"data": {"Page": {"media": [manhwa, oel]}}}),
            Response(200, json={"errors": [{"message": "Not Found."}], "data": None}),
        ]
    )
    source = AniListMetadataSource(Settings())
    try:
        first, second = await source.search_series("solo leveling")
        assert first["title"] == "Solo Leveling"
        assert first["work_type"] == "Manhwa"
        assert first["original_language"] == "ko"
        assert first["localized_titles"] == {
            "en": "Solo Leveling",
            "ko": "나 혼자만 레벨업",
        }
        assert second["work_type"] == "OEL"
        assert second["original_language"] == "en"
        with pytest.raises(RuntimeError, match="AniList: Not Found"):
            await source.get_series("404")
    finally:
        await source.aclose()


def test_catalogue_correlations_accept_ids_and_canonical_urls():
    assert normalize_correlation("mangabaka", "https://mangabaka.org/10341") == {
        "source": "mangabaka",
        "label": "MangaBaka",
        "external_id": "10341",
        "url": "https://mangabaka.org/10341",
    }
    with pytest.raises(ValueError, match="Unsupported metadata source"):
        normalize_correlation("anilist", "30145")
    with pytest.raises(ValueError):
        normalize_correlation("mangabaka", "https://example.com/10341")
    assert (
        correlation_url("mangaupdates", "57199464681")
        == "https://www.mangaupdates.com/series/q9z31ex"
    )
    assert correlation_url("kitsu", "354") == "https://kitsu.app/manga/354"
    assert correlation_url("unknown", "1") is None


@pytest.mark.asyncio
async def test_mangabaka_identity_pins_other_catalogues_by_id(tmp_path):
    from pathlib import Path

    from tankarr.database import Database
    from tankarr.metadata.service import MetadataService
    from tests.test_metadata import (
        CompleteFakeMetadataSource,
        FakeKomga,
        fake_record,
        seed,
    )

    class MangaBakaFake(CompleteFakeMetadataSource):
        name = "mangabaka"
        label = "MangaBaka"

    class MangaUpdatesFake(CompleteFakeMetadataSource):
        name = "mangaupdates"
        label = "MangaUpdates"

    class AniListFake(CompleteFakeMetadataSource):
        name = "anilist"
        label = "AniList"

    settings = Settings(
        data_dir=Path(tmp_path) / "data",
        library_dir=Path(tmp_path) / "library",
        metadata_enabled=True,
    )
    settings.ensure_directories()
    database = Database(settings.database_path)
    database.initialize()
    seed(database)
    mangabaka = MangaBakaFake(
        [
            fake_record(
                "10341",
                source="mangabaka",
                external_ids={
                    "mangaupdates": "57199464681",
                    "anilist": "30145",
                    "kitsu": "354",
                },
            )
        ]
    )
    # These catalogues also expose a title search, but a decoy with the same
    # title proves they are reached by ID instead of being searched again.
    mangaupdates = MangaUpdatesFake(
        [
            fake_record("decoy", source="mangaupdates", authors=[]),
            fake_record("57199464681", source="mangaupdates"),
        ]
    )
    anilist = AniListFake([fake_record("30145", source="anilist")])
    service = MetadataService(
        settings,
        database,
        FakeKomga(),  # type: ignore[arg-type]
        {},
        [mangabaka, mangaupdates, anilist],
    )
    try:
        result = await service.enrich_series("manga-1")
    finally:
        await service.stop()

    assert mangabaka.queries, "MangaBaka is the only catalogue searched by title"
    assert mangaupdates.queries == []
    assert mangaupdates.detail_requests == ["57199464681"]
    assert anilist.queries == []
    assert anilist.detail_requests == ["30145"]
    canonical = result["metadata"]["data"]
    assert canonical["external_ids"]["mangabaka"] == "10341"
    assert canonical["external_ids"]["mangaupdates"] == "57199464681"
    assert canonical["external_ids"]["anilist"] == "30145"
    # Kitsu has no catalogue adapter, so it never becomes canonical evidence;
    # the identifier survives in MangaBaka's persisted record for later use.
    mangabaka_record = next(
        item
        for item in database.list_metadata_source_records("manga-1", entity_key="")
        if item["source"] == "mangabaka"
    )
    assert mangabaka_record["data"]["external_ids"]["kitsu"] == "354"
    statuses = {
        item["name"]: item for item in result["metadata"].get("source_status", [])
    }
    assert statuses["mangaupdates"]["correlation_origin"] == "catalogue"
    assert "MangaBaka" in statuses["mangaupdates"]["reason"]


def test_mangabaka_description_markdown_becomes_readable_lines():
    raw = (
        "A collection of stories. • *[Bianca](https://www.mangaupdates.com/series.html?id=1)* "
        "(1970, 16 pages) • *Girl on Porch* (1971, 12 pages) A boy&#39;s trip. "
        "• **Hanshin** (1984) <i>twins</i>."
    )
    assert plain_description(raw) == (
        "A collection of stories.\n"
        "• *[Bianca](https://www.mangaupdates.com/series.html?id=1)* (1970, 16 pages)\n"
        "• *Girl on Porch* (1971, 12 pages) A boy's trip.\n"
        "• **Hanshin** (1984) *twins*."
    )
    assert plain_description("Luffy sets out…\n\n*Source: VIZ Media, Vol. 1* ") == (
        "Luffy sets out…\n\n*Source: VIZ Media, Vol. 1*"
    )


def test_mangabaka_cover_beats_the_origin_duplicate_and_shikimori_is_gone():
    from tankarr.metadata.mangabaka import SITE_PAGES
    from tankarr.metadata.service import MetadataService

    url = "https://images.mangabaka.dev/abc"
    records = [
        {"source": "catalogue", "cover": {"origin_url": url}},
        {"source": "mangabaka", "cover": {"url": url, "width": 1800, "height": 2700}},
        {"source": "anilist", "cover": {"url": "https://s4.anilist.co/x.jpg"}},
    ]
    candidates = MetadataService._artwork_candidate_records(records)
    assert [(c["source"], c["url"]) for c in candidates] == [
        ("mangabaka", url),
        ("anilist", "https://s4.anilist.co/x.jpg"),
    ]
    assert "shikimori" not in SITE_PAGES


def test_mangabaka_keeps_official_platform_links_for_the_ranking():
    from tankarr.metadata.mangabaka import MangaBakaMetadataSource

    record = MangaBakaMetadataSource._normalize(
        {
            "id": 377,
            "title": "ONE PIECE",
            "type": "manga",
            "status": "releasing",
            "links_v2": [
                {
                    "url": "https://mangaplus.shueisha.co.jp/titles/100020",
                    "name_display": "MANGA Plus",
                    "type": "webplatform",
                    "language": "en",
                },
                {
                    "url": "https://www.viz.com/one-piece",
                    "name_display": "VIZ Media",
                    "type": "publisher",
                    "language": "en",
                },
                {
                    "url": "https://x.com/opcom_info",
                    "name_display": "X / Twitter",
                    "type": "social",
                    "language": "unknown",
                },
                {
                    "url": "https://en.wikipedia.org/wiki/One_Piece",
                    "name_display": "Wikipedia",
                    "type": "info",
                    "language": "en",
                },
            ],
        }
    )
    assert [
        (link["name"], link["type"], link["language"])
        for link in record["official_links"]
    ] == [
        ("MANGA Plus", "webplatform", "en"),
        ("VIZ Media", "publisher", "en"),
    ]
