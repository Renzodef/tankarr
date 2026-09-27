from __future__ import annotations

import asyncio
import os

import pytest
from fastapi.testclient import TestClient
from test_deletion import (
    ReconcilingKomga,
    add_downloaded_chapter,
    chapter,
    make_reconciling_service,
    make_service,
    manga,
)

from tankarr.app import create_app
from tankarr.database import TorrentDownloadsExistError
from tankarr.service import TankarrService
from tests.test_torrents import release


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["imported", "failed"])
@pytest.mark.parametrize("delete_files", [True, False])
async def test_terminal_download_history_does_not_block_series_removal(
    tmp_path, status, delete_files
):
    database, service, _ = make_service(tmp_path)
    output, _ = add_downloaded_chapter(
        database, service.settings.library_dir, manga(), chapter("c1", "1")
    )
    job = database.create_torrent_download("manga-1", release())
    database.update_torrent_download(job["id"], status=status)
    result = await service.enqueue_manga_deletion("manga-1", delete_files=delete_files)
    assert result["deleted"]
    with pytest.raises(KeyError):
        database.get_manga("manga-1")
    with pytest.raises(KeyError):
        database.get_torrent_download(job["id"])
    await service.process_series_deletions()
    assert output.exists() is not delete_files
    assert not database.pending_series_deletions()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status", ["queued", "downloading", "completed", "importing", "review"]
)
async def test_unfinished_download_remains_protected(tmp_path, status):
    database, service, _ = make_service(tmp_path)
    database.upsert_manga(manga(), "en", "none")
    job = database.create_torrent_download("manga-1", release())
    database.update_torrent_download(job["id"], status=status)
    with pytest.raises(TorrentDownloadsExistError):
        await service.enqueue_manga_deletion("manga-1", delete_files=True)
    assert database.get_manga("manga-1")
    assert database.get_torrent_download(job["id"])["status"] == status
    assert not database.pending_series_deletions()


@pytest.mark.asyncio
async def test_removal_never_waits_for_library_or_mutation_lock(tmp_path, monkeypatch):
    database, service, _ = make_service(tmp_path)
    output, _ = add_downloaded_chapter(
        database, service.settings.library_dir, manga(), chapter("c1", "1")
    )

    def unavailable():
        raise AssertionError("Request must not access the NAS")

    monkeypatch.setattr(service, "_library_root", unavailable)
    async with service._mutation_lock:
        result = await asyncio.wait_for(
            service.enqueue_manga_deletion("manga-1", delete_files=True), 2
        )
    assert result["deleted"] and result["cleanup_pending"]
    assert output.exists()
    with pytest.raises(KeyError):
        database.get_manga("manga-1")
    assert len(database.pending_series_deletions()) == 1


@pytest.mark.asyncio
async def test_cleanup_resumes_on_new_service_and_reconciles_reader(tmp_path):
    reader = ReconcilingKomga()
    database, service = make_reconciling_service(tmp_path, reader)
    output, _ = add_downloaded_chapter(
        database, service.settings.library_dir, manga(), chapter("c1", "1")
    )
    await service.enqueue_manga_deletion("manga-1", delete_files=True)
    restarted = TankarrService(service.settings, database, service.provider, reader)
    await restarted.process_series_deletions()
    assert not output.exists()
    assert not database.pending_series_deletions()
    assert reader.paths and not database.list_deletion_operations()
    await restarted.process_series_deletions()
    assert len(reader.paths) == 1


@pytest.mark.asyncio
async def test_offline_storage_keeps_durable_request_then_retries(tmp_path):
    database, service, _ = make_service(tmp_path)
    output, _ = add_downloaded_chapter(
        database, service.settings.library_dir, manga(), chapter("c1", "1")
    )
    marker = service.settings.library_dir / ".tankarr-library-id"
    identity = marker.read_text()
    marker.unlink()
    await service.enqueue_manga_deletion("manga-1", delete_files=True)
    await service.process_series_deletions()
    request = database.pending_series_deletions()[0]
    assert request["attempts"] == 1 and request["last_error"]
    assert output.exists()
    marker.write_text(identity)
    database.retry_series_deletion(request["id"], "", 0)
    await service.process_series_deletions()
    assert not output.exists() and not database.pending_series_deletions()


@pytest.mark.asyncio
async def test_record_only_removal_preserves_files_and_creates_no_cleanup(tmp_path):
    database, service, _ = make_service(tmp_path)
    output, _ = add_downloaded_chapter(
        database, service.settings.library_dir, manga(), chapter("c1", "1")
    )
    await service.enqueue_manga_deletion("manga-1", delete_files=False)
    await service.process_series_deletions()
    assert output.exists() and not database.pending_series_deletions()


@pytest.mark.asyncio
async def test_readded_series_is_protected_from_old_cleanup(tmp_path):
    database, service, _ = make_service(tmp_path)
    output, _ = add_downloaded_chapter(
        database, service.settings.library_dir, manga(), chapter("c1", "1")
    )
    await service.enqueue_manga_deletion("manga-1", delete_files=True)
    database.upsert_manga(manga(), "en", "none")
    await service.process_series_deletions()
    assert output.exists()
    assert "added again" in database.pending_series_deletions()[0]["last_error"]


@pytest.mark.asyncio
async def test_failed_database_delete_rolls_back_cleanup_request(tmp_path):
    database, service, _ = make_service(tmp_path)
    database.upsert_manga(manga(), "en", "none")
    with database.connect() as connection:
        connection.execute(
            "CREATE TRIGGER refuse_removal BEFORE DELETE ON manga BEGIN SELECT RAISE(ABORT, 'test failure'); END"
        )
    with pytest.raises(Exception, match="test failure"):
        await service.enqueue_manga_deletion("manga-1", delete_files=True)
    assert database.get_manga("manga-1")
    assert not database.pending_series_deletions()


@pytest.mark.asyncio
async def test_reader_failure_does_not_restore_series_or_lose_receipt(tmp_path):
    reader = ReconcilingKomga(failure=RuntimeError("reader offline"))
    database, service = make_reconciling_service(tmp_path, reader)
    output, _ = add_downloaded_chapter(
        database, service.settings.library_dir, manga(), chapter("c1", "1")
    )
    await service.enqueue_manga_deletion("manga-1", delete_files=True)
    await service.process_series_deletions()
    assert not output.exists()
    assert not database.pending_series_deletions()
    assert database.list_deletion_operations()[0]["state"] == "committed"


@pytest.mark.asyncio
async def test_reader_pending_scan_retains_receipt_until_confirmed(tmp_path):
    class PendingReader(ReconcilingKomga):
        pending = True

        async def reconcile_deleted(self, paths, *, safety_check):
            safety_check()
            return {"configured": True, "sync_pending": self.pending}

    reader = PendingReader()
    database, service = make_reconciling_service(tmp_path, reader)
    output, _ = add_downloaded_chapter(
        database, service.settings.library_dir, manga(), chapter("c1", "1")
    )
    await service.enqueue_manga_deletion("manga-1", delete_files=True)
    await service.process_series_deletions()
    assert not output.exists()
    assert database.list_deletion_operations()[0]["state"] == "committed"
    reader.pending = False
    await service.retry_pending_komga_reconciliation()
    assert not database.list_deletion_operations()


@pytest.mark.asyncio
async def test_file_replaced_after_request_is_not_removed(tmp_path):
    database, service, _ = make_service(tmp_path)
    output, _ = add_downloaded_chapter(
        database, service.settings.library_dir, manga(), chapter("c1", "1")
    )
    await service.enqueue_manga_deletion("manga-1", delete_files=True)
    timestamp = database.pending_series_deletions()[0]["scope"]["requested_at_ns"]
    output.write_bytes(b"replacement")
    os.utime(output, ns=(timestamp + 1_000_000_000, timestamp + 1_000_000_000))
    await service.process_series_deletions()
    assert output.read_bytes() == b"replacement"
    assert "changed after" in database.pending_series_deletions()[0]["last_error"]


@pytest.mark.asyncio
async def test_crash_after_staging_is_recovered_before_cleanup_retry(
    tmp_path, monkeypatch
):
    database, service, _ = make_service(tmp_path)
    output, _ = add_downloaded_chapter(
        database, service.settings.library_dir, manga(), chapter("c1", "1")
    )
    await service.enqueue_manga_deletion("manga-1", delete_files=True)

    class SimulatedCrash(BaseException):
        pass

    def crash(*_args):
        raise SimulatedCrash

    with monkeypatch.context() as patch:
        patch.setattr(database, "finish_series_deletion", crash)
        with pytest.raises(SimulatedCrash):
            await service.process_series_deletions()
    assert not output.exists()
    assert database.pending_series_deletions()
    assert database.list_deletion_operations()[0]["state"] == "prepared"
    await service.recover_file_quarantines()
    assert output.exists()
    await service.process_series_deletions()
    assert not output.exists() and not database.pending_series_deletions()


def test_api_accepts_file_removal_without_preview_or_storage_access(
    tmp_path, monkeypatch
):
    database, service, _ = make_service(tmp_path)
    app = create_app(service.settings)
    with TestClient(app) as client:
        app.state.database.upsert_manga(manga(), "en", "none")

        def unavailable():
            raise AssertionError("API deletion must not scan the library")

        monkeypatch.setattr(app.state.service, "_library_root", unavailable)
        response = client.delete("/api/manga/manga-1", params={"delete_files": True})
        assert response.status_code == 200
        assert response.json()["cleanup_pending"] is True
        with pytest.raises(KeyError):
            app.state.database.get_manga("manga-1")
