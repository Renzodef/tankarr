from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from tankarr.app import create_app
from tankarr.config import Settings
from tankarr.tasks import (
    ScheduledTask,
    TaskBusy,
    TaskNotRunnable,
    TaskRegistry,
    describe_interval,
)
from tankarr.updates import RELEASES_API


def provision_library_identity(settings: Settings) -> None:
    identity = "0123456789abcdef0123456789abcdef"
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.library_dir.mkdir(parents=True, exist_ok=True)
    (settings.data_dir / ".tankarr-library-id").write_text(f"{identity}\n")
    (settings.library_dir / ".tankarr-library-id").write_text(f"{identity}\n")


def test_intervals_read_like_a_schedule():
    assert describe_interval(None) == "Manual"
    assert describe_interval(900) == "Every 15 minutes"
    assert describe_interval(3600) == "Every hour"
    assert describe_interval(21600) == "Every 6 hours"
    assert describe_interval(86400) == "Every day"
    assert describe_interval(90) == "Every 90 seconds"


async def test_the_registry_describes_runs_and_refuses_a_second_pass():
    registry = TaskRegistry()
    gate = asyncio.Event()
    calls: list[str] = []

    async def slow():
        calls.append("slow")
        await gate.wait()
        return {"done": True}

    async def failing():
        raise RuntimeError("no service at http://user:secret@host/api")

    registry.register(
        ScheduledTask(
            "slow",
            "Slow task",
            "Waits for the gate.",
            status=lambda: {
                "enabled": True,
                "last_run_at": "2026-09-28T10:00:00+00:00",
            },
            run=slow,
            interval_seconds=lambda: 3600,
        )
    )
    registry.register(
        ScheduledTask(
            "failing", "Failing task", "Always fails.", status=lambda: {}, run=failing
        )
    )
    registry.register(
        ScheduledTask(
            "readonly",
            "Read-only task",
            "Only reports.",
            status=lambda: {"enabled": False},
        )
    )

    rows = {row["id"]: row for row in registry.snapshot()}
    assert rows["slow"]["schedule"] == "Every hour"
    assert rows["slow"]["next_run_at"] == "2026-09-28T11:00:00+00:00"
    assert rows["slow"]["can_run"] is True and rows["slow"]["running"] is False
    assert rows["readonly"]["can_run"] is False
    assert rows["readonly"]["next_run_at"] is None

    first = asyncio.create_task(registry.run("slow"))
    await asyncio.sleep(0)
    assert registry.snapshot()[0]["running"] is True
    with pytest.raises(TaskBusy):
        await registry.run("slow")
    gate.set()
    outcome = await first
    assert outcome["ok"] is True
    assert outcome["result"] == {"done": True}
    assert outcome["task"]["manual"]["duration_ms"] >= 0
    assert calls == ["slow"]

    failed = await registry.run("failing")
    assert failed["ok"] is False
    assert failed["error"].startswith("RuntimeError")
    assert "secret" not in failed["error"]
    with pytest.raises(TaskNotRunnable):
        await registry.run("readonly")
    with pytest.raises(KeyError):
        await registry.run("missing")


@respx.mock
def test_the_system_page_lists_the_tasks_and_runs_one(tmp_path: Path):
    respx.get(RELEASES_API).mock(
        return_value=httpx.Response(
            200, json={"tag_name": "v0.0.1", "html_url": "https://example.test/r"}
        )
    )
    settings = Settings(
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "library",
        monitor_enabled=False,
        metadata_enabled=False,
        update_check_enabled=True,
        auth_required=False,
    )
    provision_library_identity(settings)
    with TestClient(create_app(settings)) as client:
        listing = client.get("/api/system/tasks").json()["tasks"]
        ids = [task["id"] for task in listing]
        assert ids == [
            "release_monitor",
            "wanted_search",
            "metadata_refresh",
            "download_clients",
            "orphan_sweep",
            "nightly_maintenance",
            "komga_refresh",
            "update_check",
            "suwayomi_maintenance",
        ]
        by_id = {task["id"]: task for task in listing}
        assert by_id["release_monitor"]["enabled"] is False
        assert by_id["nightly_maintenance"]["schedule"].startswith("Every night")
        assert by_id["update_check"]["schedule"] == "Every day"

        ran = client.post("/api/system/tasks/update_check/run")
        assert ran.status_code == 200
        payload = ran.json()
        assert payload["ok"] is True
        assert payload["result"]["latest"] == "0.0.1"
        assert payload["task"]["last_run_at"]
        assert payload["task"]["manual"]["last_error"] is None

        monitor_run = client.post("/api/system/tasks/release_monitor/run")
        assert monitor_run.status_code == 200
        assert monitor_run.json()["ok"] is True

        assert client.post("/api/system/tasks/nope/run").status_code == 404
        maintenance = client.post("/api/system/tasks/nightly_maintenance/run").json()
        assert maintenance["ok"] is True
        jobs = client.get("/api/system/maintenance").json()["jobs"]
        assert all(job["last_attempt_at"] for job in jobs.values())
