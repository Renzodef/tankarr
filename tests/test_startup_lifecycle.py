from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tankarr.app import create_app
from tankarr.config import Settings
from tankarr.komga import KomgaClient
from tankarr.metadata.service import MetadataService
from tankarr.service import TankarrService
from tankarr.stump import StumpLibraryClient
from tests.test_app_e2e import provision_library_identity


def test_readiness_opens_only_after_the_cold_wanted_snapshot_is_warm(
    tmp_path: Path, monkeypatch
):
    entered = threading.Event()
    release = threading.Event()
    original = TankarrService.list_wanted

    def delayed_wanted(self):
        entered.set()
        assert release.wait(5)
        return original(self)

    monkeypatch.setattr(TankarrService, "list_wanted", delayed_wanted)
    settings = Settings(
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "library",
        monitor_enabled=False,
        metadata_enabled=False,
        suwayomi_enabled=False,
    )
    provision_library_identity(settings)

    with TestClient(create_app(settings)) as client:
        assert entered.wait(2)
        assert client.get("/api/ready").status_code == 503
        release.set()
        for _attempt in range(100):
            if client.get("/api/ready").status_code == 200:
                break
        assert client.get("/api/ready").status_code == 200


def test_restart_paints_wanted_snapshot_while_changed_inputs_reconcile(
    tmp_path: Path, monkeypatch
):
    settings = Settings(
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "library",
        monitor_enabled=False,
        metadata_enabled=False,
        suwayomi_enabled=False,
    )
    provision_library_identity(settings)

    first = create_app(settings)
    with TestClient(first) as client:
        for _attempt in range(100):
            if client.get("/api/ready").status_code == 200:
                break
        assert client.get("/api/wanted", params={"fresh": "true"}).status_code == 200

    with first.state.database.connect() as connection:
        connection.execute(
            "UPDATE read_model_revision SET revision=revision+1 "
            "WHERE table_name='manga'"
        )

    def unexpected_rebuild(self):
        raise AssertionError("the first request must not wait for reconciliation")

    monkeypatch.setattr(TankarrService, "list_wanted", unexpected_rebuild)
    with TestClient(create_app(settings)) as client:
        for _attempt in range(100):
            response = client.get("/api/ready")
            if response.status_code == 200:
                break
        assert response.status_code == 200
        assert client.get("/api/wanted").status_code == 200


def test_reader_api_is_not_part_of_startup_or_readiness(tmp_path: Path, monkeypatch):
    entered = asyncio.Event()

    async def wait_forever(_: KomgaClient):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(KomgaClient, "prepare_for_moves", wait_forever)
    settings = Settings(
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "library",
        komga_url="http://komga:25600",
        komga_api_key="test-key",
    )
    provision_library_identity(settings)

    with TestClient(create_app(settings)) as client:
        health = client.get("/api/system/health")
        ready = client.get("/api/ready")

        assert health.status_code == 200
        assert health.json()["library_alignment"]["reader_independent"] is True
        assert health.json()["library_organization"]["organization_blocked"] is False
        assert ready.status_code == 200
        assert not entered.is_set()


async def test_reader_metadata_sync_does_not_delay_safe_worker_startup(
    tmp_path: Path,
    monkeypatch,
):
    metadata_sync_entered = asyncio.Event()

    async def prepared(_self):
        return {"ready": True}

    async def aligned(_self, expected):
        return {
            "configured": True,
            "ready": True,
            "triggered": False,
            "expected_books": len(expected),
            "matched_expected_books": len(expected),
            "missing_books": 0,
        }

    async def no_stale_records(_self, expected, *, path_exists):
        return {
            "purged": False,
            "expected_books": len(expected),
            "stale_books": 0,
            "stale_series": 0,
        }

    async def wait_forever(_self, *, force=None):
        metadata_sync_entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(StumpLibraryClient, "prepare_for_moves", prepared)
    monkeypatch.setattr(StumpLibraryClient, "ensure_present", aligned)
    monkeypatch.setattr(StumpLibraryClient, "purge_stale_trash", no_stale_records)
    monkeypatch.setattr(MetadataService, "sync_all_to_komga", wait_forever)
    settings = Settings(
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "library",
        reader_kind="stump",
        reader_url="http://stump:10801",
        reader_username="test-user",
        reader_password="test-password",
        metadata_enabled=False,
        monitor_enabled=False,
    )
    provision_library_identity(settings)
    app = create_app(settings)

    async with asyncio.timeout(5):
        async with app.router.lifespan_context(app):
            for _attempt in range(100):
                if app.state.worker.status()["running"]:
                    break
                await asyncio.sleep(0.01)

            assert app.state.worker.status()["running"] is True
            # The reader is reconciled by maintenance, independently of readiness.
            assert app.state.service.last_komga_library_alignment["ready"] is False
            assert not metadata_sync_entered.is_set()


@pytest.mark.parametrize(
    "reader_method",
    ["prepare_for_moves", "ensure_present", "purge_stale_trash", "scan"],
)
async def test_non_gating_reader_cannot_hold_worker_startup(
    tmp_path: Path, monkeypatch, reader_method: str
):
    entered = asyncio.Event()

    async def wait_forever(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    def moved_files(service, root, dry_run):
        return service._organization_report(scan_required=True)

    monkeypatch.setattr(StumpLibraryClient, reader_method, wait_forever)
    monkeypatch.setattr(TankarrService, "_organize_library_files", moved_files)
    settings = Settings(
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "library",
        reader_kind="stump",
        reader_url="http://stump:10801",
        reader_username="test-user",
        reader_password="test-password",
        metadata_enabled=False,
        monitor_enabled=False,
    )
    provision_library_identity(settings)
    app = create_app(settings)
    async with asyncio.timeout(5):
        async with app.router.lifespan_context(app):
            for _attempt in range(100):
                if app.state.worker.status()["running"]:
                    break
                await asyncio.sleep(0.01)
            assert app.state.worker.status()["running"] is True
            assert not entered.is_set()
            organization = app.state.service.last_library_organization
            assert not organization["organization_blocked"]
            assert organization["komga_scan"]["requested"] is True
            assert organization["komga_scan"]["deferred"] is True


@pytest.mark.parametrize("operation", ["reconcile", "refresh"])
async def test_reader_inventory_checks_do_not_block_the_event_loop(tmp_path, operation):
    import threading
    from types import SimpleNamespace

    settings = Settings(
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "library",
        monitor_enabled=False,
        metadata_enabled=False,
        suwayomi_enabled=False,
    )
    settings.data_dir.mkdir()
    settings.library_dir.mkdir()
    app = create_app(settings)
    service = app.state.service
    loop_thread = threading.get_ident()
    checked = []

    def inventory():
        checked.append(threading.get_ident())
        assert threading.get_ident() != loop_thread
        return ("Book.cbz",)

    async def aligned(expected):
        assert expected == ("Book.cbz",)
        return {"ready": True, "triggered": False}

    service._tracked_library_relative_paths = inventory
    service.komga = SimpleNamespace(
        configured=True, ensure_present=aligned, scan=aligned
    )
    service._komga_refresh_post_scan = None
    try:
        result = (
            await service.reconcile_komga_library()
            if operation == "reconcile"
            else await service.refresh_komga_library(reason="manual")
        )
        assert checked and result["ready"]
    finally:
        app.state.api_executor.shutdown(wait=True, cancel_futures=True)
