from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from tankarr.database import Database
from tankarr.worker import DownloadWorker


@pytest.mark.asyncio
async def test_pause_drains_active_job_and_retains_pending_job_across_restart(
    tmp_path, monkeypatch
):
    database = Database(tmp_path / "pause.sqlite3")
    database.initialize()
    database.upsert_manga(
        {"id": "work", "title": "Work", "authors": [], "available_languages": ["en"]},
        "en",
        "all",
    )
    database.upsert_chapters(
        "work",
        [
            {
                "id": f"c{i}",
                "chapter": str(i),
                "language": "en",
                "provider": "local",
                "groups": [],
                "source_url": f"https://example.test/{i}",
            }
            for i in (1, 2)
        ],
    )
    jobs = [database.create_job("work", f"c{i}", "en") for i in (1, 2)]
    started = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()

    class Service:
        settings = SimpleNamespace(downloads_paused=False)
        processed = []

        async def process_download_job(self, job_id):
            self.processed.append(job_id)
            database.update_job(job_id, status="downloading")
            started.set()
            await release.wait()
            database.update_job(job_id, status="completed")
            finished.set()

        async def failover_failed_release(self, _job_id):
            return []

    service = Service()
    worker = DownloadWorker(database, service)
    monkeypatch.setattr(worker, "_pipeline_depth", lambda: 1)
    monkeypatch.setattr(worker.pipeline, "slot_ceiling", lambda: 1)
    await worker.start()
    try:
        await asyncio.wait_for(started.wait(), 1)
        service.settings.downloads_paused = True
        release.set()
        await asyncio.wait_for(finished.wait(), 1)
        await asyncio.sleep(0.05)
        assert database.get_job(jobs[0]["id"])["status"] == "completed"
        assert database.get_job(jobs[1]["id"])["status"] == "queued"
        assert worker.status()["paused"] is True
        await worker.stop()
        await worker.start()
        await asyncio.sleep(0.05)
        assert service.processed == [jobs[0]["id"]]
        service.settings.downloads_paused = False
        await asyncio.wait_for(worker.queue.join(), 2)
        assert service.processed == [job["id"] for job in jobs]
    finally:
        await worker.stop()


class MissingJobDatabase:
    def list_queued_job_ids(self) -> list[int]:
        return []

    def list_queued_job_heads(self, _exclude_job_ids=()) -> list[dict]:
        return []

    def get_job(self, _: int) -> dict:
        raise KeyError


class RecordingService:
    def __init__(self):
        self.processed: list[int] = []

    async def process_download_job(self, job_id: int) -> None:
        self.processed.append(job_id)


@pytest.mark.asyncio
async def test_worker_skips_job_deleted_while_id_remains_queued():
    service = RecordingService()
    worker = DownloadWorker(
        MissingJobDatabase(),  # type: ignore[arg-type]
        service,  # type: ignore[arg-type]
    )
    await worker.start()
    try:
        await worker.enqueue(42)
        await asyncio.wait_for(worker.queue.join(), timeout=1)
        assert service.processed == []
        assert worker.task is not None
        assert not worker.task.done()
        assert worker.status()["running"] is True
    finally:
        await worker.stop()


@pytest.mark.asyncio
async def test_worker_pipelines_two_downloads_but_keeps_a_bounded_depth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    database.upsert_manga(
        {
            "id": "work",
            "title": "Work",
            "description": "",
            "authors": [],
            "available_languages": ["en"],
        },
        "en",
        "all",
    )
    database.upsert_chapters(
        "work",
        [
            {
                "id": f"chapter-{number}",
                "chapter": str(number),
                "volume": "1",
                "title": str(number),
                "language": "en",
                "provider": "local",
                "groups": [],
                "publish_at": f"2026-08-{number:02d}T00:00:00Z",
                "source_url": f"https://example.test/{number}",
            }
            for number in (1, 2)
        ],
    )
    jobs = [database.create_job("work", f"chapter-{number}", "en") for number in (1, 2)]

    class PipelinedService:
        def __init__(self):
            self.active = 0
            self.maximum_active = 0
            self.both_started = asyncio.Event()
            self.release = asyncio.Event()

        async def process_download_job(self, job_id: int) -> None:
            database.update_job(job_id, status="downloading")
            self.active += 1
            self.maximum_active = max(self.maximum_active, self.active)
            if self.active == 2:
                self.both_started.set()
            try:
                await self.release.wait()
                database.update_job(
                    job_id,
                    status="completed",
                    progress=1,
                    result_path=tmp_path / f"{job_id}.cbz",
                )
            finally:
                self.active -= 1

        async def failover_failed_release(self, _job_id: int) -> list:
            return []

    service = PipelinedService()
    worker = DownloadWorker(database, service)  # type: ignore[arg-type]
    monkeypatch.setattr(worker, "_pipeline_depth", lambda: 2)
    monkeypatch.setattr(worker.pipeline, "slot_ceiling", lambda: 2)
    await worker.start()
    try:
        await asyncio.wait_for(service.both_started.wait(), timeout=1)
        assert worker.status()["current_job_ids"] == [job["id"] for job in jobs]
        service.release.set()
        await asyncio.wait_for(worker.queue.join(), timeout=1)
        assert service.maximum_active == 2
    finally:
        await worker.stop()


@pytest.mark.asyncio
async def test_worker_prefers_an_idle_series_and_source(tmp_path: Path, monkeypatch):
    database = Database(tmp_path / "fair.sqlite3")
    database.initialize()
    for manga_id, count, host in (
        ("short", 1, "slow.test"),
        ("medium", 2, "slow.test"),
        ("long", 3, "fast.test"),
    ):
        database.upsert_manga(
            {
                "id": manga_id,
                "title": manga_id,
                "description": "",
                "authors": [],
                "available_languages": ["en"],
            },
            "en",
            "all",
        )
        database.upsert_chapters(
            manga_id,
            [
                {
                    "id": f"{manga_id}-{number}",
                    "chapter": str(number),
                    "volume": "1",
                    "title": str(number),
                    "language": "en",
                    "provider": "suwayomi",
                    "groups": [],
                    "publish_at": None,
                    "source_url": f"https://{host}/{manga_id}/{number}",
                }
                for number in range(1, count + 1)
            ],
        )
        for number in range(1, count + 1):
            database.create_job(manga_id, f"{manga_id}-{number}", "en")

    class FairService:
        def __init__(self):
            self.started: list[str] = []
            self.two_started = asyncio.Event()
            self.release = asyncio.Event()

        async def process_download_job(self, job_id: int) -> None:
            job = database.get_job(job_id)
            database.update_job(job_id, status="downloading")
            self.started.append(job["manga_id"])
            if len(self.started) == 2:
                self.two_started.set()
            await self.release.wait()
            database.update_job(
                job_id,
                status="completed",
                progress=1,
                result_path=tmp_path / f"{job_id}.cbz",
            )

        async def failover_failed_release(self, _job_id: int) -> list:
            return []

    service = FairService()
    worker = DownloadWorker(database, service)  # type: ignore[arg-type]
    monkeypatch.setattr(worker, "_pipeline_depth", lambda: 2)
    monkeypatch.setattr(worker.pipeline, "slot_ceiling", lambda: 2)
    await worker.start()
    try:
        await asyncio.wait_for(service.two_started.wait(), timeout=2)
        assert set(service.started[:2]) == {"short", "long"}
        assert worker.status()["active_series"] == 2
        assert worker.status()["active_sources"] == 2
        service.release.set()
        await asyncio.wait_for(worker.queue.join(), timeout=2)
    finally:
        await worker.stop()
