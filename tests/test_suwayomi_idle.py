from __future__ import annotations

import sys
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from tankarr.config import Settings
from tankarr.monitor import ReleaseMonitor
from tankarr.suwayomi_runtime import SuwayomiRuntime
from tests.test_suwayomi_runtime import fake_installed, python_command


def _runtime(tmp_path, *, minutes: int = 10) -> SuwayomiRuntime:
    root = tmp_path / "suwayomi"
    fake_installed(root)
    runtime = SuwayomiRuntime(
        root,
        java_executable=sys.executable,
        command_builder=python_command("import time; time.sleep(30)"),
        ready_probe=lambda: True,
    )
    runtime.configure_idle(minutes)
    return runtime


@pytest.mark.asyncio
async def test_status_reports_idle_without_starting_the_server(tmp_path):
    runtime = _runtime(tmp_path)
    status = runtime.status()
    assert status["running"] is False
    assert status["activity"] == "idle"
    assert runtime._process is None
    await runtime.aclose()


@pytest.mark.asyncio
async def test_retain_is_shared_and_idle_stop_respects_the_timeout(tmp_path):
    runtime = _runtime(tmp_path)
    async with runtime.retain():
        assert runtime.running is True
        assert runtime.in_use == 1
        async with runtime.retain():
            assert runtime.in_use == 2
        assert runtime.in_use == 1
    assert runtime.in_use == 0
    assert runtime.running is True
    assert await runtime.stop_if_idle() is False

    runtime._last_used = time.monotonic() - 1000
    runtime.needed_within = lambda: 60.0
    assert await runtime.stop_if_idle() is False
    assert runtime.running is True

    runtime.needed_within = lambda: 10_000.0
    assert await runtime.stop_if_idle() is True
    assert runtime.running is False
    assert runtime.activity() == "idle"
    await runtime.aclose()


@pytest.mark.asyncio
async def test_zero_timeout_and_an_operator_stop_stay_distinct(tmp_path):
    runtime = _runtime(tmp_path, minutes=0)
    await runtime.start()
    runtime._last_used = time.monotonic() - 10_000
    assert await runtime.stop_if_idle() is False
    assert runtime.running is True
    await runtime.stop(operator=True)
    assert runtime.activity() == "stopped"
    await runtime.aclose()


@pytest.mark.asyncio
async def test_one_monitor_hold_covers_the_whole_pass(tmp_path):
    settings = Settings(
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "library",
        suwayomi_enabled=True,
        suwayomi_mode="managed",
        monitor_enabled=True,
        monitor_interval_seconds=1800,
        wanted_search_enabled=False,
    )
    entered = 0

    @asynccontextmanager
    async def session():
        nonlocal entered
        entered += 1
        yield

    monitor = SimpleNamespace(
        suwayomi_session=session,
        settings=settings,
        cycle_active=False,
        last_cycle_at=None,
        _wanted_search_task=None,
    )
    monitor._seconds_until_cycle = lambda: ReleaseMonitor._seconds_until_cycle(monitor)
    monitor._seconds_until_wanted = lambda: ReleaseMonitor._seconds_until_wanted(
        monitor
    )
    async with ReleaseMonitor._suwayomi_hold(monitor):
        async with ReleaseMonitor._suwayomi_hold(monitor):
            assert entered == 2
    assert entered == 2
    assert ReleaseMonitor._seconds_until_cycle(monitor) == 0.0
    monitor.last_cycle_at = (datetime.now(UTC) - timedelta(seconds=30)).isoformat()
    gap = ReleaseMonitor.seconds_until_suwayomi(monitor)
    assert 1500 < gap < 1800
