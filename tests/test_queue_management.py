from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from tankarr.config import Settings
from tankarr.database import Database
from tankarr.komga import KomgaClient
from tankarr.providers.base import ProviderRequestError, ProviderUnavailableError
from tankarr.service import TankarrService
from tankarr.source_ranking import SourceRanking
from tankarr.worker import DownloadWorker


def seed_manga(database: Database, monitor_mode: str = "all") -> None:
    database.upsert_manga(
        {
            "id": "manga-1",
            "provider": "mangadex",
            "title": "Example",
            "description": "",
            "cover_url": "/api/covers/mangadex/manga-1/cover.jpg",
            "authors": ["Author"],
            "original_language": "ja",
            "status": "ongoing",
            "year": 2026,
            "last_volume": None,
            "last_chapter": None,
            "available_languages": ["en"],
            "source_url": "https://mangadex.org/title/manga-1",
        },
        "en",
        monitor_mode,
    )
    database.upsert_chapters(
        "manga-1",
        [
            {
                "id": "chapter-1",
                "chapter": "1",
                "volume": "1",
                "title": "One",
                "language": "en",
                "provider": "mangadex",
                "groups": [],
                "publish_at": "2026-01-01T00:00:00Z",
                "source_url": "https://example.test/1",
            }
        ],
    )


def build_service(tmp_path: Path, database: Database) -> TankarrService:
    settings = Settings(data_dir=tmp_path / "data", library_dir=tmp_path / "library")
    return TankarrService(settings, database, object(), KomgaClient(settings))


@pytest.mark.asyncio
async def test_import_sync_uses_reader_specific_narrow_scan(tmp_path: Path):
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    settings = Settings(data_dir=tmp_path / "data", library_dir=tmp_path / "library")
    settings.ensure_directories()
    identity = "0123456789abcdef0123456789abcdef"
    (settings.data_dir / ".tankarr-library-id").write_text(identity, encoding="utf-8")
    (settings.library_dir / ".tankarr-library-id").write_text(
        identity, encoding="utf-8"
    )

    class NarrowReader:
        configured = True
        standalone = False

        def __init__(self):
            self.paths: list[str] = []

        async def sync_imported_path(self, relative_path: str) -> dict:
            self.paths.append(relative_path)
            return {"configured": True, "triggered": True, "scan_scope": "series"}

    reader = NarrowReader()
    service = TankarrService(settings, database, object(), reader)  # type: ignore[arg-type]
    book = settings.library_dir / "Series (Author)" / "Series - c001 [en].cbz"

    result = await service._sync_imported_path_with_komga(book)

    assert reader.paths == ["Series (Author)/Series - c001 [en].cbz"]
    assert result["scan_scope"] == "series"


@pytest.mark.asyncio
async def test_full_komga_refresh_is_periodic_coalesced_and_republishes_metadata(
    tmp_path: Path,
):
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    seed_manga(database)
    settings = Settings(
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "library",
        komga_refresh_interval_minutes=15,
    )
    settings.ensure_directories()
    identity = "0123456789abcdef0123456789abcdef"
    (settings.data_dir / ".tankarr-library-id").write_text(identity, encoding="utf-8")
    (settings.library_dir / ".tankarr-library-id").write_text(
        identity, encoding="utf-8"
    )
    downloaded = settings.library_dir / "file.cbz"
    downloaded.write_bytes(b"book")
    database.mark_chapter_downloaded("chapter-1", downloaded, "0" * 64)

    class RefreshKomga:
        configured = True
        standalone = False

        def __init__(self):
            self.scans: list[tuple[str, ...]] = []

        async def scan(self, expected):
            self.scans.append(tuple(expected))
            await asyncio.sleep(0.01)
            return {
                "configured": True,
                "triggered": True,
                "expected_books": len(tuple(expected)),
                "matched_expected_books": len(tuple(expected)),
            }

    komga = RefreshKomga()
    service = TankarrService(settings, database, object(), komga)  # type: ignore[arg-type]
    metadata_syncs = 0

    async def sync_metadata() -> dict:
        nonlocal metadata_syncs
        metadata_syncs += 1
        return {"complete": True}

    service.set_komga_refresh_post_scan(sync_metadata)
    service._komga_refresh_completed_monotonic = 100
    assert service.komga_periodic_refresh_due(now=999.9) is False
    assert service.komga_periodic_refresh_due(now=1000) is True

    first, second = await asyncio.gather(
        service.refresh_komga_library(reason="periodic"),
        service.refresh_komga_library(reason="download_queue_drained"),
    )

    assert first["ready"] is True
    assert second["coalesced"] is True
    assert second["requested_reason"] == "download_queue_drained"
    assert komga.scans == [("file.cbz",)]
    assert metadata_syncs == 1
    assert service.komga_refresh_status()["last_result"]["refresh_reason"] == (
        "periodic"
    )


def test_wanted_lists_every_monitored_release_missing_from_the_library(tmp_path: Path):
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    seed_manga(database, monitor_mode="all")
    service = build_service(tmp_path, database)

    wanted = service.list_wanted()
    assert len(wanted) == 1
    assert wanted[0]["manga"]["id"] == "manga-1"
    assert [chapter["id"] for chapter in wanted[0]["chapters"]] == ["chapter-1"]

    database.configure_monitor_mode("manga-1", "future")
    assert service.list_wanted() == []


def test_a_monitored_series_no_source_lists_still_appears_in_wanted(tmp_path: Path):
    # Measured live: a series whose every source vanished, with no catalogue
    # count, had nothing to put in a row - and disappeared from Wanted while
    # its library stayed empty.
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    seed_manga(database, monitor_mode="all")
    database.upsert_manga(
        {
            "id": "manga-2",
            "provider": "catalogue",
            "title": "Nobody lists it",
            "description": "",
            "cover_url": None,
            "authors": [],
            "original_language": "ja",
            "status": "ended",
            "year": 1990,
            "last_volume": None,
            "last_chapter": None,
            "available_languages": ["en"],
            "source_url": "",
        },
        "en",
        "all",
    )
    service = build_service(tmp_path, database)

    entries = {entry["manga"]["id"]: entry for entry in service.list_wanted()}
    assert entries["manga-1"].get("no_sources") is False
    orphan = entries["manga-2"]
    assert orphan["no_sources"] is True
    assert orphan["chapters"] == []
    assert orphan["recovery"]["verdict"] == "unsearched"

    database.record_wanted_attempt(
        "manga-2", "series", channel="sources", outcome="not_offered", detail="none"
    )
    database.record_wanted_attempt(
        "manga-2",
        "series",
        channel="indexer_book",
        outcome="not_offered",
        detail="none",
    )
    orphan = {e["manga"]["id"]: e for e in service.list_wanted()}["manga-2"]
    assert orphan["recovery"]["verdict"] == "exhausted"

    database.configure_monitor_mode("manga-2", "none")
    assert "manga-2" not in {e["manga"]["id"] for e in service.list_wanted()}

    database.configure_monitor_mode("manga-1", "existing")
    assert [chapter["id"] for chapter in service.list_wanted()[0]["chapters"]] == [
        "chapter-1"
    ]
    database.mark_chapter_downloaded(
        "chapter-1", tmp_path / "library" / "file.cbz", "0" * 64
    )
    assert service.list_wanted() == []


def test_wanted_keeps_active_missing_releases_visible_without_requeueing(
    tmp_path: Path,
):
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    seed_manga(database, monitor_mode="all")
    service = build_service(tmp_path, database)
    job = database.create_job("manga-1", "chapter-1", "en")

    wanted = service.list_wanted()
    assert wanted[0]["chapters"][0]["queue_job_id"] == job["id"]
    assert wanted[0]["chapters"][0]["queue_status"] == "queued"
    assert database.preferred_download_candidates("manga-1") == []


@pytest.mark.asyncio
async def test_official_frontier_filters_verified_early_chapter_only_at_acquisition(
    tmp_path: Path,
):
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    database.upsert_manga(
        {
            "id": "frontier",
            "title": "Frontier",
            "description": "",
            "cover_url": None,
            "authors": [],
            "original_language": "en",
            "status": "ongoing",
            "available_languages": ["en"],
        },
        "en",
        "all",
    )

    def save_metadata(status: str, chapter_count: int | None = None) -> None:
        database.save_series_metadata(
            "frontier",
            {
                "status": status,
                "chapter_count": chapter_count,
                "official_links": [
                    {
                        "url": "https://www.webtoons.com/en/example/list",
                        "language": "en",
                    }
                ],
            },
            artwork_path=None,
            artwork_sha256=None,
            artwork_media_type=None,
            source_status=[],
        )

    save_metadata("ongoing")
    official = [
        {
            "id": f"official-{number}",
            "chapter": str(number),
            "volume": None,
            "title": f"Chapter {number}",
            "language": "en",
            "provider": "suwayomi",
            "source_key": "suwayomi:webtoons",
            "source_name": "Webtoons.com (EN)",
            "groups": [],
            "publish_at": None,
            "source_url": f"https://www.webtoons.com/en/example/{number}",
        }
        for number in range(1, 4)
    ]
    mirror = [
        {
            **official[0],
            "id": f"mirror-{number}",
            "chapter": str(number),
            "title": f"Chapter {number}",
            "source_key": "suwayomi:mirror",
            "source_name": "Mirror (EN)",
            "source_url": f"https://mirror.example/{number}",
        }
        for number in range(1, 7)
    ]
    database.upsert_chapters("frontier", [*official, *mirror])
    assert database.get_chapter("mirror-6")["numbering_method"] == (
        "verified_identity_continuation"
    )
    for number in range(1, 4):
        database.mark_chapter_downloaded(
            f"official-{number}", tmp_path / f"chapter-{number}.cbz"
        )

    service = build_service(tmp_path, database)

    def database_chapters() -> list[str]:
        return [
            item["chapter"] for item in database.preferred_missing_releases("frontier")
        ]

    def wanted_chapters() -> list[str]:
        wanted = service.list_wanted()
        return [
            item["chapter"]
            for series in wanted
            for item in series.get("chapters", [])
            if item.get("provider") != "expected"
        ]

    assert database_chapters() == []
    assert wanted_chapters() == []

    database.source_ranking = SourceRanking(acquisition_policy="first_available")
    assert database_chapters() == ["4", "5", "6"]
    assert wanted_chapters() == ["4", "5", "6"]

    stale = database.create_job("frontier", "mirror-4", "en")
    database.source_ranking = SourceRanking(acquisition_policy="prefer_official")
    assert database_chapters() == []
    service.settings.ensure_directories()
    identity = "0123456789abcdef0123456789abcdef"
    (service.settings.data_dir / ".tankarr-library-id").write_text(
        identity, encoding="utf-8"
    )
    (service.settings.library_dir / ".tankarr-library-id").write_text(
        identity, encoding="utf-8"
    )
    # The provider is an inert object: reaching it would fail the test.  The
    # worker must discard the mapped-but-now-early job before claiming it.
    await service.process_download_job(stale["id"])
    with pytest.raises(KeyError):
        database.get_job(stale["id"])

    class ManualProvider:
        name = "suwayomi"

        def __init__(self):
            self.called = False

        async def download_pages(self, *_args, **_kwargs):
            self.called = True
            raise ProviderRequestError("manual probe reached provider")

    manual_provider = ManualProvider()
    service.providers["suwayomi"] = manual_provider  # type: ignore[assignment]
    manual = await service.create_manual_download_job("mirror-4")
    assert manual["origin"] == "manual"
    assert await service.rerank_queued_jobs("frontier") == 0
    assert database.get_job(manual["id"])["chapter_id"] == "mirror-4"
    await service.process_download_job(manual["id"])
    assert manual_provider.called is True
    assert database.get_job(manual["id"])["status"] == "failed"

    save_metadata("ended", 4)
    assert database_chapters() == ["4"]
    assert wanted_chapters() == ["4"]

    database.update_manga(
        "frontier",
        {
            "expected_count_override": 6,
            "expected_count_unit_override": "chapter",
        },
    )
    assert database_chapters() == ["4", "5", "6"]
    assert wanted_chapters() == ["4", "5", "6"]

    database.update_manga("frontier", {"status_override": "continuing"})
    assert database_chapters() == []
    assert wanted_chapters() == []
    database.update_manga("frontier", {"status_override": "ended"})
    assert database_chapters() == ["4", "5", "6"]
    assert wanted_chapters() == ["4", "5", "6"]


@pytest.mark.asyncio
async def test_existing_queued_slot_is_retargeted_to_the_current_best_source(
    tmp_path: Path,
):
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    database.upsert_manga(
        {
            "id": "rerank",
            "provider": "catalogue",
            "title": "Re-rank me",
            "description": "",
            "authors": [],
            "status": "completed",
            "available_languages": ["en"],
        },
        "en",
        "all",
    )
    database.upsert_chapters(
        "rerank",
        [
            {
                "id": "mangafire-1",
                "chapter": "1",
                "volume": "1",
                "title": "",
                "language": "en",
                "provider": "suwayomi",
                "source_key": "suwayomi:mangafire",
                "source_name": "MangaFire (EN)",
                "groups": [],
                "publish_at": "2025-01-01T00:00:00Z",
                "source_url": "https://mangafire.test/chapter-1",
            },
            {
                "id": "weebcentral-1",
                "chapter": "1",
                "volume": "1",
                "title": "",
                "language": "en",
                "provider": "suwayomi",
                "source_key": "suwayomi:weebcentral",
                "source_name": "Weeb Central (EN)",
                "groups": [],
                "publish_at": "2025-01-01T00:00:00Z",
                "source_url": "https://weebcentral.test/chapter-1",
            },
        ],
    )
    job = database.create_job("rerank", "mangafire-1", "en")
    service = build_service(tmp_path, database)

    created = await service.create_missing_download_jobs("rerank")

    assert created == []
    updated = database.get_job(job["id"])
    assert updated["chapter_id"] == "weebcentral-1"
    assert updated["message"] == "Re-ranked to preferred source"
    assert await service.rerank_queued_jobs("rerank") == 0


def test_wanted_includes_expected_chapter_without_a_provider_release(tmp_path: Path):
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    database.upsert_manga(
        {
            "id": "ended",
            "provider": "mangadex",
            "title": "Ended work",
            "description": "",
            "authors": [],
            "status": "completed",
            "last_chapter": "3",
            "available_languages": ["en"],
        },
        "en",
        "all",
    )

    def expected_release(identifier: str, number: str) -> dict:
        return {
            "id": identifier,
            "chapter": number,
            "volume": "1",
            "title": "",
            "language": "en",
            "provider": "mangadex",
            "groups": [],
            "publish_at": None,
            "source_url": f"https://example.test/{identifier}",
        }

    database.upsert_chapters(
        "ended",
        [expected_release("ended-1", "1"), expected_release("ended-3", "3")],
    )
    database.mark_chapter_downloaded(
        "ended-1", tmp_path / "library" / "one.cbz", "1" * 64
    )
    database.mark_chapter_downloaded(
        "ended-3", tmp_path / "library" / "three.cbz", "3" * 64
    )
    service = build_service(tmp_path, database)

    wanted = service.list_wanted()

    assert len(wanted) == 1
    assert [(item["chapter"], item["provider"]) for item in wanted[0]["chapters"]] == [
        ("2", "expected")
    ]
    assert wanted[0]["unmapped_expected_count"] == 0


def test_volume_override_is_reversible_and_excludes_missing_volume_from_wanted(
    tmp_path: Path,
):
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    database.upsert_manga(
        {
            "id": "volumes",
            "provider": "local",
            "title": "Three volumes",
            "description": "",
            "authors": [],
            "status": "completed",
            "available_languages": ["en"],
        },
        "en",
        "none",
    )
    database.upsert_chapters(
        "volumes",
        [
            {
                "id": f"volume-{number}",
                "chapter": None,
                "volume": str(number),
                "title": "",
                "language": "en",
                "provider": "local",
                "groups": [],
                "publish_at": None,
                "source_url": "",
            }
            for number in (1, 2)
        ],
    )
    for number in (1, 2):
        database.mark_chapter_downloaded(
            f"volume-{number}",
            tmp_path / "library" / f"volume-{number}.cbz",
            str(number) * 64,
        )
    database.save_series_metadata(
        "volumes",
        {
            "status": "ended",
            "volume_count": 3,
            "provenance": {"volume_count": "mangaupdates"},
        },
        artwork_path=None,
        artwork_sha256=None,
        artwork_media_type=None,
        source_status=[],
    )
    service = build_service(tmp_path, database)

    database.set_volume_monitor_override("volumes", "3", "monitored")
    wanted = service.list_wanted()
    assert wanted[0]["chapters"][0]["volume"] == "3"
    assert wanted[0]["chapters"][0]["provider"] == "expected"

    database.update_manga("volumes", {"library_status_override": "up_to_date"})
    assert service.list_wanted() == []
    database.update_manga("volumes", {"library_status_override": "automatic"})
    assert service.list_wanted()[0]["chapters"][0]["volume"] == "3"

    database.set_volume_monitor_override("volumes", "3", "ignored")
    assert service.list_wanted() == []
    assert database.list_volume_monitor_overrides("volumes")[0]["state"] == "ignored"

    database.set_volume_monitor_override("volumes", "3.0", "automatic")
    assert database.list_volume_monitor_overrides("volumes") == []


def test_failed_jobs_can_be_retried_and_terminal_jobs_removed(tmp_path: Path):
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    seed_manga(database)
    job = database.create_job("manga-1", "chapter-1", "en")

    with pytest.raises(ValueError):
        database.retry_job(job["id"])  # still queued, not failed

    database.update_job(job["id"], status="failed", message="boom")
    retried = database.retry_job(job["id"])
    assert retried["status"] == "queued"
    assert retried["message"] == "Retry requested"
    assert retried["origin"] == "manual"

    with pytest.raises(KeyError):
        database.delete_job(9999)

    database.update_job(job["id"], status="completed")
    removed = database.delete_job(job["id"])
    assert removed["id"] == job["id"]
    assert database.list_jobs() == []


def test_delete_job_refuses_active_downloads(tmp_path: Path):
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    seed_manga(database)
    job = database.create_job("manga-1", "chapter-1", "en")
    database.update_job(job["id"], status="downloading")
    with pytest.raises(ValueError):
        database.delete_job(job["id"])


def test_cancel_job_removes_queued_or_interrupted_jobs_but_not_imports(
    tmp_path: Path,
):
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    seed_manga(database)

    queued = database.create_job("manga-1", "chapter-1", "en")
    assert database.cancel_job(queued["id"])["status"] == "queued"
    with pytest.raises(KeyError):
        database.get_job(queued["id"])

    importing = database.create_job("manga-1", "chapter-1", "en")
    database.update_job(importing["id"], status="importing")
    with pytest.raises(ValueError, match="library import"):
        database.cancel_job(importing["id"])


@pytest.mark.asyncio
async def test_worker_safely_cancels_an_active_download_and_cleans_up(tmp_path: Path):
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    seed_manga(database)
    job = database.create_job("manga-1", "chapter-1", "en")

    class BlockingService:
        def __init__(self):
            import asyncio

            self._mutation_lock = asyncio.Lock()
            self.started = asyncio.Event()
            self.cleaned = asyncio.Event()

        async def process_download_job(self, job_id: int) -> None:
            database.update_job(job_id, status="downloading", progress=0.2)
            self.started.set()
            try:
                await __import__("asyncio").Event().wait()
            finally:
                self.cleaned.set()

        async def failover_failed_release(self, job_id: int) -> list:
            return []

    service = BlockingService()
    worker = DownloadWorker(database, service)  # type: ignore[arg-type]
    await worker.start()
    try:
        await __import__("asyncio").wait_for(service.started.wait(), timeout=1)
        removed = await worker.remove(job["id"])
        assert removed["status"] == "downloading"
        assert service.cleaned.is_set()
        with pytest.raises(KeyError):
            database.get_job(job["id"])
    finally:
        await worker.stop()


@pytest.mark.asyncio
async def test_worker_refreshes_komga_once_after_successful_queue_drain(tmp_path: Path):
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    seed_manga(database)
    database.upsert_chapters(
        "manga-1",
        [
            {
                "id": "chapter-2",
                "chapter": "2",
                "volume": "1",
                "title": "Two",
                "language": "en",
                "provider": "mangadex",
                "groups": [],
                "publish_at": "2026-01-02T00:00:00Z",
                "source_url": "https://example.test/2",
            }
        ],
    )
    jobs = [
        database.create_job("manga-1", chapter_id, "en")
        for chapter_id in ("chapter-1", "chapter-2")
    ]

    class SuccessfulService:
        def __init__(self):
            self.refreshes: list[str] = []

        async def process_download_job(self, job_id: int) -> None:
            database.update_job(
                job_id,
                status="completed",
                progress=1,
                result_path=tmp_path / f"{job_id}.cbz",
            )

        async def refresh_komga_library(self, *, reason: str) -> dict:
            self.refreshes.append(reason)
            return {"ready": True}

        async def failover_failed_release(self, job_id: int) -> list:
            return []

    service = SuccessfulService()
    worker = DownloadWorker(database, service)  # type: ignore[arg-type]
    await worker.start()
    try:
        await asyncio.wait_for(worker.queue.join(), timeout=1)
        assert [database.get_job(job["id"])["status"] for job in jobs] == [
            "completed",
            "completed",
        ]
        assert service.refreshes == ["download_queue_drained"]
    finally:
        await worker.stop()


def test_list_jobs_filters_by_status(tmp_path: Path):
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    seed_manga(database)
    job = database.create_job("manga-1", "chapter-1", "en")
    database.update_job(job["id"], status="failed", message="boom")
    assert database.list_jobs(statuses=["failed"])[0]["id"] == job["id"]
    assert database.list_jobs(statuses=["completed"]) == []
    assert len(database.list_jobs()) == 1


def test_startup_requeues_jobs_stuck_in_active_statuses(tmp_path: Path):
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    seed_manga(database)
    job = database.create_job("manga-1", "chapter-1", "en")
    database.update_job(job["id"], status="downloading", progress=0.4)

    restarted = Database(tmp_path / "tankarr.sqlite3")
    restarted.initialize()
    recovered = restarted.get_job(job["id"])
    assert recovered["status"] == "queued"
    assert recovered["message"] == "Recovered after restart"


@pytest.mark.asyncio
async def test_failed_release_failover_blocks_and_queues_next_provider(tmp_path: Path):
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    seed_manga(database, monitor_mode="all")
    database.upsert_chapters(
        "manga-1",
        [
            {
                "id": "chapter-1-pill",
                "chapter": "1",
                "volume": "1",
                "title": "One",
                "language": "en",
                "provider": "mangapill",
                "groups": [],
                "publish_at": "2026-01-02T00:00:00Z",
                "source_url": "https://example.test/pill-1",
            }
        ],
    )
    service = build_service(tmp_path, database)

    job = database.create_job("manga-1", "chapter-1", "en")
    database.update_job(job["id"], status="failed", message="HTTP 502 from provider")

    replacements = await service.failover_failed_release(job["id"])
    assert [item["chapter_id"] for item in replacements] == ["chapter-1-pill"]
    assert "chapter-1" in database.blocked_releases("manga-1")

    # Failing the fallback too leaves the slot fully blocked: visible in
    # Wanted, never re-queued automatically.
    database.update_job(
        replacements[0]["id"], status="failed", message="HTTP 502 from provider"
    )
    assert await service.failover_failed_release(replacements[0]["id"]) == []
    slot = service.list_wanted()[0]["chapters"][0]
    assert slot["blocked"] is True
    assert "HTTP 502" in slot["block_reason"]
    assert await service.create_missing_download_jobs("manga-1") == []

    # History retry is the explicit operator decision that clears the block.
    retried = database.retry_job(job["id"])
    assert retried["status"] == "queued"
    assert retried["origin"] == "manual"
    assert "chapter-1" not in database.blocked_releases("manga-1")
    assert database.preferred_download_candidates("manga-1") == []  # active again


@pytest.mark.asyncio
async def test_transient_infrastructure_failure_does_not_condemn_the_release(
    tmp_path: Path,
):
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    seed_manga(database, monitor_mode="all")
    service = build_service(tmp_path, database)

    job = database.create_job("manga-1", "chapter-1", "en")
    database.update_job(
        job["id"],
        status="failed",
        message="ProviderUnavailableError: GET https://x failed after retries",
        failure_code="ProviderUnavailableError",
        failure_scope="service",
    )

    # A rebooted or briefly unreachable source engine must not blocklist a
    # good release: the job stays failed and the Wanted pass retries it.
    assert await service.failover_failed_release(job["id"]) == []
    assert database.blocked_releases("manga-1") == {}
    assert [
        item["id"] for item in database.preferred_download_candidates("manga-1")
    ] == ["chapter-1"]


@pytest.mark.asyncio
async def test_transient_download_attempts_advance_and_eventually_fail(tmp_path: Path):
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    seed_manga(database)
    settings = Settings(data_dir=tmp_path / "data", library_dir=tmp_path / "library")
    settings.ensure_directories()
    identity = "0123456789abcdef0123456789abcdef"
    (settings.data_dir / ".tankarr-library-id").write_text(identity, encoding="utf-8")
    (settings.library_dir / ".tankarr-library-id").write_text(
        identity, encoding="utf-8"
    )

    class OfflineProvider:
        name = "mangadex"

        async def download_pages(self, *_args, **_kwargs):
            raise ProviderUnavailableError("source engine is offline")

    service = TankarrService(
        settings,
        database,
        OfflineProvider(),  # type: ignore[arg-type]
        KomgaClient(settings),
    )
    job = database.create_job("manga-1", "chapter-1", "en")

    for expected in (1, 2, 3):
        database.update_job(job["id"], next_retry_at=0)
        with database.connect() as connection:
            connection.execute("UPDATE source_circuit SET next_retry_at=0")
        await service.process_download_job(job["id"])
        current = database.get_job(job["id"])
        assert current["status"] == "queued"
        assert f"attempt {expected}/3" in current["message"]
        summary = database.job_series_summary()
        assert summary[0]["current"]["id"] == job["id"]

    database.update_job(job["id"], next_retry_at=0)
    with database.connect() as connection:
        connection.execute("UPDATE source_circuit SET next_retry_at=0")
    await service.process_download_job(job["id"])
    terminal = database.get_job(job["id"])
    assert terminal["status"] == "failed"
    assert terminal["message"].startswith("ProviderUnavailableError:")


@pytest.mark.asyncio
async def test_provider_rejection_fails_once_without_transient_pause(tmp_path: Path):
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    seed_manga(database)
    settings = Settings(data_dir=tmp_path / "data", library_dir=tmp_path / "library")
    settings.ensure_directories()
    identity = "0123456789abcdef0123456789abcdef"
    (settings.data_dir / ".tankarr-library-id").write_text(identity, encoding="utf-8")
    (settings.library_dir / ".tankarr-library-id").write_text(
        identity, encoding="utf-8"
    )

    class RejectingProvider:
        name = "mangadex"

        async def download_pages(self, *_args, **_kwargs):
            raise ProviderRequestError("provider rejected this chapter")

    service = TankarrService(
        settings,
        database,
        RejectingProvider(),  # type: ignore[arg-type]
        KomgaClient(settings),
    )
    job = database.create_job("manga-1", "chapter-1", "en")

    await service.process_download_job(job["id"])

    terminal = database.get_job(job["id"])
    assert terminal["status"] == "failed"
    assert terminal["message"].startswith("ProviderRequestError:")
    assert terminal["next_retry_at"] == 0


def _write_library_file(root: Path, relative: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"PK\x05\x06" + b"\x00" * 18)
    return path


def test_library_files_no_release_claims_are_named_as_orphans(tmp_path: Path):
    # Live: a series deleted from Tankarr without its files left 27 chapters
    # on disk, which the reader kept serving as "stale books" for two days.
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    seed_manga(database)
    service = build_service(tmp_path, database)
    root = service.settings.library_dir
    root.mkdir(parents=True, exist_ok=True)
    (root / ".tankarr-library-id").write_text("0" * 32, encoding="utf-8")
    (service.settings.data_dir).mkdir(parents=True, exist_ok=True)
    (service.settings.data_dir / ".tankarr-library-id").write_text(
        "0" * 32, encoding="utf-8"
    )

    tracked = _write_library_file(root, "Example (Author)/Example - c001 [en].cbz")
    database.mark_chapter_downloaded("chapter-1", tracked)
    _write_library_file(root, "Orphan Work (Someone)/Orphan Work - c001 [en].cbz")
    _write_library_file(root, "Orphan Work (Someone)/Orphan Work - c002 [en].cbz")
    # Quarantine staging must never be reported as content.
    _write_library_file(root, ".tankarr-delete-abc/000001.quarantined.cbz")

    report = service.library_orphans()

    assert report["count"] == 2
    assert [item["folder"] for item in report["folders"]] == ["Orphan Work (Someone)"]
    assert report["folders"][0]["files"] == 2


@pytest.mark.asyncio
async def test_deleting_orphans_keeps_tracked_files_and_rescans_the_reader(
    tmp_path: Path,
):
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    seed_manga(database)
    service = build_service(tmp_path, database)
    root = service.settings.library_dir
    root.mkdir(parents=True, exist_ok=True)
    (root / ".tankarr-library-id").write_text("0" * 32, encoding="utf-8")
    service.settings.data_dir.mkdir(parents=True, exist_ok=True)
    (service.settings.data_dir / ".tankarr-library-id").write_text(
        "0" * 32, encoding="utf-8"
    )
    tracked = _write_library_file(root, "Example (Author)/Example - c001 [en].cbz")
    database.mark_chapter_downloaded("chapter-1", tracked)
    orphan = _write_library_file(
        root, "Orphan Work (Someone)/Orphan Work - c001 [en].cbz"
    )
    keep = _write_library_file(root, "Other Orphan (Someone)/Other - c001 [en].cbz")

    result = await service.delete_library_orphans(["Orphan Work (Someone)"])

    assert result["deleted"] == 1
    assert result["folders"] == ["Orphan Work (Someone)"]
    assert not orphan.exists()
    assert not orphan.parent.exists()  # the empty folder goes too
    assert keep.exists()  # a folder not named is left alone
    assert tracked.exists()
    assert service.library_orphans()["count"] == 1


def test_a_map_canonical_decimal_chapter_is_a_download_candidate(tmp_path: Path):
    # And, live: the index showed 5.5 as expected, but the candidate
    # selection still dropped it as an extra, so "queue missing" queued 0.
    from tankarr.chapter_map import entries_from_releases

    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    seed_manga(database, monitor_mode="all")
    database.upsert_chapters(
        "manga-1",
        [
            {
                "id": "chapter-5.5",
                "chapter": "5.5",
                "volume": "1",
                "title": "Omake",
                "language": "en",
                "provider": "mangadex",
                "groups": [],
                "publish_at": "2026-01-02T00:00:00Z",
                "source_url": "https://example.test/5.5",
            }
        ],
    )
    without_map = {c["id"] for c in database.preferred_download_candidates("manga-1")}
    assert "chapter-5.5" not in without_map

    database.replace_chapter_map(
        "manga-1",
        "mangaupdates",
        entries_from_releases([{"volume": "1", "chapter": "5.5"}]),
    )
    # Catalogue hints need confirmation before changing acquisition coverage.
    assert "chapter-5.5" not in {
        c["id"] for c in database.preferred_download_candidates("manga-1")
    }
    database.replace_chapter_map(
        "manga-1",
        "operator",
        entries_from_releases([{"volume": "1", "chapter": "5.5"}], source="operator"),
    )
    with_map = {c["id"] for c in database.preferred_download_candidates("manga-1")}
    assert "chapter-5.5" in with_map


@pytest.mark.asyncio
async def test_missing_jobs_skip_only_explicitly_mapped_book_chapters(tmp_path: Path):
    """An asymmetric verified map, not the book count, determines coverage."""
    from tankarr.chapter_map import MapEntry

    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    seed_manga(database, monitor_mode="all")
    database.update_manga(
        "manga-1",
        {
            "status_override": "ended",
            "series_unit_override": "chapters",
            "edition_book_count": 2,
        },
    )
    with database.connect() as connection:
        connection.execute(
            "UPDATE manga SET status='completed', last_chapter='24' WHERE id='manga-1'"
        )
    database.upsert_chapters(
        "manga-1",
        [
            {
                "id": f"chapter-{number}",
                "chapter": str(number),
                "volume": None,
                "title": f"Chapter {number}",
                "language": "en",
                "provider": "mangadex",
                "groups": [],
                "publish_at": "2026-01-01T00:00:00Z",
                "source_url": f"https://example.test/{number}",
            }
            for number in range(2, 25)
        ]
        + [
            {
                "id": "book-1",
                "chapter": None,
                "volume": "1",
                "release_unit": "volume",
                "title": "Volume 1",
                "language": "en",
                "provider": "prowlarr",
                "groups": [],
                "publish_at": "2026-01-01T00:00:00Z",
                "source_url": "https://example.test/book-1",
            }
        ],
    )
    database.mark_chapter_downloaded(
        "book-1", library_path="/library/Example/Example - v001 [en].cbz"
    )
    database.replace_chapter_map(
        "manga-1",
        "operator",
        [
            MapEntry(("1",), tuple(str(n) for n in range(1, 11)), True, "operator"),
            MapEntry(("2",), tuple(str(n) for n in range(11, 25)), True, "operator"),
        ],
    )
    service = build_service(tmp_path, database)
    created = await service.create_missing_download_jobs("manga-1")
    queued = sorted(
        int(database.get_chapter(job["chapter_id"])["chapter"]) for job in created
    )
    assert queued == list(range(11, 25))


def test_a_complete_edition_on_disk_wants_no_chapter(tmp_path: Path):
    """Whatever unit the series is followed in, and whether or not anyone
    knows which chapters each book holds: once every book of the edition is
    on disk the work is there, and fetching it again in pieces is not a
    fallback, it is a second copy."""

    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    seed_manga(database, monitor_mode="all")
    database.update_manga("manga-1", {"series_unit_override": "chapters"})
    database.save_series_metadata(
        "manga-1",
        {"status": "ended", "volume_count": 3, "chapter_count": 30},
        artwork_path=None,
        artwork_sha256=None,
        artwork_media_type=None,
        source_status=[],
    )
    books = [
        {
            "id": f"book-{volume}",
            "chapter": None,
            "volume": str(volume),
            "release_unit": "volume",
            "title": f"Volume {volume}",
            "language": "en",
            "provider": "prowlarr",
            "pages": 200,
            "groups": [],
            "publish_at": "2026-01-01T00:00:00Z",
            "source_url": f"https://example.test/v{volume}",
        }
        for volume in (1, 2, 3)
    ]
    chapters = [
        {
            "id": f"chapter-{number}",
            "chapter": str(number),
            "volume": None,
            "title": f"Chapter {number}",
            "language": "en",
            "provider": "mangadex",
            "pages": 20,
            "groups": [],
            "publish_at": "2026-01-02T00:00:00Z",
            "source_url": f"https://example.test/{number}",
        }
        for number in range(1, 31)
    ]
    database.upsert_chapters("manga-1", books + chapters)

    incomplete = database.preferred_missing_releases("manga-1")
    assert any(row.get("chapter") for row in incomplete)  # no book yet: chapters stand

    for volume in (1, 2, 3):
        path = tmp_path / f"v{volume}.cbz"
        path.write_bytes(b"x")
        database.mark_chapter_downloaded(f"book-{volume}", path, "0" * 64)

    complete = database.preferred_missing_releases("manga-1")
    assert [row for row in complete if row.get("chapter")] == []


def test_retry_deadline_survives_reopen_and_gates_claim(tmp_path: Path):
    from datetime import UTC, datetime

    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    seed_manga(database)
    job = database.create_job("manga-1", "chapter-1", "en")
    deadline = datetime.now(UTC).timestamp() + 600
    database.update_job(
        job["id"],
        retry_count=2,
        next_retry_at=deadline,
        failure_code="ConnectError",
        failure_scope="service",
        message="Translated message",
    )
    reopened = Database(database.path)
    reopened.initialize()
    assert reopened.list_queued_job_heads() == []
    assert reopened.claim_queued_job(job["id"], tmp_path / "book.cbz") is None
    assert reopened.get_job(job["id"])["retry_count"] == 2
    assert build_service(tmp_path, reopened)._transient_attempt(job["id"]) == 3
    reopened.update_job(job["id"], next_retry_at=0)
    assert reopened.list_queued_job_heads()[0]["id"] == job["id"]
