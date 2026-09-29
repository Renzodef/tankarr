"""Scheduled tasks: what runs on its own, when it last ran, and "Run now".

Tankarr's background work is spread over several loops: the release monitor,
Wanted recovery, the metadata refresh, the download-client poll, the orphan
sweep, the nightly maintenance, the Komga refresh, the update check and the
managed Suwayomi maintenance. This registry gives them one table on the
System page and one way to run one by hand, like System → Tasks of the other
*arr applications. Each loop keeps its own schedule; the registry only reads
their state and runs one pass on request, never two at once.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from tankarr.redaction import redact_secrets

logger = logging.getLogger(__name__)


class TaskBusy(RuntimeError):
    """The task is already running; a second pass would only repeat its work."""


class TaskNotRunnable(RuntimeError):
    """The task reports its state but cannot be started by hand."""


@dataclass
class ScheduledTask:
    id: str
    name: str
    description: str
    # -> {"enabled", "running", "last_run_at", "next_run_at", "last_error",
    #     "last_result"}; every key optional.
    status: Callable[[], dict[str, Any]]
    run: Callable[[], Awaitable[Any]] | None = None
    interval_seconds: Callable[[], float | None] | None = None
    schedule: str | None = None


def describe_interval(seconds: float | None) -> str:
    if not seconds or seconds <= 0:
        return "Manual"
    seconds = float(seconds)
    for unit, label in ((86400, "day"), (3600, "hour"), (60, "minute")):
        if seconds >= unit and seconds % unit == 0:
            count = int(seconds // unit)
            return f"Every {label}" if count == 1 else f"Every {count} {label}s"
    return f"Every {seconds:g} seconds"


def _parse(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _jsonable(value: Any) -> Any:
    try:
        return json.loads(json.dumps(value, default=str))
    except (TypeError, ValueError):
        return str(value)


class TaskRegistry:
    def __init__(self) -> None:
        self._tasks: dict[str, ScheduledTask] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._manual: dict[str, dict[str, Any]] = {}

    def register(self, task: ScheduledTask) -> None:
        self._tasks[task.id] = task
        self._locks[task.id] = asyncio.Lock()

    def ids(self) -> list[str]:
        return list(self._tasks)

    def snapshot(self) -> list[dict[str, Any]]:
        return [self._describe(task) for task in self._tasks.values()]

    def _describe(self, task: ScheduledTask) -> dict[str, Any]:
        try:
            status = dict(task.status() or {})
        except Exception as exc:  # noqa: BLE001 - a broken status is still a row
            status = {"last_error": f"Status unavailable ({type(exc).__name__})"}
        interval = task.interval_seconds() if task.interval_seconds else None
        last_run = status.get("last_run_at")
        next_run = status.get("next_run_at")
        enabled = bool(status.get("enabled", True))
        if next_run is None and interval and last_run and enabled:
            parsed = _parse(last_run)
            if parsed is not None:
                next_run = (parsed + timedelta(seconds=float(interval))).isoformat(
                    timespec="seconds"
                )
        lock = self._locks[task.id]
        return {
            "id": task.id,
            "name": task.name,
            "description": task.description,
            "schedule": task.schedule or describe_interval(interval),
            "interval_seconds": interval,
            "enabled": enabled,
            "running": bool(status.get("running")) or lock.locked(),
            "last_run_at": last_run,
            "next_run_at": next_run if enabled else None,
            "last_error": status.get("last_error"),
            "last_result": _jsonable(status.get("last_result")),
            "can_run": task.run is not None,
            "manual": dict(self._manual.get(task.id, {})),
        }

    async def run(self, task_id: str) -> dict[str, Any]:
        """Run one pass by hand and report how it went, never raising for it."""

        task = self._tasks[task_id]
        if task.run is None:
            raise TaskNotRunnable(task.name)
        lock = self._locks[task_id]
        if lock.locked():
            raise TaskBusy(task.name)
        async with lock:
            started = time.monotonic()
            record: dict[str, Any] = {
                "last_run_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "last_error": None,
                "duration_ms": None,
            }
            self._manual[task_id] = record
            ok = True
            result: Any = None
            try:
                result = await task.run()
            except Exception as exc:  # noqa: BLE001 - the outcome is the report
                ok = False
                record["last_error"] = redact_secrets(f"{type(exc).__name__}: {exc}")[
                    :300
                ]
                logger.warning(
                    "Task %s failed when run by hand: %s", task.name, type(exc).__name__
                )
            finally:
                record["duration_ms"] = int((time.monotonic() - started) * 1000)
            return {
                "ok": ok,
                "error": record["last_error"],
                "result": _jsonable(result),
                "task": self._describe(task),
            }
