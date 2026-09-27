from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from tankarr.chapter_map import MapEntry
from tankarr.chapter_mapping import build_chapter_index
from tankarr.config import Settings
from tankarr.database import Database
from tankarr.monitor import ReleaseMonitor
from tests.test_monitor import FakeService, FakeTorrents, RecordingWorker
from tests.test_queue_management import seed_manga


def chapter(number):
    return {
        "id": f"chapter-{number}",
        "chapter": str(number),
        "volume": None,
        "title": f"Chapter {number}",
        "language": "en",
        "provider": "mangadex",
        "pages": 20,
        "groups": [],
        "publish_at": "2026-01-02T00:00:00Z",
        "source_url": f"https://example.test/c{number}",
    }


def book(number):
    return {
        **chapter(number),
        "id": f"book-{number}",
        "chapter": None,
        "volume": str(number),
        "release_unit": "volume",
        "provider": "prowlarr",
        "title": f"Volume {number}",
        "pages": 200,
    }


def prepare(tmp_path, status, *, mapped=True):
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    seed_manga(database)
    database.save_series_metadata(
        "manga-1",
        {"status": status, "volume_count": 2, "chapter_count": 5},
        artwork_path=None,
        artwork_sha256=None,
        artwork_media_type=None,
        source_status=[],
    )
    if mapped:
        database.replace_chapter_map(
            "manga-1",
            "operator",
            [
                MapEntry(("1",), ("1", "2"), True, "operator"),
                MapEntry(("2",), ("3", "4"), True, "operator"),
            ],
        )
    return database


@pytest.mark.parametrize("status", ["ongoing", "hiatus", "on_hiatus", "paused"])
@pytest.mark.parametrize("mapped", [True, False])
def test_current_books_do_not_close_a_continuing_chapter_search(
    tmp_path: Path, status, mapped
):
    database = prepare(tmp_path, status, mapped=mapped)
    database.upsert_chapters(
        "manga-1", [book(1), book(2), *(chapter(n) for n in range(1, 6))]
    )
    for number in (1, 2):
        database.set_release_book(f"book-{number}", str(number))
        path = tmp_path / f"v{number}.cbz"
        path.write_bytes(b"book")
        database.mark_chapter_downloaded(f"book-{number}", path, "0" * 64)

    wanted = database.preferred_download_candidates("manga-1")
    expected = {"chapter-5"} if mapped else {f"chapter-{n}" for n in range(1, 6)}
    assert {row["id"] for row in wanted} == expected

    index = build_chapter_index(
        database.get_manga("manga-1"),
        database.get_series_metadata("manga-1")["data"],
        database.list_chapters("manga-1", "en"),
        chapter_map=database.chapter_map("manga-1"),
    )
    tail = next(slot for slot in index["slots"] if slot["chapter"] == "5")
    assert tail["expected"]
    assert not tail["covered_unmapped"]
    assert tail["covered_by_volume"] is None
    assert index["raw_missing_count"] == len(expected)


@pytest.mark.parametrize("status", ["ongoing", "hiatus"])
@pytest.mark.parametrize("mapped", [True, False])
def test_source_books_fill_gaps_without_replacing_available_chapters(
    tmp_path: Path, status, mapped
):
    database = prepare(tmp_path, status, mapped=mapped)
    database.upsert_chapters("manga-1", [chapter(1), chapter(2), book(1), book(2)])
    for number in (1, 2):
        database.set_release_book(f"book-{number}", str(number))
    candidates = database.preferred_download_candidates("manga-1")
    assert {row["id"] for row in candidates} == (
        {"chapter-1", "chapter-2", "book-2"}
        if mapped
        else {"chapter-1", "chapter-2", "book-1", "book-2"}
    )

    database.upsert_chapters("manga-1", [chapter(n) for n in (3, 4, 5)])
    candidates = database.preferred_download_candidates("manga-1")
    assert {row["id"] for row in candidates} == {f"chapter-{n}" for n in range(1, 6)}
    database.block_release("chapter-4", reason="No usable pages")
    candidates = database.preferred_download_candidates("manga-1")
    assert "book-2" in {row["id"] for row in candidates}
    assert next(row for row in candidates if row["id"] == "chapter-4")["blocked"]
    assert not next(row for row in candidates if row["id"] == "book-2")["blocked"]


@pytest.mark.asyncio
async def test_book_hunt_waits_for_available_chapters_and_queued_source_books(
    tmp_path: Path, monkeypatch
):
    import tankarr.volume_hunt as volume_hunt

    database = prepare(tmp_path, "ongoing")
    database.upsert_chapters("manga-1", [chapter(1), chapter(2), book(2)])
    database.set_release_book("book-2", "2")
    settings = Settings(data_dir=tmp_path / "data", library_dir=tmp_path / "library")
    monitor = ReleaseMonitor(
        settings, database, FakeService(database, []), RecordingWorker()
    )
    monitor.torrents = FakeTorrents()
    hunt = AsyncMock(return_value={"grabbed": [], "errors": []})
    monkeypatch.setattr(volume_hunt, "hunt_volumes", hunt)
    entry = {
        "manga": database.get_manga("manga-1"),
        "chapters": [chapter(1), chapter(2)],
    }
    await monitor._hunt_missing_volumes(entry)
    hunt.assert_not_awaited()

    # The snapshot predates queue_missing: consult the current source jobs.
    database.create_job("manga-1", "book-2", "en")
    entry["chapters"] = [{**chapter(3), "expected": True, "provider": "expected"}]
    result = await monitor._hunt_missing_volumes(entry, chapter_fallback=True)
    hunt.assert_not_awaited()
    assert result["pending_volumes"] == [2]
    monitor.torrents.search = AsyncMock(return_value={"results": []})
    await monitor._recover_one_chapter(entry["manga"], entry["chapters"][0])
    hunt.assert_not_awaited()
    assert any(
        attempt["channel"] == "indexer_book" and attempt["outcome"] == "pending"
        for attempt in database.wanted_attempts("manga-1")["chapter:3"]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("mapped", [True, False])
async def test_indexers_try_chapters_before_books(tmp_path, monkeypatch, mapped):
    import tankarr.volume_hunt as volume_hunt

    database = prepare(tmp_path, "hiatus", mapped=mapped)
    settings = Settings(data_dir=tmp_path / "data", library_dir=tmp_path / "library")
    monitor = ReleaseMonitor(
        settings, database, FakeService(database, []), RecordingWorker()
    )
    events = []

    async def search(*args, **kwargs):
        events.append("chapter")
        return {"results": []}

    async def hunt(*args, **kwargs):
        events.append("book")
        return {"grabbed": [], "errors": [], "searched_volumes": [1, 2]}

    monitor.torrents = FakeTorrents()
    monitor.torrents.search = search
    monkeypatch.setattr(volume_hunt, "hunt_volumes", hunt)
    manga = database.get_manga("manga-1")
    row = {**chapter(3), "expected": True, "provider": "expected"}
    await monitor._hunt_missing_volumes({"manga": manga, "chapters": [row]})
    assert events == []
    await monitor._recover_one_chapter(manga, row)
    assert events[0] == "chapter" and events[-1] == "book"
    assert events.count("book") == 1
