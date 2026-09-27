from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path

import httpx
import pytest

from tankarr.database import Database
from tankarr.providers.base import ProviderHTTP, ProviderRequestError
from tankarr.worker import DownloadWorker


@pytest.mark.asyncio
async def test_failover_error_does_not_cancel_other_downloads(tmp_path, monkeypatch):
    database = Database(tmp_path / "jobs.sqlite3")
    database.initialize()
    database.upsert_manga(
        {"id": "work", "title": "Work", "authors": [], "description": ""},
        "en",
        "all",
    )
    database.upsert_chapters(
        "work",
        [
            {
                "id": f"chapter-{number}",
                "chapter": str(number),
                "title": str(number),
                "language": "en",
                "provider": "local",
                "groups": [],
                "source_url": f"https://example.test/chapter/{number}",
            }
            for number in (1, 2, 3)
        ],
    )
    jobs = [
        database.create_job("work", f"chapter-{number}", "en") for number in (1, 2, 3)
    ]
    both_started = asyncio.Event()
    first_failed_over = asyncio.Event()
    release_other = asyncio.Event()
    cancelled: list[int] = []

    class Service:
        async def process_download_job(self, job_id: int) -> None:
            database.update_job(job_id, status="downloading")
            if job_id == jobs[1]["id"]:
                both_started.set()
            try:
                if job_id == jobs[0]["id"]:
                    await both_started.wait()
                else:
                    await release_other.wait()
                database.update_job(
                    job_id,
                    status="failed" if job_id == jobs[0]["id"] else "completed",
                    result_path=tmp_path / f"{job_id}.cbz",
                )
            except asyncio.CancelledError:
                cancelled.append(job_id)
                raise

        async def failover_failed_release(self, job_id: int) -> list:
            if job_id == jobs[0]["id"]:
                first_failed_over.set()
                raise RuntimeError("source ranking unavailable")
            return []

    worker = DownloadWorker(database, Service())  # type: ignore[arg-type]
    monkeypatch.setattr(worker, "_pipeline_depth", lambda: 2)
    monkeypatch.setattr(worker.pipeline, "slot_ceiling", lambda: 2)
    await worker.start()
    try:
        await asyncio.wait_for(first_failed_over.wait(), timeout=2)
        release_other.set()
        await asyncio.wait_for(worker.queue.join(), timeout=2)
        assert worker.status()["running"] is True
        assert cancelled == []
        assert database.get_job(jobs[0]["id"])["status"] == "failed"
        assert all(
            database.get_job(job["id"])["status"] == "completed" for job in jobs[1:]
        )
        assert "source ranking unavailable" in worker.status()["last_job_error"]
    finally:
        await worker.stop()


def test_malformed_source_url_cannot_break_queue_selection():
    assert (
        DownloadWorker._source_key(
            {
                "chapter_source_url": "https://[invalid",
                "chapter_source_name": "Example",
                "chapter_provider": "suwayomi",
            }
        )
        == "suwayomi:example"
    )


@pytest.mark.asyncio
async def test_waiting_slot_obeys_a_new_lower_concurrency_limit(tmp_path, monkeypatch):
    database = Database(tmp_path / "limits.sqlite3")
    database.initialize()
    database.upsert_manga(
        {"id": "work", "title": "Work", "authors": [], "description": ""},
        "en",
        "all",
    )
    database.upsert_chapters(
        "work",
        [
            {
                "id": f"chapter-{number}",
                "chapter": str(number),
                "title": str(number),
                "language": "en",
                "provider": "local",
                "groups": [],
                "source_url": f"https://example.test/chapter/{number}",
            }
            for number in (1, 2)
        ],
    )
    active = 0
    maximum_active = 0
    started = asyncio.Event()
    release = asyncio.Event()
    waiting = asyncio.Event()
    acquisitions = 0
    depth = 2

    class Service:
        async def process_download_job(self, job_id: int) -> None:
            nonlocal active, maximum_active
            database.update_job(job_id, status="downloading")
            active += 1
            maximum_active = max(maximum_active, active)
            started.set()
            try:
                await release.wait()
                database.update_job(job_id, status="completed")
            finally:
                active -= 1

        async def failover_failed_release(self, _job_id: int) -> list:
            return []

    worker = DownloadWorker(database, Service())  # type: ignore[arg-type]
    original_get = worker.queue.get

    async def get():
        nonlocal acquisitions
        acquisitions += 1
        if acquisitions == 2:
            waiting.set()
        return await original_get()

    monkeypatch.setattr(worker.queue, "get", get)
    monkeypatch.setattr(worker, "_pipeline_depth", lambda: depth)
    monkeypatch.setattr(worker.pipeline, "slot_ceiling", lambda: 2)
    await worker.start()
    try:
        await asyncio.wait_for(waiting.wait(), timeout=1)
        depth = 1
        for number in (1, 2):
            job = database.create_job("work", f"chapter-{number}", "en")
            await worker.enqueue(job["id"])
        await asyncio.wait_for(started.wait(), timeout=1)
        release.set()
        await asyncio.wait_for(worker.queue.join(), timeout=2)
        assert maximum_active == 1
        assert worker.status()["running"] is True
    finally:
        await worker.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["empty", "cancelled", "write"])
async def test_failed_stream_preserves_destination_and_cleans_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
):
    destination = tmp_path / "chapter.cbz"
    destination.write_bytes(b"existing valid archive")
    stream_started = asyncio.Event()
    closed = asyncio.Event()

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            if failure == "empty":
                return
            yield b"x" * (256 * 1024)
            stream_started.set()
            if failure == "cancelled":
                await asyncio.Event().wait()

        async def aclose(self):
            closed.set()

    client = ProviderHTTP(requests_per_second=1000, max_attempts=1)
    await client._client.aclose()
    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=Stream()))
    )
    if failure == "write":

        def refuse_replace(*_args, **_kwargs):
            raise OSError("disk unavailable")

        monkeypatch.setattr(Path, "replace", refuse_replace)
    try:
        transfer = asyncio.create_task(
            client.download_file("https://example.test/archive", destination)
        )
        if failure == "cancelled":
            await asyncio.wait_for(stream_started.wait(), timeout=1)
            transfer.cancel()
            with pytest.raises(asyncio.CancelledError):
                await transfer
        else:
            expected = ProviderRequestError if failure == "empty" else OSError
            with pytest.raises(expected):
                await transfer
        assert destination.read_bytes() == b"existing valid archive"
        assert not destination.with_suffix(".cbz.part").exists()
        assert closed.is_set()
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_retry_after_accepts_http_date_and_rejects_nonfinite_values(monkeypatch):
    client = ProviderHTTP(backoff_base_seconds=2, backoff_cap_seconds=30)
    monkeypatch.setattr("tankarr.providers.base.random.random", lambda: 0)
    try:
        response = httpx.Response(
            429,
            headers={
                "Retry-After": format_datetime(
                    datetime.now(UTC) + timedelta(seconds=20)
                )
            },
        )
        assert 18 <= client._retry_delay(0, response) <= 20
        assert (
            client._retry_delay(0, httpx.Response(429, headers={"Retry-After": "-10"}))
            == 0
        )
        assert (
            client._retry_delay(0, httpx.Response(429, headers={"Retry-After": "nan"}))
            == 1
        )
        assert (
            client._retry_delay(0, httpx.Response(429, headers={"Retry-After": "inf"}))
            == 1
        )
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_stream_connection_closes_before_retry_backoff(tmp_path, monkeypatch):
    closed = asyncio.Event()
    attempts = 0

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"unused"

        async def aclose(self):
            closed.set()

    def response(_request):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, stream=Stream())
        return httpx.Response(200, content=b"archive")

    client = ProviderHTTP(requests_per_second=1000, max_attempts=2)
    await client._client.aclose()
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(response))

    async def acquired(_priority):
        return 0

    async def sleep(_delay):
        assert closed.is_set(), "retry kept an unused response/connection open"

    monkeypatch.setattr(client.limiter, "acquire", acquired)
    monkeypatch.setattr("tankarr.providers.base.asyncio.sleep", sleep)
    try:
        destination = await client.download_file(
            "https://example.test/archive", tmp_path / "chapter.cbz"
        )
        assert destination.read_bytes() == b"archive"
        assert attempts == 2
    finally:
        await client.aclose()
