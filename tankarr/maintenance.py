"""Serial nightly maintenance with automatic, revalidated library repair."""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from tankarr.backups import ApplicationBackups
from tankarr.config import Settings
from tankarr.database import Database

logger = logging.getLogger(__name__)
STATE_SETTING = "automatic_maintenance_state"
MAX_ACTIONS = 100
MAX_WARNINGS = 20
JOB_NAMES = ("backup", "recycle", "repair")


def guided_repair_report(result: dict[str, Any]) -> dict[str, Any]:
    """Use the same safety boundary for legacy and nightly previews."""
    actions = result.get("actions") or []
    blocked = bool(
        result.get("organization_blocked")
        or result.get("deferred")
        or result.get("planned_duplicate_removals")
        or result.get("replacement_files_deferred")
        or any(not action.get("safe", False) for action in actions)
    )
    warnings = list(result.get("warnings") or [])
    if result.get("planned_duplicate_removals"):
        warnings.append(
            "Duplicate retirement requires the existing per-series review; guided repair will not remove files"
        )
    return {
        "actions": actions,
        "warnings": warnings,
        "snapshot": result.get("snapshot") if not blocked else None,
    }


def empty_repair() -> dict[str, Any]:
    return {
        "revision": None,
        "generated_at": None,
        "actions": [],
        "total_actions": 0,
        "truncated": False,
        "warnings": [],
        "can_apply": False,
    }


class MaintenanceWorker:
    """Run once per local date, between 03:00 and 06:00, with durable retries.

    Repair previews and execution run in the worker, with in-lock revalidation.
    GET requests only read diagnostics and never trigger filesystem work.
    """

    def __init__(
        self,
        settings: Settings,
        database: Database,
        service: Any,
        backups: ApplicationBackups,
        *,
        import_running: Callable[[], bool] = lambda: False,
        ready: Callable[[], bool] = lambda: True,
    ):
        self.settings = settings
        self.database = database
        self.service = service
        self.backups = backups
        self.import_running = import_running
        self.ready = ready
        self._lock = asyncio.Lock()
        self._state: dict[str, Any] = {
            "jobs": {
                name: {
                    "status": "pending",
                    "last_attempt_at": None,
                    "last_success_at": None,
                    "retry_after": None,
                    "error": None,
                }
                for name in JOB_NAMES
            },
            "repair": empty_repair(),
            "repair_automatic": True,
            "repair_pending": False,
        }
        with database.connect() as connection:
            row = connection.execute(
                "SELECT value FROM setting WHERE key=?", (STATE_SETTING,)
            ).fetchone()
        if row:
            try:
                saved = json.loads(row["value"])
                if not isinstance(saved, dict):
                    raise ValueError("Invalid maintenance state")
                for name in JOB_NAMES:
                    job = saved.get("jobs", {}).get(name)
                    if isinstance(job, dict):
                        self._state["jobs"][name].update(job)
                repair = saved.get("repair")
                if isinstance(repair, dict):
                    self._state["repair"].update(repair)
                self._state["repair_pending"] = bool(saved.get("repair_pending"))
                if not saved.get("repair_automatic") and (
                    self._state["repair"]["total_actions"]
                    or self._state["repair"]["warnings"]
                ):
                    # Old releases stopped at a preview. Revisit it once after
                    # readiness, even when today's nightly check already ran.
                    self._state["repair_pending"] = True
                    self._state["jobs"]["repair"].update(
                        last_success_at=None, retry_after=None
                    )
                    self._state["repair"]["can_apply"] = False
                    self._state["repair"]["snapshot"] = None
            except (ValueError, TypeError, AttributeError):
                logger.warning(
                    "Invalid persisted maintenance state; rebuilding nightly"
                )

    def status(self) -> dict[str, Any]:
        result = copy.deepcopy(self._state)
        result["repair"].pop("snapshot", None)
        if self._lock.locked() or self.settings.restored_safe_mode:
            result["repair"]["can_apply"] = False
        return result

    async def _save(self) -> None:
        await asyncio.to_thread(
            self.database.save_setting, STATE_SETTING, json.dumps(self._state)
        )

    def _blocked(self) -> str | None:
        if self.settings.restored_safe_mode:
            return "Restored safe mode is active"
        if not self.ready():
            return "Waiting for application readiness"
        if self.import_running():
            return "Waiting for the active import to finish"
        with self.database.connect() as connection:
            busy = connection.execute(
                "SELECT EXISTS(SELECT 1 FROM download_job WHERE status IN "
                "('running','downloading','packaging','importing')) OR "
                "EXISTS(SELECT 1 FROM torrent_download WHERE status IN ('adding','importing'))"
            ).fetchone()[0]
        if busy:
            return "Waiting for active downloads and imports to finish"
        return None

    @staticmethod
    def _due(job: dict[str, Any], now: datetime) -> bool:
        try:
            if (
                job["last_success_at"]
                and datetime.fromisoformat(job["last_success_at"]).date() >= now.date()
            ):
                return False
            if job["retry_after"] and datetime.fromisoformat(job["retry_after"]) > now:
                return False
        except (TypeError, ValueError):
            pass
        return True

    async def run(self) -> None:
        while True:
            try:
                await self.run_once()
            except Exception:  # noqa: BLE001 - persistence failures must not kill the worker
                logger.exception("Nightly maintenance could not persist its status")
            await asyncio.sleep(60)

    async def run_once(
        self, now: datetime | None = None, *, force: bool = False
    ) -> None:
        """One pass: nightly on its own, or every job at once when forced by hand."""

        now = now or datetime.now().astimezone()
        nightly = force or 3 <= now.hour < 6
        if self._lock.locked() or (not nightly and not self._state["repair_pending"]):
            return
        async with self._lock:
            for name in JOB_NAMES:
                if not nightly and name != "repair":
                    continue
                job = self._state["jobs"][name]
                if not force and not self._due(job, now):
                    continue
                blocked = await asyncio.to_thread(self._blocked)
                job["last_attempt_at"] = now.isoformat()
                job["retry_after"] = (now + timedelta(hours=1)).isoformat()
                if blocked:
                    job.update(
                        status="skipped",
                        error=blocked,
                        retry_after=(now + timedelta(minutes=5)).isoformat(),
                    )
                    await self._save()
                    continue
                job.update(status="running", error=None)
                await self._save()
                try:
                    if name == "backup":
                        await asyncio.to_thread(self.backups.create)
                    elif name == "recycle":
                        result = await self.service.purge_recycle_bin()
                        if result.get("errors"):
                            raise RuntimeError(
                                "Some expired recycle bin files could not be removed"
                            )
                        from tankarr.download_recycle import purge_download_recycle

                        downloads = await asyncio.to_thread(
                            purge_download_recycle, self.settings
                        )
                        if downloads.get("errors"):
                            raise RuntimeError(
                                "Some expired download payloads could not be removed"
                            )
                    else:
                        await self._repair_automatically(now)
                        self._state["repair_pending"] = False
                    job.update(
                        status="ok", last_success_at=now.isoformat(), retry_after=None
                    )
                except Exception as exc:  # noqa: BLE001 - jobs retry independently
                    logger.exception("Nightly %s maintenance failed", name)
                    job.update(
                        status="error",
                        error=f"{name.title()} maintenance failed ({type(exc).__name__}); review the application logs. It will retry automatically.",
                    )
                await self._save()

    async def _repair_automatically(self, now: datetime) -> None:
        result = await self.service.organize_library(dry_run=True)
        report = guided_repair_report(result)
        self._cache_repair(result, now)
        # Keep bounded diagnostics, but never queue an Apply button. Execution
        # uses the full preview digest, regardless of diagnostic truncation.
        self._state["repair"].update(can_apply=False, snapshot=None)
        await self._save()
        if (
            result.get("deferred")
            or result.get("organization_blocked")
            or result.get("planned_duplicate_removals")
            or result.get("replacement_files_deferred")
            or any(not action.get("safe", False) for action in report["actions"])
        ):
            raise RuntimeError("Automatic library repair is deferred or blocked")
        if report["actions"]:
            if not report["snapshot"]:
                raise RuntimeError("Automatic library repair has no valid snapshot")
            blocked = await asyncio.to_thread(self._blocked)
            if blocked:
                raise RuntimeError(blocked)
            applied = await self.service.organize_library(
                confirmation_snapshot=report["snapshot"]
            )
            if applied.get("deferred") or applied.get("organization_blocked"):
                raise RuntimeError("Automatic library repair was deferred or blocked")
        self._state["repair"] = {**empty_repair(), "generated_at": now.isoformat()}

    def _cache_repair(self, result: dict[str, Any], now: datetime) -> None:
        report = guided_repair_report(result)
        actions = report["actions"]
        truncated = len(actions) > MAX_ACTIONS
        warnings = [str(item)[:1000] for item in report["warnings"][:MAX_WARNINGS]]
        if truncated:
            warnings = warnings[: MAX_WARNINGS - 1] + [
                "Too many actions for one review; review the affected series individually."
            ]
        # Persist only the confirmation digest and a bounded review, never the
        # full organization report or a library-sized execution plan.
        self._state["repair"] = {
            "revision": uuid.uuid4().hex,
            "generated_at": now.isoformat(),
            "actions": [
                {
                    key: value if isinstance(value, bool) else str(value)[:2000]
                    for key, value in action.items()
                    if key in {"id", "kind", "path", "destination", "reason", "safe"}
                }
                for action in actions[:MAX_ACTIONS]
            ],
            "total_actions": len(actions),
            "truncated": truncated,
            "warnings": warnings,
            "can_apply": bool(actions and report["snapshot"] and not truncated),
            "snapshot": report["snapshot"],
        }

    async def apply_repair(self, revision: object) -> dict[str, Any]:
        if self._lock.locked():
            raise ValueError(
                "Maintenance is already running; try again after it finishes"
            )
        async with self._lock:
            repair = self._state["repair"]
            if (
                not revision
                or revision != repair["revision"]
                or not repair["can_apply"]
            ):
                raise ValueError(
                    "Repair review changed or was already applied; reload System"
                )
            blocked = await asyncio.to_thread(self._blocked)
            if blocked:
                raise ValueError(blocked)
            snapshot = repair.get("snapshot")
            if not snapshot:
                raise ValueError("A current repair review is required")
            # Consume the review durably before starting a mutation. A failed or
            # interrupted apply must never silently replay on another click.
            repair["can_apply"] = False
            repair["snapshot"] = None
            await self._save()
            try:
                result = await self.service.organize_library(
                    confirmation_snapshot=snapshot
                )
                if result.get("organization_blocked") or result.get("deferred"):
                    raise ValueError(
                        "Library changed or repair is blocked; wait for the next nightly review"
                    )
            except ValueError as exc:
                repair["warnings"] = [str(exc)[:1000]]
                await self._save()
                raise
            except Exception:
                repair["warnings"] = [
                    "Repair failed; review the application logs. The next nightly check will refresh this review."
                ]
                await self._save()
                raise
            self._state["repair"] = {
                **empty_repair(),
                "generated_at": datetime.now().astimezone().isoformat(),
            }
            await self._save()
            return result
