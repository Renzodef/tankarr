from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tankarr.config import Settings
from tankarr.database import Database
from tankarr.maintenance import MAX_ACTIONS, STATE_SETTING, MaintenanceWorker
from tankarr.operations import register_operations_routes

NIGHT = datetime(2026, 1, 2, 3, tzinfo=UTC)
ACTION = {
    "id": "move-chapter",
    "kind": "move",
    "path": "/srv/library/old.cbz",
    "destination": "/srv/library/series/new.cbz",
    "reason": "Normalize the tracked path",
    "safe": True,
}


@pytest.fixture
def context(tmp_path):
    settings = Settings(
        _env_file=None, data_dir=tmp_path / "data", library_dir=tmp_path / "library"
    )
    settings.ensure_directories()
    database = Database(settings.database_path)
    database.initialize()
    service = SimpleNamespace(
        organize_library=AsyncMock(
            return_value={"snapshot": "library-digest", "actions": [ACTION]}
        ),
        purge_recycle_bin=AsyncMock(return_value={"files_deleted": 0, "errors": []}),
    )
    backups = SimpleNamespace(create=Mock(return_value={"verified": True}))
    return settings, database, service, backups


async def test_nightly_jobs_run_serially_once_per_day_and_survive_restart(context):
    settings, database, service, backups = context
    worker = MaintenanceWorker(*context)
    await worker.run_once(NIGHT.replace(hour=12))
    backups.create.assert_not_called()
    await worker.run_once(NIGHT)
    await worker.run_once(NIGHT + timedelta(minutes=1))
    restarted = MaintenanceWorker(*context)
    await restarted.run_once(NIGHT + timedelta(minutes=2))
    backups.create.assert_called_once()
    service.purge_recycle_bin.assert_awaited_once()
    assert service.organize_library.await_count == 2
    service.organize_library.assert_awaited_with(confirmation_snapshot="library-digest")
    assert all(job["status"] == "ok" for job in restarted.status()["jobs"].values())
    await restarted.run_once(NIGHT + timedelta(days=1))
    assert backups.create.call_count == 2
    assert database.get_setting_overrides()[STATE_SETTING]


@pytest.mark.parametrize(
    "blocked", ["safe_mode", "import", "startup", "torrent", "download"]
)
async def test_busy_or_safe_mode_skips_every_job_without_consuming_the_day(
    context, blocked
):
    settings, database, service, backups = context
    if blocked == "safe_mode":
        settings.restored_safe_mode = True
    elif blocked in {"torrent", "download"}:
        # Only statuses matter: avoid constructing a library or opening files.
        database.upsert_manga(
            {"id": "series", "title": "Fixture", "authors": []}, "en", "all"
        )
        with database.connect() as connection:
            if blocked == "torrent":
                connection.execute(
                    "INSERT INTO torrent_download (manga_id,source,source_id,info_hash,title,language,category,source_url,torrent_url,status,created_at,updated_at) "
                    "VALUES ('series','fixture','pack','hash','Pack','en','manga','https://nas.local/pack','https://nas.local/pack','importing','now','now')"
                )
            else:
                connection.execute(
                    "INSERT INTO download_job (manga_id,chapter_id,requested_language,status,message,created_at,updated_at) "
                    "VALUES ('series','chapter','en','importing','','now','now')"
                )
    worker = MaintenanceWorker(
        *context,
        import_running=lambda: blocked == "import",
        ready=lambda: blocked != "startup",
    )
    await worker.run_once(NIGHT)
    backups.create.assert_not_called()
    service.organize_library.assert_not_awaited()
    service.purge_recycle_bin.assert_not_awaited()
    assert all(job["status"] == "skipped" for job in worker.status()["jobs"].values())
    assert all(
        job["last_success_at"] is None for job in worker.status()["jobs"].values()
    )


async def test_failed_backup_retries_after_an_hour_without_repeating_other_jobs(
    context,
):
    _settings, _database, service, backups = context
    backups.create.side_effect = [OSError("disk full"), {"verified": True}]
    worker = MaintenanceWorker(*context)
    await worker.run_once(NIGHT)
    status = worker.status()["jobs"]
    assert status["backup"]["status"] == "error"
    assert "OSError" in status["backup"]["error"]
    assert status["repair"]["status"] == "ok"
    restarted = MaintenanceWorker(*context)
    await restarted.run_once(NIGHT + timedelta(minutes=59))
    assert backups.create.call_count == 1
    await restarted.run_once(NIGHT + timedelta(hours=1))
    assert backups.create.call_count == 2
    assert service.organize_library.await_count == 2
    assert restarted.status()["jobs"]["backup"]["status"] == "ok"


async def test_skip_retries_within_same_night(context):
    busy = True
    worker = MaintenanceWorker(*context, import_running=lambda: busy)
    await worker.run_once(NIGHT)
    busy = False
    await worker.run_once(NIGHT + timedelta(minutes=4))
    context[3].create.assert_not_called()
    await worker.run_once(NIGHT + timedelta(minutes=5))
    context[3].create.assert_called_once()


async def test_cached_legacy_repair_is_bounded_and_cannot_apply_an_unreviewed_overflow(
    context,
):
    context[2].organize_library.return_value = {
        "snapshot": "digest",
        "actions": [ACTION] * (MAX_ACTIONS + 1),
        "warnings": ["warning"] * 1000,
        "huge_plan": "unbounded internal report",
    }
    worker = MaintenanceWorker(*context)
    worker._cache_repair(context[2].organize_library.return_value, NIGHT)
    await worker._save()
    report = worker.status()["repair"]
    assert len(report["actions"]) == MAX_ACTIONS
    assert report["total_actions"] == MAX_ACTIONS + 1
    assert report["truncated"] and not report["can_apply"]
    assert len(report["warnings"]) == 20
    assert "snapshot" not in report
    assert "huge_plan" not in context[1].get_setting_overrides()[STATE_SETTING]
    with pytest.raises(ValueError, match="changed"):
        await worker.apply_repair(report["revision"])


@pytest.mark.parametrize(
    "unsafe",
    [
        "planned_duplicate_removals",
        "replacement_files_deferred",
        "organization_blocked",
    ],
)
async def test_unsafe_repair_is_never_offered_for_apply(context, unsafe):
    context[2].organize_library.return_value = {
        "snapshot": "digest",
        "actions": [ACTION],
        unsafe: 1,
    }
    worker = MaintenanceWorker(*context)
    await worker.run_once(NIGHT)
    assert not worker.status()["repair"]["can_apply"]


async def test_apply_revalidates_persisted_snapshot_and_is_single_use(context):
    worker = MaintenanceWorker(*context)
    worker._cache_repair({"snapshot": "library-digest", "actions": [ACTION]}, NIGHT)
    await worker._save()
    restarted = MaintenanceWorker(*context)
    revision = restarted.status()["repair"]["revision"]
    with pytest.raises(ValueError, match="changed"):
        await restarted.apply_repair("invented-revision")
    context[2].organize_library.return_value = {"moved": 1}
    await restarted.apply_repair(revision)
    context[2].organize_library.assert_awaited_with(
        confirmation_snapshot="library-digest"
    )
    with pytest.raises(ValueError, match="changed"):
        await MaintenanceWorker(*context).apply_repair(revision)


async def test_apply_consumes_stale_snapshot_and_exposes_revalidation_error(context):
    worker = MaintenanceWorker(*context)
    worker._cache_repair({"snapshot": "library-digest", "actions": [ACTION]}, NIGHT)
    await worker._save()
    revision = worker.status()["repair"]["revision"]
    context[2].organize_library.side_effect = ValueError(
        "Library repair preview is stale"
    )
    with pytest.raises(ValueError, match="stale"):
        await worker.apply_repair(revision)
    restarted = MaintenanceWorker(*context)
    assert not restarted.status()["repair"]["can_apply"]
    assert "stale" in restarted.status()["repair"]["warnings"][0]


async def test_nightly_job_cannot_overlap_apply(context):
    entered, finish = asyncio.Event(), asyncio.Event()

    async def purge():
        entered.set()
        await finish.wait()
        return {"errors": []}

    context[2].purge_recycle_bin.side_effect = purge
    worker = MaintenanceWorker(*context)
    task = asyncio.create_task(worker.run_once(NIGHT))
    await entered.wait()
    await worker.run_once(NIGHT)
    with pytest.raises(ValueError, match="already running"):
        await worker.apply_repair("revision")
    finish.set()
    await task
    context[3].create.assert_called_once()
    assert context[2].organize_library.await_count == 2


def test_system_status_get_never_scans_library_and_apply_uses_revision(context):
    worker = MaintenanceWorker(*context)
    worker._cache_repair({"snapshot": "digest", "actions": [ACTION]}, NIGHT)
    app = FastAPI()
    register_operations_routes(
        app,
        settings=context[0],
        database=context[1],
        service=context[2],
        add_manga=AsyncMock(),
        reader_link=AsyncMock(),
        probes=lambda: {},
        maintenance=worker,
    )
    with TestClient(app) as client:
        report = client.get("/api/system/maintenance").json()
        assert report["repair"]["can_apply"]
        context[2].organize_library.assert_not_awaited()
        assert "digest" not in json.dumps(report)
        assert (
            client.post(
                "/api/system/maintenance/repair/apply", json={"revision": "fake"}
            ).status_code
            == 409
        )
        response = client.post(
            "/api/system/maintenance/repair/apply",
            json={"revision": report["repair"]["revision"]},
        )
        assert response.json() == {"applied": True, "warnings": []}
        context[2].organize_library.assert_awaited_once_with(
            confirmation_snapshot="digest"
        )


async def test_interrupted_job_waits_for_persisted_retry_after_restart(context):
    entered = asyncio.Event()

    async def interrupted_purge():
        entered.set()
        await asyncio.Event().wait()

    context[2].purge_recycle_bin.side_effect = interrupted_purge
    worker = MaintenanceWorker(*context)
    task = asyncio.create_task(worker.run_once(NIGHT))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    restarted = MaintenanceWorker(*context)
    await restarted.run_once(NIGHT + timedelta(minutes=1))
    # The independent repair may proceed, but an interrupted purge is not
    # replayed on every restart or polling tick.
    assert context[2].purge_recycle_bin.await_count == 1
    assert context[2].organize_library.await_count == 2
    context[2].purge_recycle_bin.side_effect = None
    await restarted.run_once(NIGHT + timedelta(hours=1))
    assert context[2].purge_recycle_bin.await_count == 2
    assert restarted.status()["jobs"]["recycle"]["status"] == "ok"


async def test_apply_in_safe_mode_preserves_the_review_for_later(context):
    worker = MaintenanceWorker(*context)
    worker._cache_repair({"snapshot": "library-digest", "actions": [ACTION]}, NIGHT)
    await worker._save()
    revision = worker.status()["repair"]["revision"]
    context[0].restored_safe_mode = True
    assert not worker.status()["repair"]["can_apply"]
    with pytest.raises(ValueError, match="safe mode"):
        await worker.apply_repair(revision)
    context[2].organize_library.assert_not_awaited()
    context[0].restored_safe_mode = False
    assert worker.status()["repair"]["can_apply"]


async def test_partial_recycle_purge_failure_is_visible_and_retried(context):
    context[2].purge_recycle_bin.return_value = {
        "files_deleted": 1,
        "errors": ["expired file could not be removed"],
    }
    worker = MaintenanceWorker(*context)
    await worker.run_once(NIGHT)
    assert worker.status()["jobs"]["recycle"]["status"] == "error"
    assert worker.status()["jobs"]["repair"]["status"] == "ok"
    await worker.run_once(NIGHT + timedelta(minutes=30))
    context[2].purge_recycle_bin.assert_awaited_once()
    context[2].purge_recycle_bin.return_value = {"files_deleted": 1, "errors": []}
    await worker.run_once(NIGHT + timedelta(hours=1))
    assert context[2].purge_recycle_bin.await_count == 2


async def test_safe_repairs_apply_automatically_even_above_display_limit(context):
    context[2].organize_library.side_effect = [
        {"snapshot": "full-preview", "actions": [ACTION] * (MAX_ACTIONS + 1)},
        {"moved": MAX_ACTIONS + 1},
    ]
    worker = MaintenanceWorker(*context)
    await worker.run_once(NIGHT)
    assert context[2].organize_library.await_args_list == [
        call(dry_run=True),
        call(confirmation_snapshot="full-preview"),
    ]
    assert worker.status()["repair"]["total_actions"] == 0
    assert not worker.status()["repair"]["can_apply"]
    assert worker.status()["jobs"]["repair"]["status"] == "ok"


async def test_changed_snapshot_retries_with_fresh_preview_without_confirmation(
    context,
):
    context[2].organize_library.side_effect = [
        {"snapshot": "first", "actions": [ACTION]},
        ValueError("Library repair preview is stale"),
        {"snapshot": "second", "actions": [ACTION]},
        {"moved": 1},
    ]
    worker = MaintenanceWorker(*context)
    await worker.run_once(NIGHT)
    assert worker.status()["jobs"]["repair"]["status"] == "error"
    assert not worker.status()["repair"]["can_apply"]
    restarted = MaintenanceWorker(*context)
    await restarted.run_once(NIGHT + timedelta(minutes=59))
    assert context[2].organize_library.await_count == 2
    await restarted.run_once(NIGHT + timedelta(hours=1))
    assert context[2].organize_library.await_args_list == [
        call(dry_run=True),
        call(confirmation_snapshot="first"),
        call(dry_run=True),
        call(confirmation_snapshot="second"),
    ]
    assert restarted.status()["jobs"]["repair"]["status"] == "ok"


async def test_legacy_review_is_rechecked_after_readiness_outside_nightly_window(
    context,
):
    worker = MaintenanceWorker(*context)
    worker._cache_repair({"snapshot": "obsolete", "actions": [ACTION]}, NIGHT)
    worker._state["jobs"]["repair"].update(
        status="ok", last_success_at=NIGHT.isoformat()
    )
    worker._state.pop("repair_automatic")
    await worker._save()
    ready = False
    migrated = MaintenanceWorker(*context, ready=lambda: ready)
    noon = NIGHT.replace(hour=12)
    await migrated.run_once(noon)
    context[2].organize_library.assert_not_awaited()
    ready = True
    restarted = MaintenanceWorker(*context)
    await restarted.run_once(noon + timedelta(minutes=5))
    context[2].organize_library.assert_awaited_with(
        confirmation_snapshot="library-digest"
    )
    context[3].create.assert_not_called()
    context[2].purge_recycle_bin.assert_not_awaited()
    await MaintenanceWorker(*context).run_once(noon + timedelta(minutes=6))
    assert context[2].organize_library.await_count == 2


async def test_nightly_repair_moves_real_file_and_updates_database_automatically(
    tmp_path,
):
    from tests.test_guided_library_repair import legacy_download

    database, service, _reader, legacy, canonical, _job = legacy_download(tmp_path)
    worker = MaintenanceWorker(service.settings, database, service, Mock())
    # Isolate repair from unrelated backup/recycle operations in this fixture.
    for name in ("backup", "recycle"):
        worker._state["jobs"][name]["last_success_at"] = NIGHT.isoformat()
    await worker.run_once(NIGHT)
    assert not legacy.exists()
    assert canonical.read_bytes() == b"cbz"
    assert database.get_chapter("chapter-1")["library_path"] == str(canonical)
    assert worker.status()["jobs"]["repair"]["status"] == "ok"
    assert worker.status()["repair"]["total_actions"] == 0
