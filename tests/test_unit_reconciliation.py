from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import pytest

from tankarr.archive import sha256
from tankarr.config import Settings
from tankarr.database import Database
from tankarr.service import TankarrService
from tankarr.unit_reconciliation import (
    misclassified_explicit_chapters,
    release_source,
    volume_numbered_providers,
)


def release(provider: str, chapter: str, volume: str | None = None) -> dict:
    return {"provider": provider, "chapter": chapter, "volume": volume}


def test_source_that_numbers_volumes_as_chapters_is_detected():
    releases = [release("mangapill", str(n)) for n in range(1, 35)]
    releases += [release("suwayomi", str(n)) for n in range(1, 103)]

    verdicts = volume_numbered_providers(
        releases,
        volume_count=34,
        chapter_total=103,
        page_counts={"mangapill": [214, 201, 206], "suwayomi": [64, 61]},
    )

    assert [item.provider for item in verdicts] == ["mangapill"]
    assert verdicts[0].numbered == 34
    assert "34 numbered items" in verdicts[0].reason
    assert "~206 pages" in verdicts[0].reason


def test_page_evidence_vetoes_the_count_heuristic():
    releases = [release("mangapill", str(n)) for n in range(1, 35)]
    assert (
        volume_numbered_providers(
            releases,
            volume_count=34,
            chapter_total=103,
            page_counts={"mangapill": [40, 44]},
        )
        == []
    )


def test_partial_translation_matching_book_count_does_not_prove_whole_books():
    rows = [release("suwayomi", str(n)) for n in range(1, 25)]
    assert volume_numbered_providers(rows, volume_count=24, chapter_total=240) == []
    assert (
        volume_numbered_providers(
            rows,
            volume_count=24,
            chapter_total=240,
            page_counts={"suwayomi": [0, -1]},
        )
        == []
    )


def test_book_evidence_stays_with_its_extension_and_exact_release_ids(tmp_path):
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    database.upsert_manga(
        {"id": "m", "title": "Example", "description": "", "authors": []}, "en"
    )
    rows = [
        {
            **release("suwayomi", str(n)),
            "id": f"{source}-{n}",
            "source_key": source,
            "source_name": source,
            "title": f"Chapter {n}",
            "language": "en",
            "groups": [],
            "publish_at": None,
            "source_url": "",
        }
        for source in ("books", "chapters", "unknown")
        for n in range(1, 5)
    ]
    database.upsert_chapters("m", rows)
    verdicts = volume_numbered_providers(
        rows,
        volume_count=4,
        chapter_total=40,
        page_counts={"suwayomi/books": [210, 225], "suwayomi/chapters": [20, 24]},
    )
    assert len(verdicts) == 1
    assert verdicts[0].release_ids == tuple(f"books-{n}" for n in range(1, 5))
    assert release_source(rows[0]) == "suwayomi/books"
    assert (
        database.reclassify_provider_releases_as_volumes(
            "m",
            "suwayomi",
            release_ids=verdicts[0].release_ids,
        )
        == 4
    )
    saved = database.list_all_chapters("m")
    assert {row["id"] for row in saved if row["release_unit"] == "volume"} == set(
        verdicts[0].release_ids
    )
    assert (
        database.reclassify_provider_releases_as_volumes(
            "m", "suwayomi", release_ids=()
        )
        == 0
    )


def test_chapter_dense_sources_and_thin_catalogues_are_left_alone():
    chapters = [release("suwayomi", str(n)) for n in range(1, 103)]
    assert volume_numbered_providers(chapters, volume_count=34, chapter_total=103) == []
    # A one-shot or a work whose chapters barely exceed its volumes proves nothing.
    few = [release("mangapill", str(n)) for n in range(1, 4)]
    assert volume_numbered_providers(few, volume_count=3, chapter_total=4) == []
    # Releases that already carry a volume are not numbered volumes.
    with_volume = [release("mangapill", str(n), volume="1") for n in range(1, 35)]
    assert (
        volume_numbered_providers(with_volume, volume_count=34, chapter_total=103) == []
    )
    # Reclassified releases are never reclassified again.
    done = [
        {**release("mangapill", str(n)), "release_unit": "volume"} for n in range(1, 35)
    ]
    assert volume_numbered_providers(done, volume_count=34, chapter_total=103) == []


def test_explicit_webtoon_chapters_are_not_books_even_with_many_tiles():
    manga = {"id": "orv", "series_unit_override": None}
    metadata = {"content_kind": "webtoon", "volume_count": 20}
    releases = [
        {
            "id": "official-17",
            "release_unit": "volume",
            "volume": "17",
            "title": "Episode 17 (ch. 17)",
            "pages": 160,
        },
        {
            "id": "mirror-17",
            "release_unit": "volume",
            "volume": "17",
            "title": "Chapter 17 - Line of Hypocrisy (Part 2)",
            "pages": 128,
        },
        {
            "id": "printed-2",
            "release_unit": "volume",
            "volume": "2",
            "title": "Volume 2",
            "pages": 240,
        },
    ]
    assert misclassified_explicit_chapters(manga, metadata, releases) == {
        "official-17": "17",
        "mirror-17": "17",
    }
    manga["series_unit_override"] = "volumes"
    assert misclassified_explicit_chapters(manga, metadata, releases) == {}


def test_webtoon_season_title_keeps_the_global_source_chapter():
    release = {
        "id": "season-three-1",
        "release_unit": "volume",
        "volume": "418",
        "source_chapter": "418",
        "title": "S3 - Chapter 1",
        "pages": 150,
    }

    assert misclassified_explicit_chapters(
        {"id": "webtoon", "series_unit_override": None},
        {"content_kind": "webtoon"},
        [release],
    ) == {"season-three-1": "418"}


def test_long_printed_chapter_with_a_volume_hint_is_not_a_book():
    releases = [
        {
            "id": "mirror-21",
            "release_unit": "volume",
            "volume": "21",
            "source_chapter": "21",
            "title": "Chapter 21 - Volume 2 Part 1",
            "pages": 263,
        },
        {
            "id": "actual-volume",
            "release_unit": "volume",
            "volume": "2",
            "source_chapter": "2",
            "title": "Volume 2",
            "pages": 485,
        },
    ]

    assert misclassified_explicit_chapters(
        {"id": "printed", "series_unit_override": None},
        {"content_kind": "manga"},
        releases,
    ) == {"mirror-21": "21"}


def test_printed_books_named_chapter_are_not_restored_without_independent_evidence():
    releases = [
        {
            "id": "book-1",
            "release_unit": "volume",
            "volume": "1",
            "source_chapter": "1",
            "title": "Chapter 1",
            "pages": 294,
        },
        {
            "id": "book-2",
            "release_unit": "volume",
            "volume": "2",
            "source_chapter": "2",
            "title": "Chapter 2",
            "pages": 238,
        },
    ]

    assert (
        misclassified_explicit_chapters(
            {"id": "printed", "series_unit_override": None},
            {"content_kind": "manga"},
            releases,
        )
        == {}
    )


@pytest.mark.parametrize("pages", [19, 44, 90])
async def test_short_chapters_repair_old_book_guess_and_survive_source_refresh(
    tmp_path, pages
):
    settings = Settings(
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "library",
        metadata_enabled=False,
        monitor_enabled=False,
    )
    settings.ensure_directories()
    database = Database(settings.database_path)
    database.initialize()
    database.upsert_manga(
        {"id": "m", "title": "Example", "description": "", "authors": []}, "en"
    )
    row = {
        "id": "chapter-3",
        "provider": "suwayomi",
        "chapter": "3",
        "source_chapter": "3",
        "title": "Chapter 3",
        "pages": pages,
        "language": "en",
        "groups": [],
        "publish_at": None,
        "source_url": "",
    }
    database.upsert_chapters("m", [row])
    assert database.set_release_book("chapter-3", "3")
    service = TankarrService(settings, database, object(), object())  # type: ignore[arg-type]

    assert await service.reconcile_persisted_explicit_chapters() == {
        "series_checked": 1,
        "restored": 1,
    }
    database.upsert_chapters("m", [row])
    assert await service.reconcile_persisted_explicit_chapters() == {
        "series_checked": 1,
        "restored": 0,
    }
    restored = database.get_chapter("chapter-3")
    assert restored["release_unit"] == "chapter"
    assert restored["chapter"] == restored["canonical_chapter"] == "3"
    assert restored["volume"] is None


@pytest.mark.parametrize(
    "fields",
    [
        {"pages": None},
        {"pages": -1},
        {"pages": 0},
        {"pages": 91},
        {"pages": 210},
        {"pages": 22, "source_chapter": "30"},
        {"pages": 22, "title": "Volume 3"},
        {"pages": 22, "title": "Chapter 3 - Volume 3"},
    ],
)
def test_short_chapter_repair_requires_independent_unambiguous_evidence(fields):
    row = {
        "id": "release",
        "release_unit": "volume",
        "volume": "3",
        "source_chapter": "3",
        "title": "Chapter 3",
        **fields,
    }
    assert misclassified_explicit_chapters({}, {}, [row]) == {}


async def test_persisted_explicit_chapters_are_repaired_before_organization(tmp_path):
    settings = Settings(
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "library",
        metadata_enabled=False,
        monitor_enabled=False,
    )
    settings.ensure_directories()
    database = Database(settings.database_path)
    database.initialize()
    database.upsert_manga(
        {
            "id": "printed",
            "title": "Printed Series",
            "description": "",
            "authors": [],
            "series_unit_override": None,
        },
        "en",
    )
    database.upsert_chapters(
        "printed",
        [
            {
                "id": "mirror-21",
                "provider": "suwayomi",
                "chapter": "21",
                "volume": None,
                "title": "Chapter 21 - Volume 2 Part 1",
                "language": "en",
                "groups": [],
                "publish_at": None,
                "source_url": "",
            }
        ],
    )
    assert database.set_release_book("mirror-21", "21") is True
    service = TankarrService(settings, database, object(), object())  # type: ignore[arg-type]

    result = await service.reconcile_persisted_explicit_chapters()

    restored = database.get_chapter("mirror-21")
    assert result == {"series_checked": 1, "restored": 1}
    assert restored["release_unit"] == "chapter"
    assert restored["chapter"] == "21"
    assert restored["volume"] is None


async def test_long_explicit_chapter_is_not_reclassified_again(tmp_path, monkeypatch):
    settings = Settings(
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "library",
        metadata_enabled=False,
        monitor_enabled=False,
    )
    settings.ensure_directories()
    identity = "0123456789abcdef0123456789abcdef"
    for root in (settings.data_dir, settings.library_dir):
        (root / ".tankarr-library-id").write_text(f"{identity}\n", encoding="utf-8")
    database = Database(settings.database_path)
    database.initialize()
    database.upsert_manga(
        {
            "id": "printed",
            "title": "Printed Series",
            "description": "",
            "authors": [],
            "series_unit_override": None,
        },
        "en",
    )
    database.save_series_metadata(
        "printed",
        {"content_kind": "manga", "volume_count": 18, "chapter_count": 54},
        artwork_path=None,
        artwork_sha256=None,
        artwork_media_type=None,
        source_status=[],
    )
    database.upsert_chapters(
        "printed",
        [
            {
                "id": "mirror-21",
                "provider": "suwayomi",
                "chapter": "21",
                "source_chapter": "21",
                "volume": None,
                "title": "Chapter 21 - Volume 2 Part 1",
                "language": "en",
                "groups": [],
                "publish_at": None,
                "source_url": "",
                "pages": 263,
            }
        ],
    )
    service = TankarrService(settings, database, object(), object())  # type: ignore[arg-type]

    get_manga = Mock(wraps=database.get_manga)
    monkeypatch.setattr(database, "get_manga", get_manga)
    assert await service._reconcile_release_units("printed") == []
    assert await service._reconcile_release_units("printed") == []
    # Series identity is invariant during a pass. Re-reading it for every
    # release made catalogue refresh quadratic and blocked the HTTP event loop.
    assert get_manga.call_count == 2
    restored = database.get_chapter("mirror-21")
    assert restored["release_unit"] == "chapter"
    assert restored["chapter"] == "21"
    assert restored["volume"] is None


async def test_season_local_title_repair_preserves_global_webtoon_file(tmp_path):
    settings = Settings(
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "library",
        metadata_enabled=False,
        monitor_enabled=False,
    )
    settings.ensure_directories()
    identity = "0123456789abcdef0123456789abcdef"
    for root in (settings.data_dir, settings.library_dir):
        (root / ".tankarr-library-id").write_text(f"{identity}\n", encoding="utf-8")
    database = Database(settings.database_path)
    database.initialize()
    database.upsert_manga(
        {
            "id": "tower",
            "title": "Tower",
            "description": "",
            "authors": [],
            "series_unit_override": None,
        },
        "en",
    )
    database.save_series_metadata(
        "tower",
        {"content_kind": "webtoon", "chapter_count": 650},
        artwork_path=None,
        artwork_sha256=None,
        artwork_media_type=None,
        source_status=[],
    )
    releases = [
        {
            "id": "chapter-1",
            "provider": "suwayomi",
            "chapter": "1",
            "source_chapter": "1",
            "volume": None,
            "title": "Chapter 1",
            "language": "en",
            "groups": [],
            "publish_at": None,
            "source_url": "",
            "pages": 80,
        },
        {
            "id": "season-three-1",
            "provider": "suwayomi",
            "chapter": "418",
            "source_chapter": "418",
            "volume": None,
            "title": "S3 - Chapter 1",
            "language": "en",
            "groups": [],
            "publish_at": None,
            "source_url": "",
            "pages": 175,
        },
    ]
    database.upsert_chapters("tower", releases)
    series = settings.library_dir / "Tower"
    series.mkdir()
    chapter_one = series / "Tower - c001 [en].cbz"
    season_chapter = series / "Tower - cUnknown-season-three-1 [en].cbz"
    chapter_one.write_bytes(b"chapter one")
    season_chapter.write_bytes(b"season three chapter one")
    database.mark_chapter_downloaded("chapter-1", chapter_one, sha256(chapter_one))
    database.mark_chapter_downloaded(
        "season-three-1", season_chapter, sha256(season_chapter)
    )
    assert database.set_release_book("season-three-1", "418") is True
    service = TankarrService(settings, database, object(), object())  # type: ignore[arg-type]

    repaired = await service.reconcile_persisted_explicit_chapters()
    organized = await service.organize_library(reconcile_reader=False)

    restored = database.get_chapter("season-three-1")
    wanted = settings.library_dir / "Tower (Unknown Author)" / "Tower - c418 [en].cbz"
    assert repaired == {"series_checked": 1, "restored": 1}
    assert organized["organization_blocked"] is False
    assert organized["duplicates_removed"] == 0
    assert restored["chapter"] == "418"
    assert restored["source_chapter"] == "418"
    assert restored["downloaded"] is True
    assert restored["library_path"] == str(wanted)
    stored_one = database.get_chapter("chapter-1")
    assert Path(stored_one["library_path"]).read_bytes() == b"chapter one"
    assert wanted.read_bytes() == b"season three chapter one"
