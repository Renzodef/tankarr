from __future__ import annotations

from pathlib import Path

import pytest

from tankarr.authors import AuthorRegistry
from tankarr.catalogue import manga_from_record
from tankarr.config import Settings
from tankarr.database import Database


class FakeMangaBaka:
    def __init__(self, results: dict[str, list[dict]]):
        self.results = results
        self.queries: list[str] = []

    async def search_series_by_author(self, author: str, limit: int = 50) -> list[dict]:
        self.queries.append(author)
        return [dict(item) for item in self.results.get(author, [])][:limit]


def work(
    catalogue_id: str,
    title: str,
    authors: list[str],
    *,
    year: int = 2000,
) -> dict:
    return {
        "source": "mangabaka",
        "external_id": catalogue_id,
        "title": title,
        "localized_titles": {"en": title},
        "alternate_titles": [],
        "description": "",
        "authors": authors,
        "creators": [{"name": author, "role": "writer"} for author in authors],
        "year": year,
        "status": "ongoing",
        "work_type": "Manga",
        "original_language": "ja",
        "volume_count": None,
        "chapter_count": None,
        "latest_release_chapter": None,
        "rating": None,
        "genres": [],
        "cover": None,
        "external_sources": [],
    }


def registry(
    tmp_path: Path, results: dict[str, list[dict]] | None = None
) -> tuple[AuthorRegistry, Database, FakeMangaBaka]:
    settings = Settings(
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "library",
        metadata_enabled=False,
        monitor_enabled=False,
    )
    database = Database(settings.database_path)
    database.initialize()
    source = FakeMangaBaka(results or {})
    return AuthorRegistry(settings, database, source), database, source


def add_work(
    authors: AuthorRegistry, database: Database, record: dict
) -> tuple[str, list[str]]:
    manga_id = f"manga-{record['external_id']}"
    database.upsert_manga(
        manga_from_record(record, language="en", manga_id=manga_id),
        "en",
        "none",
    )
    return manga_id, authors.register_catalogue_series(manga_id, record)


def test_registration_collapses_family_name_order_into_one_entity(tmp_path: Path):
    authors, database, _source = registry(tmp_path)
    first_id, first_authors = add_work(
        authors, database, work("1", "First", ["MIZUNO Junko"])
    )
    second_id, second_authors = add_work(
        authors, database, work("2", "Second", ["Junko Mizuno"])
    )

    assert first_authors == second_authors
    assert len(database.list_authors()) == 1
    assert database.list_manga_authors(first_id)[0]["name"] == "Junko Mizuno"
    assert database.list_manga_authors(second_id)[0]["id"] == first_authors[0]
    assert database.get_author(first_authors[0])["work_ids"] == ["1", "2"]


def test_library_backfill_uses_original_mangabaka_credit_not_manual_override(
    tmp_path: Path,
):
    authors, database, _source = registry(tmp_path)
    record = work("3", "Existing Series", ["Original Author"])
    manga = manga_from_record(record, language="en", manga_id="existing-series")
    database.upsert_manga(manga, "en", "none")
    database.set_manga_authors_override("existing-series", ["Manual Override"])

    result = authors.sync_library()

    assert result == {"checked": 1, "linked": 1}
    stored = database.list_authors()
    assert [item["display_name"] for item in stored] == ["Original Author"]
    assert stored[0]["work_ids"] == ["3"]
    assert database.list_manga_authors("existing-series")[0]["name"] == (
        "Original Author"
    )

    revision = database.library_revision()
    assert authors.sync_library() == {"checked": 1, "linked": 0}
    assert database.library_revision() == revision


@pytest.mark.asyncio
async def test_refresh_merges_similar_spellings_with_shared_work_evidence(
    tmp_path: Path,
):
    first = work("10", "First", ["Yuichi Yokoyama"])
    second = work("11", "Second", ["Yuichi Yokoyma"])
    authors, database, source = registry(
        tmp_path,
        {
            "Yuichi Yokoyama": [first, second],
            "Yuichi Yokoyma": [first, second],
        },
    )
    first_manga, first_authors = add_work(authors, database, first)
    second_manga, second_authors = add_work(authors, database, second)
    assert first_authors != second_authors

    page = await authors.refresh(first_authors[0])

    assert source.queries == ["Yuichi Yokoyama"]
    assert len(database.list_authors()) == 1
    assert page["work_count"] == 2
    assert set(page["aliases"]) == {"Yuichi Yokoyama", "Yuichi Yokoyma"}
    assert page["merged_from"][0]["id"] == second_authors[0]
    assert database.list_manga_authors(first_manga)[0]["id"] == page["id"]
    assert database.list_manga_authors(second_manga)[0]["id"] == page["id"]
    assert database.get_author(second_authors[0])["id"] == page["id"]


@pytest.mark.asyncio
async def test_refresh_does_not_merge_similar_names_without_shared_works(
    tmp_path: Path,
):
    first = work("20", "First", ["Yuichi Yokoyama"])
    second = work("21", "Second", ["Yuichi Yokoyma"])
    authors, database, _source = registry(
        tmp_path,
        {"Yuichi Yokoyama": [first], "Yuichi Yokoyma": [second]},
    )
    _first_manga, first_authors = add_work(authors, database, first)
    _second_manga, second_authors = add_work(authors, database, second)

    await authors.refresh(first_authors[0])
    await authors.refresh(second_authors[0])

    assert len(database.list_authors()) == 2
    assert database.get_author(first_authors[0])["redirected_from"] is None
    assert database.get_author(second_authors[0])["redirected_from"] is None
    candidates = authors.audit_duplicates()
    assert len(candidates) == 1
    assert candidates[0]["reason"] == "no shared MangaBaka work evidence"


@pytest.mark.asyncio
async def test_parenthetical_creator_qualifier_separates_same_romanization(
    tmp_path: Path,
):
    citrus = work("40", "Citrus", ["Ayuko"])
    tempest = work("41", "After the Tempest, in Late Summer", ["Ayuko"])
    other = work("42", "30-sai no Watashi-tachi", ["Ayuko (あゆこ)"])
    authors, database, source = registry(
        tmp_path,
        {
            "Ayuko": [citrus, tempest, other],
            "Ayuko (あゆこ)": [citrus, tempest, other],
        },
    )
    citrus_manga, ayuko_ids = add_work(authors, database, citrus)
    other_manga, qualified_ids = add_work(authors, database, other)

    assert ayuko_ids != qualified_ids
    assert len(database.list_authors()) == 2
    assert database.list_manga_authors(citrus_manga)[0]["id"] == ayuko_ids[0]
    assert database.list_manga_authors(other_manga)[0]["id"] == qualified_ids[0]

    ayuko_page = await authors.refresh(ayuko_ids[0])
    qualified_page = await authors.refresh(qualified_ids[0])

    assert source.queries == ["Ayuko", "Ayuko (あゆこ)"]
    assert [item["title"] for item in ayuko_page["works"]] == [
        "After the Tempest, in Late Summer",
        "Citrus",
    ]
    assert [item["title"] for item in qualified_page["works"]] == [
        "30-sai no Watashi-tachi"
    ]


def test_registration_merges_disjoint_deterministic_romanization_pages(
    tmp_path: Path,
):
    authors, database, _source = registry(tmp_path)
    _first_manga, first_authors = add_work(
        authors, database, work("22", "First", ["Akino Kondo"])
    )
    _second_manga, second_authors = add_work(
        authors, database, work("23", "Second", ["Akino Kondoh"])
    )
    _third_manga, third_authors = add_work(
        authors, database, work("24", "Third", ["Akino Kondou"])
    )

    assert first_authors == second_authors == third_authors
    assert len(database.list_authors()) == 1
    merged = database.get_author(first_authors[0])
    assert set(alias["name"] for alias in merged["aliases"]) == {
        "Akino Kondo",
        "Akino Kondoh",
        "Akino Kondou",
    }
    assert merged["work_ids"] == ["22", "23", "24"]


@pytest.mark.asyncio
async def test_refresh_blocks_merge_when_both_people_are_credited_on_shared_work(
    tmp_path: Path,
):
    shared = work("30", "Shared", ["Yuichi Yokoyama", "Yuuichi Yokoyama"])
    authors, database, _source = registry(
        tmp_path,
        {
            "Yuichi Yokoyama": [shared],
            "Yuuichi Yokoyama": [shared],
        },
    )
    _manga_id, author_ids = add_work(authors, database, shared)
    assert len(author_ids) == 2

    await authors.refresh(author_ids[0])
    await authors.refresh(author_ids[1])

    assert len(database.list_authors()) == 2
    assert all(len(item["aliases"]) == 1 for item in database.list_authors())
    candidates = authors.audit_duplicates()
    assert len(candidates) == 1
    assert candidates[0]["shared_works"] == ["30"]
    assert candidates[0]["reason"] == "conflicting shared credit"


def test_bulk_reads_match_the_per_series_queries(tmp_path: Path):
    """The startup backfill reads every series' work record and author links
    in one query each; what it sees must be what the per-series readers see."""

    authors, database, _source = registry(tmp_path)
    for identifier, title, credit in (
        ("1", "First", "Author One"),
        ("2", "Second", "Author Two"),
    ):
        record = work(identifier, title, [credit])
        manga = manga_from_record(
            record, language="en", manga_id=f"series-{identifier}"
        )
        database.upsert_manga(manga, "en", "none")
    assert authors.sync_library() == {"checked": 2, "linked": 2}
    database.save_metadata_source_record(
        "series-1",
        entity_type="work",
        entity_key="",
        source="mangabaka",
        external_id="1",
        match_confidence=1.0,
        match_reason="test",
        data={"external_id": "1", "title": "First", "authors": ["Author One"]},
        raw={},
    )
    database.save_metadata_source_record(
        "series-1",
        entity_type="work",
        entity_key="",
        source="anilist",
        external_id="9",
        match_confidence=1.0,
        match_reason="test",
        data={"title": "Other source"},
        raw={},
    )

    records = database.work_records_by_series("mangabaka")
    assert set(records) == {"series-1"}
    assert records["series-1"]["title"] == "First"
    per_series = database.list_metadata_source_records("series-1", entity_type="work")
    assert [item["data"] for item in per_series if item["source"] == "mangabaka"] == [
        records["series-1"]
    ]

    links = database.manga_authors_by_series()
    assert set(links) == {"series-1", "series-2"}
    for manga_id in links:
        assert links[manga_id] == database.list_manga_authors(manga_id)
