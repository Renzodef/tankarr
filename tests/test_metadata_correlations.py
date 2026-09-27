from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tankarr.config import Settings
from tankarr.database import Database
from tankarr.metadata.base import MetadataSource
from tankarr.metadata.service import MATCH_POLICY_VERSION, MetadataService


def source_record(external_id: str, title: str) -> dict[str, Any]:
    return {
        "source": "myanimelist",
        "catalogue_scope": "work",
        "external_id": external_id,
        "title": title,
        "hit_title": "",
        "alternate_titles": [],
        "description": f"Metadata for {title}",
        "authors": ["Test Author"],
        "creators": [{"name": "Test Author", "role": "writer"}],
        "genres": [],
        "tags": [],
        "publisher": None,
        "year": 2001,
        "status": "ended",
        "work_type": "Manga",
        "original_language": "ja",
        "volume_count": 1,
        "chapter_count": 1,
        "rating": None,
        "cover": None,
        "links": [
            {
                "label": "MyAnimeList",
                "url": f"https://myanimelist.net/manga/{external_id}",
            }
        ],
        "raw": {"id": external_id},
    }


class ExactMyAnimeList(MetadataSource):
    name = "myanimelist"
    label = "MyAnimeList"

    def __init__(self, records: dict[str, dict[str, Any]]):
        self.records = records
        self.search_calls: list[str] = []
        self.detail_calls: list[str] = []

    @property
    def configured(self) -> bool:
        return True

    async def search_series(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        self.search_calls.append(query)
        return list(self.records.values())[:limit]

    async def get_series(self, external_id: str) -> dict[str, Any]:
        self.detail_calls.append(external_id)
        return dict(self.records[external_id])


EXACT_CORRELATIONS = [
    {
        "source": "mangabaka",
        "label": "MangaBaka",
        "external_id": "35015",
        "url": "https://mangabaka.org/35015",
    },
    {
        "source": "mangaupdates",
        "label": "MangaUpdates",
        "external_id": "48304492057",
        "url": "https://www.mangaupdates.com/series/m6v8wm1",
    },
    {
        "source": "myanimelist",
        "label": "MyAnimeList",
        "external_id": "5015",
        "url": "https://myanimelist.net/manga/5015",
    },
    {
        "source": "anilist",
        "label": "AniList",
        "external_id": "35015",
        "url": "https://anilist.co/manga/35015",
    },
    {
        "source": "kitsu",
        "label": "Kitsu",
        "external_id": "10585",
        "url": "https://kitsu.io/manga/10585",
    },
    {
        "source": "animeplanet",
        "label": "Anime-Planet",
        "external_id": "the-legend-of-mother-sarah",
        "url": "https://www.anime-planet.com/manga/the-legend-of-mother-sarah",
    },
]


def origin_manga(correlations: list[dict[str, str]] | None = None) -> dict[str, Any]:
    """A series whose origin provider supplied exact catalogue identities."""

    return {
        "id": "manga-1",
        "provider": "mangadex",
        "title": "Example Story",
        "description": "",
        "cover_url": None,
        "authors": ["Test Author"],
        "original_language": "ja",
        "status": "completed",
        "year": 2001,
        "last_volume": "1",
        "last_chapter": "1",
        "available_languages": ["en"],
        "source_url": "https://mangadex.org/title/manga-1",
        "external_correlations": (
            EXACT_CORRELATIONS if correlations is None else correlations
        ),
    }


def seed(database: Database) -> None:
    database.upsert_manga(origin_manga(), "en", "none")


def test_provider_correlations_survive_partial_or_empty_mangadex_refresh(
    tmp_path: Path,
):
    settings = Settings(data_dir=tmp_path / "data", library_dir=tmp_path / "library")
    settings.ensure_directories()
    database = Database(settings.database_path)
    database.initialize()
    seed(database)

    partial = origin_manga(
        [
            {
                "source": "myanimelist",
                "label": "MyAnimeList",
                "external_id": "999",
                "url": "https://myanimelist.net/manga/999",
            }
        ]
    )
    database.upsert_manga(partial, "en")
    database.upsert_manga(origin_manga([]), "en")

    exact = {
        item["source"]: item
        for item in database.list_provider_metadata_correlations("manga-1")
    }
    assert set(exact) == {
        "mangabaka",
        "mangaupdates",
        "myanimelist",
        "anilist",
        "kitsu",
        "animeplanet",
    }
    assert exact["myanimelist"]["external_id"] == "999"
    assert exact["mangaupdates"]["external_id"] == "48304492057"
    assert database.metadata_overview()["series_with_external_metadata"] == 1


@pytest.mark.asyncio
async def test_mangadex_exact_identity_bypasses_search_and_adds_link_only_sources(
    tmp_path: Path,
):
    settings = Settings(data_dir=tmp_path / "data", library_dir=tmp_path / "library")
    settings.ensure_directories()
    database = Database(settings.database_path)
    database.initialize()
    seed(database)
    source = ExactMyAnimeList({"5015": source_record("5015", "Example Story")})
    service = MetadataService(settings, database, None, {}, [source])

    try:
        result = await service.enrich_series("manga-1", force=True)
    finally:
        await service.stop()

    canonical = result["metadata"]["data"]
    assert source.search_calls == []
    assert source.detail_calls == ["5015"]
    assert canonical["external_ids"]["myanimelist"] == "5015"
    assert {item["provider"] for item in canonical["provider_correlations"]} == {
        "mangadex",
        "myanimelist",
    }
    stored = next(
        item
        for item in database.list_metadata_source_records("manga-1")
        if item["source"] == "myanimelist"
    )
    assert stored["data"]["match_origin"] == "mangadex"
    assert stored["data"]["match_policy_version"] == MATCH_POLICY_VERSION


@pytest.mark.asyncio
async def test_manual_override_wins_and_clear_restores_mangadex_identity(
    tmp_path: Path,
):
    settings = Settings(data_dir=tmp_path / "data", library_dir=tmp_path / "library")
    settings.ensure_directories()
    database = Database(settings.database_path)
    database.initialize()
    seed(database)

    class ExactMangaBaka(ExactMyAnimeList):
        name = "mangabaka"
        label = "MangaBaka"

        async def get_series(self, external_id: str) -> dict[str, Any]:
            record = await super().get_series(external_id)
            return {**record, "source": self.name}

        async def search_series(
            self, query: str, limit: int = 10
        ) -> list[dict[str, Any]]:
            return [
                {**record, "source": self.name}
                for record in await super().search_series(query, limit)
            ]

    source = ExactMangaBaka(
        {
            "35015": source_record("35015", "Example Story"),
            "999": source_record("999", "Intentional Manual Work"),
        }
    )
    service = MetadataService(settings, database, None, {}, [source])

    try:
        pinned = await service.update_manual_correlations(
            "manga-1", {"mangabaka": "https://mangabaka.org/999"}
        )
        assert (
            pinned["refresh"]["metadata"]["data"]["external_ids"]["mangabaka"] == "999"
        )
        editor = service.correlations_for("manga-1")
        pinned_source = next(
            item for item in editor["sources"] if item["source"] == "mangabaka"
        )
        assert pinned_source["origin"] == "manual"
        assert pinned_source["provider_url"] == "https://mangabaka.org/35015"

        cleared = await service.update_manual_correlations(
            "manga-1", {"mangabaka": None}
        )
    finally:
        await service.stop()

    assert (
        cleared["refresh"]["metadata"]["data"]["external_ids"]["mangabaka"] == "35015"
    )
    assert database.list_manual_metadata_correlations("manga-1") == []
    stored = next(
        item
        for item in database.list_metadata_source_records("manga-1")
        if item["source"] == "mangabaka"
    )
    assert stored["data"]["match_origin"] == "mangadex"


@pytest.mark.asyncio
async def test_match_policy_is_not_marked_complete_after_refresh_errors(
    tmp_path: Path,
):
    settings = Settings(data_dir=tmp_path / "data", library_dir=tmp_path / "library")
    settings.ensure_directories()
    database = Database(settings.database_path)
    database.initialize()
    service = MetadataService(settings, database, None, {}, [])

    async def failed_cycle(*, force: bool = False) -> dict[str, Any]:
        return {
            "force": force,
            "checked": 1,
            "errors": [{"manga_id": "manga-1", "error": "provider unavailable"}],
        }

    service.run_cycle = failed_cycle  # type: ignore[method-assign]
    try:
        await service._run_bulk_refresh_loop(force=True)
    finally:
        await service.stop()

    assert database.get_setting_overrides().get(
        "_metadata_match_policy_version"
    ) != str(MATCH_POLICY_VERSION)
