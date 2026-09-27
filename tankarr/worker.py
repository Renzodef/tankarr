from __future__ import annotations

import asyncio
import logging
from collections import Counter
from urllib.parse import urlsplit

from tankarr.database import Database
from tankarr.download_tuning import AdaptivePipelineConcurrency
from tankarr.service import RecoveryBlocked, TankarrService

logger = logging.getLogger(__name__)


class DownloadWorker:
    def __init__(self, database: Database, service: TankarrService):
        self.database = database
        self.service = service
        self.queue: asyncio.Queue[int] = asyncio.Queue()
        self.task: asyncio.Task | None = None
        self.enqueued: set[int] = set()
        self.current_job_id: int | None = None
        self.current_task: asyncio.Task[None] | None = None
        self._active_tasks: dict[int, asyncio.Task[None]] = {}
        self._active_jobs: dict[int, dict] = {}
        self._selection_lock = asyncio.Lock()
        self._cancel_lock = asyncio.Lock()
        self._stopping = False
        self._successful_import_since_drain = False
        self._last_job_error: str | None = None
        self.pipeline = AdaptivePipelineConcurrency(
            configured_ceiling=int(
                getattr(
                    getattr(self.service, "settings", None),
                    "download_pipeline_max",
                    0,
                )
            )
        )

    async def start(self) -> None:
        self._stopping = False
        for job_id in self.database.list_queued_job_ids():
            await self.enqueue(job_id)
        self.task = asyncio.create_task(self._run(), name="tankarr-download-worker")

    async def stop(self) -> None:
        if self.task is None:
            return
        self._stopping = True
        self.task.cancel()
        try:
            await self.task
        except asyncio.CancelledError:
            pass
        self.task = None
        self._active_tasks.clear()
        self._active_jobs.clear()
        self.current_task = None
        self.current_job_id = None

    async def enqueue(self, job_id: int) -> None:
        if job_id in self.enqueued:
            return
        self.enqueued.add(job_id)
        await self.queue.put(job_id)

    def status(self) -> dict:
        task = self.task
        running = task is not None and not task.done()
        error = None
        if task is not None and task.done() and not task.cancelled():
            exception = task.exception()
            if exception is not None:
                error = f"{type(exception).__name__}: {exception}"
        pipeline_status = self.pipeline.status()
        pipeline_depth = int(
            pipeline_status.get("effective") or self.pipeline.current_concurrency or 1
        )
        return {
            "running": running,
            "paused": self._downloads_paused(),
            "current_job_id": self.current_job_id,
            "current_job_ids": sorted(self._active_tasks),
            "queued_in_memory": self.queue.qsize(),
            "pipeline_depth": pipeline_depth,
            "pipeline_maximum": self.pipeline.maximum_concurrency,
            "active_series": len(
                {str(job.get("manga_id") or "") for job in self._active_jobs.values()}
            ),
            "active_sources": len(
                {self._source_key(job) for job in self._active_jobs.values()}
            ),
            "adaptive": pipeline_status,
            "error": error,
            "last_job_error": self._last_job_error,
        }

    def _downloads_paused(self) -> bool:
        return bool(
            getattr(getattr(self.service, "settings", None), "downloads_paused", False)
        )

    async def _wait_for_downloads(self) -> None:
        while self._downloads_paused():
            await asyncio.sleep(0.5)

    def _pipeline_depth(self) -> int:
        provider = getattr(self.service, "provider", None)
        tuning = getattr(provider, "page_concurrency", None)
        page_status = tuning.status() if tuning is not None else None
        return self.pipeline.recommend(
            len(self._active_tasks),
            page_status,
            configured_ceiling=int(
                getattr(
                    getattr(self.service, "settings", None),
                    "download_pipeline_max",
                    0,
                )
            ),
        )

    @staticmethod
    def _source_key(job: dict) -> str:
        raw_url = str(job.get("chapter_source_url") or "").strip()
        try:
            host = urlsplit(raw_url).hostname if raw_url else None
        except ValueError:
            # Provider metadata can contain a malformed URL. Fair scheduling
            # must still work; fetching the release will validate its URL.
            host = None
        source = str(job.get("chapter_source_name") or "").strip().casefold()
        provider = str(job.get("chapter_provider") or "unknown").strip().casefold()
        return f"{provider}:{host or source or 'unknown'}"

    def _select_queued_job(self) -> dict | None:
        heads = self.database.list_queued_job_heads(self._active_tasks)
        if not heads:
            return None
        series_load = Counter(
            str(job.get("manga_id") or "") for job in self._active_jobs.values()
        )
        source_load = Counter(
            self._source_key(job) for job in self._active_jobs.values()
        )
        _rank, selected = min(
            enumerate(heads),
            key=lambda item: (
                series_load[str(item[1].get("manga_id") or "")],
                source_load[self._source_key(item[1])],
                item[0],
            ),
        )
        try:
            return self.database.get_job(int(selected["id"]))
        except KeyError:
            return None

    def _refresh_current_task(self) -> None:
        first = min(self._active_tasks, default=None)
        self.current_job_id = first
        self.current_task = self._active_tasks.get(first) if first is not None else None

    async def remove(self, job_id: int) -> dict:
        """Remove history/queue entries and safely interrupt active downloads."""

        async with self._cancel_lock:
            job = self.database.get_job(job_id)
            if job["status"] == "queued":
                return self.database.cancel_job(job_id)
            if job["status"] not in {
                "running",
                "downloading",
                "packaging",
                "importing",
            }:
                return self.database.delete_job(job_id)
            if job["status"] == "importing":
                raise ValueError(
                    "Wait for the atomic library import to finish before removing this job"
                )
            task = self._active_tasks.get(job_id)
            if task is None or task.done():
                raise ValueError("The active job is changing state; retry cancellation")

            # The service lock also guards the short atomic import commit.  If
            # import won the race, its status is now 'importing' and we fail
            # closed. Otherwise cancellation cannot race into publication.
            async with self.service._mutation_lock:
                current = self.database.get_job(job_id)
                if current["status"] == "importing":
                    raise ValueError(
                        "Wait for the atomic library import to finish before removing this job"
                    )
                task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            return self.database.cancel_job(job_id)

    async def _failover_failed_release(self, job_id: int) -> None:
        """Queue replacement releases after a terminal download failure."""

        try:
            replacements = await self.service.failover_failed_release(job_id)
        except RecoveryBlocked:
            raise
        except Exception as exc:  # noqa: BLE001 - isolate optional recovery work
            # The original job already records its outcome. A ranking, metadata,
            # or notification failure here must not cancel other active slots.
            # Do not retry blindly or block another release on this error: the
            # next Wanted reconciliation can attempt recovery again.
            self._last_job_error = f"Job {job_id} failover: {type(exc).__name__}: {exc}"
            logger.exception("Failed to select a replacement for job %s", job_id)
            return
        for job in replacements:
            if job["status"] == "queued":
                await self.enqueue(job["id"])

    async def _run_slot(self, slot: int) -> None:
        while True:
            await self._wait_for_downloads()
            while slot >= self._pipeline_depth():
                await asyncio.sleep(2)
            token = await self.queue.get()
            requeue = False
            idle = False
            job_id = token
            processing: asyncio.Task[None] | None = None
            try:
                # Pause can be requested while this slot awaits a queue token
                # or source recovery. Retain the token until resumed.
                await self._wait_for_downloads()
                async with self._selection_lock:
                    if self._downloads_paused():
                        continue
                    # The slot may have been waiting on an empty queue while
                    # memory/source pressure lowered the permitted depth.
                    # Recheck before starting any work, not only before wait.
                    if slot >= self._pipeline_depth():
                        continue
                    # Queue entries only wake a slot. Re-read one durable head
                    # per series and prefer an idle series/source before giving
                    # a second slot to either.
                    job = self._select_queued_job()
                    if job is None:
                        idle = True
                        continue
                    job_id = int(job["id"])
                    if job["status"] != "queued":
                        continue
                    processing = asyncio.create_task(
                        self.service.process_download_job(job_id),
                        name=f"tankarr-download-{job_id}",
                    )
                    self._active_tasks[job_id] = processing
                    self._active_jobs[job_id] = job
                    self._refresh_current_task()
                try:
                    try:
                        await processing
                        try:
                            completed = self.database.get_job(job_id)
                        except KeyError:
                            completed = None
                        if (
                            completed is not None
                            and completed.get("status") == "completed"
                            and completed.get("result_path")
                        ):
                            self._successful_import_since_drain = True
                        if (
                            completed is not None
                            and completed.get("status") == "queued"
                        ):
                            requeue = True  # transient failure: back in line
                    except asyncio.CancelledError:
                        if self._stopping:
                            raise
                    await self._failover_failed_release(job_id)
                except RecoveryBlocked:
                    # A runtime recovery failure must not kill the long-lived
                    # worker task or mutate the queued job.
                    continue
            finally:
                async with self._selection_lock:
                    if self._active_tasks.get(job_id) is processing:
                        self._active_tasks.pop(job_id, None)
                        self._active_jobs.pop(job_id, None)
                    self._refresh_current_task()
                self.enqueued.discard(job_id)
                self.enqueued.discard(token)
                if requeue:
                    await self.enqueue(job_id)
                remaining = [
                    candidate
                    for candidate in self.database.list_queued_job_ids()
                    if candidate not in self._active_tasks
                ]
                if remaining and self.queue.empty():
                    self.enqueued.discard(remaining[0])
                    await self.enqueue(remaining[0])
                should_refresh = False
                async with self._selection_lock:
                    if (
                        self._successful_import_since_drain
                        and self.queue.empty()
                        and not self.enqueued
                        and not self._active_tasks
                    ):
                        self._successful_import_since_drain = False
                        should_refresh = True
                if should_refresh:
                    refresh = getattr(self.service, "refresh_komga_library", None)
                    if refresh is not None:
                        try:
                            await refresh(reason="download_queue_drained")
                        except Exception:  # noqa: BLE001 - maintenance retries it
                            pass
                self.queue.task_done()
                if idle and remaining:
                    # A persisted retry may leave every pending job ineligible.
                    # Wait outside the selection lock, never for deleted jobs.
                    await asyncio.sleep(1)

    async def _run(self) -> None:
        slot_ceiling = self.pipeline.slot_ceiling()
        slots = [
            asyncio.create_task(
                self._run_slot(slot), name=f"tankarr-download-pipeline-{slot}"
            )
            for slot in range(slot_ceiling)
        ]
        try:
            await asyncio.gather(*slots)
        finally:
            for task in slots:
                task.cancel()
            await asyncio.gather(*slots, return_exceptions=True)
