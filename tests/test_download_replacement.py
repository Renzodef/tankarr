from __future__ import annotations

from pathlib import Path

import pytest

from tankarr.archive import sha256, validate_cbz
from tankarr.naming import final_library_path
from tankarr.service import TankarrService
from tests.test_deletion import (
    NeverDownloadProvider,
    PageProvider,
    RecordingKomga,
    add_downloaded_chapter,
    chapter,
    make_service,
    manga,
)


async def replacement_fixture(tmp_path: Path):
    database, base, _ = make_service(tmp_path)
    series = manga()
    current = {**chapter("old-release", "1"), "provider": "suwayomi"}
    current_path, _ = add_downloaded_chapter(
        database, base.settings.library_dir, series, current
    )
    database.mark_chapter_downloaded(current["id"], current_path, sha256(current_path))
    replacement = {**chapter("new-release", "1"), "provider": "suwayomi"}
    database.upsert_chapters(series["id"], [replacement])
    service = TankarrService(base.settings, database, PageProvider(), RecordingKomga())
    job = await service.create_manual_download_job(replacement["id"], replace=True)
    return database, service, current_path, job


@pytest.mark.asyncio
async def test_replacement_publishes_before_retiring_same_canonical_path(
    tmp_path: Path, monkeypatch
):
    database, service, current_path, job = await replacement_fixture(tmp_path)
    retire = service._retire_superseded_download_locked
    observed = []

    def verify_order(new_id, old_id):
        assert current_path.read_bytes() == b"cbz"
        assert database.get_chapter(old_id)["downloaded"] is True
        new = database.get_chapter(new_id)
        new_path = Path(new["library_path"])
        assert new["downloaded"] is True
        assert new_path != current_path
        assert validate_cbz(new_path)["sha256"] == new["library_sha256"]
        observed.append(new_path)
        return retire(new_id, old_id)

    monkeypatch.setattr(service, "_retire_superseded_download_locked", verify_order)
    await service.process_download_job(job["id"])

    completed = database.get_job(job["id"])
    assert completed["status"] == "completed", completed["message"]
    assert len(observed) == 1
    assert not observed[0].exists()
    assert database.get_chapter("old-release")["downloaded"] is False
    assert database.get_chapter("new-release")["library_path"] == str(current_path)
    assert completed["planned_path"] == completed["result_path"] == str(current_path)
    assert validate_cbz(current_path)["page_count"] == 1


@pytest.mark.asyncio
async def test_failed_replacement_copy_preserves_current_file_and_database(
    tmp_path: Path, monkeypatch
):
    database, service, current_path, job = await replacement_fixture(tmp_path)
    original_hash = sha256(current_path)

    def fail_copy(*args, **kwargs):
        raise OSError("simulated staging disk failure")

    monkeypatch.setattr("tankarr.service.install_atomically", fail_copy)
    await service.process_download_job(job["id"])

    failed = database.get_job(job["id"])
    assert failed["status"] == "failed"
    assert "simulated staging disk failure" in failed["message"]
    assert sha256(current_path) == original_hash
    assert database.get_chapter("old-release")["downloaded"] is True
    assert database.get_chapter("new-release")["downloaded"] is False
    assert not Path(failed["planned_path"]).exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "boundary", ["directory-fsync", "mark-downloaded", "retirement", "normalization"]
)
async def test_replacement_retries_after_publication_without_redownloading(
    tmp_path: Path, monkeypatch, boundary: str
):
    database, service, current_path, job = await replacement_fixture(tmp_path)

    def fail_boundary(*args, **kwargs):
        raise OSError(f"simulated {boundary} failure")

    if boundary == "directory-fsync":
        monkeypatch.setattr("tankarr.archive.fsync_directory", fail_boundary)
    elif boundary == "mark-downloaded":
        monkeypatch.setattr(database, "mark_chapter_downloaded", fail_boundary)
    elif boundary == "retirement":
        monkeypatch.setattr(
            service, "_retire_superseded_download_locked", fail_boundary
        )
    else:
        monkeypatch.setattr(service, "_organize_imported_chapter_locked", fail_boundary)

    await service.process_download_job(job["id"])

    paused = database.get_job(job["id"])
    assert paused["status"] == "queued", paused["message"]
    published = Path(paused["planned_path"])
    assert published != current_path
    assert (
        validate_cbz(published)["sha256"]
        == paused["language_evidence"]["archive"]["sha256"]
    )
    if boundary != "normalization":
        assert current_path.read_bytes() == b"cbz"
        assert database.get_chapter("old-release")["downloaded"] is True
    monkeypatch.undo()

    provider = NeverDownloadProvider()
    database.initialize()
    restarted = TankarrService(service.settings, database, provider, RecordingKomga())
    recovery = await restarted.recover_file_quarantines()
    assert recovery["recovery_blocked"] is False
    organization = await restarted.organize_library()
    assert organization["organization_blocked"] is False, organization["warnings"]
    assert organization["duplicates_removed"] == 0
    if boundary == "normalization":
        # The old file is already retired, so maintenance can safely complete
        # the replacement's hash-verified relocation before the worker resumes.
        assert current_path.exists()
        assert database.get_job(job["id"])["planned_path"] == str(current_path)
    else:
        assert published.exists()
        assert database.get_job(job["id"])["planned_path"] == str(published)
    assert database.prune_duplicate_queued_jobs() == 0
    await restarted.rerank_queued_jobs("manga-1")
    await restarted.process_download_job(job["id"])

    completed = database.get_job(job["id"])
    assert completed["status"] == "completed", completed["message"]
    assert provider.calls == 0
    assert database.get_chapter("old-release")["downloaded"] is False
    assert database.get_chapter("new-release")["downloaded"] is True
    assert database.get_chapter("new-release")["library_path"] == str(current_path)
    assert completed["planned_path"] == completed["result_path"] == str(current_path)
    assert validate_cbz(current_path)["page_count"] == 1
    assert not published.exists()


@pytest.mark.asyncio
async def test_replacement_never_retires_a_path_shared_by_another_release(
    tmp_path: Path,
):
    database, service, current_path, job = await replacement_fixture(tmp_path)
    alias = chapter("shared-release", "2")
    database.upsert_chapters("manga-1", [alias])
    database.mark_chapter_downloaded(alias["id"], current_path)

    await service.process_download_job(job["id"])

    paused = database.get_job(job["id"])
    assert paused["status"] == "queued", paused["message"]
    assert "referenced by another" in paused["message"]
    assert current_path.read_bytes() == b"cbz"
    assert database.get_chapter("old-release")["downloaded"] is True
    assert database.get_chapter(alias["id"])["downloaded"] is True
    assert validate_cbz(Path(paused["planned_path"]))["page_count"] == 1
    assert not final_library_path(service.settings.library_dir, manga(), alias).exists()


@pytest.mark.asyncio
async def test_legacy_replacement_plan_is_migrated_only_off_verified_old_file(
    tmp_path: Path,
):
    database, service, current_path, job = await replacement_fixture(tmp_path)
    assert database.claim_queued_job(job["id"], current_path)
    database.update_job(job["id"], status="queued")
    # Upgrade/restart sees the old worker's canonical planned_path and must
    # preserve this explicit replacement even though its slot is satisfied.
    database.initialize()
    assert database.prune_duplicate_queued_jobs() == 0
    await service.process_download_job(job["id"])

    completed = database.get_job(job["id"])
    assert completed["status"] == "completed", completed["message"]
    assert completed["result_path"] == str(current_path)
    assert database.get_chapter("old-release")["downloaded"] is False
    assert validate_cbz(current_path)["page_count"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("file_identity", ["new-published", "externally-changed"])
async def test_legacy_replanning_never_reinterprets_a_published_or_unknown_file(
    tmp_path: Path, file_identity: str
):
    database, service, current_path, job = await replacement_fixture(tmp_path)
    assert database.claim_queued_job(job["id"], current_path)
    if file_identity == "new-published":
        database.update_job(
            job["id"],
            language_evidence={"archive": {"sha256": sha256(current_path)}},
        )
    else:
        current_path.write_bytes(b"unrecognized external edit")
    database.update_job(job["id"], status="queued")
    previous = database.get_job(job["id"])

    planned = service._replan_legacy_replacement_locked(
        previous, database.get_chapter("new-release"), current_path
    )

    assert planned == current_path
    assert database.get_job(job["id"])["planned_path"] == str(current_path)
    assert (
        database.get_job(job["id"])["language_evidence"]
        == previous["language_evidence"]
    )


@pytest.mark.asyncio
async def test_organizer_preserves_pending_replacement_but_cleans_unrelated_duplicates(
    tmp_path: Path, monkeypatch
):
    database, service, current_path, job = await replacement_fixture(tmp_path)

    def before_retirement(*args):
        raise OSError("interrupt before retirement")

    monkeypatch.setattr(
        service, "_retire_superseded_download_locked", before_retirement
    )
    await service.process_download_job(job["id"])
    new_path = Path(database.get_job(job["id"])["planned_path"])
    other = chapter("other-keeper", "3")
    keep_path, _ = add_downloaded_chapter(
        database, service.settings.library_dir, manga(), other
    )
    duplicate = chapter("other-duplicate", "3")
    database.upsert_chapters("manga-1", [duplicate])
    duplicate_path = keep_path.with_name("legacy-duplicate.cbz")
    duplicate_path.write_bytes(b"duplicate book")
    database.mark_chapter_downloaded(
        duplicate["id"], duplicate_path, sha256(duplicate_path)
    )

    organization = await service.organize_library()

    assert organization["organization_blocked"] is False, organization["warnings"]
    assert organization["duplicates_removed"] == 1
    assert organization["replacement_files_deferred"] == 2
    assert current_path.read_bytes() == b"cbz"
    assert validate_cbz(new_path)["page_count"] == 1
    assert database.get_chapter("new-release")["downloaded"] is True
    assert database.get_job(job["id"])["planned_path"] == str(new_path)
    assert not duplicate_path.exists()
    assert keep_path.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("old_downloaded", [True, False])
async def test_replacement_pruning_still_reserves_only_one_job_per_slot(
    tmp_path: Path, old_downloaded: bool
):
    database, service, _current_path, first = await replacement_fixture(tmp_path)
    second_release = chapter("second-replacement", "1")
    database.upsert_chapters("manga-1", [second_release])
    second = await service.create_manual_download_job(
        second_release["id"], replace=True
    )
    if not old_downloaded:
        database.reset_chapter_file("manga-1", "old-release")

    assert database.prune_duplicate_queued_jobs() == 1
    assert database.get_job(first["id"])["status"] == "queued"
    with pytest.raises(KeyError):
        database.get_job(second["id"])


@pytest.mark.asyncio
async def test_legacy_replacement_path_io_error_is_recorded_without_escaping_worker(
    tmp_path: Path, monkeypatch
):
    database, service, current_path, job = await replacement_fixture(tmp_path)
    database.claim_queued_job(job["id"], current_path)
    database.update_job(job["id"], status="queued")

    def cannot_read(path):
        raise OSError("simulated legacy archive read failure")

    monkeypatch.setattr("tankarr.service.sha256", cannot_read)
    await service.process_download_job(job["id"])

    stored = database.get_job(job["id"])
    assert stored["status"] == "failed"
    assert "Unable to verify replacement path" in stored["message"]
    assert "simulated legacy archive read failure" in stored["message"]
    assert current_path.read_bytes() == b"cbz"
    assert database.get_chapter("old-release")["downloaded"] is True
    assert database.get_chapter("new-release")["downloaded"] is False


@pytest.mark.asyncio
async def test_legacy_replacement_replanning_does_not_override_concurrent_claim(
    tmp_path: Path, monkeypatch
):
    database, service, current_path, job = await replacement_fixture(tmp_path)
    database.claim_queued_job(job["id"], current_path)
    database.update_job(job["id"], status="queued")
    replan = database.replan_queued_replacement

    def claim_before_replan(job_id, **kwargs):
        database.update_job(job_id, status="running", message="Another worker claimed")
        return replan(job_id, **kwargs)

    monkeypatch.setattr(database, "replan_queued_replacement", claim_before_replan)
    await service.process_download_job(job["id"])

    stored = database.get_job(job["id"])
    assert stored["status"] == "running"
    assert stored["message"] == "Another worker claimed"
    assert stored["planned_path"] == str(current_path)
    assert current_path.read_bytes() == b"cbz"
    assert database.get_chapter("new-release")["downloaded"] is False


@pytest.mark.asyncio
async def test_restart_adopts_replacement_renamed_before_its_database_commit(
    tmp_path: Path, monkeypatch
):
    database, service, current_path, job = await replacement_fixture(tmp_path)

    class SimulatedCrash(BaseException):
        pass

    def crash_after_rename(*args, **kwargs):
        assert current_path.exists()
        raise SimulatedCrash()

    monkeypatch.setattr(database, "relocate_chapter_library_file", crash_after_rename)
    with pytest.raises(SimulatedCrash):
        await service.process_download_job(job["id"])

    interrupted = database.get_job(job["id"])
    journalled_path = Path(interrupted["planned_path"])
    assert interrupted["status"] == "importing"
    assert journalled_path != current_path
    assert not journalled_path.exists()
    assert (
        validate_cbz(current_path)["sha256"]
        == interrupted["language_evidence"]["archive"]["sha256"]
    )
    assert database.get_chapter("old-release")["downloaded"] is False
    monkeypatch.undo()

    database.initialize()
    provider = NeverDownloadProvider()
    restarted = TankarrService(service.settings, database, provider, RecordingKomga())
    assert (await restarted.recover_file_quarantines())["recovery_blocked"] is False
    organization = await restarted.organize_library()
    assert organization["organization_blocked"] is False, organization["warnings"]
    assert organization["adopted"] == 1
    assert database.get_job(job["id"])["planned_path"] == str(current_path)
    assert database.prune_duplicate_queued_jobs() == 0
    await restarted.rerank_queued_jobs("manga-1")
    await restarted.process_download_job(job["id"])

    completed = database.get_job(job["id"])
    assert completed["status"] == "completed", completed["message"]
    assert provider.calls == 0
    assert completed["result_path"] == str(current_path)
    assert database.get_chapter("new-release")["library_path"] == str(current_path)


@pytest.mark.asyncio
async def test_replacement_normalization_never_overwrites_a_competing_file(
    tmp_path: Path, monkeypatch
):
    from tankarr.archive import publish_without_overwrite

    database, service, current_path, job = await replacement_fixture(tmp_path)

    def competing_file(source, destination):
        assert destination == current_path
        assert not destination.exists()
        destination.write_bytes(b"concurrent external file")
        publish_without_overwrite(source, destination)

    monkeypatch.setattr("tankarr.service.publish_without_overwrite", competing_file)
    await service.process_download_job(job["id"])

    paused = database.get_job(job["id"])
    assert paused["status"] == "queued", paused["message"]
    assert current_path.read_bytes() == b"concurrent external file"
    assert validate_cbz(Path(paused["planned_path"]))["page_count"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_preferred_source", [False, True])
async def test_profile_upgrade_reaches_import_and_does_not_repeat(
    tmp_path, failed_preferred_source
):
    import asyncio

    from tankarr.monitor import ReleaseMonitor
    from tankarr.worker import DownloadWorker

    database, service, old_path, job = await replacement_fixture(tmp_path)
    database.cancel_job(job["id"])
    with database.connect() as connection:
        connection.execute(
            "UPDATE chapter_release SET source_key='suwayomi:old' WHERE id='old-release'"
        )
        connection.execute(
            "UPDATE chapter_release SET source_key='suwayomi:preferred' WHERE id='new-release'"
        )
        connection.execute(
            "UPDATE manga SET monitored=1, status_override=NULL, library_status_override=NULL"
        )
    service.settings.source_upgrade_enabled = True
    service.settings.source_priority_backfill = (
        "suwayomi:broken,suwayomi:preferred,suwayomi:old"
    )
    if failed_preferred_source:
        database.upsert_chapters(
            "manga-1",
            [
                {
                    **database.get_chapter("new-release"),
                    "id": "broken-release",
                    "source_key": "suwayomi:broken",
                }
            ],
        )
        database.block_release("broken-release", reason="Corrupt archive")
    worker = DownloadWorker(database, service)
    monitor = ReleaseMonitor(service.settings, database, service, worker)
    await worker.start()
    try:
        assert await monitor.upgrade_to_official() == 1
        pending = database.list_queued_job_ids()
        assert len(pending) == 1
        await asyncio.wait_for(worker.queue.join(), timeout=5)
        assert database.get_job(pending[0])["status"] == "completed"
        assert database.get_chapter("new-release")["downloaded"]
        assert validate_cbz(old_path)["page_count"] == 1
        assert await monitor.upgrade_to_official() == 0
    finally:
        await worker.stop()
