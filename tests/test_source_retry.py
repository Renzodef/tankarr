from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from tankarr.monitor import ReleaseMonitor
from tankarr.providers.base import ProviderRequestError
from tankarr.release_sources import ReleaseSourceManager
from tankarr.service import RecoveryBlocked
from tests.test_deletion import make_service, manga
from tests.test_monitor import RecordingWorker
from tests.test_release_sources import FakeProvider, chapter


class FailingPrimary(FakeProvider):
    name = "primary"

    def __init__(self, *, failure_at: str = "get_manga"):
        super().__init__([], {})
        self.failure_at = failure_at
        self.calls = 0

    async def get_manga(self, manga_id: str) -> dict:
        if self.failure_at == "get_manga":
            self.calls += 1
            raise ProviderRequestError("Source temporarily unavailable")
        return {**manga(manga_id), "provider": self.name}

    async def list_chapters(self, manga_id: str, language: str) -> list[dict]:
        self.calls += 1
        raise ProviderRequestError("Source temporarily unavailable")


class RecordingAlternate(FakeProvider):
    def __init__(self):
        super().__init__(
            [],
            {
                "alternate-series": [
                    {
                        **chapter("alternate-1", "1"),
                        "volume": None,
                        "source_name": "Alternate",
                        "source_key": "alternate:1",
                    }
                ]
            },
        )
        self.calls = 0
        self.error: Exception | None = None

    async def list_chapters(self, manga_id: str, language: str) -> list[dict]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return await super().list_chapters(manga_id, language)


def make_monitor(tmp_path: Path, *, failure_at: str = "get_manga"):
    database, service, _ = make_service(tmp_path)
    database.upsert_manga(
        {**manga(), "provider": "primary", "last_chapter": "1"}, "en", "all"
    )
    database.update_manga(
        "manga-1",
        {"expected_count_override": 1, "expected_count_unit_override": "chapter"},
    )
    database.upsert_release_source(
        "manga-1",
        provider="alternate",
        provider_manga_id="alternate-series",
        title="Example",
        source_url="https://example.test/alternate-series",
        source_name="Alternate",
        language="en",
        match_confidence=1,
        match_reason="fixture",
    )
    primary = FailingPrimary(failure_at=failure_at)
    alternate = RecordingAlternate()
    service.providers = {"primary": primary, "alternate": alternate}
    manager = ReleaseSourceManager(database, service.providers)
    worker = RecordingWorker()
    monitor = ReleaseMonitor(service.settings, database, service, worker, manager)
    return monitor, primary, alternate


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_at", ["get_manga", "list_chapters"])
async def test_refresh_retries_primary_and_queues_new_alternative_release(
    tmp_path: Path, failure_at: str
):
    monitor, primary, alternate = make_monitor(tmp_path, failure_at=failure_at)
    monitor.database.update_manga("manga-1", {"monitor_mode": "future"})
    monitor.database.record_monitor_result("manga-1")
    for _ in range(8):
        monitor.database.record_source_health("primary", ok=False, reason="offline")
    assert "primary" in monitor.database.global_demoted_sources()

    result = await monitor.refresh_one("manga-1")

    assert primary.calls == alternate.calls == 1
    assert result["seen"] == result["new"] == result["queued"] == 1
    assert monitor.database.list_jobs()[0]["chapter_id"] == "alternate-1"
    assert monitor.database.get_manga("manga-1")["primary_source_error"]
    assert monitor.database.get_manga("manga-1")["last_check_error"] is None

    again = await monitor.refresh_one("manga-1")

    assert primary.calls == alternate.calls == 2
    assert again["new"] == again["queued"] == 0


@pytest.mark.asyncio
async def test_wanted_search_continues_discovery_and_queue_after_primary_failure(
    tmp_path: Path,
):
    monitor, primary, alternate = make_monitor(tmp_path)
    assert len(monitor.service.list_wanted()) == 1

    result = await monitor._search_wanted_once(trigger="test")

    assert primary.calls == alternate.calls == 1
    assert result["discovered"] == result["queued"] == 1
    assert result["errors"] == []
    assert monitor.database.list_jobs()[0]["chapter_id"] == "alternate-1"
    assert len(monitor.worker.enqueued) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["refresh", "wanted"])
@pytest.mark.parametrize("error_type", [RecoveryBlocked, asyncio.CancelledError])
async def test_recovery_block_and_cancellation_stop_before_alternative_sources(
    tmp_path: Path, path: str, error_type: type[BaseException], monkeypatch
):
    monitor, _, alternate = make_monitor(tmp_path)
    monkeypatch.setattr(
        monitor.service, "refresh_manga", AsyncMock(side_effect=error_type("stop"))
    )

    with pytest.raises(error_type):
        if path == "refresh":
            await monitor.refresh_one("manga-1")
        else:
            await monitor._search_wanted_once(trigger="test")
    assert alternate.calls == 0


@pytest.mark.asyncio
async def test_database_failure_is_not_treated_as_primary_source_failure(
    tmp_path: Path, monkeypatch
):
    monitor, primary, alternate = make_monitor(tmp_path, failure_at="list_chapters")
    monkeypatch.setattr(
        monitor.database,
        "upsert_manga",
        Mock(side_effect=sqlite3.OperationalError("database is read-only")),
    )

    with pytest.raises(sqlite3.OperationalError, match="read-only"):
        await monitor.refresh_one("manga-1")
    assert primary.calls == alternate.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("unsupported_by", ["mapping", "provider"])
async def test_incompatible_language_keeps_previous_error_and_health_unchanged(
    tmp_path: Path, unsupported_by: str, monkeypatch
):
    monitor, _, alternate = make_monitor(tmp_path)
    database = monitor.database
    database.record_release_source_result(
        "manga-1", "alternate", "alternate-series", error="previous request failed"
    )
    if unsupported_by == "mapping":
        database.update_manga("manga-1", {"preferred_language": "it"})
        monkeypatch.setattr(alternate, "supports_language", lambda language: True)
    else:
        monkeypatch.setattr(alternate, "supports_language", lambda language: False)
    before_mapping = database.list_release_sources("manga-1")
    before_health = database.source_health_rows()

    result = await monitor.release_sources.refresh_mappings("manga-1", monitor_new=True)

    assert alternate.calls == 0
    assert result["sources"][0]["state"] == "unsupported"
    assert result["seen"] == 0
    assert database.list_release_sources("manga-1") == before_mapping
    assert database.source_health_rows() == before_health


@pytest.mark.asyncio
async def test_demoted_mapping_is_retried_and_success_clears_its_error(
    tmp_path: Path,
):
    monitor, _, alternate = make_monitor(tmp_path)
    manager = monitor.release_sources
    database = monitor.database
    await manager.refresh_mappings("manga-1", monitor_new=True)
    alternate.error = ProviderRequestError("No chapters found")
    for _ in range(8):
        with database.connect() as connection:
            connection.execute("UPDATE source_circuit SET next_retry_at=0")
        failed = await manager.refresh_mappings("manga-1", monitor_new=True)
        assert failed["sources"][0]["state"] == "error"
    assert "alternate:1" in database.global_demoted_sources()
    alternate.error = None
    with database.connect() as connection:
        connection.execute("UPDATE source_circuit SET next_retry_at=0")

    result = await manager.refresh_mappings("manga-1", monitor_new=True)

    assert alternate.calls == 10
    assert result["seen"] == 1
    assert result["sources"][0]["state"] == "matched"
    mapping = database.list_release_sources("manga-1")[0]
    assert mapping["last_error"] is None
    assert mapping["error_since"] is None
