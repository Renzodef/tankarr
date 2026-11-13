from __future__ import annotations

import zipfile

import pytest

from tankarr.assembly_batch import BookAssemblyBatch
from tankarr.book_policy import completed_volume_count
from tankarr.chapter_map import MapEntry
from tankarr.database import Database
from tankarr.series_unit import resolve_series_unit
from tankarr.unit_strategy import decide_unit_strategy
from tests.test_assemble import complete_book as complete_book
from tests.test_deletion import chapter
from tests.test_translation import translation as translation

METADATA = {
    "status": "ended",
    "volume_count": 13,
    "chapter_count": 56,
    "count_confidence": {"volume": "agreed", "chapter": "agreed"},
}


def completed(database):
    database.update_manga(
        "manga-1", {"series_unit_override": None, "series_unit_strategy": None}
    )
    database.save_series_metadata(
        "manga-1",
        METADATA,
        artwork_path=None,
        artwork_sha256=None,
        artwork_media_type=None,
        source_status=[],
    )


@pytest.mark.parametrize(
    "releases", [[], [chapter("c1", "1")], [chapter("c1", "1") | {"downloaded": True}]]
)
def test_finished_edition_targets_books_independently_of_current_sources(releases):
    assert resolve_series_unit({}, METADATA, releases)["unit"] == "volumes"
    decision = decide_unit_strategy(
        manga={},
        metadata=METADATA,
        releases=releases,
        translation_enabled=False,
        auto_unit_strategy=True,
    )
    assert decision.unit == "volumes"
    assert decision.chapter_fallback
    assert decision.volume_expected == 13


@pytest.mark.parametrize(
    "changes",
    [
        {"status": "continuing"},
        {"status": "hiatus"},
        {"status": "unknown"},
        {"content_kind": "webtoon"},
        {"reading_direction": "WEBTOON"},
        {"volume_count": None},
        {"volume_count": 0},
        {"volume_count": True},
        {"count_confidence": {"volume": "lone"}},
        {"count_confidence": {"volume": "conflict"}},
    ],
)
def test_uncertain_unfinished_or_scroll_native_work_has_no_automatic_book_target(
    changes,
):
    assert completed_volume_count({}, METADATA | changes) is None


def test_printed_manhwa_and_confirmed_edition_override_are_eligible():
    metadata = METADATA | {"classification": {"subtype": "manhwa"}}
    assert completed_volume_count({}, metadata) == 13
    assert (
        completed_volume_count(
            {"edition_book_count": 8},
            metadata | {"count_confidence": {"volume": "lone"}},
        )
        == 8
    )
    assert (
        resolve_series_unit({"series_unit_override": "chapters"}, metadata, [])["unit"]
        == "chapters"
    )


@pytest.mark.asyncio
async def test_default_assembles_real_book_once_and_preserves_page_order(complete_book):
    database, service, assembler, paths = complete_book
    completed(database)
    originals = [path.read_bytes() for path in paths]
    result = await BookAssemblyBatch(assembler).run_automatic()
    assert result == {"assembled": 1, "errors": []}
    books = [r for r in database.list_chapters("manga-1", "en") if r["downloaded"]]
    assert len(books) == 1
    assert books[0]["volume"] == "12"
    assert books[0]["assembled_from"]["chapters"] == ["55", "55.5", "56"]
    path = service.settings.library_dir / books[0]["library_path"]
    with zipfile.ZipFile(path) as archive:
        pages = [
            archive.read(name)
            for name in archive.namelist()
            if name.endswith((".jpg", ".png"))
        ]
    assert pages == [
        f"{n}:{side}".encode()
        for n in ("55", "55.5", "56")
        for side in ("first", "second")
    ]
    assert all(not path.exists() for path in paths)
    # Retirement keeps the original archives in the existing recycle bin.
    recycled = list(service.settings.library_dir.rglob("*.quarantined"))
    assert all(
        any(path.read_bytes() == data for path in recycled) for data in originals
    )
    assert await BookAssemblyBatch(assembler).run_automatic() == {
        "assembled": 0,
        "errors": [],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "updates",
    [
        {"assemble_books_automatically": False},
        {"book_assembly_mode": "off"},
        {"series_unit_override": "chapters"},
        {"monitor_mode": "none"},
        {"status_override": "ended"},
        {"library_status_override": "up_to_date"},
    ],
)
async def test_explicit_choices_and_pauses_are_respected(complete_book, updates):
    database, _service, assembler, paths = complete_book
    completed(database)
    database.update_manga("manga-1", updates)
    assert await BookAssemblyBatch(assembler).run_automatic() == {
        "assembled": 0,
        "errors": [],
    }
    assert all(path.exists() for path in paths)


@pytest.mark.asyncio
async def test_incomplete_book_is_not_assembled_but_its_missing_chapter_is_queued(
    complete_book,
):
    database, service, assembler, paths = complete_book
    completed(database)
    with database.connect() as connection:
        connection.execute(
            "UPDATE chapter_release SET downloaded=0, library_path=NULL WHERE id='chapter-56'"
        )
    assert await BookAssemblyBatch(assembler).run_automatic() == {
        "assembled": 0,
        "errors": [],
    }
    jobs = await service.create_missing_download_jobs("manga-1")
    assert [job["chapter_id"] for job in jobs] == ["chapter-56"]
    assert database.get_chapter("chapter-56")["monitored"]
    assert all(path.exists() for path in paths)
    service.apply_unit_strategy("manga-1")
    assert database.get_job(jobs[0]["id"])["status"] == "queued"


@pytest.mark.asyncio
async def test_book_offer_and_its_pending_job_take_precedence_over_assembly(
    complete_book,
):
    database, service, assembler, paths = complete_book
    completed(database)
    book = chapter("whole-12", "", volume="12") | {
        "chapter": None,
        "release_unit": "volume",
        "title": "Volume 12",
    }
    database.upsert_chapters("manga-1", [book])
    assert await BookAssemblyBatch(assembler).run_automatic() == {
        "assembled": 0,
        "errors": [],
    }
    assert all(path.exists() for path in paths)
    jobs = await service.create_missing_download_jobs("manga-1")
    assert [job["chapter_id"] for job in jobs] == ["whole-12"]
    assert await BookAssemblyBatch(assembler).run_automatic() == {
        "assembled": 0,
        "errors": [],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source,exact", [("provider", True), ("operator", False), ("estimated", True)]
)
async def test_hints_never_authorize_default_assembly(complete_book, source, exact):
    database, _service, assembler, paths = complete_book
    completed(database)
    database.replace_chapter_map("manga-1", "operator", [])
    database.replace_chapter_map(
        "manga-1",
        source,
        [MapEntry(("12",), ("55", "55.5", "56"), exact, source=source)],
    )
    assert await BookAssemblyBatch(assembler).run_automatic() == {
        "assembled": 0,
        "errors": [],
    }
    assert all(path.exists() for path in paths)


def test_schema_upgrade_is_idempotent_and_preserves_legacy_opt_in(complete_book):
    database, _service, _assembler, _paths = complete_book
    with database.connect() as connection:
        connection.execute("ALTER TABLE manga DROP COLUMN book_assembly_mode")
        connection.execute("UPDATE manga SET assemble_books_automatically=1")
        connection.execute("PRAGMA user_version=10")
    for _ in range(2):
        Database(database.path).initialize()
        row = database.get_manga("manga-1")
        assert row["book_assembly_mode"] == "automatic"
        assert row["assemble_books_automatically"]
    database.update_manga("manga-1", {"book_assembly_mode": "automatic"})
    assert not database.get_manga("manga-1")["assemble_books_automatically"]


@pytest.mark.parametrize(
    "changes",
    [
        {"status": "ongoing"},
        {"content_kind": "webtoon"},
        {"count_confidence": {"volume": "conflict"}},
    ],
)
def test_corrected_metadata_releases_only_the_automatic_unit_choice(
    complete_book, changes
):
    database, service, _assembler, paths = complete_book
    completed(database)
    assert service.apply_unit_strategy("manga-1")["action"] == "switch"
    database.save_series_metadata(
        "manga-1",
        METADATA | changes,
        artwork_path=None,
        artwork_sha256=None,
        artwork_media_type=None,
        source_status=[],
    )
    assert service.apply_unit_strategy("manga-1")["action"] == "reset"
    assert database.get_manga("manga-1")["series_unit_override"] is None
    assert all(path.exists() for path in paths)
    database.update_manga("manga-1", {"series_unit_override": "volumes"})
    assert service.apply_unit_strategy("manga-1")["action"] == "stay"
    assert database.get_manga("manga-1")["series_unit_override"] == "volumes"


@pytest.mark.asyncio
async def test_global_automatic_strategy_off_prevents_default_assembly(complete_book):
    database, service, assembler, paths = complete_book
    completed(database)
    service.settings.auto_unit_strategy = False
    assert await BookAssemblyBatch(assembler).run_automatic() == {
        "assembled": 0,
        "errors": [],
    }
    assert all(path.exists() for path in paths)


@pytest.mark.asyncio
async def test_default_does_not_assemble_an_extra_beyond_the_confirmed_edition(
    complete_book,
):
    database, _service, assembler, paths = complete_book
    completed(database)
    database.replace_chapter_map(
        "manga-1",
        "operator",
        [MapEntry(("14",), ("55", "55.5", "56"), True, source="operator")],
    )
    assert await BookAssemblyBatch(assembler).run_automatic() == {
        "assembled": 0,
        "errors": [],
    }
    assert all(path.exists() for path in paths)


@pytest.mark.asyncio
async def test_exact_ocr_map_also_supplies_missing_chapters(complete_book):
    database, service, _assembler, _paths = complete_book
    completed(database)
    database.replace_chapter_map("manga-1", "operator", [])
    database.replace_chapter_map(
        "manga-1", "ocr", [MapEntry(("12",), ("55", "55.5", "56"), True, source="ocr")]
    )
    with database.connect() as connection:
        connection.execute(
            "UPDATE chapter_release SET downloaded=0, library_path=NULL WHERE id='chapter-56'"
        )
    jobs = await service.create_missing_download_jobs("manga-1")
    assert [job["chapter_id"] for job in jobs] == ["chapter-56"]


@pytest.mark.asyncio
@pytest.mark.parametrize("whole", [False, True])
async def test_translation_prefers_whole_book_then_its_exact_chapters(
    translation, whole
):
    manager, database, _service, source = translation
    database.save_series_metadata(
        "manga-1",
        METADATA | {"volume_count": 1, "chapter_count": 2},
        artwork_path=None,
        artwork_sha256=None,
        artwork_media_type=None,
        source_status=[],
    )
    database.replace_chapter_map(
        "manga-1", "operator", [MapEntry(("1",), ("1", "2"), True, source="operator")]
    )
    rows = [chapter("ja-2", "2", language="ja") | {"provider": "suwayomi"}]
    if whole:
        rows.append(
            source
            | {
                "id": "ja-v1",
                "chapter": None,
                "volume": "1",
                "release_unit": "volume",
                "title": "Volume 1",
            }
        )
    database.upsert_chapters("manga-1", rows, monitor_new=False)
    assert await manager._queue_sources("manga-1", ["ja"], limit=10) == (
        1 if whole else 2
    )
    assert manager.store.slots("manga-1", "en") == (
        {"volume:1"} if whole else {"chapter:1", "chapter:2"}
    )
    assert await manager._queue_sources("manga-1", ["ja"], limit=10) == 0
