from __future__ import annotations

import asyncio
import json
import threading
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from httpx import Response
from PIL import Image

from tankarr.archive import package_cbz, sha256
from tankarr.config import Settings
from tankarr.database import Database
from tankarr.komga import KomgaClient
from tankarr.library_reader import ReaderIndependentLibrary
from tankarr.metadata.base import (
    MetadataSource,
    assess_series_match,
    canonical_person_name,
    creator_name_similarity,
    score_series_match,
    work_title_variants,
)
from tankarr.metadata.mangaupdates import MangaUpdatesMetadataSource
from tankarr.metadata.service import (
    CANONICAL_ARTWORK_DIMENSIONS,
    MATCH_POLICY_VERSION,
    MAX_STORED_ARTWORK_BYTES,
    ArtworkStore,
    MetadataService,
    _merge_external_links,
    _provider_correlations,
)
from tankarr.service import TankarrService


class FakeMetadataSource(MetadataSource):
    name = "fake"
    label = "Fake metadata"

    def __init__(self, records: list[dict[str, Any]]):
        self.records = records

    @property
    def configured(self) -> bool:
        return True

    async def search_series(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        return self.records[:limit]

    async def get_series(self, external_id: str) -> dict[str, Any]:
        return next(item for item in self.records if item["external_id"] == external_id)


class CompleteFakeMetadataSource(FakeMetadataSource):
    search_results_complete_for_matching = True
    identity_search_queries = 3

    def __init__(self, records: list[dict[str, Any]]):
        super().__init__(records)
        self.queries: list[str] = []
        self.detail_requests: list[str] = []

    async def search_series(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        self.queries.append(query)
        return await super().search_series(query, limit)

    async def get_series(self, external_id: str) -> dict[str, Any]:
        self.detail_requests.append(external_id)
        return await super().get_series(external_id)


class AuthorLookupFakeMetadataSource(FakeMetadataSource):
    supports_author_lookup = True

    def __init__(self, records: list[dict[str, Any]]):
        super().__init__(records)
        self.author_queries: list[str] = []
        self.title_queries: list[str] = []

    async def search_series(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        self.title_queries.append(query)
        return []

    async def search_series_by_author(
        self, author: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        self.author_queries.append(author)
        return self.records[:limit]


class OfflineMetadataSource(FakeMetadataSource):
    async def search_series(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        raise RuntimeError("catalogue offline")


class ComicVineFakeMetadataSource(FakeMetadataSource):
    name = "comicvine"
    label = "Comic Vine"
    supports_volumes = True

    def __init__(
        self,
        records: list[dict[str, Any]],
        issues: dict[str, list[dict[str, Any]]] | None = None,
    ):
        super().__init__(records)
        self.issues = issues or {}
        self.search_calls = 0
        self.issue_queries: list[str] = []

    async def search_series(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        self.search_calls += 1
        return await super().search_series(query, limit)

    async def search_volume(
        self, series: dict[str, Any], volume: str, limit: int = 10
    ) -> list[dict[str, Any]]:
        self.issue_queries.append(volume)
        return self.issues.get(volume, [])[:limit]


class FakeKomga:
    async def catalogue_for_paths(self, relative_paths):
        return {"configured": False, "books": {}, "series": {}}

    async def apply_catalogue_metadata(self, updates):
        return {"configured": False, "receipts": []}


class RecordingKomga:
    def __init__(self):
        self.updates: list[dict[str, Any]] = []

    async def catalogue_for_paths(self, relative_paths):
        paths = list(relative_paths)
        return {
            "configured": True,
            "books": {
                path: {"id": f"book-{index}", "series_id": "series-1"}
                for index, path in enumerate(paths, start=1)
            },
            "series": {"series-1": {"id": "series-1"}},
        }

    async def apply_catalogue_metadata(self, updates):
        self.updates = list(updates)
        return {
            "configured": True,
            "updated": len(self.updates),
            "artwork_uploaded": 0,
            "receipts": [
                {
                    "target_kind": item["target_kind"],
                    "target_id": item["target_id"],
                    "payload_sha256": item["payload_sha256"],
                    "artwork_sha256": item["artwork_sha256"],
                    "ok": True,
                }
                for item in self.updates
            ],
        }


class RecordingArtworkOnlyReader(RecordingKomga):
    supports_catalogue_metadata = False
    supports_catalogue_artwork = True


class FakeCoverProvider:
    def __init__(self, content: bytes):
        self.content = content

    async def get_cover(self, manga_id: str, filename: str) -> tuple[bytes, str]:
        return self.content, "image/jpeg"


def image_bytes(
    size: tuple[int, int],
    *,
    color: str = "red",
    image_format: str = "JPEG",
) -> bytes:
    output = BytesIO()
    Image.new("RGB", size, color).save(output, format=image_format, quality=92)
    return output.getvalue()


def seed(database: Database, *, cover_url: str | None = None) -> None:
    database.upsert_manga(
        {
            "id": "manga-1",
            "provider": "local",
            "title": "Example Story",
            "description": "",
            "cover_url": cover_url,
            "authors": ["Jane Doe"],
            "original_language": "ja",
            "status": None,
            "year": 2020,
            "available_languages": ["en"],
        },
        "en",
        "none",
    )


def fake_record(external_id: str = "source-1", **updates: Any) -> dict[str, Any]:
    result: dict[str, Any] = {
        "source": "fake",
        "external_id": external_id,
        "title": "Example Story",
        "hit_title": "Example Story",
        "alternate_titles": ["Example Monogatari"],
        "description": "An enriched description.",
        "authors": ["Doe Jane"],
        "creators": [{"name": "Doe Jane", "role": "writer"}],
        "genres": ["Drama"],
        "tags": [],
        "publisher": "Example Press",
        "year": 2020,
        "status": "ended",
        "work_type": "Manga",
        "original_language": "ja",
        "volume_count": 3,
        "chapter_count": 20,
        "rating": 8.1,
        "cover": None,
        "links": [{"label": "Fake metadata", "url": "https://example.test/1"}],
        "raw": {"id": external_id},
    }
    result.update(updates)
    return result


def test_series_match_requires_corroboration_for_ambiguous_titles():
    target = {"title": "Example", "authors": ["Jane Doe"], "year": 2020}
    strong = {"title": "Example", "authors": ["Doe Jane"], "year": 2020}
    ambiguous = {"title": "Example", "authors": [], "year": None}

    assert score_series_match(target, strong, exact_matches=2)[0] == 0.99
    assert score_series_match(target, ambiguous, exact_matches=2)[0] < 0.86


def test_series_match_normalizes_romanized_long_vowels_with_surname_anchor():
    target = {
        "title": "Blue Spring",
        "authors": ["Taiyo Matsumoto"],
        "year": 1993,
        "work_type": "Manga",
    }
    candidate = {
        "title": "Aoi Haru",
        "alternate_titles": ["Blue Spring"],
        "authors": ["Taiyou Matsumoto"],
        "year": 1993,
        "work_type": "manga",
        "catalogue_scope": "work",
    }

    assessment = assess_series_match(target, candidate, exact_matches=0)

    assert assessment.confidence >= 0.95
    assert "romanization-equivalent creator" in assessment.evidence


def test_series_match_accepts_minor_title_spelling_with_same_creator():
    target = {
        "title": "The Flowering Harbor",
        "authors": ["Jane Doe"],
        "work_type": "Manga",
    }
    candidate = {
        "title": "The Flowering Harbour",
        "authors": ["DOE Jane"],
        "work_type": "Manga",
        "catalogue_scope": "work",
    }

    assessment = assess_series_match(target, candidate)

    # "Harbor"/"Harbour" is one title spelled two ways, like Shoujo/Shōjo.
    assert assessment.title_relation in {"primary_exact", "edition_qualified_exact"}
    assert assessment.creator_similarity == 1.0
    assert assessment.confidence >= 0.86


def test_series_match_accepts_one_unique_language_edition_as_the_same_work():
    target = {"title": "Copper Harbor", "authors": [], "year": None}
    candidate = {
        "title": "COPPER HARBOR English Edition",
        "authors": ["Anthology"],
        "year": 2016,
        "work_type": "Manga",
        "catalogue_scope": "work",
    }

    assessment = assess_series_match(target, candidate, exact_matches=1)

    assert {"copper harbor english edition", "copper harbor"} <= work_title_variants(
        candidate["title"]
    )
    assert assessment.title_relation == "edition_qualified_exact"
    assert assessment.confidence >= 0.86


def test_series_match_keeps_multiple_language_editions_ambiguous_without_corroboration():
    target = {"title": "Copper Harbor", "authors": [], "year": None}
    candidate = {
        "title": "Copper Harbor Italian Edition",
        "authors": [],
        "year": None,
    }

    assessment = assess_series_match(target, candidate, exact_matches=2)

    assert assessment.title_relation == "edition_qualified_exact"
    assert assessment.confidence < 0.86


def test_series_match_rejects_contained_story_alias_of_creator_anthology():
    target = {
        "title": "PEZ",
        "authors": ["Hiroyuki Asada"],
        "work_type": "Manga",
    }
    candidate = {
        "title": "Robot",
        "alternate_titles": ["Pez", "Pez & Hot Strawberry"],
        "authors": [
            "ASADA Hiroyuki",
            "ABE Yoshitoshi",
            "MURATA Range",
            "TAJIMA Sho-u",
            "YASUDA Suzuhito",
        ],
        "work_type": "Manga",
        "catalogue_scope": "work",
    }

    assessment = assess_series_match(target, candidate)

    assert assessment.title_relation == "primary_matches_candidate_alias"
    assert assessment.creator_similarity == 1.0
    assert assessment.confidence < 0.86
    assert any(
        "broader multi-creator work" in item for item in assessment.contradictions
    )


@pytest.mark.asyncio
async def test_pre_add_completeness_uses_matcher_without_persisting_series(
    tmp_path: Path,
):
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    source = CompleteFakeMetadataSource([fake_record(chapter_count=3, status="ended")])
    service = MetadataService(
        Settings(
            data_dir=tmp_path / "data",
            library_dir=tmp_path / "library",
            metadata_enabled=True,
        ),
        database,
        FakeKomga(),  # type: ignore[arg-type]
        {},
        [source],
    )
    remote = {
        "id": "preview-only",
        "provider": "mangadex",
        "title": "Example Story",
        "description": "",
        "cover_url": None,
        "authors": ["Jane Doe"],
        "original_language": "ja",
        "status": "completed",
        "year": 2020,
        "last_volume": "1",
        "last_chapter": "3",
        "source_url": "https://example.test/preview-only",
    }
    releases = [
        {"id": f"chapter-{number}", "chapter": str(number), "volume": "1"}
        for number in (1, 2, 3)
    ]

    try:
        result = await service.preview_chapter_completeness(remote, releases, "en", 3)
    finally:
        await service.stop()

    assert result["status"] == "complete"
    assert result["verified_source_count"] == 1
    assert source.queries
    assert database.list_metadata_source_records("preview-only") == []


@pytest.mark.asyncio
async def test_matcher_can_constrain_work_candidates_through_exact_author(
    tmp_path: Path,
):
    settings = Settings(data_dir=tmp_path, library_dir=tmp_path / "library")
    settings.ensure_directories()
    database = Database(settings.database_path)
    database.initialize()
    database.upsert_manga(
        {
            "id": "manga-1",
            "provider": "local",
            "title": "The Flowering Harbor",
            "description": "",
            "cover_url": None,
            "authors": ["Jane Doe"],
            "original_language": None,
            "status": None,
            "year": None,
            "available_languages": ["en"],
        },
        "en",
        "none",
    )
    source = AuthorLookupFakeMetadataSource(
        [
            fake_record(
                title="The Flowering Harbour",
                hit_title="The Flowering Harbour",
                authors=["DOE Jane"],
                year=None,
                work_type="Manga",
                catalogue_scope="work",
            )
        ]
    )
    service = MetadataService(settings, database, FakeKomga(), {}, [source])  # type: ignore[arg-type]
    manga = database.get_manga("manga-1")
    try:
        outcome = await service._match_source(
            manga, source, [service._origin_record(manga)]
        )
    finally:
        await service.stop()

    assert outcome.state == "matched"
    assert outcome.record is not None
    assert outcome.record["external_id"] == "source-1"
    assert source.author_queries == ["Jane Doe"]
    assert source.title_queries == ["The Flowering Harbor"]


@pytest.mark.asyncio
async def test_matcher_uses_work_evidence_and_prefers_primary_title_over_alias(
    tmp_path: Path,
):
    settings = Settings(data_dir=tmp_path, library_dir=tmp_path / "library")
    settings.ensure_directories()
    database = Database(settings.database_path)
    database.initialize()
    database.upsert_manga(
        {
            "id": "manga-1",
            "provider": "local",
            "title": "Hajime no Ippo",
            "description": "",
            "cover_url": None,
            "authors": [],
            "original_language": "ja",
            "status": "ongoing",
            "year": None,
            "available_languages": ["en"],
        },
        "en",
        "none",
    )
    correct = fake_record(
        external_id="correct",
        title="Hajime no Ippo",
        alternate_titles=["The First Step"],
        authors=["George Morikawa"],
        year=1989,
        work_type="manga",
        volume_count=144,
        chapter_count=1515,
        catalogue_scope="work",
    )
    alias_decoy = fake_record(
        external_id="decoy",
        title="Yami Kariudo",
        alternate_titles=["Hajime no Ippo!"],
        authors=["Different Creator"],
        year=1984,
        work_type="manga",
        volume_count=5,
        chapter_count=20,
        catalogue_scope="work",
    )
    source = CompleteFakeMetadataSource([alias_decoy, correct])
    service = MetadataService(settings, database, FakeKomga(), {}, [source])  # type: ignore[arg-type]
    manga = database.get_manga("manga-1")
    origin = service._origin_record(manga)
    work_evidence = fake_record(
        source="mangaupdates",
        external_id="mu-1",
        title="Hajime no Ippo",
        authors=["MORIKAWA Jyoji"],
        year=1989,
        work_type="Manga",
        volume_count=144,
        chapter_count=1515,
        catalogue_scope="work",
    )
    try:
        outcome = await service._match_source(manga, source, [origin, work_evidence])
    finally:
        await service.stop()

    assert outcome.state == "matched"
    assert outcome.record is not None
    assert outcome.record["external_id"] == "correct"
    assert outcome.confidence >= 0.95
    assert source.detail_requests == ["correct"]
    assert source.queries[0] == "Hajime no Ippo"


@pytest.mark.asyncio
async def test_matcher_resolves_a_unique_language_edition_as_the_same_work(
    tmp_path: Path,
):
    settings = Settings(data_dir=tmp_path, library_dir=tmp_path / "library")
    settings.ensure_directories()
    database = Database(settings.database_path)
    database.initialize()
    database.upsert_manga(
        {
            "id": "manga-1",
            "provider": "local",
            "title": "Copper Harbor",
            "description": "",
            "cover_url": None,
            "authors": [],
            "original_language": None,
            "status": None,
            "year": None,
            "available_languages": ["en"],
        },
        "en",
        "none",
    )
    expected = fake_record(
        external_id="edition",
        title="Copper Harbor English Edition",
        authors=["Jane Doe"],
        year=2016,
        work_type="Manga",
        catalogue_scope="work",
    )
    decoy = fake_record(
        external_id="decoy",
        title="Copper Bay",
        authors=["Someone Else"],
        year=2020,
        work_type="Manga",
        catalogue_scope="work",
    )
    source = CompleteFakeMetadataSource([decoy, expected])
    service = MetadataService(settings, database, FakeKomga(), {}, [source])  # type: ignore[arg-type]
    manga = database.get_manga("manga-1")
    try:
        outcome = await service._match_source(
            manga, source, [service._origin_record(manga)]
        )
    finally:
        await service.stop()

    assert outcome.state == "matched"
    assert outcome.record is not None
    assert outcome.record["external_id"] == "edition"
    assert outcome.confidence >= 0.86
    assert source.detail_requests == ["edition"]


@pytest.mark.asyncio
async def test_matcher_refuses_equally_supported_catalogue_candidates(tmp_path: Path):
    settings = Settings(data_dir=tmp_path, library_dir=tmp_path / "library")
    settings.ensure_directories()
    database = Database(settings.database_path)
    database.initialize()
    seed(database)
    records = [
        fake_record(external_id="candidate-a", catalogue_scope="work"),
        fake_record(external_id="candidate-b", catalogue_scope="work"),
    ]
    source = CompleteFakeMetadataSource(records)
    service = MetadataService(settings, database, FakeKomga(), {}, [source])  # type: ignore[arg-type]
    manga = database.get_manga("manga-1")
    try:
        outcome = await service._match_source(
            manga, source, [service._origin_record(manga)]
        )
    finally:
        await service.stop()

    assert outcome.state == "ambiguous"
    assert outcome.record is None
    assert outcome.margin == 0.0
    assert source.detail_requests == []


def test_mangaupdates_normalizer_preserves_series_metadata():
    manga_updates = MangaUpdatesMetadataSource._normalize(
        {
            "series_id": 42,
            "title": "Example",
            "description": "<b>Summary</b>",
            "type": "Manga",
            "year": "2020",
            "status": "3 Volumes (Complete)",
            "latest_chapter": 9,
            "authors": [
                {
                    "name": "DOE Jane",
                    "type": "Author",
                    "author_id": 77,
                    "url": "https://www.mangaupdates.com/author/example",
                }
            ],
            "genres": [{"genre": "Drama"}],
            "publishers": [{"publisher_name": "Example Press", "type": "Original"}],
            "associated": [{"title": "Example Alt"}],
            "image": {"url": {"original": "https://cdn.mangaupdates.com/x.jpg"}},
            "url": "https://www.mangaupdates.com/series/example",
        }
    )
    assert manga_updates["description"] == "Summary"
    assert manga_updates["volume_count"] == 3
    assert manga_updates["chapter_count"] is None
    assert manga_updates["latest_release_chapter"] == 9
    assert manga_updates["status"] == "ended"
    assert manga_updates["publisher"] == "Example Press"
    assert manga_updates["creator_links"] == [
        {
            "name": "DOE Jane",
            "role": "author",
            "source": "mangaupdates",
            "label": "MangaUpdates",
            "external_id": "77",
            "url": "https://www.mangaupdates.com/author/example",
        }
    ]


@pytest.mark.parametrize(
    ("raw_status", "expected"),
    [
        ("1 Volume (Complete)", "ended"),
        ("12 Volumes (Ongoing)", "ongoing"),
        ("4 Volumes (Hiatus)", "hiatus"),
        ("2 Volumes (Discontinued)", "abandoned"),
        ("N/A", None),
        ("Incomplete catalogue record", None),
    ],
)
def test_mangaupdates_normalizes_only_explicit_publication_statuses(
    raw_status: str, expected: str | None
):
    assert MangaUpdatesMetadataSource._publication_status(raw_status) == expected


def test_mangaupdates_keeps_edition_fields_separate_but_preserves_work_status():
    record = MangaUpdatesMetadataSource._normalize(
        {
            "series_id": 42,
            "title": "An English Work Title",
            "description": "The same underlying work.",
            "type": "Manga",
            "year": "2015",
            "status": "1 Volume (Complete)",
            "latest_chapter": 9,
            "authors": [{"name": "DOE Jane", "type": "Author"}],
            "publishers": [{"publisher_name": "Translation Press", "type": "English"}],
            "url": "https://www.mangaupdates.com/series/example",
        }
    )

    assert record["catalogue_scope"] == "work"
    assert record["authors"] == ["DOE Jane"]
    assert record["work_type"] == "Manga"
    assert record["year"] is None
    assert record["publisher"] is None
    assert record["status"] == "ended"
    assert record["volume_count"] == 1
    assert record["chapter_count"] is None
    assert record["latest_release_chapter"] == 9
    assert record["publication_context"] == {
        "scope": "translated_edition",
        "language": "en",
        "publisher": "Translation Press",
        "publication_year": 2015,
        "volume_count": 1,
        "chapter_count": None,
    }


@pytest.mark.asyncio
@respx.mock
async def test_mangaupdates_author_index_returns_only_same_creator_works():
    settings = Settings()
    author_search = respx.post("https://api.mangaupdates.com/v1/authors/search").mock(
        return_value=Response(
            200,
            json={
                "results": [
                    {
                        "record": {
                            "id": 101,
                            "name": "DOE Jane",
                            "stats": {"total_series": 2},
                        }
                    },
                    {
                        "record": {
                            "id": 202,
                            "name": "DOE Janet",
                            "stats": {"total_series": 20},
                        }
                    },
                ]
            },
        )
    )
    author_series = respx.post(
        "https://api.mangaupdates.com/v1/authors/101/series"
    ).mock(
        return_value=Response(
            200,
            json={
                "total_series": 2,
                "series_list": [
                    {
                        "series_id": 301,
                        "title": "First Work",
                        "url": "https://www.mangaupdates.com/series/first",
                    },
                    {
                        "series_id": 302,
                        "title": "Second Work",
                        "url": "https://www.mangaupdates.com/series/second",
                    },
                ],
            },
        )
    )
    source = MangaUpdatesMetadataSource(settings)
    try:
        first = await source.search_series_by_author("Jane Doe")
        cached = await source.search_series_by_author("DOE Jane")
    finally:
        await source.aclose()

    assert [item["external_id"] for item in first] == ["301", "302"]
    assert first[0]["authors"] == ["DOE Jane"]
    assert cached == first
    assert author_search.call_count == 1
    assert author_series.call_count == 1


def test_identity_queries_are_deduplicated_and_bounded():
    queries = MetadataService._identity_queries(
        {
            "title": "Short title",
            "alternate_titles": [
                "Short   title",
                "A very long translated title " * 10,
            ],
        }
    )

    assert queries[0] == "Short title"
    assert len(queries) == 2
    assert max(map(len, queries)) <= 64


def test_work_identity_never_feeds_a_previous_display_title_back_into_matching(
    tmp_path: Path,
):
    settings = Settings(data_dir=tmp_path / "data", library_dir=tmp_path / "library")
    database = Database(settings.database_path)
    database.initialize()
    seed(database)
    database.set_manga_metadata_title("manga-1", "Wrong Parent Collection")
    service = MetadataService(settings, database, FakeKomga(), {}, [])  # type: ignore[arg-type]
    manga = database.get_manga("manga-1")

    identity = service._work_identity(manga, [service._origin_record(manga)])

    assert manga["title"] == "Wrong Parent Collection"
    assert manga["source_title"] == "Example Story"
    assert identity["title"] == "Example Story"
    assert "Wrong Parent Collection" not in identity["alternate_titles"]


def test_alias_only_match_cannot_reuse_another_series_primary_work_id(
    tmp_path: Path,
):
    settings = Settings(data_dir=tmp_path, library_dir=tmp_path / "library")
    database = Database(settings.database_path)
    database.initialize()
    seed(database)
    database.upsert_manga(
        {
            "id": "parent-series",
            "provider": "local",
            "title": "Parent Collection",
            "description": "",
            "authors": ["Jane Doe"],
            "original_language": "ja",
            "available_languages": ["en"],
        },
        "en",
        "none",
    )
    candidate = fake_record(
        source="mangaupdates",
        external_id="parent-work",
        title="Parent Collection",
        alternate_titles=["Example Story"],
        authors=["Jane Doe"],
    )
    database.save_metadata_source_record(
        "parent-series",
        entity_type="work",
        entity_key="",
        source="mangaupdates",
        external_id="parent-work",
        match_confidence=0.99,
        match_reason="primary exact",
        data=candidate,
        raw={},
    )
    service = MetadataService(settings, database, FakeKomga(), {}, [])  # type: ignore[arg-type]
    manga = database.get_manga("manga-1")
    assessment = assess_series_match(
        service._origin_record(manga), candidate, exact_matches=0
    )

    reason = service._identity_collision_reason(
        manga, "mangaupdates", candidate, assessment
    )

    assert assessment.title_relation == "primary_matches_candidate_alias"
    assert reason is not None
    assert "already identified" in reason


@pytest.mark.asyncio
async def test_artwork_store_bounds_large_komga_uploads(tmp_path: Path):
    settings = Settings(data_dir=tmp_path, library_dir=tmp_path / "library")
    image = Image.effect_noise((1800, 2600), 100).convert("RGB")
    source = BytesIO()
    image.save(source, format="JPEG", quality=100)
    assert len(source.getvalue()) > MAX_STORED_ARTWORK_BYTES

    store = ArtworkStore(settings)
    try:
        stored = store.store_bytes("manga-1", "series", source.getvalue())
    finally:
        await store.aclose()

    path = settings.data_dir / stored["path"]
    assert stored["media_type"] == "image/jpeg"
    assert path.stat().st_size <= MAX_STORED_ARTWORK_BYTES
    with Image.open(path) as prepared:
        assert prepared.format == "JPEG"
        assert prepared.mode == "RGB"
        assert prepared.size == CANONICAL_ARTWORK_DIMENSIONS


@pytest.mark.asyncio
async def test_artwork_store_downscales_larger_trusted_local_cover(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    settings = Settings(data_dir=tmp_path, library_dir=tmp_path / "library")
    image = Image.new("RGB", (100, 100), "red")
    source = BytesIO()
    image.save(source, format="PNG")
    content = source.getvalue()
    monkeypatch.setattr("tankarr.metadata.service.MAX_ARTWORK_BYTES", 32)
    monkeypatch.setattr("tankarr.metadata.service.MAX_LOCAL_ARTWORK_BYTES", 1_000_000)
    monkeypatch.setattr("tankarr.metadata.service.MAX_ARTWORK_PIXELS", 100)
    monkeypatch.setattr("tankarr.metadata.service.MAX_LOCAL_ARTWORK_PIXELS", 20_000)

    store = ArtworkStore(settings)
    try:
        with pytest.raises(ValueError, match="safety limit"):
            store.store_bytes("manga-1", "remote", content)
        stored = store.store_bytes("manga-1", "series", content, trusted_local=True)
    finally:
        await store.aclose()

    assert (settings.data_dir / stored["path"]).is_file()


@pytest.mark.asyncio
async def test_cached_oversized_artwork_is_normalized_before_retry(tmp_path: Path):
    settings = Settings(data_dir=tmp_path, library_dir=tmp_path / "library")
    settings.ensure_directories()
    database = Database(settings.database_path)
    database.initialize()
    image = Image.effect_noise((1800, 2600), 100).convert("RGB")
    source = BytesIO()
    image.save(source, format="JPEG", quality=100)
    path = settings.data_dir / "metadata" / "artwork" / "old.jpg"
    path.parent.mkdir(parents=True)
    path.write_bytes(source.getvalue())
    service = MetadataService(settings, database, FakeKomga(), {}, [])  # type: ignore[arg-type]
    try:
        cached = service._cached_artwork(
            "manga-1",
            "series",
            {
                "artwork_path": path.relative_to(settings.data_dir).as_posix(),
                "artwork_sha256": "old-hash",
                "artwork_media_type": "image/jpeg",
                "data": {
                    "artwork_source": "local",
                    "artwork_source_url": "/api/covers/local/manga-1/cover.jpg",
                },
            },
        )
    finally:
        await service.stop()

    assert cached is not None
    normalized = settings.data_dir / cached["path"]
    assert normalized.stat().st_size <= MAX_STORED_ARTWORK_BYTES
    assert cached["sha256"] != "old-hash"


@pytest.mark.asyncio
async def test_artwork_store_normalizes_small_png_to_uniform_jpeg(tmp_path: Path):
    settings = Settings(data_dir=tmp_path, library_dir=tmp_path / "library")
    store = ArtworkStore(settings)
    try:
        stored = store.store_bytes(
            "manga-1",
            "series",
            image_bytes((420, 420), color="purple", image_format="PNG"),
        )
    finally:
        await store.aclose()

    path = settings.data_dir / stored["path"]
    assert path.suffix == ".jpg"
    assert stored["media_type"] == "image/jpeg"
    assert stored["source_width"] == 420
    assert stored["source_height"] == 420
    with Image.open(path) as normalized:
        assert normalized.format == "JPEG"
        assert normalized.mode == "RGB"
        assert normalized.size == CANONICAL_ARTWORK_DIMENSIONS


@pytest.mark.asyncio
@respx.mock
async def test_series_artwork_compares_downloaded_images_not_reported_size(
    tmp_path: Path,
):
    settings = Settings(data_dir=tmp_path, library_dir=tmp_path / "library")
    settings.ensure_directories()
    database = Database(settings.database_path)
    database.initialize()
    cover_url = "/api/covers/local/manga-1/cover.jpg"
    seed(database, cover_url=cover_url)
    local_provider = FakeCoverProvider(image_bytes((800, 1200), color="blue"))
    service = MetadataService(
        settings,
        database,
        FakeKomga(),  # type: ignore[arg-type]
        {"local": local_provider},  # type: ignore[dict-item]
        [],
    )
    remote_url = "https://cdn.mangaupdates.com/image/tiny.jpg"
    respx.get(remote_url).mock(
        return_value=Response(
            200,
            content=image_bytes((160, 230), color="green"),
            headers={"content-type": "image/jpeg"},
        )
    )
    manga = database.get_manga("manga-1")
    records = [
        service._origin_record(manga),
        fake_record(
            source="mangaupdates",
            cover={"url": remote_url, "width": 4000, "height": 6000},
        ),
    ]
    try:
        selected = await service._select_series_artwork(
            manga, records, previous=None, force=True
        )
        assert selected is not None
        assert selected["source"] == "mangaupdates"
        assert selected["source_width"] == 160
        assert selected["source_height"] == 230
        assert selected["candidates_evaluated"] == 2

        local_provider.content = image_bytes((1800, 800), color="blue")
        selected = await service._select_series_artwork(
            manga, records, previous=None, force=True
        )
    finally:
        await service.stop()

    assert selected is not None
    assert selected["source"] == "mangaupdates"
    assert selected["source_width"] == 160
    assert selected["source_height"] == 230


@pytest.mark.asyncio
@respx.mock
async def test_series_artwork_keeps_every_candidate_and_honors_manual_choice(
    tmp_path: Path,
):
    settings = Settings(data_dir=tmp_path, library_dir=tmp_path / "library")
    settings.ensure_directories()
    database = Database(settings.database_path)
    database.initialize()
    cover_url = "/api/covers/local/manga-1/cover.jpg"
    seed(database, cover_url=cover_url)
    local_provider = FakeCoverProvider(image_bytes((800, 1200), color="blue"))
    service = MetadataService(
        settings,
        database,
        None,
        {"local": local_provider},  # type: ignore[dict-item]
        [],
    )
    remote_url = "https://cdn.mangaupdates.com/image/alternative.jpg"
    respx.get(remote_url).mock(
        return_value=Response(
            200,
            content=image_bytes((600, 900), color="green"),
            headers={"content-type": "image/jpeg"},
        )
    )
    manga = database.get_manga("manga-1")
    records = [
        service._origin_record(manga),
        fake_record(source="mangaupdates", cover={"url": remote_url}),
    ]
    try:
        automatic = await service._select_series_artwork(
            manga, records, previous=None, force=True
        )
        candidates = database.list_series_artwork_candidates("manga-1")
        assert automatic is not None
        assert len(candidates) == 2
        local = next(item for item in candidates if item["source"] == "local")
        database.set_series_artwork_preference("manga-1", str(local["candidate_id"]))

        manual = await service._select_series_artwork(
            manga, records, previous=None, force=True
        )
    finally:
        await service.stop()

    assert manual is not None
    assert manual["source"] == "local"
    assert manual["selection_mode"] == "manual"
    assert manual["candidate_id"] == local["candidate_id"]


@pytest.mark.asyncio
async def test_metadata_service_persists_provenance_and_canonical_record(
    tmp_path: Path,
):
    settings = Settings(
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "library",
        metadata_enabled=True,
    )
    settings.ensure_directories()
    database = Database(settings.database_path)
    database.initialize()
    seed(database)
    service = MetadataService(
        settings,
        database,
        FakeKomga(),  # type: ignore[arg-type]
        {},
        [FakeMetadataSource([fake_record()])],
    )
    try:
        result = await service.enrich_series("manga-1")
    finally:
        await service.stop()

    canonical = result["metadata"]["data"]
    assert canonical["description"] == "An enriched description."
    assert canonical["authors"] == ["Jane Doe"]
    assert canonical["publisher"] == "Example Press"
    assert canonical["reading_direction"] == "RIGHT_TO_LEFT"
    assert canonical["external_ids"]["fake"] == "source-1"
    assert canonical["links"] == [
        {"label": "Fake metadata", "url": "https://example.test/1"}
    ]
    assert canonical["provider_correlations"] == [
        {
            "provider": "fake",
            "label": "Fake metadata",
            "external_id": "source-1",
            "url": "https://example.test/1",
        }
    ]
    source_records = database.list_metadata_source_records("manga-1")
    assert {item["source"] for item in source_records} == {"local", "fake"}
    overview = database.metadata_overview()
    assert overview["series_enriched"] == 1
    assert overview["series_with_external_metadata"] == 1
    assert overview["series_without_external_metadata"] == 0


@pytest.mark.asyncio
async def test_verified_english_provider_alias_becomes_display_title(
    tmp_path: Path,
):
    settings = Settings(
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "library",
        metadata_enabled=True,
    )
    settings.ensure_directories()
    database = Database(settings.database_path)
    database.initialize()
    seed(database)
    record = fake_record(
        source="mangaupdates",
        title="Ax - Alternative Manga",
        hit_title="Example Story",
        alternate_titles=["Example Story", "Example Monogatari"],
        catalogue_alternate_titles=["Example Story"],
    )
    service = MetadataService(
        settings,
        database,
        FakeKomga(),  # type: ignore[arg-type]
        {},
        [FakeMetadataSource([record])],
    )
    try:
        result = await service.enrich_series("manga-1")
    finally:
        await service.stop()

    stored = database.get_manga("manga-1")
    canonical = result["metadata"]["data"]
    assert canonical["title"] == "Example Story"
    assert canonical["provenance"]["title"] == "mangaupdates"
    assert canonical["title_selection"]["strategy"] == "verified_provider_alias"
    assert "Ax - Alternative Manga" in canonical["alternate_titles"]
    assert "Example Monogatari" in canonical["alternate_titles"]
    assert stored["source_title"] == "Example Story"
    assert stored["title"] == "Example Story"


def test_lexically_related_english_catalogue_title_beats_short_provider_alias(
    tmp_path: Path,
):
    settings = Settings(data_dir=tmp_path / "data", library_dir=tmp_path / "library")
    database = Database(settings.database_path)
    database.initialize()
    seed(database)
    service = MetadataService(settings, database, FakeKomga(), {}, [])  # type: ignore[arg-type]
    manga = {**database.get_manga("manga-1"), "source_title": "Alternative Manga"}
    canonical = service._merge_series(
        manga,
        [
            service._origin_record(manga),
            fake_record(
                source="mangaupdates",
                title="Ax - Alternative Manga",
                alternate_titles=["Alternative Manga"],
                catalogue_alternate_titles=["Alternative Manga"],
            ),
        ],
    )

    assert canonical["title"] == "Ax - Alternative Manga"
    assert canonical["title_selection"]["strategy"] == "catalogue_primary"
    assert "Alternative Manga" in canonical["alternate_titles"]


@pytest.mark.asyncio
async def test_authoritative_catalogue_title_replaces_unverified_provider_title(
    tmp_path: Path,
):
    settings = Settings(data_dir=tmp_path / "data", library_dir=tmp_path / "library")
    settings.ensure_directories()
    database = Database(settings.database_path)
    database.initialize()
    seed(database)
    record = fake_record(
        source="mangaupdates",
        title="Ax - Alternative Manga",
        hit_title="Example Story",
        alternate_titles=["AX: A Collection of Alternative Manga"],
    )
    service = MetadataService(
        settings, database, FakeKomga(), {}, [FakeMetadataSource([record])]
    )  # type: ignore[arg-type]
    try:
        result = await service.enrich_series("manga-1")
    finally:
        await service.stop()

    canonical = result["metadata"]["data"]
    stored = database.get_manga("manga-1")
    assert canonical["title"] == "Ax - Alternative Manga"
    assert canonical["work"]["display_title"] == "Ax - Alternative Manga"
    assert stored["source_title"] == "Example Story"
    assert stored["metadata_title"] == "Ax - Alternative Manga"
    assert stored["title"] == "Ax - Alternative Manga"
    assert stored["title_source"] == "metadata"


@pytest.mark.asyncio
async def test_cached_legacy_title_requires_an_exact_manual_correlation(tmp_path: Path):
    settings = Settings(
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "library",
    )
    settings.ensure_directories()
    database = Database(settings.database_path)
    database.initialize()
    seed(database)
    record = fake_record(
        source="mangaupdates",
        title="Verified Legacy Title",
    )
    database.save_metadata_source_record(
        "manga-1",
        entity_type="work",
        entity_key="",
        source="mangaupdates",
        external_id="source-1",
        match_confidence=1.0,
        match_reason="legacy exact match",
        data=record,
        raw={},
    )
    database.save_series_metadata(
        "manga-1",
        {
            "title": "Example Story",
            "work": {"title": "Verified Legacy Title"},
            "provenance": {"title": "local"},
            "matched_sources": ["mangaupdates"],
        },
        artwork_path=None,
        artwork_sha256=None,
        artwork_media_type=None,
        source_status=[],
    )
    service = MetadataService(settings, database, FakeKomga(), {}, [])  # type: ignore[arg-type]

    skipped = await service.apply_cached_titles()
    assert skipped == {"checked": 0, "updated": 0, "errors": []}
    assert database.get_manga("manga-1")["metadata_title"] is None

    database.replace_manual_metadata_correlations(
        "manga-1",
        {
            "mangaupdates": {
                "external_id": "source-1",
                "url": "https://www.mangaupdates.com/series/example",
            }
        },
    )
    migrated = await service.apply_cached_titles()
    assert migrated == {"checked": 1, "updated": 1, "errors": []}
    assert database.get_manga("manga-1")["title"] == "Verified Legacy Title"


def test_explicit_translation_title_beats_unlabelled_romanization(tmp_path: Path):
    settings = Settings(data_dir=tmp_path / "data", library_dir=tmp_path / "library")
    database = Database(settings.database_path)
    database.initialize()
    seed(database)
    service = MetadataService(settings, database, FakeKomga(), {}, [])  # type: ignore[arg-type]
    manga = database.get_manga("manga-1")
    canonical = service._merge_series(
        manga,
        [
            service._origin_record(manga),
            fake_record(
                source="mangaupdates",
                title="Hanasaku Minato",
                catalogue_alternate_titles=["Flowering Harbour"],
            ),
            fake_record(
                source="myanimelist",
                title="Hanasaku Minato",
                localized_titles={"en": "Flowering Harbour"},
            ),
        ],
    )

    assert canonical["title"] == "Flowering Harbour"
    assert canonical["provenance"]["title"] == "myanimelist"
    assert canonical["title_selection"] == {
        "source": "myanimelist",
        "language": "en",
        "localized": True,
        "strategy": "localized",
        "priority": 160,
    }
    assert "Hanasaku Minato" in canonical["alternate_titles"]


def test_authoritative_title_removes_only_redundant_creator_and_year_qualifiers(
    tmp_path: Path,
):
    settings = Settings(data_dir=tmp_path / "data", library_dir=tmp_path / "library")
    database = Database(settings.database_path)
    database.initialize()
    seed(database)
    service = MetadataService(settings, database, FakeKomga(), {}, [])  # type: ignore[arg-type]
    manga = database.get_manga("manga-1")
    origin = service._origin_record(manga)

    astronauts = service._merge_series(
        manga,
        [
            origin,
            fake_record(
                source="mangaupdates",
                title="Astronauts (YOKOYAMA Yuichi)",
                authors=["Yuichi Yokoyama"],
                creators=[{"name": "Yuichi Yokoyama", "role": "Story & Art"}],
            ),
        ],
    )
    colored = service._merge_series(
        manga,
        [
            origin,
            fake_record(
                source="mangaupdates",
                title="Double Prince (Colored)",
                authors=["Yuichi Yokoyama"],
                creators=[{"name": "Yuichi Yokoyama", "role": "Story & Art"}],
            ),
        ],
    )
    dated = service._merge_series(
        manga,
        [
            origin,
            fake_record(
                source="mangaupdates",
                title="Astronauts (YOKOYAMA Yuichi) (2009)",
                year=2009,
                authors=["Yuichi Yokoyama"],
                creators=[{"name": "Yuichi Yokoyama", "role": "Story & Art"}],
            ),
        ],
    )
    unrelated_year = service._merge_series(
        manga,
        [
            origin,
            fake_record(
                source="mangaupdates",
                title="Astronauts (2008)",
                year=2009,
                authors=["Yuichi Yokoyama"],
            ),
        ],
    )

    assert astronauts["title"] == "Astronauts"
    assert colored["title"] == "Double Prince (Colored)"
    assert dated["title"] == "Astronauts"
    assert dated["year"] == 2009
    assert unrelated_year["title"] == "Astronauts (2008)"


@pytest.mark.asyncio
async def test_enrichment_publishes_portable_comicinfo_without_reader_api(
    tmp_path: Path,
):
    settings = Settings(
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "library",
        metadata_enabled=True,
    )
    settings.ensure_directories()
    identity = "0123456789abcdef0123456789abcdef"
    (settings.data_dir / ".tankarr-library-id").write_text(
        f"{identity}\n", encoding="utf-8"
    )
    (settings.library_dir / ".tankarr-library-id").write_text(
        f"{identity}\n", encoding="utf-8"
    )
    database = Database(settings.database_path)
    database.initialize()
    seed(database)
    chapter = {
        "id": "chapter-1",
        "volume": "1",
        "chapter": "1",
        "title": "Provider title",
        "language": "en",
        "provider": "local",
        "groups": [],
        "source_url": "https://example.test/chapter-1",
    }
    database.upsert_chapters("manga-1", [chapter])
    page = tmp_path / "001.jpg"
    page.write_bytes(b"portable-page")
    book = settings.library_dir / "Example Story" / "Example Story - c001 [en].cbz"
    package_cbz(book, [page], database.get_manga("manga-1"), chapter)
    original_digest = sha256(book)
    database.mark_chapter_downloaded("chapter-1", book, original_digest)

    library_service = TankarrService(
        settings,
        database,
        {},
        ReaderIndependentLibrary(),  # type: ignore[arg-type]
    )
    metadata = MetadataService(
        settings,
        database,
        None,
        {},
        [FakeMetadataSource([fake_record()])],
        library_publisher=library_service.publish_metadata_to_library,
    )
    try:
        result = await metadata.enrich_series("manga-1")
    finally:
        await metadata.stop()

    assert result["library"] == {
        "reader_independent": True,
        "checked": 1,
        "updated": 1,
        "updated_paths": [str(book)],
        "covers_written": 0,
        "series_covers_written": 0,
        "book_covers_written": 0,
        "covers_unchanged": 0,
        "errors": [],
    }
    with zipfile.ZipFile(book) as archive:
        comic_info = archive.read("ComicInfo.xml").decode("utf-8")
    assert "An enriched description." in comic_info
    assert "Jane Doe" in comic_info
    assert sha256(book) != original_digest
    assert database.get_chapter("chapter-1")["library_sha256"] == sha256(book)

    artwork = settings.data_dir / "metadata" / "artwork" / "series.jpg"
    artwork.parent.mkdir(parents=True, exist_ok=True)
    artwork.write_bytes(image_bytes(CANONICAL_ARTWORK_DIMENSIONS, color="navy"))
    saved = database.get_series_metadata("manga-1")
    assert saved is not None
    database.save_series_metadata(
        "manga-1",
        saved["data"],
        artwork_path=artwork.relative_to(settings.data_dir).as_posix(),
        artwork_sha256=sha256(artwork),
        artwork_media_type="image/jpeg",
        source_status=saved["source_status"],
    )
    old_cover = book.parent / "cover.png"
    old_cover.write_bytes(image_bytes((300, 400), color="yellow", image_format="PNG"))
    published = await library_service.publish_metadata_to_library("manga-1")
    assert published["covers_written"] == 1
    assert published["series_covers_written"] == 1
    assert published["book_covers_written"] == 0
    assert (book.parent / "cover.jpg").read_bytes() == artwork.read_bytes()
    assert not old_cover.exists()


@pytest.mark.asyncio
async def test_reclassified_range_file_is_relabelled_as_its_volume(tmp_path: Path):
    """Billy Bat: "c019-027" became volume 3, but the archive still said
    "Chapter 19-27" and the reader kept showing that label."""

    settings = Settings(data_dir=tmp_path / "data", library_dir=tmp_path / "library")
    settings.ensure_directories()
    identity = "0123456789abcdef0123456789abcdef"
    (settings.data_dir / ".tankarr-library-id").write_text(
        f"{identity}\n", encoding="utf-8"
    )
    (settings.library_dir / ".tankarr-library-id").write_text(
        f"{identity}\n", encoding="utf-8"
    )
    database = Database(settings.database_path)
    database.initialize()
    seed(database)
    chapter = {
        "id": "chapter-range",
        "volume": None,
        "chapter": "19-27",
        "title": "Chapter 19-27",
        "language": "en",
        "provider": "local",
        "groups": [],
        "source_url": "https://example.test/chapter-19-27",
    }
    database.upsert_chapters("manga-1", [chapter])
    page = tmp_path / "001.jpg"
    page.write_bytes(b"portable-page")
    book = settings.library_dir / "Example Story" / "Example Story - v003 [en].cbz"
    package_cbz(book, [page], database.get_manga("manga-1"), chapter)
    database.mark_chapter_downloaded("chapter-range", book, sha256(book))
    with zipfile.ZipFile(book) as archive:
        assert "<Title>Chapter 19-27</Title>" in archive.read("ComicInfo.xml").decode()

    assert database.set_release_book("chapter-range", "3")
    library_service = TankarrService(
        settings,
        database,
        {},
        ReaderIndependentLibrary(),  # type: ignore[arg-type]
    )
    report = await library_service._relabel_reclassified_books("manga-1")

    assert report["updated"] == 1
    assert report["updated_paths"] == [str(book)]
    with zipfile.ZipFile(book) as archive:
        comic_info = archive.read("ComicInfo.xml").decode("utf-8")
    assert "<Title>Volume 3</Title>" in comic_info
    assert "<Number>3</Number>" in comic_info
    assert "<Volume>3</Volume>" in comic_info
    assert "Chapter 19-27" not in comic_info
    assert database.get_chapter("chapter-range")["library_sha256"] == sha256(book)

    repeated = await library_service._relabel_reclassified_books("manga-1")
    assert repeated["updated"] == 0


def test_external_links_require_an_exact_provider_correlation_and_safe_url():
    assert _provider_correlations(
        [
            fake_record(),
            fake_record(source="local", external_id="local-1", links=[]),
            fake_record(
                source="unsafe",
                external_id="unsafe-1",
                links=[{"label": "Unsafe", "url": "javascript:alert(1)"}],
            ),
            fake_record(
                source="anilist",
                external_id="123",
                title="",
                links=[{"label": "AniList", "url": "https://anilist.co/manga/123"}],
            ),
        ]
    ) == [
        {
            "provider": "fake",
            "label": "Fake metadata",
            "external_id": "source-1",
            "url": "https://example.test/1",
        },
        {
            "provider": "unsafe",
            "label": "Unsafe",
            "external_id": "unsafe-1",
            "url": None,
        },
    ]
    assert _merge_external_links(
        [{"label": "Unsafe", "url": "javascript:alert(1)"}],
        [{"label": "Safe", "url": "https://example.test/work"}],
    ) == [{"label": "Safe", "url": "https://example.test/work"}]


def test_canonical_creators_collapse_long_vowel_romanization_aliases(tmp_path: Path):
    settings = Settings(data_dir=tmp_path, library_dir=tmp_path / "library")
    database = Database(settings.database_path)
    database.initialize()
    seed(database)
    service = MetadataService(settings, database, FakeKomga(), {}, [])  # type: ignore[arg-type]
    manga = database.get_manga("manga-1")
    manga["title"] = "Blue Spring"
    manga["authors"] = ["Taiyo Matsumoto"]
    origin = service._origin_record(manga)
    myanimelist = fake_record(
        source="myanimelist",
        title="Aoi Haru",
        alternate_titles=["Blue Spring"],
        authors=["Taiyou Matsumoto"],
        creators=[
            {"name": "Taiyou Matsumoto", "role": "Story"},
            {"name": "Taiyo Matsumoto", "role": "Art"},
        ],
        work_type="Manga",
        catalogue_scope="work",
    )

    canonical = service._merge_series(manga, [origin, myanimelist])

    assert canonical["authors"] == ["Taiyou Matsumoto"]
    assert canonical["creators"] == [
        {"name": "Taiyou Matsumoto", "role": "writer"},
        {"name": "Taiyou Matsumoto", "role": "penciller"},
    ]


def test_canonical_creators_do_not_absorb_lower_priority_anthology_credits(
    tmp_path: Path,
):
    settings = Settings(data_dir=tmp_path, library_dir=tmp_path / "library")
    database = Database(settings.database_path)
    database.initialize()
    seed(database)
    service = MetadataService(settings, database, FakeKomga(), {}, [])  # type: ignore[arg-type]
    manga = database.get_manga("manga-1")
    manga["title"] = "PEZ"
    manga["authors"] = ["Hiroyuki Asada"]
    origin = service._origin_record(manga)
    myanimelist = fake_record(
        source="myanimelist",
        title="Pez",
        authors=["Hiroyuki Asada"],
        creators=[
            {"name": "Hiroyuki Asada", "role": "Story"},
            {"name": "Hiroyuki Asada", "role": "Art"},
        ],
        work_type="Manga",
        catalogue_scope="work",
    )
    anthology = fake_record(
        source="mangaupdates",
        title="Robot",
        alternate_titles=["Pez"],
        authors=[
            "ASADA Hiroyuki",
            "ABE Yoshitoshi",
            "MURATA Range",
            "TAJIMA Sho-u",
        ],
        creators=[
            {"name": "ASADA Hiroyuki", "role": "Author"},
            {"name": "ABE Yoshitoshi", "role": "Artist"},
            {"name": "MURATA Range", "role": "Artist"},
            {"name": "TAJIMA Sho-u", "role": "Artist"},
        ],
        work_type="Manga",
        catalogue_scope="work",
    )

    canonical = service._merge_series(manga, [origin, anthology, myanimelist])

    assert canonical["authors"] == ["Hiroyuki Asada"]
    assert canonical["creators"] == [
        {"name": "Hiroyuki Asada", "role": "writer"},
        {"name": "Hiroyuki Asada", "role": "penciller"},
    ]


@pytest.mark.parametrize(
    ("work_type", "expected"),
    [
        ("Manga", "manga"),
        ("One Shot", "manga"),
        ("Manhwa", "webtoon"),
        ("Manhua", "webtoon"),
        ("Webtoon", "webtoon"),
        ("OEL", "webtoon"),
        ("Comic", "comic"),
        ("Graphic Novel", "comic"),
    ],
)
def test_work_type_is_mapped_to_stable_content_family(work_type: str, expected: str):
    assert MetadataService._content_kind_for_type(work_type) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("YOKOYAMA Yuichi", "Yuichi Yokoyama"),
        ("NAKAZAWA Keiji", "Keiji Nakazawa"),
        ("Banri Hidaka", "Banri Hidaka"),
        ("CLAMP", "CLAMP"),
    ],
)
def test_canonical_person_name_normalizes_catalogue_order(raw: str, expected: str):
    assert canonical_person_name(raw) == expected


@pytest.mark.asyncio
async def test_metadata_service_syncs_series_and_chapter_to_komga(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    settings = Settings(
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "library",
        metadata_enabled=True,
    )
    settings.ensure_directories()
    database = Database(settings.database_path)
    database.initialize()
    seed(database)
    database.upsert_chapters(
        "manga-1",
        [
            {
                "id": "chapter-1",
                "volume": "1",
                "chapter": "1",
                "title": "A provider-specific title",
                "language": "en",
                "provider": "local",
                "groups": [],
                "source_url": "https://example.test/chapter-1",
            }
        ],
    )
    book = settings.library_dir / "Example Story" / "Example Story - c001 [en].cbz"
    book.parent.mkdir(parents=True)
    book.write_bytes(b"book")
    database.mark_chapter_downloaded("chapter-1", book)
    komga = RecordingKomga()
    published: list[str] = []

    async def publish(manga_id: str) -> dict[str, Any]:
        published.append(manga_id)
        return {
            "reader_independent": True,
            "checked": 1,
            "updated": 1,
            "covers_written": 1,
            "errors": [],
        }

    service = MetadataService(
        settings,
        database,
        komga,  # type: ignore[arg-type]
        {},
        [FakeMetadataSource([fake_record()])],
        library_publisher=publish,
    )
    try:
        result = await service.enrich_series("manga-1")
        event_loop_thread = threading.get_ident()
        resolved_on: list[int] = []
        resolve = Path.resolve

        def record_resolve(path, *args, **kwargs):
            if path == book:
                resolved_on.append(threading.get_ident())
            return resolve(path, *args, **kwargs)

        monkeypatch.setattr(Path, "resolve", record_resolve)
        unchanged = await service.sync_to_komga("manga-1")
        assert komga.updates == []
        forced = await service.sync_to_komga("manga-1", force=True)
        full = await service.sync_all_to_komga(force=True)
    finally:
        await service.stop()

    assert published == ["manga-1"]
    assert len(resolved_on) >= 4
    assert event_loop_thread not in resolved_on
    assert result["library"]["reader_independent"] is True
    assert result["komga"]["synced"] is True
    assert unchanged["synced"] is True
    assert forced["synced"] is True
    assert forced["forced"] is True
    assert len(komga.updates) == 2
    assert all(item["payload"] for item in komga.updates)
    assert full["complete"] is True
    assert full["eligible"] == 1
    assert full["synced"] == 1
    assert service.komga_sync_pending() is False
    series_update = next(
        item for item in komga.updates if item["target_kind"] == "series"
    )
    book_update = next(item for item in komga.updates if item["target_kind"] == "book")
    assert series_update["payload"]["totalBookCount"] == 20
    assert book_update["payload"]["title"] == "Chapter 1"
    assert database.get_series_metadata("manga-1")["last_synced_at"] is not None


@pytest.mark.asyncio
async def test_artwork_only_reader_receives_cover_without_komga_payload(tmp_path: Path):
    settings = Settings(
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "library",
        metadata_enabled=True,
    )
    settings.ensure_directories()
    database = Database(settings.database_path)
    database.initialize()
    seed(database)
    database.upsert_chapters(
        "manga-1",
        [
            {
                "id": "chapter-1",
                "volume": "1",
                "chapter": "1",
                "title": "One",
                "language": "en",
                "provider": "local",
                "groups": [],
                "source_url": "https://example.test/chapter-1",
            }
        ],
    )
    book = settings.library_dir / "Example Story" / "Example Story - c001 [en].cbz"
    book.parent.mkdir(parents=True)
    book.write_bytes(b"book")
    database.mark_chapter_downloaded("chapter-1", book)
    artwork = settings.data_dir / "metadata" / "artwork" / "cover.jpg"
    artwork.parent.mkdir(parents=True)
    artwork.write_bytes(b"cover")
    database.save_series_metadata(
        "manga-1",
        fake_record(),
        artwork_path="metadata/artwork/cover.jpg",
        artwork_sha256="artwork-hash",
        artwork_media_type="image/jpeg",
        source_status=[],
    )
    reader = RecordingArtworkOnlyReader()
    service = MetadataService(
        settings,
        database,
        reader,  # type: ignore[arg-type]
        {},
        [],
    )
    try:
        first = await service.sync_to_komga("manga-1", force=True)
        assert len(reader.updates) == 1
        update = reader.updates[0]
        assert update["target_kind"] == "series"
        assert update["payload"] is None
        assert update["payload_sha256"] is None
        assert update["artwork_path"] == "metadata/artwork/cover.jpg"

        second = await service.sync_to_komga("manga-1")
    finally:
        await service.stop()

    assert first["synced"] is True and first["artwork_uploaded"] == 0
    assert second["synced"] is True
    assert reader.updates == []
    state = database.get_metadata_sync_state("series", "series-1")
    assert state["payload_sha256"] is None
    assert state["artwork_sha256"] == "artwork-hash"


@pytest.mark.asyncio
async def test_local_volume_cover_is_normalized_and_published_as_book_sidecar(
    tmp_path: Path,
):
    settings = Settings(
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "library",
        metadata_enabled=True,
    )
    settings.ensure_directories()
    identity = "0123456789abcdef0123456789abcdef"
    (settings.data_dir / ".tankarr-library-id").write_text(
        f"{identity}\n", encoding="utf-8"
    )
    (settings.library_dir / ".tankarr-library-id").write_text(
        f"{identity}\n", encoding="utf-8"
    )
    database = Database(settings.database_path)
    database.initialize()
    seed(database)
    chapter = {
        "id": "volume-1",
        "volume": "1",
        "chapter": None,
        "title": "",
        "language": "en",
        "provider": "local",
        "groups": [],
        "source_url": "https://example.test/volume-1",
    }
    database.upsert_chapters("manga-1", [chapter])
    page = tmp_path / "001.png"
    page.write_bytes(image_bytes((900, 1350), color="teal", image_format="PNG"))
    book = settings.library_dir / "Example Story" / "Example Story - v001 [en].cbz"
    package_cbz(book, [page], database.get_manga("manga-1"), chapter)
    database.mark_chapter_downloaded("volume-1", book, sha256(book))

    metadata = MetadataService(
        settings,
        database,
        FakeKomga(),
        {},
        [],  # type: ignore[arg-type]
    )
    try:
        volumes = await metadata._enrich_volumes(
            database.get_manga("manga-1"), fake_record(), force=True
        )
    finally:
        await metadata.stop()

    record = volumes[0]
    assert record["data"]["artwork_source"] == "local"
    assert record["data"]["artwork_source_width"] == 900
    assert record["data"]["artwork_source_height"] == 1350
    normalized = settings.data_dir / record["artwork_path"]
    with Image.open(normalized) as cover:
        assert cover.format == "JPEG"
        assert cover.size == CANONICAL_ARTWORK_DIMENSIONS

    library_service = TankarrService(
        settings,
        database,
        {},
        ReaderIndependentLibrary(),  # type: ignore[arg-type]
    )
    published = await library_service.publish_metadata_to_library("manga-1")
    sidecar = book.with_suffix(".jpg")
    assert published["book_covers_written"] == 1
    assert published["series_covers_written"] == 0
    assert sidecar.read_bytes() == normalized.read_bytes()

    repeated = await library_service.publish_metadata_to_library("manga-1")
    assert repeated["book_covers_written"] == 0
    assert repeated["covers_unchanged"] == 1


@pytest.mark.asyncio
async def test_landscape_volume_first_page_is_normalized_as_last_resort(
    tmp_path: Path,
):
    settings = Settings(data_dir=tmp_path / "data", library_dir=tmp_path / "library")
    settings.ensure_directories()
    database = Database(settings.database_path)
    database.initialize()
    seed(database)
    chapter = {
        "id": "volume-1",
        "volume": "1",
        "chapter": None,
        "title": "",
        "language": "en",
        "provider": "local",
        "groups": [],
        "source_url": "https://example.test/volume-1",
    }
    database.upsert_chapters("manga-1", [chapter])
    page = tmp_path / "001.jpg"
    page.write_bytes(image_bytes((1800, 800), color="black"))
    book = settings.library_dir / "Example Story" / "Example Story - v001 [en].cbz"
    package_cbz(book, [page], database.get_manga("manga-1"), chapter)
    database.mark_chapter_downloaded("volume-1", book, sha256(book))
    metadata = MetadataService(
        settings,
        database,
        FakeKomga(),
        {},
        [],  # type: ignore[arg-type]
    )
    try:
        volumes = await metadata._enrich_volumes(
            database.get_manga("manga-1"), fake_record(), force=True
        )
    finally:
        await metadata.stop()

    record = volumes[0]
    assert record["data"]["artwork_source"] == "local"
    assert record["data"]["artwork_source_width"] == 1800
    assert record["data"]["artwork_source_height"] == 800
    assert record["data"]["artwork_score"] < 40
    normalized = settings.data_dir / record["artwork_path"]
    with Image.open(normalized) as cover:
        assert cover.format == "JPEG"
        assert cover.size == CANONICAL_ARTWORK_DIMENSIONS


@pytest.mark.asyncio
async def test_automatic_noop_does_not_hide_previous_bulk_result(tmp_path: Path):
    settings = Settings(data_dir=tmp_path, library_dir=tmp_path / "library")
    settings.ensure_directories()
    database = Database(settings.database_path)
    database.initialize()
    service = MetadataService(settings, database, FakeKomga(), {}, [])  # type: ignore[arg-type]
    service.last_cycle_at = "2026-01-01T00:00:00+00:00"
    service.last_cycle_result = {
        "enabled": True,
        "force": True,
        "checked": 34,
        "synced": 34,
        "errors": [],
    }
    try:
        result = await service.run_cycle(force=False)
    finally:
        await service.stop()

    assert result["checked"] == 0
    assert service.last_cycle_result["checked"] == 34
    assert service.last_cycle_at == "2026-01-01T00:00:00+00:00"


@pytest.mark.asyncio
async def test_credential_change_queues_forced_refresh_after_active_cycle(
    tmp_path: Path,
):
    settings = Settings(data_dir=tmp_path, library_dir=tmp_path / "library")
    settings.ensure_directories()
    database = Database(settings.database_path)
    database.initialize()
    service = MetadataService(settings, database, FakeKomga(), {}, [])  # type: ignore[arg-type]
    first_cycle_gate = asyncio.Event()
    calls: list[bool] = []

    async def fake_cycle(*, force: bool = False) -> dict[str, Any]:
        calls.append(force)
        if len(calls) == 1:
            await first_cycle_gate.wait()
        return {"checked": 0}

    service.run_cycle = fake_cycle  # type: ignore[method-assign]
    first = service.start_bulk_refresh(force=False)
    await asyncio.sleep(0)
    queued = service.start_bulk_refresh(force=True)
    first_cycle_gate.set()
    assert service.bulk_task is not None
    await service.bulk_task
    await service.stop()

    assert first == {"started": True, "queued": False, "force": False}
    assert queued["queued"] is True
    assert calls == [False, True]


@pytest.mark.asyncio
async def test_successful_no_match_removes_stale_provider_identity(tmp_path: Path):
    settings = Settings(data_dir=tmp_path, library_dir=tmp_path / "library")
    settings.ensure_directories()
    database = Database(settings.database_path)
    database.initialize()
    seed(database)
    stale = fake_record(catalogue_scope="work")
    database.save_metadata_source_record(
        "manga-1",
        entity_type="work",
        entity_key="",
        source="fake",
        external_id="source-1",
        match_confidence=0.99,
        match_reason="old policy",
        data={key: value for key, value in stale.items() if key != "raw"},
        raw=stale["raw"],
    )
    unrelated = fake_record(
        external_id="unrelated",
        title="Completely Different",
        alternate_titles=[],
        authors=["Other Person"],
        year=1970,
        work_type="Comic",
        catalogue_scope="work",
    )
    service = MetadataService(
        settings,
        database,
        FakeKomga(),  # type: ignore[arg-type]
        {},
        [CompleteFakeMetadataSource([unrelated])],
    )
    try:
        result = await service.enrich_series("manga-1")
    finally:
        await service.stop()

    remaining = database.list_metadata_source_records("manga-1")
    assert "fake" not in {record["source"] for record in remaining}
    fake_status = next(
        item for item in result["metadata"]["source_status"] if item["name"] == "fake"
    )
    assert fake_status["state"] == "no_match"
    assert fake_status["candidate"]["external_id"] == "unrelated"


@pytest.mark.asyncio
async def test_old_match_policy_cache_is_not_reused_when_provider_is_offline(
    tmp_path: Path,
):
    settings = Settings(data_dir=tmp_path, library_dir=tmp_path / "library")
    settings.ensure_directories()
    database = Database(settings.database_path)
    database.initialize()
    seed(database)
    stale = fake_record(
        authors=["Jane Doe", "Unrelated Anthology Contributor"],
        match_policy_version=MATCH_POLICY_VERSION - 1,
        catalogue_scope="work",
    )
    database.save_metadata_source_record(
        "manga-1",
        entity_type="work",
        entity_key="",
        source="fake",
        external_id="source-1",
        match_confidence=0.99,
        match_reason="accepted by an obsolete matcher",
        data={key: value for key, value in stale.items() if key != "raw"},
        raw=stale["raw"],
    )
    service = MetadataService(
        settings,
        database,
        FakeKomga(),  # type: ignore[arg-type]
        {},
        [OfflineMetadataSource([])],
    )

    try:
        result = await service.enrich_series("manga-1")
    finally:
        await service.stop()

    canonical = result["metadata"]["data"]
    status = result["metadata"]["source_status"][0]
    assert canonical["authors"] == ["Jane Doe"]
    assert "fake" not in canonical["external_ids"]
    assert status["state"] == "error"
    assert status["matched"] is False
    assert "fake" not in {
        record["source"] for record in database.list_metadata_source_records("manga-1")
    }


def test_series_with_sync_error_is_retried_before_stale_deadline(tmp_path: Path):
    settings = Settings(data_dir=tmp_path, library_dir=tmp_path / "library")
    database = Database(settings.database_path)
    database.initialize()
    seed(database)
    database.save_series_metadata(
        "manga-1",
        fake_record(),
        artwork_path=None,
        artwork_sha256=None,
        artwork_media_type=None,
        source_status=[],
        error="Komga upload failed",
    )

    pending = database.list_manga_needing_metadata("2000-01-01T00:00:00+00:00")
    assert [item["id"] for item in pending] == ["manga-1"]

    database.mark_series_metadata_synced("manga-1")
    assert database.list_manga_needing_metadata("2000-01-01T00:00:00+00:00") == []


@pytest.mark.asyncio
@respx.mock
async def test_komga_catalogue_mapping_and_metadata_artwork_upload(tmp_path: Path):
    base_url = "https://komga.test"
    settings = Settings(
        data_dir=tmp_path,
        library_dir=tmp_path / "library",
        komga_url=base_url,
        komga_library_id="library-1",
        komga_api_key="key",
    )
    respx.get(f"{base_url}/api/v1/libraries").mock(
        return_value=Response(
            200,
            json=[
                {
                    "id": "library-1",
                    "name": "Comics",
                    "root": "/comics",
                    "unavailable": False,
                }
            ],
        )
    )
    respx.post(f"{base_url}/api/v1/books/list").mock(
        return_value=Response(
            200,
            json={
                "content": [
                    {
                        "id": "book-1",
                        "seriesId": "series-1",
                        "url": "/comics/Example/Example - v001.cbz",
                    }
                ]
            },
        )
    )
    respx.post(f"{base_url}/api/v1/series/list").mock(
        return_value=Response(
            200,
            json={"content": [{"id": "series-1", "url": "/comics/Example"}]},
        )
    )
    patch_route = respx.patch(f"{base_url}/api/v1/series/series-1/metadata").mock(
        return_value=Response(204)
    )
    upload_route = respx.post(f"{base_url}/api/v1/series/series-1/thumbnails").mock(
        return_value=Response(200, json={"id": "thumbnail-1"})
    )

    client = KomgaClient(settings)
    catalogue = await client.catalogue_for_paths(["Example/Example - v001.cbz"])
    assert catalogue["books"]["Example/Example - v001.cbz"]["id"] == "book-1"

    artwork = tmp_path / "metadata" / "artwork" / "cover.jpg"
    artwork.parent.mkdir(parents=True)
    artwork.write_bytes(b"\xff\xd8\xffcover")
    result = await client.apply_catalogue_metadata(
        [
            {
                "target_kind": "series",
                "target_id": "series-1",
                "payload": {"title": "Example", "titleLock": True},
                "payload_sha256": "payload-hash",
                "artwork_path": "metadata/artwork/cover.jpg",
                "artwork_sha256": "artwork-hash",
                "artwork_media_type": "image/jpeg",
            }
        ]
    )
    assert result["updated"] == 1
    assert result["artwork_uploaded"] == 1
    assert result["receipts"][0]["ok"] is True
    assert json.loads(patch_route.calls[0].request.content) == {
        "title": "Example",
        "titleLock": True,
    }
    assert upload_route.called


@pytest.mark.asyncio
async def test_matcher_prefers_edition_bearing_duplicate_record(tmp_path: Path):
    """MangaBaka lists one work several times (story, collection, reprint).

    Same title and creator on every record: the one carrying the English
    edition wins deterministically instead of stalling as ambiguous."""

    settings = Settings(data_dir=tmp_path, library_dir=tmp_path / "library")
    settings.ensure_directories()
    database = Database(settings.database_path)
    database.initialize()
    seed(database)
    records = [
        fake_record(
            external_id="story",
            catalogue_scope="work",
            volume_count=None,
            publishers=[],
            links=[{"label": "Fake metadata", "url": "https://example.test/s"}],
        ),
        fake_record(
            external_id="collection",
            catalogue_scope="work",
            volume_count=1,
            publishers=[
                {"name": "Drawn & Quarterly", "type": "English"},
                {"name": "Seirindo", "type": "Original"},
            ],
        ),
        fake_record(
            external_id="reprint",
            catalogue_scope="work",
            volume_count=1,
            publishers=[],
        ),
    ]
    source = CompleteFakeMetadataSource(records)
    service = MetadataService(settings, database, FakeKomga(), {}, [source])  # type: ignore[arg-type]
    manga = database.get_manga("manga-1")
    try:
        outcome = await service._match_source(
            manga, source, [service._origin_record(manga)]
        )
    finally:
        await service.stop()

    assert outcome.state == "matched"
    assert outcome.record is not None
    assert outcome.record["external_id"] == "collection"
    assert "duplicate catalogue records" in outcome.reason


@pytest.mark.asyncio
async def test_matcher_prefers_exact_title_over_near_tied_other_work(tmp_path: Path):
    """A near tie is only ambiguous when both candidates fit equally well.

    A catalogue often ranks the anthology that contains the story just below
    the story itself; the anthology's title is not the work's title, so it
    must not block the match."""

    settings = Settings(data_dir=tmp_path, library_dir=tmp_path / "library")
    settings.ensure_directories()
    database = Database(settings.database_path)
    database.initialize()
    seed(database)
    records = [
        fake_record(external_id="work", catalogue_scope="work"),
        fake_record(
            external_id="anthology",
            title="Example Story, Other Story",
            alternate_titles=["Example Story"],
            catalogue_scope="work",
        ),
    ]
    source = CompleteFakeMetadataSource(records)
    service = MetadataService(settings, database, FakeKomga(), {}, [source])  # type: ignore[arg-type]
    manga = database.get_manga("manga-1")
    try:
        outcome = await service._match_source(
            manga, source, [service._origin_record(manga)]
        )
    finally:
        await service.stop()

    assert outcome.state == "matched"
    assert outcome.record is not None
    assert outcome.record["external_id"] == "work"


@pytest.mark.asyncio
async def test_matcher_keeps_two_exact_title_candidates_ambiguous(tmp_path: Path):
    """Two records with the same title and no distinguishing edition evidence
    stay ambiguous: there is nothing to choose between them."""

    settings = Settings(data_dir=tmp_path, library_dir=tmp_path / "library")
    settings.ensure_directories()
    database = Database(settings.database_path)
    database.initialize()
    seed(database)
    records = [
        fake_record(external_id="a", catalogue_scope="work", publishers=[]),
        fake_record(external_id="b", catalogue_scope="work", publishers=[]),
    ]
    source = CompleteFakeMetadataSource(records)
    service = MetadataService(settings, database, FakeKomga(), {}, [source])  # type: ignore[arg-type]
    manga = database.get_manga("manga-1")
    try:
        outcome = await service._match_source(
            manga, source, [service._origin_record(manga)]
        )
    finally:
        await service.stop()

    assert outcome.state == "ambiguous"


@pytest.mark.asyncio
async def test_matcher_resolves_duplicates_beside_a_differently_titled_near_tie(
    tmp_path: Path,
):
    """Both shapes of near tie can appear together.

    MangaBaka holds the work three times and also ranks the anthology that
    contains it just below: the anthology must not block the choice, and the
    edition-bearing duplicate must still win."""

    settings = Settings(data_dir=tmp_path, library_dir=tmp_path / "library")
    settings.ensure_directories()
    database = Database(settings.database_path)
    database.initialize()
    seed(database)
    records = [
        fake_record(
            external_id="story",
            catalogue_scope="work",
            volume_count=None,
            publishers=[],
        ),
        fake_record(
            external_id="edition",
            catalogue_scope="work",
            volume_count=1,
            publishers=[{"name": "Drawn & Quarterly", "type": "English"}],
        ),
        fake_record(
            external_id="reprint", catalogue_scope="work", volume_count=1, publishers=[]
        ),
        fake_record(
            external_id="anthology",
            title="Example Story, Other Story",
            alternate_titles=["Example Story"],
            catalogue_scope="work",
        ),
    ]
    source = CompleteFakeMetadataSource(records)
    service = MetadataService(settings, database, FakeKomga(), {}, [source])  # type: ignore[arg-type]
    manga = database.get_manga("manga-1")
    try:
        outcome = await service._match_source(
            manga, source, [service._origin_record(manga)]
        )
    finally:
        await service.stop()

    assert outcome.state == "matched"
    assert outcome.record is not None
    assert outcome.record["external_id"] == "edition"


def test_creator_similarity_absorbs_romanization_and_joined_credits():
    """Local imports write creators the way the file names do.

    A dropped long vowel ("Keichi" for "Keiichi") and several creators stored
    in one string are spelling accidents, not different people. A shared
    surname alone still is not a match."""

    assert creator_name_similarity("Keichi Koike", "Keiichi Koike") >= 0.90
    assert creator_name_similarity("Yuichi Yokoyama", "Yuuichi Yokoyama") >= 0.90
    assert (
        creator_name_similarity("Koji Aihara Kentaro Takekuma", "Kentarou Takekuma")
        >= 0.90
    )
    assert creator_name_similarity("Yoshiharu Tsuge", "Tadao Tsuge") < 0.90
    assert creator_name_similarity("Jane Doe", "John Doe") < 0.90
    assert creator_name_similarity("Yoshikazu Kazuhiko", "Kazutoshi Handou") == 0.0


def test_english_edition_subtitle_needs_creator_corroboration():
    """ "Igaguri" and "Igaguri: Young Judo Master" are the same work.

    The expanded subtitle is weaker evidence than an exact title, so it only
    identifies the work together with the creator."""

    target = {"title": "Igaguri", "authors": ["Eiichi Fukui"], "work_type": "Manga"}
    expanded = {
        "title": "Igaguri: Young Judo Master",
        "authors": ["Eiichi Fukui"],
        "work_type": "Manga",
    }
    other_author = {**expanded, "authors": ["Someone Else"]}
    unknown_author = {**expanded, "authors": []}

    assert score_series_match(target, expanded, exact_matches=1)[0] >= 0.86
    assert score_series_match(target, other_author, exact_matches=1)[0] < 0.86
    assert score_series_match(target, unknown_author, exact_matches=1)[0] < 0.86


def test_anthology_and_various_are_not_creators():
    """A catalogue writes publication facts in the creator field.

    "Anthology" and "Various" describe how a book was made, not who made it,
    so they must not become the series author or reach the reader's library."""

    from tankarr.metadata.service import _is_person_credit, _unique_people

    assert _is_person_credit("Yoshiharu Tsuge") is True
    assert _is_person_credit("Anthology") is False
    assert _is_person_credit("Various") is False
    assert _is_person_credit("Unknown Author") is False
    assert _unique_people(["Anthology", "Suehiro Maruo", "Various"]) == [
        "Suehiro Maruo"
    ]


def test_a_spent_daily_quota_is_not_reported_as_an_error():
    """The setup is fine and the next cycle asks again."""

    from tankarr.metadata.service import _source_failure_status

    request = httpx.Request("GET", "https://example.test")
    quota = httpx.HTTPStatusError(
        "429", request=request, response=httpx.Response(429, request=request)
    )
    broken = httpx.HTTPStatusError(
        "500", request=request, response=httpx.Response(500, request=request)
    )

    from tankarr.providers.base import ProviderRequestError

    retried = ProviderRequestError("GET url failed after retries", status_code=429)

    assert _source_failure_status(quota, cached=False)["state"] == "rate_limited"
    # The shared client retries a 429 and reports its own error afterwards.
    assert _source_failure_status(retried, cached=False)["state"] == "rate_limited"
    assert _source_failure_status(quota, cached=False)["error"] is None
    # A catalogue answering 5xx is down, not misconfigured: unreachable, and
    # asked again next time (see the dedicated test below).
    assert _source_failure_status(broken, cached=False)["state"] == "unavailable"
    assert _source_failure_status(broken, cached=True)["state"] == "unavailable"
    assert _source_failure_status(broken, cached=True)["matched"] is True


def test_one_title_spelled_differently_is_the_same_work():
    """Sources disagree on capitals, spacing and romanization.

    Those are spellings of one title: deciding between them is not a human's
    job, so they must never reach the confirmation list."""

    same = [
        ("unORDINARY", "unOrdinary"),
        ("Nekokappa", "Neko Kappa"),
        ("Four Shōjo Stories", "Four Shoujo Stories"),
        ("Ryō no Hanashi", "Ryou no Hanashi"),
    ]
    different = [
        ("Ax: Alternative Manga", "AX"),
        ("Kappa at Work", "Neko Kappa"),
        ("ONE PIECE", "One Piece: Destiny"),
    ]

    for left, right in same:
        assert work_title_variants(left) & work_title_variants(right), (left, right)
    for left, right in different:
        assert not (work_title_variants(left) & work_title_variants(right)), (
            left,
            right,
        )


def test_an_exact_title_with_a_different_creator_never_matches_automatically():
    # Yuuichi Yokoyama's "Garden" was mapped to another comic of the same name
    # at 0.99 - exact title, unique result, matching length - and a whole
    # different work was downloaded under it.
    target = {
        "title": "Garden",
        "authors": ["Yuuichi Yokoyama"],
        "year": 2007,
        "chapter_count": 1,
        "work_type": "Manga",
    }
    candidate = {
        "title": "GARDEN",
        "authors": ["Someone Else"],
        "chapter_count": 1,
        "work_type": "Manga",
        "catalogue_scope": "work",
    }

    assessment = assess_series_match(target, candidate, exact_matches=1)

    assert assessment.title_relation == "primary_exact"
    assert "creator disagreement" in assessment.contradictions
    # Below the automatic threshold, above the review floor: it is a question,
    # not a decision and not a discarded result.
    assert 0.5 <= assessment.confidence < 0.86


def test_the_creator_ceiling_leaves_a_corroborated_match_alone():
    target = {
        "title": "Garden",
        "authors": ["Yuuichi Yokoyama"],
        "year": 2007,
        "work_type": "Manga",
    }
    candidate = {
        "title": "Garden",
        "authors": ["YOKOYAMA Yuuichi"],
        "year": 2007,
        "work_type": "Manga",
        "catalogue_scope": "work",
    }

    assessment = assess_series_match(target, candidate, exact_matches=1)

    assert not assessment.contradictions
    assert assessment.confidence >= 0.86


def test_a_source_that_names_no_creator_is_not_a_disagreement():
    # Most Suwayomi extensions expose no author at all; that must stay a
    # match on the catalogue title, not become a question nobody can answer.
    target = {"title": "Galaxy Express 999", "authors": ["Leiji Matsumoto"]}
    candidate = {"title": "Galaxy Express 999", "authors": []}

    assessment = assess_series_match(target, candidate, exact_matches=1)

    assert not assessment.contradictions
    assert assessment.confidence >= 0.86


def test_a_source_with_many_times_the_works_chapters_is_another_work():
    # Katsuhiro Otomo's Hansel & Gretel was filed under Junko Mizuno's: exact
    # title, source naming no creator, and 22 chapters against the work's 1
    # cost the match nothing.
    target = {
        "title": "Hansel and Gretel",
        "authors": ["Junko Mizuno"],
        "chapter_count": 1,
        "volume_count": 1,
        "work_type": "Manga",
    }
    candidate = {
        "title": "Hansel and Gretel",
        "authors": [],
        "chapter_count": 22,
        "work_type": "Manga",
        "catalogue_scope": "work",
    }

    assessment = assess_series_match(target, candidate, exact_matches=1)

    assert any("where the work has" in item for item in assessment.contradictions)
    assert assessment.confidence < 0.86


def test_an_incomplete_source_is_still_the_work():
    # MangaDex carrying 3 of 55 chapters is the work, only incomplete: fewer
    # is never suspicious, only wildly more is.
    target = {
        "title": "A Long Series",
        "authors": ["Somebody"],
        "chapter_count": 55,
        "work_type": "Manga",
    }
    candidate = {
        "title": "A Long Series",
        "authors": [],
        "chapter_count": 3,
        "work_type": "Manga",
        "catalogue_scope": "work",
    }

    assessment = assess_series_match(target, candidate, exact_matches=1)

    assert not assessment.contradictions
    assert assessment.confidence >= 0.86


def test_split_releases_within_reason_are_not_another_work():
    # A source splitting chapters (55 -> 60) or counting a few extras stays
    # inside the tolerance band; the penalty needs several times more.
    target = {"title": "A Series", "authors": ["Somebody"], "chapter_count": 55}
    candidate = {
        "title": "A Series",
        "authors": [],
        "chapter_count": 64,
        "catalogue_scope": "work",
    }

    assessment = assess_series_match(target, candidate, exact_matches=1)

    assert not any("where the work has" in item for item in assessment.contradictions)


def test_a_catalogue_that_is_down_is_unavailable_not_an_error():
    """Jikan answers 504 whenever MyAnimeList refuses it; 111 series showed
    'ProviderUnavailableError: metadata request failed' for it. A source
    that is down is reported as unreachable and retried, not as a fault."""

    import httpx

    from tankarr.metadata.service import _source_failure_status

    request = httpx.Request("GET", "https://api.jikan.moe/v4/manga/28")
    gateway = httpx.HTTPStatusError(
        "504", request=request, response=httpx.Response(504, request=request)
    )
    assert _source_failure_status(gateway, cached=False)["state"] == "unavailable"
    assert (
        _source_failure_status(httpx.ConnectError("refused"), cached=False)["state"]
        == "unavailable"
    )
    bad = httpx.HTTPStatusError(
        "400", request=request, response=httpx.Response(400, request=request)
    )
    assert _source_failure_status(bad, cached=False)["state"] == "error"
    assert _source_failure_status(bad, cached=True)["state"] == "cached_error"


@pytest.mark.asyncio
async def test_manual_ended_freezes_automatic_metadata_and_reset_resumes(tmp_path):
    from unittest.mock import AsyncMock

    settings = Settings(data_dir=tmp_path, library_dir=tmp_path / "library")
    settings.ensure_directories()
    database = Database(settings.database_path)
    database.initialize()
    seed(database)
    database.update_manga("manga-1", {"status_override": "ended"})
    service = MetadataService(settings, database, FakeKomga(), {}, [])
    service.enrich_series = AsyncMock(return_value={"metadata": {}, "library": {}})
    try:
        assert (await service.run_cycle(force=False))["checked"] == 0
        service.enrich_series.assert_not_awaited()
        assert (await service.run_cycle(force=True))["checked"] == 1
        service.enrich_series.reset_mock()
        database.update_manga("manga-1", {"status_override": "automatic"})
        assert (await service.run_cycle(force=False))["checked"] == 1
        service.enrich_series.assert_awaited_once_with("manga-1", force=False)
    finally:
        await service.stop()
