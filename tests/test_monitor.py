from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from tankarr.config import Settings
from tankarr.database import Database
from tankarr.monitor import ReleaseMonitor


def chapter(chapter_id: str, number: str) -> dict:
    return {
        "id": chapter_id,
        "chapter": number,
        "volume": "1",
        "title": f"Chapter {number}",
        "language": "en",
        "provider": "mangadex",
        "groups": ["Test Group"],
        "publish_at": "2026-01-01T00:00:00Z",
        "source_url": f"https://example.test/{chapter_id}",
        "pages": 20,
        "version": 1,
    }


class FakeService:
    def __init__(self, database: Database, batches: list[list[dict]]):
        self.database = database
        self.batches = iter(batches)
        self.manual_downloads: list[tuple[str, bool]] = []

    def assert_mutations_allowed(self) -> None:
        return None

    def can_download_release(self, row: dict) -> bool:
        return row.get("provider") != "prowlarr"

    async def backfill_page_counts(self, manga_id: str, limit: int = 60) -> int:
        return 0

    def list_wanted(self) -> list[dict]:
        entries = []
        for manga in self.database.list_manga():
            chapters = self.database.preferred_missing_releases(manga["id"])
            if chapters:
                entries.append({"manga": manga, "chapters": chapters})
        return entries

    async def refresh_manga(self, manga_id: str, language: str) -> dict:
        result = self.database.upsert_chapters(manga_id, next(self.batches))
        return {
            "manga_id": manga_id,
            "language": language,
            "monitor_mode_before": self.database.get_manga(manga_id)["monitor_mode"],
            **result,
        }

    async def create_missing_download_jobs(
        self, manga_id: str, chapter_ids: list[str] | None = None
    ) -> list[dict]:
        manga = self.database.get_manga(manga_id)
        return [
            self.database.create_job(manga_id, item["id"], manga["preferred_language"])
            for item in self.database.preferred_download_candidates(
                manga_id, chapter_ids
            )
        ]

    async def create_manual_download_job(
        self, chapter_id: str, *, replace: bool = False
    ) -> dict:
        self.manual_downloads.append((chapter_id, replace))
        return {
            "id": 1,
            "status": "queued",
            "chapter_id": chapter_id,
            "replace": replace,
        }

    async def refresh_official_edition_evidence(self, manga_id: str) -> int:
        return 0

    async def align_with_official_edition(
        self, manga_id: str, *, dry_run: bool = False
    ) -> dict:
        return {"manga_id": manga_id, "deleted": 0, "errors": [], "surplus": []}

    async def audit_library_page_quality(self, *, limit: int | None = None) -> dict:
        return {"measured": 0, "degraded": []}

    async def recover_degraded_chapters(
        self, manga_id: str, *, limit: int = 25
    ) -> dict:
        return {"requeued": [], "kept": []}


class RecordingWorker:
    def __init__(self):
        self.enqueued: list[int] = []

    async def enqueue(self, job_id: int) -> None:
        self.enqueued.append(job_id)


@pytest.mark.asyncio
async def test_official_replacement_is_opt_in(tmp_path: Path):
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    settings = Settings(data_dir=tmp_path / "data", library_dir=tmp_path / "library")
    service = FakeService(database, [])
    service.settings = settings  # type: ignore[attr-defined]
    monitor = ReleaseMonitor(
        settings,
        database,
        service,  # type: ignore[arg-type]
        RecordingWorker(),  # type: ignore[arg-type]
    )

    assert settings.prefer_official_releases is True
    assert settings.official_upgrade_enabled is False
    assert await monitor.upgrade_to_official() == 0


@pytest.mark.asyncio
async def test_official_upgrade_is_independent_from_acquisition_policy(tmp_path: Path):
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    database.upsert_manga(
        {
            "id": "manga-1",
            "title": "Example",
            "description": "",
            "cover_url": None,
            "authors": [],
            "original_language": "ja",
            "monitored": True,
        },
        "en",
        "all",
    )
    database.save_series_metadata(
        "manga-1",
        {
            "official_links": [
                {"url": "https://tapas.io/series/example", "language": "en"}
            ]
        },
        artwork_path=None,
        artwork_sha256=None,
        artwork_media_type=None,
        source_status=[],
    )
    database.upsert_chapters(
        "manga-1",
        [
            {
                **chapter("scan-1", "1"),
                "provider": "suwayomi",
                "source_key": "suwayomi:scan",
                "source_name": "Scanlator",
                "source_url": "https://scans.test/1",
            },
            {
                **chapter("official-1", "1"),
                "provider": "suwayomi",
                "source_key": "suwayomi:tapas",
                "source_name": "Tapas (EN)",
                "source_url": "https://tapas.io/episode/1",
            },
        ],
    )
    database.mark_chapter_downloaded("scan-1", tmp_path / "scan.cbz")
    settings = Settings(
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "library",
        release_acquisition_policy="first_available",
        official_upgrade_enabled=True,
    )
    service = FakeService(database, [])
    service.settings = settings  # type: ignore[attr-defined]
    monitor = ReleaseMonitor(
        settings,
        database,
        service,  # type: ignore[arg-type]
        RecordingWorker(),  # type: ignore[arg-type]
    )

    assert await monitor.upgrade_to_official() == 1
    assert service.manual_downloads == [("official-1", True)]


@pytest.mark.asyncio
async def test_auto_download_queues_only_releases_after_baseline(tmp_path: Path):
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    database.upsert_manga(
        {
            "id": "manga-1",
            "title": "Example",
            "description": "",
            "cover_url": None,
            "authors": [],
            "original_language": "ja",
        },
        "en",
        "future",
    )
    worker = RecordingWorker()
    service = FakeService(
        database,
        [
            [chapter("chapter-1", "1")],
            [chapter("chapter-1", "1"), chapter("chapter-2", "2")],
            [chapter("chapter-1", "1"), chapter("chapter-2", "2")],
        ],
    )
    monitor = ReleaseMonitor(
        Settings(data_dir=tmp_path / "data", library_dir=tmp_path / "library"),
        database,
        service,  # type: ignore[arg-type]
        worker,  # type: ignore[arg-type]
    )

    baseline = await monitor.refresh_one("manga-1")
    assert baseline["baseline"] is True
    assert baseline["new"] == 1
    assert baseline["queued"] == 0

    new_release = await monitor.refresh_one("manga-1")
    assert new_release["baseline"] is False
    assert new_release["new"] == 1
    assert new_release["queued"] == 1
    assert len(worker.enqueued) == 1
    assert database.list_jobs()[0]["chapter_id"] == "chapter-2"

    unchanged = await monitor.refresh_one("manga-1")
    assert unchanged["new"] == 0
    assert unchanged["queued"] == 0
    assert len(worker.enqueued) == 1


@pytest.mark.asyncio
async def test_all_mode_queues_one_preferred_release_per_existing_chapter(
    tmp_path: Path,
):
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    database.upsert_manga(
        {
            "id": "manga-1",
            "title": "Example",
            "description": "",
            "cover_url": None,
            "authors": [],
            "original_language": "ja",
            "status": "ongoing",
        },
        "en",
        "none",
    )
    older = chapter("chapter-1-v1", "1")
    preferred = {**chapter("chapter-1-v2", "1"), "version": 2}
    database.upsert_chapters("manga-1", [older, preferred, chapter("chapter-2", "2")])
    worker = RecordingWorker()
    monitor = ReleaseMonitor(
        Settings(data_dir=tmp_path / "data", library_dir=tmp_path / "library"),
        database,
        FakeService(database, []),  # type: ignore[arg-type]
        worker,  # type: ignore[arg-type]
    )

    configured = await monitor.configure_monitor_mode("manga-1", "all")

    assert configured["monitor_mode"] == "all"
    assert configured["queued"] == 2
    assert {job["chapter_id"] for job in database.list_jobs()} == {
        "chapter-1-v2",
        "chapter-2",
    }


@pytest.mark.asyncio
async def test_manual_wanted_recovery_queues_missing_releases_once(tmp_path: Path):
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    database.upsert_manga(
        {
            "id": "manga-1",
            "title": "Example",
            "description": "",
            "cover_url": None,
            "authors": [],
            "original_language": "ja",
        },
        "en",
        "all",
    )
    database.upsert_chapters("manga-1", [chapter("chapter-1", "1")])
    worker = RecordingWorker()
    monitor = ReleaseMonitor(
        Settings(data_dir=tmp_path / "data", library_dir=tmp_path / "library"),
        database,
        FakeService(database, []),  # type: ignore[arg-type]
        worker,  # type: ignore[arg-type]
    )

    result = await monitor.search_wanted(trigger="manual")
    repeated = await monitor.search_wanted(trigger="manual")

    assert result == {
        "trigger": "manual",
        "series": 1,
        "skipped_not_due": 0,
        "missing": 1,
        "discovered": 0,
        "searched": 0,
        "queued": 1,
        "upgraded": 0,
        "aligned": {"deleted": 0, "series": 0, "enabled": True},
        "quality": {
            "measured": 0,
            "degraded": 0,
            "replaced": 0,
            "kept": 0,
            "enabled": True,
        },
        "errors": [],
    }
    assert repeated["missing"] == 1
    assert repeated["queued"] == 0
    assert worker.enqueued == [database.list_jobs()[0]["id"]]
    assert monitor.status()["wanted_search"]["last_result"] == repeated


@pytest.mark.asyncio
async def test_one_series_can_be_searched_on_its_own(tmp_path: Path):
    # A series added minutes ago must not wait for the six-hourly pass to
    # learn what is obtainable: the same ladder runs for it alone.
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    database.upsert_manga(
        {
            "id": "manga-1",
            "title": "Solo",
            "description": "",
            "cover_url": None,
            "authors": [],
            "original_language": "ja",
        },
        "en",
        "all",
    )
    database.upsert_chapters("manga-1", [chapter("chapter-1", "1")])
    worker = RecordingWorker()
    monitor = ReleaseMonitor(
        Settings(data_dir=tmp_path / "data", library_dir=tmp_path / "library"),
        database,
        FakeService(database, []),  # type: ignore[arg-type]
        worker,  # type: ignore[arg-type]
    )

    result = await monitor.search_wanted_series("manga-1", trigger="add")
    unknown = await monitor.search_wanted_series("manga-9", trigger="add")

    assert result == {
        "trigger": "add",
        "manga_id": "manga-1",
        "missing": 1,
        "queued": 1,
        "searched": 0,
    }
    assert unknown == {"trigger": "add", "manga_id": "manga-9", "missing": 0}
    assert worker.enqueued == [database.list_jobs()[0]["id"]]


@pytest.mark.asyncio
async def test_concurrent_wanted_recovery_joins_the_inflight_pass(tmp_path: Path):
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    database.upsert_manga(
        {
            "id": "manga-1",
            "title": "Example",
            "description": "",
            "cover_url": None,
            "authors": [],
            "original_language": "ja",
        },
        "en",
        "all",
    )
    database.upsert_chapters("manga-1", [chapter("chapter-1", "1")])
    monitor = ReleaseMonitor(
        Settings(data_dir=tmp_path / "data", library_dir=tmp_path / "library"),
        database,
        FakeService(database, []),  # type: ignore[arg-type]
        RecordingWorker(),  # type: ignore[arg-type]
    )
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def controlled_queue_missing(_manga_id: str) -> int:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return 1

    monitor.queue_missing = controlled_queue_missing  # type: ignore[method-assign]
    scheduled = asyncio.create_task(monitor.search_wanted(trigger="scheduled"))
    await started.wait()
    manual = asyncio.create_task(monitor.search_wanted(trigger="manual"))
    await asyncio.sleep(0)

    assert calls == 1
    release.set()
    scheduled_result, manual_result = await asyncio.gather(scheduled, manual)

    assert calls == 1
    assert manual_result == scheduled_result
    assert scheduled_result["trigger"] == "scheduled"


@pytest.mark.asyncio
async def test_wanted_recovery_schedule_survives_monitor_restart(tmp_path: Path):
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    database.upsert_manga(
        {
            "id": "manga-1",
            "title": "Example",
            "description": "",
            "cover_url": None,
            "authors": [],
            "original_language": "ja",
        },
        "en",
        "all",
    )
    database.upsert_chapters("manga-1", [chapter("chapter-1", "1")])
    settings = Settings(
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "library",
        wanted_search_interval_seconds=3600,
    )
    first = ReleaseMonitor(
        settings,
        database,
        FakeService(database, []),  # type: ignore[arg-type]
        RecordingWorker(),  # type: ignore[arg-type]
    )
    result = await first.search_wanted(trigger="scheduled")

    restarted = ReleaseMonitor(
        settings,
        database,
        FakeService(database, []),  # type: ignore[arg-type]
        RecordingWorker(),  # type: ignore[arg-type]
    )

    assert restarted._wanted_search_due() is False
    assert restarted.status()["wanted_search"]["last_result"] == result
    assert restarted.status()["wanted_search"]["last_search_at"] is not None


@pytest.mark.asyncio
async def test_monitor_runs_wanted_recovery_only_when_schedule_is_due(
    tmp_path: Path,
):
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    database.upsert_manga(
        {
            "id": "local-1",
            "provider": "local",
            "title": "Local Example",
            "description": "",
            "cover_url": None,
            "authors": [],
            "original_language": "ja",
        },
        "en",
        "all",
    )
    database.upsert_chapters("local-1", [chapter("chapter-1", "1")])
    worker = RecordingWorker()
    monitor = ReleaseMonitor(
        Settings(
            data_dir=tmp_path / "data",
            library_dir=tmp_path / "library",
            wanted_search_enabled=True,
            wanted_search_interval_seconds=3600,
        ),
        database,
        FakeService(database, []),  # type: ignore[arg-type]
        worker,  # type: ignore[arg-type]
    )

    first = await monitor.run_cycle()
    immediate = await monitor.run_cycle()

    assert first["checked"] == 0
    assert first["queued"] == 1
    assert first["wanted_search"]["trigger"] == "scheduled"
    assert immediate["queued"] == 0
    assert immediate["wanted_search"] is None


@pytest.mark.asyncio
async def test_future_modes_are_allowed_on_a_finished_work(
    tmp_path: Path,
):
    """The stored reason explains that nothing new is expected; it does not
    override an explicit operator choice. Monitoring a finished work costs one
    discovery pass that finds nothing."""

    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    database.upsert_manga(
        {
            "id": "manga-1",
            "title": "Finished",
            "description": "",
            "cover_url": None,
            "authors": [],
            "original_language": "ja",
            "status": "completed",
            "last_chapter": "1",
        },
        "en",
        "none",
    )
    database.set_future_monitoring_capability(
        "manga-1", allowed=False, reason="Translation reached final chapter 1."
    )
    worker = RecordingWorker()
    monitor = ReleaseMonitor(
        Settings(data_dir=tmp_path / "data", library_dir=tmp_path / "library"),
        database,
        FakeService(database, []),  # type: ignore[arg-type]
        worker,  # type: ignore[arg-type]
    )

    configured = await monitor.configure_monitor_mode("manga-1", "future")
    assert configured["monitor_mode"] == "future"
    assert configured["future_monitoring_allowed"] == 0
    assert "final chapter" in configured["future_monitoring_reason"]

    configured = await monitor.configure_monitor_mode("manga-1", "existing")
    assert configured["monitor_mode"] == "existing"


@pytest.mark.asyncio
async def test_official_upgrade_leaves_a_failing_source_alone(tmp_path: Path):
    """Replacing a file that works with one that cannot be fetched is not an
    upgrade: the official release is skipped while its source is blocked or
    no longer trusted, and the library keeps what it has."""

    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    database.upsert_manga(
        {
            "id": "manga-1",
            "title": "Example",
            "description": "",
            "cover_url": None,
            "authors": [],
            "original_language": "ja",
            "monitored": True,
        },
        "en",
        "all",
    )
    database.upsert_chapters(
        "manga-1",
        [
            {
                "id": "scan-1",
                "manga_id": "manga-1",
                "volume": None,
                "chapter": "1",
                "title": "Chapter 1",
                "language": "en",
                "provider": "suwayomi",
                "source_key": "suwayomi:scan",
                "source_name": "Scanlator",
                "source_url": "https://scans.test/1",
                "groups": [],
                "publish_at": None,
                "pages": 20,
            },
            {
                "id": "official-1",
                "manga_id": "manga-1",
                "volume": None,
                "chapter": "1",
                "title": "Chapter 1",
                "language": "en",
                "provider": "suwayomi",
                "source_key": "suwayomi:official",
                "source_name": "Official",
                "source_url": "https://tapas.io/episode/1",
                "groups": [],
                "publish_at": None,
                "pages": 20,
            },
        ],
    )
    database.mark_chapter_downloaded("scan-1", tmp_path / "scan-1.cbz")
    for _ in range(database.SOURCE_DEMOTION_FAILURES):
        database.record_source_failure("manga-1", "suwayomi:official", reason="500")

    from tankarr.official_upgrade import upgrade_candidates

    chapters = database.list_chapters("manga-1", "en")
    hosts = frozenset({"tapas.io"})
    assert [item["id"] for item in upgrade_candidates(chapters, hosts)] == [
        "official-1"
    ]

    demoted = database.demoted_sources("manga-1")
    from tankarr.source_ranking import release_source_keys

    remaining = [
        item
        for item in upgrade_candidates(chapters, hosts)
        if not (set(release_source_keys(item)) & demoted)
    ]
    assert remaining == []


class FakeProwlarr:
    enabled = True
    configured = True


class FakeTorrents:
    def __init__(self) -> None:
        self.prowlarr = FakeProwlarr()


@pytest.mark.asyncio
async def test_book_offers_are_probed_for_running_works_too(
    tmp_path: Path, monkeypatch
):
    """A series reaches Wanted because its unit cannot complete it, so the
    other unit is always worth probing — waiting for the work to end means
    the choice can never move to books while it still publishes."""

    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    settings = Settings(data_dir=tmp_path / "data", library_dir=tmp_path / "library")
    service = FakeService(database, [])
    service.settings = settings  # type: ignore[attr-defined]
    monitor = ReleaseMonitor(
        settings,
        database,
        service,  # type: ignore[arg-type]
        RecordingWorker(),  # type: ignore[arg-type]
    )
    monitor.torrents = FakeTorrents()  # type: ignore[assignment]

    probed: list[str] = []

    async def fake_probe_offers(
        torrents, *, manga, publisher, single_volume, **_kwargs
    ):
        probed.append(str(manga["id"]))
        return []

    import tankarr.volume_hunt as volume_hunt

    monkeypatch.setattr(volume_hunt, "probe_offers", fake_probe_offers)

    for index, status in enumerate(("ongoing", "ended")):
        database.upsert_manga(
            {
                "id": f"m{index}",
                "title": f"Series {index}",
                "description": "",
                "cover_url": None,
                "authors": [],
                "original_language": "ja",
                "status": status,
            },
            "en",
            "all",
        )

    entries = [
        {"manga": database.get_manga("m0"), "chapters": [{"chapter": "5"}]},
        {"manga": database.get_manga("m1"), "chapters": [{"chapter": "9"}]},
    ]
    assert await monitor.probe_book_offers(entries) == 2
    # The running work is probed as well: only the mapped unit, not the
    # publication status, decides whether books are worth asking about.
    assert probed == ["m0", "m1"]

    # An Archive-only installation can still discover book offers.
    monitor.torrents.prowlarr.enabled = False
    monitor.torrents.direct_available = True
    assert await monitor.probe_book_offers(entries) == 2
    assert probed == ["m0", "m1", "m0", "m1"]


@pytest.mark.asyncio
async def test_volumes_series_are_left_to_the_hunt(tmp_path: Path, monkeypatch):
    """A series already followed as books records its offers through the
    hunt itself, so probing it again would duplicate the indexer queries."""

    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    settings = Settings(data_dir=tmp_path / "data", library_dir=tmp_path / "library")
    service = FakeService(database, [])
    service.settings = settings  # type: ignore[attr-defined]
    monitor = ReleaseMonitor(
        settings,
        database,
        service,  # type: ignore[arg-type]
        RecordingWorker(),  # type: ignore[arg-type]
    )
    monitor.torrents = FakeTorrents()  # type: ignore[assignment]

    probed: list[str] = []

    async def fake_probe_offers(
        torrents, *, manga, publisher, single_volume, **_kwargs
    ):
        probed.append(str(manga["id"]))
        return []

    import tankarr.volume_hunt as volume_hunt

    monkeypatch.setattr(volume_hunt, "probe_offers", fake_probe_offers)

    database.upsert_manga(
        {
            "id": "books",
            "title": "Books Series",
            "description": "",
            "cover_url": None,
            "authors": [],
            "original_language": "ja",
            "status": "ended",
        },
        "en",
        "all",
    )
    entries = [
        {
            "manga": database.get_manga("books"),
            "chapters": [{"volume": "3", "chapter": None}],
        }
    ]
    assert await monitor.probe_book_offers(entries) == 0
    assert probed == []


class SearchingTorrents(FakeTorrents):
    def __init__(self, results: list[dict]) -> None:
        super().__init__()
        self.results = results
        self.queries: list[str] = []

    async def search(self, manga_id: str, query: str, **_kwargs) -> dict:
        self.queries.append(query)
        return {"results": self.results}


def seed_orphan(database: Database, manga_id: str = "orphan") -> None:
    database.upsert_manga(
        {
            "id": manga_id,
            "provider": "catalogue",
            "title": "Orphan Work",
            "description": "",
            "cover_url": None,
            "authors": [],
            "available_languages": ["en"],
        },
        "en",
        "all",
    )


@pytest.mark.asyncio
async def test_indexer_offer_without_download_provider_never_looks_queued(
    tmp_path: Path,
):
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    seed_orphan(database)
    settings = Settings(data_dir=tmp_path / "data", library_dir=tmp_path / "library")
    service = FakeService(database, [])
    monitor = ReleaseMonitor(
        settings,
        database,
        service,  # type: ignore[arg-type]
        RecordingWorker(),  # type: ignore[arg-type]
    )
    monitor.torrents = FakeTorrents()  # type: ignore[assignment]
    entry = {
        "manga": {"id": "orphan", "title": "Orphan Work"},
        "chapters": [
            {"id": "old-indexer-offer", "provider": "prowlarr", "volume": "1"}
        ],
    }

    result = await monitor.recover_wanted_slots(
        entry,
        {"searched_volumes": ["1"], "needs_review": [{"title": "other work"}]},
    )

    assert result == {"grabbed": 0, "searched": 0}
    attempts = database.wanted_attempts("orphan")["volume:1"]
    assert {item["channel"]: item["outcome"] for item in attempts} == {
        "sources": "not_offered",
        "indexer_book": "ambiguous",
    }


@pytest.mark.asyncio
async def test_a_series_without_sources_gets_a_verdict_and_never_a_grab(
    tmp_path: Path,
):
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    seed_orphan(database)
    settings = Settings(data_dir=tmp_path / "data", library_dir=tmp_path / "library")
    service = FakeService(database, [])
    service.settings = settings  # type: ignore[attr-defined]
    monitor = ReleaseMonitor(
        settings,
        database,
        service,  # type: ignore[arg-type]
        RecordingWorker(),  # type: ignore[arg-type]
    )
    torrents = SearchingTorrents(
        [{"id": "t1", "title": "Orphan Work v01-v03", "provider": "prowlarr"}]
    )
    monitor.torrents = torrents  # type: ignore[assignment]
    entry = {
        "manga": {"id": "orphan", "title": "Orphan Work"},
        "chapters": [],
        "no_sources": True,
    }

    result = await monitor.recover_wanted_slots(entry, None)

    assert result == {"grabbed": 0, "searched": 1}
    assert torrents.queries and all("Orphan Work" in q for q in torrents.queries)
    ledger = {
        item["channel"]: item for item in database.wanted_attempts("orphan")["series"]
    }
    assert ledger["sources"]["outcome"] == "not_offered"
    assert ledger["indexer_book"]["outcome"] == "ambiguous"
    assert service.manual_downloads == []

    # Answered this week: the next pass does not spend a search on it.
    again = await monitor.recover_wanted_slots(entry, None)
    assert again == {"grabbed": 0, "searched": 0}

    monitor.torrents = SearchingTorrents([])  # type: ignore[assignment]
    database.forget_wanted_attempts("orphan", ["series"])
    await monitor.recover_wanted_slots(entry, None)
    ledger = {
        item["channel"]: item for item in database.wanted_attempts("orphan")["series"]
    }
    assert ledger["indexer_book"]["outcome"] == "not_offered"


class DecidingService(FakeService):
    def __init__(self, database: Database, entries: list[dict]) -> None:
        super().__init__(database, [])
        self.entries = entries
        self.sent: list[tuple[str, str]] = []
        service = self

        class Notifier:
            async def decision_needed(self, summary: str, detail: str) -> bool:
                service.sent.append((summary, detail))
                return True

        self.notifier = Notifier()

    def list_wanted(self) -> list[dict]:
        return self.entries


def wanted_row(chapter: str, recovery: dict) -> dict:
    return {
        "id": f"c{chapter}",
        "chapter": chapter,
        "volume": None,
        "slot_key": f"chapter:{chapter}",
        "recovery": recovery,
    }


@pytest.mark.asyncio
async def test_the_operator_is_told_once_about_each_slot_nobody_can_fill(
    tmp_path: Path,
):
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    settings = Settings(data_dir=tmp_path / "data", library_dir=tmp_path / "library")
    exhausted = {"verdict": "exhausted", "summary": "", "channels": []}
    unsearched = {"verdict": "unsearched", "summary": "", "channels": []}
    entries = [
        {
            "manga": {"id": "m1", "title": "Galaxy Express 999"},
            "chapters": [wanted_row("82", exhausted), wanted_row("83", unsearched)],
        },
        {
            "manga": {"id": "m2", "title": "Orphan"},
            "chapters": [],
            "no_sources": True,
            "recovery": exhausted,
        },
    ]
    service = DecidingService(database, entries)
    monitor = ReleaseMonitor(
        settings,
        database,
        service,  # type: ignore[arg-type]
        RecordingWorker(),  # type: ignore[arg-type]
    )

    await monitor._notify_decisions()
    assert len(service.sent) == 1
    summary, detail = service.sent[0]
    assert "2 not obtainable" in summary
    assert "Galaxy Express 999 c82" in detail
    assert "Orphan (no source lists it)" in detail
    assert "c83" not in detail

    # Nothing new: silence.
    await monitor._notify_decisions()
    assert len(service.sent) == 1

    # A new verdict is told; the old one is not repeated.
    entries[0]["chapters"][1]["recovery"] = exhausted
    await monitor._notify_decisions()
    assert len(service.sent) == 2
    assert "c83" in service.sent[1][1]
    assert "c82" not in service.sent[1][1]

    settings.ntfy_on_decision_needed = False
    entries[0]["chapters"].append(wanted_row("84", exhausted))
    await monitor._notify_decisions()
    assert len(service.sent) == 2


@pytest.mark.asyncio
async def test_every_series_is_hunted_by_book_for_the_chapters_no_source_offers(
    tmp_path: Path, monkeypatch
):
    """A series followed by chapter is still asked by book: a chapter no
    source offers may sit inside a volume an indexer or the archive holds.
    The map names the book when it can; without one, every book not on disk
    is a candidate."""

    import tankarr.volume_hunt as volume_hunt
    from tankarr.chapter_map import entries_from_releases

    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    seed_orphan(database, "gaps")
    settings = Settings(data_dir=tmp_path / "data", library_dir=tmp_path / "library")
    service = FakeService(database, [])
    service.settings = settings  # type: ignore[attr-defined]
    monitor = ReleaseMonitor(
        settings,
        database,
        service,  # type: ignore[arg-type]
        RecordingWorker(),  # type: ignore[arg-type]
    )
    monitor.torrents = FakeTorrents()  # type: ignore[assignment]
    monkeypatch.setattr(
        database, "get_series_metadata", lambda manga_id: {"data": {"volume_count": 3}}
    )
    asked: list[list[int]] = []

    async def fake_hunt(
        torrents,
        *,
        manga,
        missing_volumes,
        publisher,
        single_volume=False,
        volume_count=None,
        creators=(),
    ):
        asked.append(list(missing_volumes))
        return {"grabbed": [], "errors": []}

    monkeypatch.setattr(volume_hunt, "hunt_volumes", fake_hunt)
    entry = {
        "manga": {"id": "gaps", "title": "Orphan Work"},
        "chapters": [
            {"id": f"expected-{n}", "chapter": str(n), "volume": None, "expected": True}
            for n in (3, 4, 5)
        ],
    }

    await monitor._hunt_missing_volumes(entry)
    assert asked == [[1, 2, 3]]  # no map: every book the catalogue counts

    database.replace_chapter_map(
        "gaps",
        "mangaupdates",
        entries_from_releases([{"volume": "2", "chapter": "3-5"}]),
    )
    await monitor._hunt_missing_volumes(entry)
    assert asked[-1] == [1, 2, 3]  # catalogue membership is only a suggestion
    database.replace_chapter_map(
        "gaps",
        "operator",
        entries_from_releases([{"volume": "2", "chapter": "3-5"}]),
    )
    await monitor._hunt_missing_volumes(entry)
    assert asked[-1] == [2]  # confirmed boundaries name the book


@pytest.mark.asyncio
async def test_book_hunt_retries_deleted_import_and_waits_for_pending_pack(
    tmp_path: Path, monkeypatch
):
    import tankarr.volume_hunt as volume_hunt

    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    seed_orphan(database, "books")
    settings = Settings(data_dir=tmp_path / "data", library_dir=tmp_path / "library")
    service = FakeService(database, [])
    service.settings = settings  # type: ignore[attr-defined]
    monitor = ReleaseMonitor(
        settings,
        database,
        service,  # type: ignore[arg-type]
        RecordingWorker(),  # type: ignore[arg-type]
    )
    monitor.torrents = FakeTorrents()  # type: ignore[assignment]
    monkeypatch.setattr(
        database, "get_series_metadata", lambda _id: {"data": {"volume_count": 3}}
    )
    asked = []

    async def fake_hunt(_torrents, **kwargs):
        asked.append(kwargs["missing_volumes"])
        return {"grabbed": [], "errors": []}

    monkeypatch.setattr(volume_hunt, "hunt_volumes", fake_hunt)

    def download(identifier: str, volume: str):
        return database.create_torrent_download(
            "books",
            {
                "id": identifier,
                "provider": "prowlarr",
                "info_hash": identifier * 40,
                "title": f"Orphan Work v{volume}",
                "category": "Books",
                "source_url": f"https://example.test/{identifier}",
                "volume": volume,
            },
        )

    old = download("a", "1")
    database.update_torrent_download(old["id"], status="imported")
    pack = download("b", "2-3")
    database.update_torrent_download(pack["id"], status="completed")
    entry = {
        "manga": {"id": "books", "title": "Orphan Work"},
        "chapters": [
            {"id": f"expected-{n}", "volume": str(n), "chapter": None}
            for n in (1, 2, 3)
        ],
    }

    await monitor._hunt_missing_volumes(entry)
    assert asked == [[1]]

    database.update_torrent_download(pack["id"], status="failed")
    await monitor._hunt_missing_volumes(entry)
    assert asked[-1] == [1, 2, 3]


@pytest.mark.asyncio
async def test_chapters_the_catalogue_counts_but_nobody_numbers_are_hunted_by_book(
    tmp_path: Path, monkeypatch
):
    import tankarr.volume_hunt as volume_hunt

    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    seed_orphan(database, "counted")
    settings = Settings(data_dir=tmp_path / "data", library_dir=tmp_path / "library")
    service = FakeService(database, [])
    service.settings = settings  # type: ignore[attr-defined]
    monitor = ReleaseMonitor(
        settings,
        database,
        service,  # type: ignore[arg-type]
        RecordingWorker(),  # type: ignore[arg-type]
    )
    monitor.torrents = FakeTorrents()  # type: ignore[assignment]
    monkeypatch.setattr(
        database, "get_series_metadata", lambda manga_id: {"data": {"volume_count": 2}}
    )
    asked: list[list[int]] = []

    async def fake_hunt(
        torrents,
        *,
        manga,
        missing_volumes,
        publisher,
        single_volume=False,
        volume_count=None,
        creators=(),
    ):
        asked.append(list(missing_volumes))
        return {"grabbed": [], "errors": []}

    monkeypatch.setattr(volume_hunt, "hunt_volumes", fake_hunt)
    entry = {
        "manga": {"id": "counted", "title": "Orphan Work"},
        "chapters": [],
        "unmapped_expected_count": 7,
    }

    await monitor._hunt_missing_volumes(entry)
    assert asked == [[1, 2]]


@pytest.mark.asyncio
async def test_manual_ended_pauses_polling_and_automatic_restores_saved_monitoring(
    tmp_path,
):
    from unittest.mock import AsyncMock

    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    database.upsert_manga(
        {"id": "frozen", "title": "Example", "status": "hiatus"}, "en", "all"
    )
    database.update_manga("frozen", {"status_override": "ended"})
    monitor = ReleaseMonitor(
        Settings(
            data_dir=tmp_path,
            library_dir=tmp_path / "library",
            wanted_search_enabled=False,
        ),
        database,
        FakeService(database, []),
        RecordingWorker(),
    )
    monitor.refresh_one = AsyncMock(return_value={"queued": 0})
    assert (await monitor.run_cycle())["checked"] == 0
    monitor.refresh_one.assert_not_awaited()
    database.update_manga("frozen", {"status_override": "automatic"})
    assert database.get_manga("frozen")["monitor_mode"] == "all"
    assert (await monitor.run_cycle())["checked"] == 1
    monitor.refresh_one.assert_awaited_once_with("frozen")


@pytest.mark.asyncio
async def test_manual_ended_excludes_scheduled_wanted_but_allows_manual_recovery(
    tmp_path,
):
    from unittest.mock import AsyncMock

    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    database.upsert_manga(
        {"id": "frozen", "title": "Example", "status": "hiatus"}, "en", "all"
    )
    database.upsert_chapters("frozen", [chapter("missing-1", "1")])
    database.update_manga("frozen", {"status_override": "ended"})
    monitor = ReleaseMonitor(
        Settings(data_dir=tmp_path, library_dir=tmp_path / "library"),
        database,
        FakeService(database, []),
        RecordingWorker(),
    )
    monitor.queue_missing = AsyncMock(return_value=0)
    await monitor.search_wanted(trigger="scheduled")
    monitor.queue_missing.assert_not_awaited()
    await monitor.search_wanted(trigger="manual")
    monitor.queue_missing.assert_awaited_once_with("frozen")


@pytest.mark.asyncio
async def test_a_failed_cycle_does_not_end_the_monitor(tmp_path: Path):
    """One blocked organization or a transient database error used to end
    the monitor task for the life of the process. The loop records the error,
    keeps running and the next cycle succeeds."""

    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    settings = Settings(data_dir=tmp_path / "data", library_dir=tmp_path / "library")
    # Below the configurable floor on purpose: the test needs several cycles.
    settings = settings.model_copy(update={"monitor_interval_seconds": 0.01})
    service = FakeService(database, [])
    service.settings = settings  # type: ignore[attr-defined]
    monitor = ReleaseMonitor(
        settings,
        database,
        service,  # type: ignore[arg-type]
        RecordingWorker(),  # type: ignore[arg-type]
    )
    cycles: list[int] = []

    async def run_cycle(*, include_wanted=True):
        cycles.append(len(cycles))
        if len(cycles) == 1:
            raise RuntimeError("Library organization is blocked")
        return {"checked": 0, "queued": 0, "errors": []}

    monitor.run_cycle = run_cycle  # type: ignore[method-assign]
    await monitor.start()
    try:
        for _ in range(200):
            if len(cycles) >= 3:
                break
            await asyncio.sleep(0.01)
    finally:
        await monitor.stop()
    assert len(cycles) >= 3
    assert monitor.last_cycle_error == "RuntimeError: Library organization is blocked"
