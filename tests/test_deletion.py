from __future__ import annotations

import asyncio
import os
import sqlite3
import threading
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tankarr.app import create_app
from tankarr.archive import package_cbz, validate_cbz
from tankarr.config import Settings
from tankarr.database import ActiveDownloadJobsError, Database
from tankarr.naming import final_library_path
from tankarr.service import (
    LibraryUnavailable,
    RecoveryBlocked,
    StaleVolumeDeletionError,
    TankarrService,
    UnsafeLibraryPath,
)
from tankarr.worker import DownloadWorker


def manga(manga_id: str = "manga-1", title: str = "Example") -> dict:
    return {
        "id": manga_id,
        "title": title,
        "description": "",
        "cover_url": None,
        "authors": [],
        "original_language": "ja",
        "status": "ongoing",
        "last_volume": None,
        "last_chapter": None,
        "available_languages": ["en"],
        "source_url": f"https://example.test/{manga_id}",
    }


def chapter(
    chapter_id: str,
    number: str,
    *,
    volume: str = "1",
    language: str = "en",
) -> dict:
    return {
        "id": chapter_id,
        "chapter": number,
        "volume": volume,
        "title": f"Chapter {number}",
        "language": language,
        "provider": "mangadex",
        "groups": [],
        "publish_at": "2026-01-01T00:00:00Z",
        "source_url": f"https://example.test/{chapter_id}",
        "pages": 20,
        "version": 1,
    }


class UnusedProvider:
    pass


class RecordingKomga:
    def __init__(self):
        self.calls = 0

    async def scan(self, _expected_relative_paths=()) -> dict:
        self.calls += 1
        return {"configured": True, "triggered": True, "library_id": "manga"}


class ReconcilingKomga:
    configured = True

    def __init__(self, *, failure: Exception | None = None):
        self.failure = failure
        self.paths: list[list[str]] = []
        self.safety_checks = 0

    async def reconcile_deleted(self, paths, *, safety_check):
        self.paths.append(list(paths))
        if self.failure is not None:
            raise self.failure
        safety_check()
        self.safety_checks += 1
        return {
            "configured": True,
            "triggered": True,
            "purged": True,
            "matched_books": len(self.paths[-1]),
            "library_id": "manga",
        }


def provision_library_identity(settings: Settings) -> None:
    identity = "0123456789abcdef0123456789abcdef"
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.library_dir.mkdir(parents=True, exist_ok=True)
    (settings.data_dir / ".tankarr-library-id").write_text(
        f"{identity}\n", encoding="utf-8"
    )
    (settings.library_dir / ".tankarr-library-id").write_text(
        f"{identity}\n", encoding="utf-8"
    )


def make_service(tmp_path: Path) -> tuple[Database, TankarrService, RecordingKomga]:
    library_dir = tmp_path / "library"
    library_dir.mkdir()
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    komga = RecordingKomga()
    settings = Settings(data_dir=tmp_path / "data", library_dir=library_dir)
    provision_library_identity(settings)
    service = TankarrService(
        settings,
        database,
        UnusedProvider(),  # type: ignore[arg-type]
        komga,  # type: ignore[arg-type]
    )
    return database, service, komga


def make_reconciling_service(
    tmp_path: Path, komga: ReconcilingKomga
) -> tuple[Database, TankarrService]:
    library_dir = tmp_path / "library"
    library_dir.mkdir()
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    settings = Settings(data_dir=tmp_path / "data", library_dir=library_dir)
    provision_library_identity(settings)
    service = TankarrService(
        settings,
        database,
        UnusedProvider(),  # type: ignore[arg-type]
        komga,  # type: ignore[arg-type]
    )
    return database, service


def add_downloaded_chapter(
    database: Database,
    library_dir: Path,
    manga_data: dict,
    chapter_data: dict,
    *,
    stored_path: Path | None = None,
) -> tuple[Path, int]:
    try:
        database.get_manga(manga_data["id"])
    except KeyError:
        database.upsert_manga(manga_data, "en", "none")
    database.upsert_chapters(manga_data["id"], [chapter_data])
    output = final_library_path(library_dir, manga_data, chapter_data)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(b"cbz")
    recorded = stored_path or output
    database.mark_chapter_downloaded(chapter_data["id"], recorded)
    job = database.create_job(
        manga_data["id"], chapter_data["id"], chapter_data["language"]
    )
    database.update_job(job["id"], status="completed", result_path=recorded)
    return output, job["id"]


@pytest.mark.asyncio
async def test_chapter_deletion_keeps_api_responsive_and_lock_until_cancelled_work_finishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    database, service, _komga = make_service(tmp_path)
    stored, _ = add_downloaded_chapter(
        database, tmp_path / "library", manga(), chapter("chapter-1", "1")
    )
    loop = asyncio.get_running_loop()
    loop_thread = threading.get_ident()
    entered = asyncio.Event()
    release = threading.Event()
    original = service._assert_library_paths_not_shared

    def slow_guard(*args):
        assert threading.get_ident() != loop_thread
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(5)
        return original(*args)

    monkeypatch.setattr(service, "_assert_library_paths_not_shared", slow_guard)
    task = asyncio.create_task(service.delete_chapter_file("manga-1", "chapter-1"))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        assert service._mutation_lock.locked()
        assert stored.exists()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(task), 3)
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
    assert not service._mutation_lock.locked()
    assert not stored.exists()
    assert database.get_chapter("chapter-1")["downloaded"] is False


@pytest.mark.asyncio
async def test_delete_chapter_file_clears_the_imported_content_ledger(tmp_path: Path):
    """Deleting an imported book must free its slot for a different book.

    The import refuses any content whose hash differs from the one recorded for
    that language/volume/chapter, and it is right to: no filename authorizes
    overwriting a book. But a deletion that left ``local_import_sha256`` behind
    made that refusal permanent, so a slot whose file was gone could never be
    filled again - not even by a corrected edition of the very same book.
    """

    database, service, _komga = make_service(tmp_path)
    manga_data = manga()
    chapter_data = chapter("chapter-1", None, volume="1")
    chapter_data["provider"] = "manual"
    database.upsert_manga(manga_data, "en", "none")
    database.upsert_chapters("manga-1", [chapter_data])
    stored = final_library_path(tmp_path / "library", manga_data, chapter_data)
    stored.parent.mkdir(parents=True, exist_ok=True)
    stored.write_bytes(b"cbz")
    database.publish_external_chapter(
        "manga-1", chapter_data, stored, "a" * 64, "b" * 64
    )
    assert database.get_chapter("chapter-1")["local_import_sha256"] == "b" * 64
    database.record_wanted_attempt(
        "manga-1", "volume:1", channel="indexer_book", outcome="grabbed"
    )
    database.record_wanted_attempt(
        "manga-1", "volume:2", channel="indexer_book", outcome="not_offered"
    )

    await service.delete_chapter_file("manga-1", "chapter-1")

    reset = database.get_chapter("chapter-1")
    assert reset["downloaded"] is False
    assert reset["library_path"] is None
    assert reset["library_sha256"] is None
    assert reset["local_import_sha256"] is None
    assert "volume:1" not in database.wanted_attempts("manga-1")
    assert "volume:2" in database.wanted_attempts("manga-1")

    # The freed slot accepts a different book, which is the whole point.
    stored.parent.mkdir(parents=True, exist_ok=True)
    stored.write_bytes(b"other")
    database.publish_external_chapter(
        "manga-1", chapter_data, stored, "c" * 64, "d" * 64
    )
    assert database.get_chapter("chapter-1")["local_import_sha256"] == "d" * 64


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "legacy_root",
    [Path("/library"), Path("/srv/old-comics"), Path("/srv/older-manga")],
)
async def test_delete_chapter_maps_legacy_host_path_and_is_idempotent(
    tmp_path: Path, legacy_root: Path
):
    database, service, komga = make_service(tmp_path)
    service.settings.legacy_library_roots = "/srv/old-comics, /srv/older-manga"
    manga_data = manga()
    chapter_data = chapter("chapter-1", "1")
    expected = final_library_path(tmp_path / "library", manga_data, chapter_data)
    legacy = legacy_root / expected.relative_to(tmp_path / "library")
    output, _ = add_downloaded_chapter(
        database,
        tmp_path / "library",
        manga_data,
        chapter_data,
        stored_path=legacy,
    )

    result = await service.delete_chapter_file("manga-1", "chapter-1")

    assert result == {
        "manga_id": "manga-1",
        "chapter_id": "chapter-1",
        "files_deleted": 1,
        "files_missing": 0,
        "directories_removed": 1,
        "quarantine_files_remaining": 0,
        "quarantine_path": None,
        "cleanup_errors": [],
        "cleanup_warning": None,
        "chapters_reset": 1,
        "jobs_deleted": 1,
        "komga_scan": {
            "requested": True,
            "configured": True,
            "triggered": True,
            "library_id": "manga",
        },
    }
    assert not output.exists()
    stored = database.get_chapter("chapter-1")
    assert stored["downloaded"] is False
    assert stored["library_path"] is None
    assert database.list_jobs() == []
    assert komga.calls == 1

    repeated = await service.delete_chapter_file("manga-1", "chapter-1")
    assert repeated["files_deleted"] == 0
    assert repeated["files_missing"] == 0
    assert repeated["jobs_deleted"] == 0
    assert repeated["komga_scan"] == {"requested": False, "triggered": False}
    assert komga.calls == 1


@pytest.mark.asyncio
async def test_delete_chapter_quarantines_matching_artwork_sidecar(tmp_path: Path):
    database, service, _ = make_service(tmp_path)
    output, _ = add_downloaded_chapter(
        database,
        tmp_path / "library",
        manga(),
        chapter("chapter-1", "1"),
    )
    sidecar = output.with_suffix(".jpg")
    sidecar.write_bytes(b"normalized-volume-cover")

    result = await service.delete_chapter_file("manga-1", "chapter-1")

    assert result["files_deleted"] == 2
    assert not output.exists()
    assert not sidecar.exists()


@pytest.mark.asyncio
async def test_delete_persists_receipt_until_guarded_komga_purge_succeeds(
    tmp_path: Path,
):
    komga = ReconcilingKomga()
    database, service = make_reconciling_service(tmp_path, komga)
    manga_data = manga()
    chapter_data = chapter("chapter-1", "1")
    output, _ = add_downloaded_chapter(
        database,
        tmp_path / "library",
        manga_data,
        chapter_data,
    )
    relative = str(output.relative_to(tmp_path / "library"))

    result = await service.delete_chapter_file("manga-1", "chapter-1")

    assert result["komga_scan"]["purged"] is True
    assert result["komga_scan"]["completed_operations"] == 1
    assert komga.paths == [[relative]]
    assert komga.safety_checks == 1
    assert database.list_deletion_operations() == []


@pytest.mark.asyncio
async def test_failed_komga_cleanup_keeps_durable_receipt_for_retry(
    tmp_path: Path,
):
    failing = ReconcilingKomga(failure=RuntimeError("Komga offline"))
    database, service = make_reconciling_service(tmp_path, failing)
    manga_data = manga()
    chapter_data = chapter("chapter-1", "1")
    add_downloaded_chapter(
        database,
        tmp_path / "library",
        manga_data,
        chapter_data,
    )

    result = await service.delete_chapter_file("manga-1", "chapter-1")

    assert result["komga_scan"]["pending_operations"] == 1
    operation = database.list_deletion_operations()[0]
    assert operation["state"] == "committed"
    assert operation["komga_attempts"] == 1
    assert "Komga offline" in operation["komga_last_error"]

    recovered = ReconcilingKomga()
    service.komga = recovered  # type: ignore[assignment]
    retry = await service.retry_pending_komga_reconciliation()

    assert retry["purged"] is True
    assert retry["completed_operations"] == 1
    assert database.list_deletion_operations() == []


@pytest.mark.asyncio
async def test_komga_cleanup_refuses_a_path_restored_after_its_scan(tmp_path: Path):
    library_dir = tmp_path / "library"

    class RestoringKomga(ReconcilingKomga):
        async def reconcile_deleted(self, paths, *, safety_check):
            restored = library_dir / list(paths)[0]
            restored.parent.mkdir(parents=True, exist_ok=True)
            restored.write_bytes(b"restored")
            safety_check()
            raise AssertionError("the safety check should reject the restored path")

    komga = RestoringKomga()
    database, service = make_reconciling_service(tmp_path, komga)
    add_downloaded_chapter(
        database,
        library_dir,
        manga(),
        chapter("chapter-1", "1"),
    )

    result = await service.delete_chapter_file("manga-1", "chapter-1")

    assert result["komga_scan"]["pending_operations"] == 1
    assert "restored before cleanup completed" in result["komga_scan"]["error"]
    assert len(database.list_deletion_operations()) == 1


@pytest.mark.asyncio
async def test_a_receipt_the_library_has_refilled_does_not_strand_the_others(
    tmp_path: Path,
):
    """One obsolete receipt used to hold the whole queue.

    A chapter deleted on Monday and downloaded again on Tuesday leaves a
    receipt naming a path that exists once more. The path did not come back:
    the library owns a book there. Every committed receipt is checked against
    one shared list of paths, so that single name kept 582 receipts
    unreconciled for a week, and the reader was never told about any of them.
    """

    library_dir = tmp_path / "library"
    failing = ReconcilingKomga(failure=RuntimeError("Komga offline"))
    database, service = make_reconciling_service(tmp_path, failing)
    manga_data = manga()
    refilled = chapter("chapter-1", "1")
    other = chapter("chapter-2", "2")
    refilled_path, _ = add_downloaded_chapter(
        database, library_dir, manga_data, refilled
    )
    other_path, _ = add_downloaded_chapter(database, library_dir, manga_data, other)
    other_relative = str(other_path.relative_to(library_dir))

    # Monday: both deleted while the reader is unreachable, so both receipts wait.
    await service.delete_chapter_file("manga-1", "chapter-1")
    await service.delete_chapter_file("manga-1", "chapter-2")
    assert len(database.list_deletion_operations()) == 2

    # Tuesday: chapter 1 is downloaded again, under the same library name.
    add_downloaded_chapter(database, library_dir, manga_data, refilled)
    assert refilled_path.exists()

    recovered = ReconcilingKomga()
    service.komga = recovered  # type: ignore[assignment]
    retry = await service.retry_pending_komga_reconciliation()

    # The refilled name is left out of the purge; the deleted one is reported,
    # and both receipts are retired instead of being retried for ever.
    assert recovered.paths == [[other_relative]]
    assert retry["purged"] is True
    assert retry["completed_operations"] == 2
    assert database.list_deletion_operations() == []
    assert refilled_path.exists()


@pytest.mark.asyncio
async def test_a_sidecar_that_reappears_does_not_hold_the_queue(tmp_path: Path):
    """Covers are not books.

    A deletion receipt names the book and the cover written beside it. The
    reader is only ever told about books, and Tankarr writes that cover
    itself, so it comes back on its own - and held 641 receipts behind it.
    """

    library_dir = tmp_path / "library"
    failing = ReconcilingKomga(failure=RuntimeError("Komga offline"))
    database, service = make_reconciling_service(tmp_path, failing)
    manga_data = manga()
    chapter_data = chapter("chapter-1", "1")
    book, _ = add_downloaded_chapter(database, library_dir, manga_data, chapter_data)
    sidecar = book.with_suffix(".jpg")
    sidecar.write_bytes(b"cover")

    await service.delete_chapter_file("manga-1", "chapter-1")
    assert not book.exists()

    # The artwork pass writes the cover again while the receipt waits.
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_bytes(b"cover")

    recovered = ReconcilingKomga()
    service.komga = recovered  # type: ignore[assignment]
    retry = await service.retry_pending_komga_reconciliation()

    assert retry["purged"] is True
    assert database.list_deletion_operations() == []


@pytest.mark.asyncio
async def test_unconfigured_komga_keeps_the_deletion_receipt(tmp_path: Path):
    class UnconfiguredKomga(ReconcilingKomga):
        async def reconcile_deleted(self, paths, *, safety_check):
            return {
                "configured": False,
                "triggered": False,
                "purged": False,
                "reason": "Komga credentials are missing",
            }

    komga = UnconfiguredKomga()
    database, service = make_reconciling_service(tmp_path, komga)
    add_downloaded_chapter(
        database,
        tmp_path / "library",
        manga(),
        chapter("chapter-1", "1"),
    )

    result = await service.delete_chapter_file("manga-1", "chapter-1")

    assert result["komga_scan"]["pending_operations"] == 1
    assert "credentials are missing" in result["komga_scan"]["error"]
    assert len(database.list_deletion_operations()) == 1


@pytest.mark.asyncio
async def test_delete_volume_resets_only_that_manga_volume(tmp_path: Path):
    database, service, komga = make_service(tmp_path)
    manga_data = manga()
    one = chapter("chapter-1", "1", volume="1")
    two = chapter("chapter-2", "2", volume="1")
    other = chapter("chapter-3", "3", volume="2")
    first_path, _ = add_downloaded_chapter(
        database, tmp_path / "library", manga_data, one
    )
    second_path, _ = add_downloaded_chapter(
        database, tmp_path / "library", manga_data, two
    )
    other_path, _ = add_downloaded_chapter(
        database, tmp_path / "library", manga_data, other
    )

    result = await service.delete_volume_files("manga-1", "1")

    assert result["chapters_matched"] == 2
    assert result["chapters_reset"] == 2
    assert result["jobs_deleted"] == 2
    assert result["files_deleted"] == 2
    assert result["komga_scan"]["triggered"] is True
    assert komga.calls == 1
    assert not first_path.exists()
    assert not second_path.exists()
    assert other_path.exists()
    assert database.get_chapter("chapter-1")["downloaded"] is False
    assert database.get_chapter("chapter-2")["downloaded"] is False
    assert database.get_chapter("chapter-3")["downloaded"] is True
    assert [job["chapter_id"] for job in database.list_jobs()] == ["chapter-3"]


@pytest.mark.asyncio
async def test_delete_volume_defaults_to_preferred_language_and_preserves_other(
    tmp_path: Path,
):
    database, service, _ = make_service(tmp_path)
    manga_data = manga()
    english = chapter("chapter-en", "1", language="en")
    italian = chapter("chapter-it", "1", language="it")
    english_path, _ = add_downloaded_chapter(
        database, tmp_path / "library", manga_data, english
    )
    italian_path, _ = add_downloaded_chapter(
        database, tmp_path / "library", manga_data, italian
    )

    result = await service.delete_volume_files("manga-1", "1")

    assert result["language"] == "en"
    assert result["chapters_matched"] == 1
    assert not english_path.exists()
    assert italian_path.exists()
    assert database.get_chapter("chapter-en")["downloaded"] is False
    assert database.get_chapter("chapter-it")["downloaded"] is True
    assert [job["chapter_id"] for job in database.list_jobs()] == ["chapter-it"]


@pytest.mark.asyncio
async def test_batch_permission_preflight_happens_before_any_unlink(tmp_path: Path):
    database, service, _ = make_service(tmp_path)
    manga_data = manga()
    first = chapter("chapter-1", "1")
    second = chapter("chapter-2", "2")
    database.upsert_manga(manga_data, "en", "none")
    database.upsert_chapters("manga-1", [first, second])
    first_path = tmp_path / "library" / "A" / "one.cbz"
    second_path = tmp_path / "library" / "B" / "two.cbz"
    for item in (first_path, second_path):
        item.parent.mkdir()
        item.write_bytes(b"keep")
    database.mark_chapter_downloaded("chapter-1", first_path)
    database.mark_chapter_downloaded("chapter-2", second_path)
    for chapter_id, result_path in (
        ("chapter-1", first_path),
        ("chapter-2", second_path),
    ):
        job = database.create_job("manga-1", chapter_id, "en")
        database.update_job(job["id"], status="completed", result_path=result_path)

    second_path.parent.chmod(0o500)
    try:
        with pytest.raises(PermissionError):
            await service.delete_volume_files("manga-1", "1")
        assert first_path.read_bytes() == b"keep"
        assert second_path.read_bytes() == b"keep"
        assert database.get_chapter("chapter-1")["downloaded"] is True
        assert database.get_chapter("chapter-2")["downloaded"] is True
    finally:
        second_path.parent.chmod(0o700)


@pytest.mark.asyncio
async def test_delete_manga_can_keep_library_files(tmp_path: Path):
    database, service, komga = make_service(tmp_path)
    manga_data = manga()
    chapter_data = chapter("chapter-1", "1")
    output, _ = add_downloaded_chapter(
        database, tmp_path / "library", manga_data, chapter_data
    )

    result = await service.delete_manga("manga-1", delete_files=False)

    assert result["deleted"] is True
    assert result["delete_files"] is False
    assert result["chapters_deleted"] == 1
    assert result["jobs_deleted"] == 1
    assert result["files_deleted"] == 0
    assert result["komga_scan"] == {"requested": False, "triggered": False}
    assert output.exists()
    assert komga.calls == 0
    with pytest.raises(KeyError):
        database.get_manga("manga-1")
    with pytest.raises(KeyError):
        database.get_chapter("chapter-1")
    assert database.list_jobs() == []


@pytest.mark.asyncio
async def test_delete_manga_removes_only_tracked_files(tmp_path: Path):
    database, service, komga = make_service(tmp_path)
    manga_data = manga()
    chapter_data = chapter("chapter-1", "1")
    tracked, _ = add_downloaded_chapter(
        database, tmp_path / "library", manga_data, chapter_data
    )
    manual = tracked.parent / "Manually imported volume.cbz"
    manual.write_bytes(b"keep")
    book_sidecar = tracked.with_suffix(".jpg")
    book_sidecar.write_bytes(b"normalized-book-cover")
    series_sidecar = tracked.parent / "cover.jpg"
    series_sidecar.write_bytes(b"normalized-series-cover")

    preview = await service.preview_manga_deletion("manga-1")
    result = await service.delete_manga(
        "manga-1",
        delete_files=True,
        confirmation_snapshot=preview["snapshot"],
    )

    assert result["files_deleted"] == 3
    assert result["chapters_deleted"] == 1
    assert result["jobs_deleted"] == 1
    assert result["directories_removed"] == 0
    assert result["komga_scan"]["triggered"] is True
    assert not tracked.exists()
    assert not book_sidecar.exists()
    assert not series_sidecar.exists()
    assert manual.read_bytes() == b"keep"
    assert komga.calls == 1


@pytest.mark.asyncio
async def test_delete_uses_authoritative_old_path_after_series_rename(tmp_path: Path):
    database, service, _ = make_service(tmp_path)
    old_manga = manga(title="Old title")
    chapter_data = chapter("chapter-1", "1")
    managed, _ = add_downloaded_chapter(
        database, tmp_path / "library", old_manga, chapter_data
    )
    renamed = manga(title="New title")
    database.upsert_manga(renamed, "en")
    manual = final_library_path(tmp_path / "library", renamed, chapter_data)
    manual.parent.mkdir(parents=True)
    manual.write_bytes(b"manual")

    result = await service.delete_chapter_file("manga-1", "chapter-1")

    assert result["files_deleted"] == 1
    assert not managed.exists()
    assert manual.read_bytes() == b"manual"


@pytest.mark.asyncio
async def test_delete_cancels_queued_but_blocks_claimed_download(tmp_path: Path):
    database, service, komga = make_service(tmp_path)
    manga_data = manga()
    chapter_data = chapter("chapter-1", "1")
    database.upsert_manga(manga_data, "en", "none")
    database.upsert_chapters("manga-1", [chapter_data])
    queued = database.create_job("manga-1", "chapter-1", "en")

    result = await service.delete_chapter_file("manga-1", "chapter-1")

    assert result["jobs_deleted"] == 1
    with pytest.raises(KeyError):
        database.get_job(queued["id"])
    await service.process_download_job(queued["id"])

    replacement = database.create_job("manga-1", "chapter-1", "en")
    planned = final_library_path(tmp_path / "library", manga_data, chapter_data)
    assert database.claim_queued_job(replacement["id"], planned)["status"] == "running"
    with pytest.raises(ActiveDownloadJobsError):
        await service.delete_chapter_file("manga-1", "chapter-1")
    assert database.get_job(replacement["id"])["status"] == "running"
    assert komga.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "unsafe_kind", ["outside", "suffix_clone", "non_cbz", "parent_symlink"]
)
async def test_delete_file_rejects_unsafe_library_paths(
    tmp_path: Path, unsafe_kind: str
):
    database, service, komga = make_service(tmp_path)
    manga_data = manga()
    chapter_data = chapter("chapter-1", "1")
    database.upsert_manga(manga_data, "en", "none")
    database.upsert_chapters("manga-1", [chapter_data])
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()

    if unsafe_kind == "outside":
        unsafe = outside_dir / "unrelated.cbz"
        unsafe.write_bytes(b"keep")
    elif unsafe_kind == "suffix_clone":
        unsafe = outside_dir / "Example" / "Chapter 1 - Chapter 1 [en].cbz"
        unsafe.parent.mkdir()
        unsafe.write_bytes(b"keep")
    elif unsafe_kind == "non_cbz":
        unsafe = tmp_path / "library" / "Example" / "unrelated.txt"
        unsafe.parent.mkdir()
        unsafe.write_bytes(b"keep")
    else:
        target = outside_dir / "Chapter 1 - Chapter 1 [en].cbz"
        target.write_bytes(b"keep")
        (tmp_path / "library" / "Example").symlink_to(
            outside_dir, target_is_directory=True
        )
        unsafe = target

    database.mark_chapter_downloaded("chapter-1", unsafe)

    with pytest.raises(UnsafeLibraryPath):
        await service.delete_chapter_file("manga-1", "chapter-1")

    assert unsafe.exists()
    assert database.get_chapter("chapter-1")["downloaded"] is True
    assert komga.calls == 0


@pytest.mark.asyncio
async def test_delete_rejects_path_shared_by_release_outside_target(tmp_path: Path):
    database, service, _ = make_service(tmp_path)
    manga_data = manga()
    volume_one = chapter("chapter-v1", "1", volume="1")
    volume_two = chapter("chapter-v2", "1", volume="2")
    database.upsert_manga(manga_data, "en", "none")
    database.upsert_chapters("manga-1", [volume_one, volume_two])
    shared = final_library_path(tmp_path / "library", manga_data, volume_one)
    shared.parent.mkdir(parents=True)
    shared.write_bytes(b"shared")
    database.mark_chapter_downloaded("chapter-v1", shared)
    database.mark_chapter_downloaded("chapter-v2", shared)

    with pytest.raises(UnsafeLibraryPath, match="another chapter release"):
        await service.delete_chapter_file("manga-1", "chapter-v1")

    assert shared.exists()
    assert database.get_chapter("chapter-v1")["downloaded"] is True
    assert database.get_chapter("chapter-v2")["downloaded"] is True


@pytest.mark.asyncio
async def test_delete_uses_running_job_planned_path_after_metadata_rename(
    tmp_path: Path,
):
    database, service, _ = make_service(tmp_path)
    manga_data = manga()
    downloaded = chapter("chapter-v1", "1", volume="1")
    running = chapter("chapter-v2", "1", volume="1")
    shared, _ = add_downloaded_chapter(
        database, tmp_path / "library", manga_data, downloaded
    )
    database.upsert_chapters("manga-1", [running])
    job = database.create_job("manga-1", "chapter-v2", "en")
    planned = final_library_path(tmp_path / "library", manga_data, running)
    assert database.claim_queued_job(job["id"], planned)["status"] == "running"
    database.upsert_manga(manga(title="Renamed while downloading"), "en")

    with pytest.raises(UnsafeLibraryPath, match="running download job"):
        await service.delete_chapter_file("manga-1", "chapter-v1")

    assert shared.exists()
    assert database.get_chapter("chapter-v1")["downloaded"] is True
    assert database.get_job(job["id"])["status"] == "running"


@pytest.mark.asyncio
async def test_delete_uses_requeued_job_planned_path_after_metadata_rename(
    tmp_path: Path,
):
    database, service, _ = make_service(tmp_path)
    manga_data = manga()
    downloaded = chapter("chapter-v1", "1", volume="1")
    requeued = chapter("chapter-v2", "1", volume="1")
    shared, _ = add_downloaded_chapter(
        database, tmp_path / "library", manga_data, downloaded
    )
    database.upsert_chapters("manga-1", [requeued])
    job = database.create_job("manga-1", "chapter-v2", "en")
    planned = final_library_path(tmp_path / "library", manga_data, requeued)
    database.claim_queued_job(job["id"], planned)
    database.update_job(
        job["id"], status="queued", message="Paused for imported-file recovery"
    )
    database.upsert_manga(manga(title="Renamed while paused"), "en")

    with pytest.raises(UnsafeLibraryPath, match="queued download job"):
        await service.delete_chapter_file("manga-1", "chapter-v1")

    assert shared.exists()
    assert database.get_chapter("chapter-v1")["downloaded"] is True
    assert database.get_job(job["id"])["status"] == "queued"
    assert database.get_job(job["id"])["planned_path"] == str(planned)


class BlockingProvider:
    def __init__(self, manga_data: dict, chapter_data: dict):
        self.manga_data = manga_data
        self.chapter_data = chapter_data
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def get_manga(self, _: str) -> dict:
        self.started.set()
        await self.release.wait()
        return dict(self.manga_data)

    async def list_chapters(self, _: str, __: str) -> list[dict]:
        return [dict(self.chapter_data)]


@pytest.mark.asyncio
async def test_delete_waits_for_refresh_and_manga_is_not_resurrected(tmp_path: Path):
    database, _, komga = make_service(tmp_path)
    manga_data = manga()
    chapter_data = chapter("chapter-1", "1")
    database.upsert_manga(manga_data, "en", "future")
    provider = BlockingProvider(manga_data, chapter_data)
    service = TankarrService(
        Settings(data_dir=tmp_path / "data", library_dir=tmp_path / "library"),
        database,
        provider,  # type: ignore[arg-type]
        komga,  # type: ignore[arg-type]
    )

    refresh = asyncio.create_task(service.refresh_manga("manga-1", "en"))
    await asyncio.wait_for(provider.started.wait(), timeout=1)
    deletion = asyncio.create_task(service.delete_manga("manga-1"))
    await asyncio.sleep(0)
    assert not deletion.done()

    provider.release.set()
    await refresh
    await deletion

    with pytest.raises(KeyError):
        database.get_manga("manga-1")


@pytest.mark.asyncio
async def test_concurrent_job_producer_cannot_leave_orphan_after_delete(
    tmp_path: Path,
):
    database, service, _ = make_service(tmp_path)
    manga_data = manga()
    chapter_data = chapter("chapter-1", "1")
    database.upsert_manga(manga_data, "en", "future")
    database.upsert_chapters("manga-1", [chapter_data])

    await service._mutation_lock.acquire()
    try:
        producer = asyncio.create_task(service.create_missing_download_jobs("manga-1"))
        await asyncio.sleep(0)
        deletion = asyncio.create_task(service.delete_manga("manga-1"))
    finally:
        service._mutation_lock.release()

    produced = await producer
    assert len(produced) == 1
    await deletion

    assert database.list_jobs() == []
    with pytest.raises(KeyError):
        database.get_manga("manga-1")
    with pytest.raises(KeyError):
        await service.create_manual_download_job("chapter-1")


def test_delete_api_validates_chapter_ownership_and_reports_active_conflict(
    tmp_path: Path,
):
    settings = Settings(
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "library",
        monitor_enabled=False,
    )
    provision_library_identity(settings)
    app = create_app(settings)
    database: Database = app.state.database
    database.upsert_manga(manga("manga-1", "One"), "en", "none")
    database.upsert_manga(manga("manga-2", "Two"), "en", "none")
    database.upsert_chapters("manga-2", [chapter("chapter-2", "2")])

    with TestClient(app) as client:
        mismatch = client.delete("/api/manga/manga-1/chapters/chapter-2/file")
        assert mismatch.status_code == 404

        job = database.create_job("manga-2", "chapter-2", "en")
        planned = final_library_path(
            tmp_path / "library", manga("manga-2", "Two"), chapter("chapter-2", "2")
        )
        assert database.claim_queued_job(job["id"], planned)["status"] == "running"
        conflict = client.delete("/api/manga/manga-2/chapters/chapter-2/file")
        assert conflict.status_code == 409
        assert "active" in conflict.json()["detail"]


@pytest.mark.asyncio
async def test_staging_second_file_failure_rolls_back_entire_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    database, service, komga = make_service(tmp_path)
    manga_data = manga()
    first_path, _ = add_downloaded_chapter(
        database,
        tmp_path / "library",
        manga_data,
        chapter("chapter-1", "1"),
    )
    second_path, _ = add_downloaded_chapter(
        database,
        tmp_path / "library",
        manga_data,
        chapter("chapter-2", "2"),
    )
    real_replace = os.replace
    staged_moves = 0

    def fail_second_staged_move(source, destination):
        nonlocal staged_moves
        if Path(source).suffix.lower() == ".cbz" and Path(destination).suffix == (
            ".quarantined"
        ):
            staged_moves += 1
            if staged_moves == 2:
                raise OSError("simulated second rename failure")
        return real_replace(source, destination)

    monkeypatch.setattr("tankarr.service.os.replace", fail_second_staged_move)

    with pytest.raises(OSError, match="second rename failure"):
        await service.delete_volume_files("manga-1", "1")

    assert first_path.exists()
    assert second_path.exists()
    assert database.get_chapter("chapter-1")["downloaded"] is True
    assert database.get_chapter("chapter-2")["downloaded"] is True
    assert database.list_deletion_operations() == []
    assert list((tmp_path / "library").glob(".tankarr-delete-*")) == []
    assert komga.calls == 0


@pytest.mark.asyncio
async def test_recovery_rolls_back_pre_db_quarantine_after_restart(tmp_path: Path):
    database, service, komga = make_service(tmp_path)
    manga_data = manga()
    output, _ = add_downloaded_chapter(
        database,
        tmp_path / "library",
        manga_data,
        chapter("chapter-1", "1"),
    )

    quarantine = service._stage_library_files([output])

    assert not output.exists()
    assert quarantine.operation_id is not None
    assert database.get_deletion_operation(quarantine.operation_id)["state"] == (
        "prepared"
    )
    restarted = TankarrService(
        service.settings,
        database,
        UnusedProvider(),  # type: ignore[arg-type]
        komga,  # type: ignore[arg-type]
    )

    recovery = await restarted.recover_file_quarantines()

    assert recovery["rolled_back"] == 1
    assert recovery["purged"] == 0
    assert recovery["recovery_blocked"] is False
    assert recovery["komga_scan"] == {"requested": False, "triggered": False}
    assert output.exists()
    assert database.get_chapter("chapter-1")["downloaded"] is True
    assert database.list_deletion_operations() == []
    assert komga.calls == 0


@pytest.mark.asyncio
async def test_recovery_purges_db_committed_quarantine_and_scans_komga(
    tmp_path: Path,
):
    database, service, komga = make_service(tmp_path)
    manga_data = manga()
    output, _ = add_downloaded_chapter(
        database,
        tmp_path / "library",
        manga_data,
        chapter("chapter-1", "1"),
    )
    quarantine = service._stage_library_files([output])
    assert quarantine.operation_id is not None
    database.reset_chapter_file("manga-1", "chapter-1", quarantine.operation_id)
    assert database.get_deletion_operation(quarantine.operation_id)["state"] == (
        "committed"
    )

    restarted = TankarrService(
        service.settings,
        database,
        UnusedProvider(),  # type: ignore[arg-type]
        komga,  # type: ignore[arg-type]
    )
    recovery = await restarted.recover_file_quarantines()

    assert recovery["rolled_back"] == 0
    assert recovery["purged"] == 1
    assert recovery["files_deleted"] == 1
    assert recovery["recovery_blocked"] is False
    assert recovery["komga_scan"]["triggered"] is True
    assert not output.exists()
    assert database.get_chapter("chapter-1")["downloaded"] is False
    assert database.list_deletion_operations() == []
    assert komga.calls == 1


@pytest.mark.asyncio
async def test_cleanup_failure_is_journaled_and_retried_after_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    database, service, komga = make_service(tmp_path)
    manga_data = manga()
    output, _ = add_downloaded_chapter(
        database,
        tmp_path / "library",
        manga_data,
        chapter("chapter-1", "1"),
    )
    real_unlink = Path.unlink

    def fail_quarantine_unlink(path: Path, *args, **kwargs):
        if path.suffix == ".quarantined":
            raise OSError("simulated quarantine cleanup failure")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_quarantine_unlink)

    deletion = await service.delete_chapter_file("manga-1", "chapter-1")

    assert deletion["files_deleted"] == 0
    assert deletion["quarantine_files_remaining"] == 1
    assert deletion["cleanup_warning"]
    assert "simulated quarantine cleanup failure" in " ".join(
        deletion["cleanup_errors"]
    )
    assert deletion["quarantine_path"] is not None
    assert not output.exists()
    assert database.get_chapter("chapter-1")["downloaded"] is False
    operations = database.list_deletion_operations()
    assert len(operations) == 1
    assert operations[0]["state"] == "committed"
    assert service.last_deletion_recovery["recovery_blocked"] is True
    with pytest.raises(RecoveryBlocked):
        await service.create_manual_download_job("chapter-1")

    monkeypatch.undo()
    restarted = TankarrService(
        service.settings,
        database,
        UnusedProvider(),  # type: ignore[arg-type]
        komga,  # type: ignore[arg-type]
    )
    recovery = await restarted.recover_file_quarantines()

    assert recovery["purged"] == 1
    assert recovery["files_deleted"] == 1
    assert recovery["recovery_blocked"] is False
    assert database.list_deletion_operations() == []
    assert list((tmp_path / "library").glob(".tankarr-delete-*")) == []
    # Komga is contacted only after the committed quarantine is fully purged;
    # a partial filesystem cleanup must never authorize catalogue deletion.
    assert komga.calls == 1


@pytest.mark.asyncio
async def test_post_commit_database_exception_never_rolls_library_file_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    database, service, komga = make_service(tmp_path)
    manga_data = manga()
    output, _ = add_downloaded_chapter(
        database,
        tmp_path / "library",
        manga_data,
        chapter("chapter-1", "1"),
    )
    real_reset = database.reset_chapter_file

    def commit_then_raise(*args, **kwargs):
        real_reset(*args, **kwargs)
        raise RuntimeError("simulated wrapper failure after commit")

    monkeypatch.setattr(database, "reset_chapter_file", commit_then_raise)

    with pytest.raises(RuntimeError, match="transaction committed"):
        await service.delete_chapter_file("manga-1", "chapter-1")

    assert not output.exists()
    assert database.get_chapter("chapter-1")["downloaded"] is False
    assert database.list_jobs() == []
    operations = database.list_deletion_operations()
    assert len(operations) == 1
    assert operations[0]["state"] == "committed"
    assert service.last_deletion_recovery["recovery_blocked"] is True
    quarantine_dir = Path(operations[0]["manifest_path"]).parent
    assert list(quarantine_dir.glob("*.quarantined"))

    restarted = TankarrService(
        service.settings,
        database,
        UnusedProvider(),  # type: ignore[arg-type]
        komga,  # type: ignore[arg-type]
    )
    recovery = await restarted.recover_file_quarantines()
    assert recovery["purged"] == 1
    assert recovery["recovery_blocked"] is False
    assert database.list_deletion_operations() == []
    assert not quarantine_dir.exists()
    assert komga.calls == 1


@pytest.mark.asyncio
async def test_incomplete_runtime_rollback_blocks_followup_mutations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    database, service, _ = make_service(tmp_path)
    manga_data = manga()
    first_path, _ = add_downloaded_chapter(
        database,
        tmp_path / "library",
        manga_data,
        chapter("chapter-1", "1"),
    )
    second_path, _ = add_downloaded_chapter(
        database,
        tmp_path / "library",
        manga_data,
        chapter("chapter-2", "2"),
    )
    real_replace = os.replace
    staged_moves = 0

    def fail_stage_and_rollback(source, destination):
        nonlocal staged_moves
        source_path = Path(source)
        destination_path = Path(destination)
        if (
            source_path.suffix.lower() == ".cbz"
            and destination_path.suffix == ".quarantined"
        ):
            staged_moves += 1
            if staged_moves == 2:
                raise OSError("simulated staging failure")
        if (
            source_path.suffix == ".quarantined"
            and destination_path.suffix.lower() == ".cbz"
        ):
            raise OSError("simulated rollback failure")
        return real_replace(source, destination)

    monkeypatch.setattr("tankarr.service.os.replace", fail_stage_and_rollback)

    with pytest.raises(RuntimeError, match="rollback was incomplete"):
        await service.delete_volume_files("manga-1", "1")

    assert not first_path.exists()
    assert second_path.exists()
    assert database.get_chapter("chapter-1")["downloaded"] is True
    assert database.get_chapter("chapter-2")["downloaded"] is True
    assert service.last_deletion_recovery["recovery_blocked"] is True
    with pytest.raises(RecoveryBlocked):
        await service.delete_chapter_file("manga-1", "chapter-2")


@pytest.mark.asyncio
async def test_unprovisioned_library_is_read_only_and_health_never_bootstraps(
    tmp_path: Path,
):
    settings = Settings(
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "library",
        monitor_enabled=False,
    )
    settings.data_dir.mkdir()
    settings.library_dir.mkdir()
    database = Database(settings.database_path)
    database.initialize()
    manga_data = manga()
    chapter_data = chapter("chapter-1", "1")
    database.upsert_manga(manga_data, "en", "none")
    database.upsert_chapters("manga-1", [chapter_data])
    output = final_library_path(settings.library_dir, manga_data, chapter_data)
    output.parent.mkdir(parents=True)
    output.write_bytes(b"keep")
    database.mark_chapter_downloaded("chapter-1", output)
    service = TankarrService(
        settings,
        database,
        UnusedProvider(),  # type: ignore[arg-type]
        RecordingKomga(),  # type: ignore[arg-type]
    )

    assert service.library_status()["available"] is False
    assert not (settings.data_dir / ".tankarr-library-id").exists()
    assert not (settings.library_dir / ".tankarr-library-id").exists()
    with pytest.raises(LibraryUnavailable, match="not provisioned"):
        await service.delete_chapter_file("manga-1", "chapter-1")
    assert output.exists()
    assert database.get_chapter("chapter-1")["downloaded"] is True

    app = create_app(settings)
    with TestClient(app) as client:
        health = client.get("/api/system/health")
        assert health.status_code == 200
        assert health.json()["library"]["available"] is False
        assert client.get("/api/ready").status_code == 503
        rejected = client.delete("/api/manga/manga-1/chapters/chapter-1/file")
        assert rejected.status_code == 503
        assert app.state.worker.task is None
        assert app.state.monitor.task is None
        kept = client.delete("/api/manga/manga-1", params={"delete_files": False})
        assert kept.status_code == 200
        assert kept.json()["deleted"] is True
        assert kept.json()["delete_files"] is False

    assert not (settings.data_dir / ".tankarr-library-id").exists()
    assert not (settings.library_dir / ".tankarr-library-id").exists()
    assert output.exists()


@pytest.mark.asyncio
async def test_missing_library_marker_returns_503_without_reset(tmp_path: Path):
    database, service, _ = make_service(tmp_path)
    manga_data = manga()
    output, _ = add_downloaded_chapter(
        database,
        tmp_path / "library",
        manga_data,
        chapter("chapter-1", "1"),
    )
    (tmp_path / "library" / ".tankarr-library-id").unlink()

    with pytest.raises(LibraryUnavailable, match="marker is missing"):
        await service.delete_chapter_file("manga-1", "chapter-1")

    assert output.exists()
    assert database.get_chapter("chapter-1")["downloaded"] is True
    assert len(database.list_jobs()) == 1


@pytest.mark.asyncio
async def test_volume_snapshot_mismatch_is_409_without_mutation(tmp_path: Path):
    database, service, komga = make_service(tmp_path)
    manga_data = manga()
    first, _ = add_downloaded_chapter(
        database,
        tmp_path / "library",
        manga_data,
        chapter("chapter-1", "1"),
    )
    second, _ = add_downloaded_chapter(
        database,
        tmp_path / "library",
        manga_data,
        chapter("chapter-2", "2"),
    )

    with pytest.raises(StaleVolumeDeletionError, match="Volume changed"):
        await service.delete_volume_files(
            "manga-1", "1", "en", expected_downloaded_count=1
        )

    assert first.exists()
    assert second.exists()
    assert database.get_chapter("chapter-1")["downloaded"] is True
    assert database.get_chapter("chapter-2")["downloaded"] is True
    assert database.list_deletion_operations() == []
    assert komga.calls == 0


@pytest.mark.asyncio
async def test_volume_snapshot_detects_same_count_chapter_swap(tmp_path: Path):
    database, service, komga = make_service(tmp_path)
    manga_data = manga()
    confirmed = chapter("chapter-a", "1")
    replacement = chapter("chapter-b", "2")
    confirmed_path, _ = add_downloaded_chapter(
        database, tmp_path / "library", manga_data, confirmed
    )
    database.upsert_chapters("manga-1", [replacement])

    database.reset_chapter_file("manga-1", "chapter-a")
    replacement_path = final_library_path(tmp_path / "library", manga_data, replacement)
    replacement_path.write_bytes(b"replacement")
    database.mark_chapter_downloaded("chapter-b", replacement_path)
    database.create_job("manga-1", "chapter-b", "en")

    with pytest.raises(StaleVolumeDeletionError, match="Volume changed"):
        await service.delete_volume_files(
            "manga-1",
            "1",
            "en",
            expected_downloaded_count=1,
            expected_downloaded_chapter_ids=["chapter-a"],
        )

    assert confirmed_path.exists()
    assert replacement_path.exists()
    assert database.get_chapter("chapter-a")["downloaded"] is False
    assert database.get_chapter("chapter-b")["downloaded"] is True
    assert komga.calls == 0


class PageProvider:
    async def download_pages(
        self, _, destination: Path, progress, *, concurrency: int
    ) -> list[Path]:
        del concurrency
        destination.mkdir(parents=True, exist_ok=True)
        page = destination / "0001.jpg"
        page.write_bytes(b"page")
        await progress(1, 1)
        return [page]


class WaitingPageProvider(PageProvider):
    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def download_pages(
        self, chapter_id, destination: Path, progress, *, concurrency: int
    ) -> list[Path]:
        self.started.set()
        await self.release.wait()
        return await super().download_pages(
            chapter_id, destination, progress, concurrency=concurrency
        )


class NeverDownloadProvider:
    def __init__(self):
        self.calls = 0

    async def download_pages(self, *args, **kwargs):
        self.calls += 1
        raise AssertionError("provider must not run for an imported-file recovery")


class BlockingScanKomga(RecordingKomga):
    def __init__(self):
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def scan(self, _expected_relative_paths=()) -> dict:
        self.calls += 1
        self.started.set()
        await self.release.wait()
        return {"configured": True, "triggered": True, "library_id": "manga"}


@pytest.mark.asyncio
async def test_worker_remains_active_until_file_and_db_snapshot_are_published(
    tmp_path: Path,
):
    database, _, _ = make_service(tmp_path)
    settings = Settings(data_dir=tmp_path / "data", library_dir=tmp_path / "library")
    manga_data = manga()
    chapter_data = chapter("chapter-1", "1")
    database.upsert_manga(manga_data, "en", "none")
    database.upsert_chapters("manga-1", [chapter_data])
    job = database.create_job("manga-1", "chapter-1", "en")
    komga = BlockingScanKomga()
    service = TankarrService(
        settings,
        database,
        PageProvider(),  # type: ignore[arg-type]
        komga,  # type: ignore[arg-type]
    )

    processing = asyncio.create_task(service.process_download_job(job["id"]))
    await asyncio.wait_for(komga.started.wait(), timeout=2)

    imported = database.get_chapter("chapter-1")
    assert imported["downloaded"] is True
    assert Path(imported["library_path"]).exists()
    assert database.get_job(job["id"])["status"] == "importing"
    with pytest.raises(ActiveDownloadJobsError):
        await service.delete_chapter_file("manga-1", "chapter-1")
    assert Path(imported["library_path"]).exists()

    service._mark_recovery_blocked("simulated recovery failure after import")
    komga.release.set()
    await asyncio.wait_for(processing, timeout=2)
    assert database.get_job(job["id"])["status"] == "completed"


@pytest.mark.asyncio
async def test_pre_destination_failure_marks_job_failed_without_killing_worker(
    tmp_path: Path,
):
    database, base_service, _ = make_service(tmp_path)
    manga_data = manga()
    chapter_data = chapter("chapter-1", "1")
    database.upsert_manga(manga_data, "en", "none")
    database.upsert_chapters("manga-1", [chapter_data])
    job = database.create_job("manga-1", "chapter-1", "it")
    provider = NeverDownloadProvider()
    service = TankarrService(
        base_service.settings,
        database,
        provider,  # type: ignore[arg-type]
        RecordingKomga(),  # type: ignore[arg-type]
    )
    worker = DownloadWorker(database, service)

    await worker.start()
    try:
        await asyncio.wait_for(worker.queue.join(), timeout=2)
        failed = database.get_job(job["id"])
        assert failed["status"] == "failed"
        assert "does not match requested language it" in failed["message"]
        assert provider.calls == 0
        assert worker.task is not None
        assert not worker.task.done()
    finally:
        await worker.stop()


@pytest.mark.asyncio
async def test_claimed_job_is_requeued_if_recovery_blocks_before_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    database, _, _ = make_service(tmp_path)
    settings = Settings(data_dir=tmp_path / "data", library_dir=tmp_path / "library")
    manga_data = manga()
    downloading = chapter("chapter-a", "1")
    cleanup_target = chapter("chapter-b", "2")
    database.upsert_manga(manga_data, "en", "none")
    database.upsert_chapters("manga-1", [downloading, cleanup_target])
    cleanup_path, _ = add_downloaded_chapter(
        database, settings.library_dir, manga_data, cleanup_target
    )
    job = database.create_job("manga-1", "chapter-a", "en")
    provider = WaitingPageProvider()
    service = TankarrService(
        settings,
        database,
        provider,  # type: ignore[arg-type]
        RecordingKomga(),  # type: ignore[arg-type]
    )
    real_unlink = Path.unlink

    def fail_quarantine_unlink(path: Path, *args, **kwargs):
        if path.suffix == ".quarantined":
            raise OSError("simulated runtime cleanup failure")
        return real_unlink(path, *args, **kwargs)

    processing = asyncio.create_task(service.process_download_job(job["id"]))
    await asyncio.wait_for(provider.started.wait(), timeout=2)
    monkeypatch.setattr(Path, "unlink", fail_quarantine_unlink)
    cleanup = await service.delete_chapter_file("manga-1", "chapter-b")
    assert cleanup["cleanup_warning"]
    assert not cleanup_path.exists()
    assert service.last_deletion_recovery["recovery_blocked"] is True

    provider.release.set()
    await asyncio.wait_for(processing, timeout=2)

    stored_job = database.get_job(job["id"])
    assert stored_job["status"] == "queued"
    assert stored_job["message"] == "Paused for deletion recovery"
    assert database.get_chapter("chapter-a")["downloaded"] is False
    assert not final_library_path(
        settings.library_dir, manga_data, downloading
    ).exists()


@pytest.mark.asyncio
async def test_retry_adopts_hash_verified_file_at_persisted_pre_rename_path(
    tmp_path: Path,
):
    database, base_service, _ = make_service(tmp_path)
    settings = base_service.settings
    original_manga = manga(title="Original title")
    chapter_data = chapter("chapter-1", "1")
    database.upsert_manga(original_manga, "en", "none")
    database.upsert_chapters("manga-1", [chapter_data])
    job = database.create_job("manga-1", "chapter-1", "en")
    destination = final_library_path(settings.library_dir, original_manga, chapter_data)
    page = tmp_path / "recovered-page.jpg"
    page.write_bytes(b"recovered page")
    package_cbz(destination, [page], original_manga, chapter_data)
    archive_info = validate_cbz(destination)
    database.claim_queued_job(job["id"], destination)
    database.update_job(
        job["id"],
        status="importing",
        progress=0.88,
        message="Importing into manga library",
        language_evidence={
            "provider": {"verdict": "confirmed"},
            "archive": archive_info,
        },
    )
    database.upsert_manga(manga(title="Renamed title"), "en")
    renamed_destination = final_library_path(
        settings.library_dir, manga(title="Renamed title"), chapter_data
    )
    database.initialize()
    assert database.get_job(job["id"])["status"] == "queued"

    provider = NeverDownloadProvider()
    komga = RecordingKomga()
    restarted = TankarrService(
        settings,
        database,
        provider,  # type: ignore[arg-type]
        komga,  # type: ignore[arg-type]
    )
    await restarted.process_download_job(job["id"])

    recovered = database.get_job(job["id"])
    assert provider.calls == 0
    assert recovered["status"] == "completed"
    assert recovered["message"] == "Recovered imported file"
    assert recovered["result_path"] == str(renamed_destination)
    assert recovered["planned_path"] == str(renamed_destination)
    assert recovered["language_evidence"]["recovery"] == {
        "recovered_existing_import": True,
        "sha256_verified": True,
    }
    stored_chapter = database.get_chapter("chapter-1")
    assert stored_chapter["downloaded"] is True
    assert stored_chapter["library_path"] == str(renamed_destination)
    assert stored_chapter["library_sha256"] == archive_info["sha256"]
    assert renamed_destination.exists()
    assert not destination.exists()
    assert komga.calls == 1


@pytest.mark.asyncio
async def test_retry_refuses_different_file_at_persisted_import_path(tmp_path: Path):
    database, base_service, _ = make_service(tmp_path)
    settings = base_service.settings
    manga_data = manga()
    chapter_data = chapter("chapter-1", "1")
    database.upsert_manga(manga_data, "en", "none")
    database.upsert_chapters("manga-1", [chapter_data])
    job = database.create_job("manga-1", "chapter-1", "en")
    destination = final_library_path(settings.library_dir, manga_data, chapter_data)
    expected_page = tmp_path / "expected.jpg"
    expected_page.write_bytes(b"expected")
    package_cbz(destination, [expected_page], manga_data, chapter_data)
    expected_archive = validate_cbz(destination)
    database.claim_queued_job(job["id"], destination)
    database.update_job(
        job["id"],
        status="importing",
        language_evidence={"archive": expected_archive},
    )
    different_page = tmp_path / "different.jpg"
    different_page.write_bytes(b"different")
    package_cbz(destination, [different_page], manga_data, chapter_data)
    different_hash = validate_cbz(destination)["sha256"]
    assert different_hash != expected_archive["sha256"]
    database.initialize()
    provider = NeverDownloadProvider()
    service = TankarrService(
        settings,
        database,
        provider,  # type: ignore[arg-type]
        RecordingKomga(),  # type: ignore[arg-type]
    )

    await service.process_download_job(job["id"])

    failed = database.get_job(job["id"])
    assert failed["status"] == "failed"
    assert "hash differs" in failed["message"]
    assert provider.calls == 0
    assert validate_cbz(destination)["sha256"] == different_hash
    assert database.get_chapter("chapter-1")["downloaded"] is False


@pytest.mark.asyncio
async def test_post_replace_fsync_failure_requeues_and_recovers_without_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    database, base_service, _ = make_service(tmp_path)
    settings = Settings(
        data_dir=base_service.settings.data_dir,
        library_dir=base_service.settings.library_dir,
    )
    manga_data = manga()
    chapter_data = chapter("chapter-1", "1")
    database.upsert_manga(manga_data, "en", "none")
    database.upsert_chapters("manga-1", [chapter_data])
    job = database.create_job("manga-1", "chapter-1", "en")
    first_provider = PageProvider()
    komga = RecordingKomga()
    service = TankarrService(
        settings,
        database,
        first_provider,  # type: ignore[arg-type]
        komga,  # type: ignore[arg-type]
    )

    def fail_directory_fsync(_: Path) -> None:
        raise OSError("simulated directory fsync failure")

    monkeypatch.setattr("tankarr.archive.fsync_directory", fail_directory_fsync)
    await service.process_download_job(job["id"])

    paused = database.get_job(job["id"])
    destination = Path(paused["planned_path"])
    assert paused["status"] == "queued"
    assert "ImportDurabilityError" in paused["message"]
    assert (
        paused["language_evidence"]["archive"]["sha256"]
        == validate_cbz(destination)["sha256"]
    )
    assert destination.exists()
    assert database.get_chapter("chapter-1")["downloaded"] is False
    assert komga.calls == 0

    monkeypatch.undo()
    retry_provider = NeverDownloadProvider()
    restarted = TankarrService(
        settings,
        database,
        retry_provider,  # type: ignore[arg-type]
        komga,  # type: ignore[arg-type]
    )
    await restarted.process_download_job(job["id"])

    completed = database.get_job(job["id"])
    assert completed["status"] == "completed"
    assert completed["message"] == "Recovered imported file"
    assert retry_provider.calls == 0
    assert database.get_chapter("chapter-1")["downloaded"] is True
    assert komga.calls == 1


@pytest.mark.asyncio
async def test_ambiguous_post_install_db_mark_requeues_hash_verified_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    database, base_service, _ = make_service(tmp_path)
    settings = Settings(
        data_dir=base_service.settings.data_dir,
        library_dir=base_service.settings.library_dir,
    )
    manga_data = manga()
    chapter_data = chapter("chapter-1", "1")
    database.upsert_manga(manga_data, "en", "none")
    database.upsert_chapters("manga-1", [chapter_data])
    job = database.create_job("manga-1", "chapter-1", "en")
    real_mark = database.mark_chapter_downloaded

    def commit_mark_then_raise(*args, **kwargs):
        real_mark(*args, **kwargs)
        raise RuntimeError("simulated mark wrapper failure")

    monkeypatch.setattr(database, "mark_chapter_downloaded", commit_mark_then_raise)
    service = TankarrService(
        settings,
        database,
        PageProvider(),  # type: ignore[arg-type]
        RecordingKomga(),  # type: ignore[arg-type]
    )

    await service.process_download_job(job["id"])

    paused = database.get_job(job["id"])
    destination = Path(paused["planned_path"])
    assert paused["status"] == "queued"
    assert "simulated mark wrapper failure" in paused["message"]
    assert destination.exists()
    assert database.get_chapter("chapter-1")["downloaded"] is True

    monkeypatch.undo()
    provider = NeverDownloadProvider()
    restarted = TankarrService(
        settings,
        database,
        provider,  # type: ignore[arg-type]
        RecordingKomga(),  # type: ignore[arg-type]
    )
    await restarted.process_download_job(job["id"])

    assert provider.calls == 0
    assert database.get_job(job["id"])["status"] == "completed"
    assert database.get_job(job["id"])["message"] == "Recovered imported file"


def test_ambiguous_recovery_blocks_readiness_and_background_mutation(tmp_path: Path):
    settings = Settings(
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "library",
        monitor_enabled=True,
        monitor_interval_seconds=3600,
    )
    provision_library_identity(settings)
    app = create_app(settings)
    database: Database = app.state.database
    manga_data = manga()
    chapter_data = chapter("chapter-1", "1")
    database.upsert_manga(manga_data, "en", "future")
    database.upsert_chapters("manga-1", [chapter_data])
    job = database.create_job("manga-1", "chapter-1", "en")
    original = final_library_path(settings.library_dir, manga_data, chapter_data)
    original.parent.mkdir(parents=True)
    original.write_bytes(b"original")

    operation_id = "a" * 32
    quarantine_dir = settings.library_dir / f".tankarr-delete-{operation_id}"
    quarantine_dir.mkdir()
    staged = quarantine_dir / "000000.quarantined"
    staged.write_bytes(b"staged")
    manifest = quarantine_dir / "manifest.json"
    manifest.write_text(
        '{"files":[{"original":"Example/Chapter 1 - Chapter 1 [en].cbz",'
        '"staged":"000000.quarantined"}],"missing":0,'
        f'"operation_id":"{operation_id}","state":"pre-db",'
        '"version":1}\n',
        encoding="utf-8",
    )
    database.create_deletion_operation(operation_id, manifest)

    with TestClient(app) as client:
        assert client.get("/api/ready").status_code == 503
        health = client.get("/api/system/health").json()
        assert health["deletion_recovery"]["recovery_blocked"] is True
        assert health["deletion_recovery"]["warnings"]
        assert app.state.worker.task is None
        assert app.state.monitor.task is None
        rejected = client.post(
            "/api/chapters/chapter-1/download", json={"force": False}
        )
        assert rejected.status_code == 503

    assert original.read_bytes() == b"original"
    assert staged.read_bytes() == b"staged"
    assert database.get_job(job["id"])["status"] == "queued"
    assert database.get_deletion_operation(operation_id)["state"] == "prepared"


def test_manifestless_nonempty_quarantine_blocks_startup(tmp_path: Path):
    settings = Settings(
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "library",
        monitor_enabled=True,
        monitor_interval_seconds=3600,
    )
    provision_library_identity(settings)
    operation_id = "b" * 32
    quarantine_dir = settings.library_dir / f".tankarr-delete-{operation_id}"
    quarantine_dir.mkdir()
    staged = quarantine_dir / "000000.quarantined"
    staged.write_bytes(b"hidden managed file")
    app = create_app(settings)

    with TestClient(app) as client:
        ready = client.get("/api/ready")
        assert ready.status_code == 503
        recovery = client.get("/api/system/health").json()["deletion_recovery"]
        assert recovery["recovery_blocked"] is True
        assert any("no manifest" in warning for warning in recovery["warnings"])
        assert app.state.worker.task is None
        assert app.state.monitor.task is None

    assert staged.read_bytes() == b"hidden managed file"


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["operator", "mangaupdates"])
async def test_delete_duplicates_removes_only_chapters_covered_by_owned_volumes(
    tmp_path: Path,
    source,
):
    from tankarr.chapter_map import entries_from_releases

    database, service, komga = make_service(tmp_path)
    manga_data = manga()
    library = tmp_path / "library"
    # A source that numbers whole volumes as chapters, reclassified as volume 1.
    volume_row = {**chapter("vol-1", "1"), "provider": "mangapill", "volume": None}
    database.upsert_manga(manga_data, "en", "none")
    database.upsert_chapters("manga-1", [volume_row])
    assert database.reclassify_provider_releases_as_volumes("manga-1", "mangapill") == 1
    volume_path = library / "Example (Unknown Author)" / "Example - v001 [en].cbz"
    volume_path.parent.mkdir(parents=True, exist_ok=True)
    volume_path.write_bytes(b"cbz")
    database.mark_chapter_downloaded("vol-1", volume_path)
    # Real chapters 1-3: the map puts 1-2 inside volume 1, chapter 3 elsewhere.
    paths = {}
    for number in ("1", "2", "3"):
        row = {**chapter(f"c{number}", number), "volume": None}
        paths[number], _ = add_downloaded_chapter(database, library, manga_data, row)
    database.replace_chapter_map(
        "manga-1",
        source,
        entries_from_releases([{"volume": "1", "chapter": "1-2"}]),
    )

    duplicates = service.duplicate_chapter_files("manga-1")
    if source != "operator":
        assert duplicates == []
        result = await service.delete_duplicate_chapter_files("manga-1")
        assert result["chapters_matched"] == 0
        assert all(path.exists() for path in paths.values())
        return
    assert sorted(item["id"] for item in duplicates) == ["c1", "c2"]
    assert {item["duplicate_of_volume"] for item in duplicates} == {"1"}

    with pytest.raises(StaleVolumeDeletionError):
        await service.delete_duplicate_chapter_files("manga-1", ["c1"])

    result = await service.delete_duplicate_chapter_files("manga-1", ["c2", "c1"])

    assert result["chapters_matched"] == 2
    assert result["chapters_reset"] == 2
    assert result["files_deleted"] == 2
    assert result["volumes"] == ["1"]
    assert komga.calls == 1
    assert not paths["1"].exists() and not paths["2"].exists()
    assert paths["3"].exists() and volume_path.exists()
    assert database.get_chapter("vol-1")["downloaded"] is True
    assert database.get_chapter("c3")["downloaded"] is True
    assert service.duplicate_chapter_files("manga-1") == []
    assert (await service.delete_duplicate_chapter_files("manga-1"))[
        "chapters_matched"
    ] == 0


@pytest.mark.asyncio
async def test_replace_keeps_the_current_file_until_the_other_release_has_passed(
    tmp_path: Path,
):
    # ONE PIECE 554, live: the old file was deleted before the replacement
    # was judged, the replacement was refused, and the chapter was gone. A
    # replacement now names what it supersedes; the worker deletes the old
    # file only once the new one has passed every gate.
    database, service, komga = make_service(tmp_path)
    manga_data = manga()
    current = {**chapter("wc-1", "1"), "provider": "suwayomi"}
    current_path, _ = add_downloaded_chapter(
        database, tmp_path / "library", manga_data, current
    )
    other = {**chapter("fire-1", "1"), "provider": "suwayomi", "id": "fire-1"}
    database.upsert_chapters("manga-1", [other])

    with pytest.raises(ValueError, match="already downloaded"):
        await service.create_manual_download_job("wc-1")

    job = await service.create_manual_download_job("fire-1", replace=True)

    assert job["chapter_id"] == "fire-1" and job["status"] == "queued"
    assert job["supersedes_chapter_id"] == "wc-1"
    assert current_path.exists()
    assert database.get_chapter("wc-1")["downloaded"] is True
    assert komga.calls == 0


@pytest.mark.asyncio
async def test_bulk_chapter_deletion_scans_shared_paths_once(tmp_path: Path):
    """Retiring a whole series must not repeat the library-wide safety scan.

    The scan walks every series and the pending queue, so one pass per deleted
    file made retiring a long series take hours and blocked the event loop."""

    database, service, _komga = make_service(tmp_path)
    manga_data = manga()
    chapter_ids = []
    for index in range(1, 6):
        chapter_data = chapter(f"chapter-{index}", str(index))
        add_downloaded_chapter(database, tmp_path / "library", manga_data, chapter_data)
        chapter_ids.append(chapter_data["id"])

    scans = 0
    original = service._assert_library_paths_not_shared

    def counting_scan(*args: object, **kwargs: object) -> None:
        nonlocal scans
        scans += 1
        return original(*args, **kwargs)  # type: ignore[arg-type]

    service._assert_library_paths_not_shared = counting_scan  # type: ignore[method-assign]
    result = await service.delete_chapter_files("manga-1", chapter_ids)

    assert result["deleted"] == 5
    assert result["errors"] == []
    assert scans == 1
    assert all(
        not database.get_chapter(chapter_id)["downloaded"] for chapter_id in chapter_ids
    )


@pytest.mark.asyncio
async def test_backfill_counts_pages_of_files_already_in_the_library(tmp_path: Path):
    """Books imported from disk arrive without a page count."""

    database, service, _komga = make_service(tmp_path)
    manga_data = manga()
    chapter_data = chapter("chapter-1", "1")
    output, _job = add_downloaded_chapter(
        database, tmp_path / "library", manga_data, chapter_data
    )
    with zipfile.ZipFile(output, "w") as archive:
        for index in range(3):
            archive.writestr(f"{index:03}.jpg", b"image")
    database.set_release_pages("chapter-1", 1)
    with sqlite3.connect(database.path) as connection:
        connection.execute("UPDATE chapter_release SET pages=NULL WHERE id='chapter-1'")
    assert database.get_chapter("chapter-1")["pages"] is None

    counted = await service.backfill_page_counts("manga-1")

    assert counted == 1
    assert database.get_chapter("chapter-1")["pages"] == 3
    assert await service.backfill_page_counts("manga-1") == 0


@pytest.mark.asyncio
async def test_organization_shifts_a_renumbered_run(tmp_path: Path):
    """Renumbering a source moves a whole run onto its own file names.

    Each move is legitimate because the file occupying the destination is
    itself moving away; the pass orders them so no file is ever overwritten."""

    database, service, _komga = make_service(tmp_path)
    manga_data = manga()
    library = tmp_path / "library"
    for index in (1, 2, 3):
        chapter_data = chapter(f"chapter-{index}", str(index))
        add_downloaded_chapter(database, library, manga_data, chapter_data)
    # The source renumbered: what was chapter 2 is chapter 1, and so on.
    for index in (1, 2, 3):
        database.upsert_chapters(
            "manga-1",
            [
                {
                    **chapter(f"chapter-{index}", str(index - 1)),
                    "id": f"chapter-{index}",
                }
            ],
        )

    report = await service.organize_library()

    assert report["warnings"] == []
    assert report["moved"] == 3
    names = sorted(path.name for path in library.rglob("*.cbz"))
    assert names == [
        "Example - v001 c000 [en].cbz",
        "Example - v001 c001 [en].cbz",
        "Example - v001 c002 [en].cbz",
    ]


@pytest.mark.asyncio
async def test_a_swap_is_reported_and_does_not_block_the_library(tmp_path: Path):
    """Two files that want each other's name need a temporary name. That is
    rare and worth a look; it must not leave the whole library unready."""

    database, service, _komga = make_service(tmp_path)
    manga_data = manga()
    library = tmp_path / "library"
    for index in (1, 2):
        add_downloaded_chapter(
            database, library, manga_data, chapter(f"chapter-{index}", str(index))
        )
    # Renumbered the other way round: 1 becomes 2 and 2 becomes 1.
    for index, number in ((1, "2"), (2, "1")):
        database.upsert_chapters(
            "manga-1",
            [{**chapter(f"chapter-{index}", number), "id": f"chapter-{index}"}],
        )

    report = await service.organize_library()

    assert report["organization_blocked"] is False
    assert any("swap" in warning for warning in report["warnings"])
    assert sorted(path.name for path in library.rglob("*.cbz")) == [
        "Example - v001 c001 [en].cbz",
        "Example - v001 c002 [en].cbz",
    ]


@pytest.mark.asyncio
async def test_monitor_removes_chapter_files_duplicated_by_an_owned_book_on_its_own(
    tmp_path: Path,
):
    """Library rule: the automation keeps the best copy only. A book proven to
    contain a chapter is that copy; the loose chapter file goes through the
    same quarantine as the button, without waiting for anyone."""

    from types import SimpleNamespace

    from tankarr.chapter_map import entries_from_releases
    from tankarr.monitor import ReleaseMonitor

    database, service, _komga = make_service(tmp_path)
    manga_data = manga()
    library = tmp_path / "library"
    volume_row = {**chapter("vol-1", "1"), "provider": "mangapill", "volume": None}
    database.upsert_manga(manga_data, "en", "none")
    database.upsert_chapters("manga-1", [volume_row])
    assert database.reclassify_provider_releases_as_volumes("manga-1", "mangapill") == 1
    volume_path = library / "Example (Unknown Author)" / "Example - v001 [en].cbz"
    volume_path.parent.mkdir(parents=True, exist_ok=True)
    volume_path.write_bytes(b"cbz")
    database.mark_chapter_downloaded("vol-1", volume_path)
    paths = {}
    for number in ("1", "2", "3"):
        row = {**chapter(f"c{number}", number), "volume": None}
        paths[number], _ = add_downloaded_chapter(database, library, manga_data, row)
    database.replace_chapter_map(
        "manga-1",
        "operator",
        entries_from_releases([{"volume": "1", "chapter": "1-2"}]),
    )
    assert database.manga_ids_with_books_and_chapters() == ["manga-1"]

    disabled = SimpleNamespace(
        settings=Settings(
            data_dir=tmp_path / "data",
            library_dir=library,
            duplicate_cleanup_enabled=False,
        ),
        database=database,
        service=service,
    )
    assert await ReleaseMonitor.retire_duplicate_chapter_files(disabled) == {
        "deleted": 0,
        "series": 0,
        "enabled": False,
    }
    assert paths["1"].exists()

    monitor = SimpleNamespace(
        settings=Settings(data_dir=tmp_path / "data", library_dir=library),
        database=database,
        service=service,
    )
    outcome = await ReleaseMonitor.retire_duplicate_chapter_files(monitor)

    assert outcome == {"deleted": 2, "series": 1, "enabled": True}
    assert not paths["1"].exists() and not paths["2"].exists()
    assert paths["3"].exists() and volume_path.exists()
    assert database.get_chapter("c3")["downloaded"] is True
    # Nothing left to do: the next cycle is a no-op, not a second deletion.
    assert await ReleaseMonitor.retire_duplicate_chapter_files(monitor) == {
        "deleted": 0,
        "series": 0,
        "enabled": True,
    }


def test_duplicate_keeper_prefers_the_measured_better_file_then_the_source_class(
    tmp_path: Path,
):
    _database, service, _komga = make_service(tmp_path)
    key = service._duplicate_keeper_key
    quality = {
        "wide": {"verdict": "ok", "median_width": 1600},
        "narrow": {"verdict": "ok", "median_width": 800},
        "broken": {"verdict": "degraded", "median_width": 2000},
    }
    wide = {
        "id": "wide",
        "provider": "suwayomi",
        "source_name": "Weeb Central",
        "pages": 18,
    }
    narrow = {
        "id": "narrow",
        "provider": "suwayomi",
        "source_name": "Asura Scans",
        "pages": 18,
    }
    broken = {
        "id": "broken",
        "provider": "suwayomi",
        "source_name": "Asura Scans",
        "pages": 18,
    }
    origin = {
        "id": "z-origin",
        "provider": "suwayomi",
        "source_name": "Asura Scans",
        "pages": 18,
    }
    aggregator = {
        "id": "a-agg",
        "provider": "mangapill",
        "source_name": "Mangapill",
        "pages": 18,
    }
    longer = {
        "id": "b-agg",
        "provider": "mangapill",
        "source_name": "Mangapill",
        "pages": 24,
    }

    ranked = sorted([narrow, broken, wide], key=lambda item: key(item, set(), quality))
    assert [item["id"] for item in ranked] == ["wide", "narrow", "broken"]
    # Unmeasured files fall back to the source class, then to the page count.
    ranked = sorted([aggregator, longer, origin], key=lambda item: key(item, set(), {}))
    assert [item["id"] for item in ranked] == ["z-origin", "b-agg", "a-agg"]
    # A demoted source loses whatever its measurements say.
    ranked = sorted([wide, narrow], key=lambda item: key(item, {""}, quality))
    assert ranked[0]["id"] == "wide"
    wide_demoted = {**wide, "source_key": "suwayomi:weeb"}
    ranked = sorted(
        [wide_demoted, narrow], key=lambda item: key(item, {"suwayomi:weeb"}, quality)
    )
    assert ranked[0]["id"] == "narrow"


def test_series_calendar_estimates_the_next_chapter_and_keeps_an_overdue_one(
    tmp_path: Path,
):
    from datetime import UTC, datetime, timedelta

    database, service, _komga = make_service(tmp_path)
    manga_data = {**manga(), "status": "ongoing"}
    database.upsert_manga(manga_data, "en", "none")
    today = datetime.now(UTC).date()
    last = today - timedelta(days=9)  # weekly work, the last slot was skipped
    history = [
        {
            "chapter": str(390 + index),
            "volume": None,
            "release_date": (last - timedelta(days=7 * (7 - index))).isoformat(),
        }
        for index in range(8)
    ]
    database.replace_release_history("manga-1", "mangaupdates", history)
    database.upsert_chapters(
        "manga-1",
        [{**chapter("c398", "398"), "provider": "suwayomi", "source_name": "Webtoons"}],
    )

    calendar = service.series_calendar("manga-1")

    assert calendar["source"] == "MangaUpdates release history"
    assert calendar["cadence"]["label"] == "weekly"
    assert calendar["recent"][0]["chapter"] == "397"
    first = calendar["expected"][0]
    assert first["chapter"] == "398"
    assert first["estimated"] is True
    assert first["overdue_days"] == 2
    assert first["available"] is True and first["official"] is True
    assert first["downloaded"] is False
    assert calendar["reason"] is None

    database.update_manga("manga-1", {"status_override": "hiatus"})
    paused = service.series_calendar("manga-1")
    assert paused["expected"] == [] and paused["reason"] == "paused"


def test_series_calendar_renumbers_a_catalogue_that_counts_differently(tmp_path: Path):
    """The World After the Fall: MangaUpdates logged chapter 250 on Sep 1
    while every source and the library stop at 235. The catalogue's dates
    carry the rhythm; the numbers come from the series."""

    from datetime import UTC, datetime, timedelta

    database, service, _komga = make_service(tmp_path)
    database.upsert_manga({**manga(), "status": "ongoing"}, "en", "none")
    today = datetime.now(UTC).date()
    last = today - timedelta(days=2)
    history = [
        {
            "chapter": str(243 + index),
            "volume": None,
            "release_date": (last - timedelta(days=7 * (7 - index))).isoformat(),
        }
        for index in range(8)
    ]
    database.replace_release_history("manga-1", "mangaupdates", history)
    database.upsert_chapters(
        "manga-1",
        [
            {
                **chapter(f"c{n}", str(n)),
                "provider": "suwayomi",
                "source_name": "Weeb Central",
            }
            for n in (233, 234, 235)
        ],
    )

    calendar = service.series_calendar("manga-1")

    assert calendar["cadence"]["renumbered_by"] == -15
    assert calendar["cadence"]["last_chapter"] == "235"
    assert calendar["expected"][0]["chapter"] == "236"
    assert calendar["recent"][0]["chapter"] == "235"


def test_series_calendar_lists_an_announced_official_date_before_the_estimates(
    tmp_path: Path,
):
    """The Beginning After the End: Tapas lists "🔒 252" a week early, dated.
    That date is announced, not estimated, and 252 is not release history."""

    from datetime import UTC, datetime, timedelta

    database, service, _komga = make_service(tmp_path)
    database.upsert_manga({**manga(), "status": "ongoing"}, "en", "none")
    database.save_series_metadata(
        "manga-1",
        {
            "official_links": [
                {"url": "https://tapas.io/series/tbate", "language": "en"}
            ]
        },
        artwork_path=None,
        artwork_sha256=None,
        artwork_media_type=None,
        source_status=[],
    )
    today = datetime.now(UTC).date()
    last = today - timedelta(days=2)
    rows = []
    for index in range(8):
        number = 245 + index
        released = last - timedelta(days=7 * (7 - index))
        rows.append(
            {
                **chapter(f"tapas-{number}", str(number)),
                "provider": "suwayomi",
                "source_name": "Tapas (EN)",
                "source_url": f"https://tapas.io/episode/{number}",
                "publish_at": f"{released.isoformat()}T16:00:00+00:00",
            }
        )
    announced = last + timedelta(days=7)
    rows.append(
        {
            **chapter("tapas-253", "253"),
            "title": "\U0001f512 253. Turning Point",
            "provider": "suwayomi",
            "source_name": "Tapas (EN)",
            "source_url": "https://tapas.io/episode/253",
            "publish_at": f"{announced.isoformat()}T16:00:00+00:00",
        }
    )
    database.upsert_chapters("manga-1", rows)

    calendar = service.series_calendar("manga-1")

    assert calendar["source"] == "official platform release dates"
    assert calendar["recent"][0] == {"chapter": "252", "released_at": last.isoformat()}
    first, second = calendar["expected"][:2]
    assert first["chapter"] == "253"
    assert first["expected_at"] == announced.isoformat()
    assert first["estimated"] is False
    assert first["official"] is True and first["available"] is False
    assert second["chapter"] == "254" and second["estimated"] is True
    assert second["expected_at"] == (announced + timedelta(days=7)).isoformat()
    assert calendar["cadence"]["last_chapter"] == "252"
    assert calendar["cadence"]["last_release_at"] == last.isoformat()


@pytest.mark.asyncio
async def test_monitor_announces_a_work_entering_and_leaving_hiatus(tmp_path: Path):
    from types import SimpleNamespace

    from tankarr.monitor import ReleaseMonitor

    database, service, _komga = make_service(tmp_path)
    database.upsert_manga({**manga(), "status": "ongoing"}, "en", "none")
    sent: list[tuple[str, str]] = []

    class Notifier:
        configured = True

        async def send(self, title, message, **_kwargs):
            sent.append((title, message))
            return True

    service.notifier = Notifier()
    monitor = SimpleNamespace(database=database, service=service)

    # First cycle: the work is running; nothing to announce.
    assert await ReleaseMonitor.track_publication_pauses(monitor) == {
        "entered": [],
        "left": [],
    }
    assert sent == []

    with database.connect() as connection:
        connection.execute(
            "INSERT INTO metadata_source_record (manga_id, entity_type, entity_key, source, external_id, "
            "match_confidence, match_reason, data_json, raw_json, fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                "manga-1",
                "work",
                "",
                "myanimelist",
                "28",
                1.0,
                "test",
                '{"status": "hiatus"}',
                "{}",
                "2026-09-05T00:00:00+00:00",
            ),
        )
    outcome = await ReleaseMonitor.track_publication_pauses(monitor)
    assert outcome["entered"] == ["Example"] and sent[-1][0] == "Hiatus"
    assert "myanimelist" in sent[-1][1]
    # Unchanged: silent.
    assert await ReleaseMonitor.track_publication_pauses(monitor) == {
        "entered": [],
        "left": [],
    }
    assert len(sent) == 1

    with database.connect() as connection:
        connection.execute(
            'UPDATE metadata_source_record SET data_json=\'{"status": "ongoing"}\''
        )
    outcome = await ReleaseMonitor.track_publication_pauses(monitor)
    assert outcome["left"] == ["Example"] and sent[-1][0] == "Back from hiatus"


@pytest.mark.asyncio
async def test_unmapping_a_source_forgets_its_rows_even_when_it_lists_nothing(
    tmp_path: Path,
):
    """A dead or uninstalled source lists nothing, so forgetting "what it
    lists now" kept 30 000 stale rows. Forgetting goes by the source."""

    database, service, _komga = make_service(tmp_path)
    manga_data = manga()
    database.upsert_manga(manga_data, "en", "none")
    rows = [
        {
            **chapter(f"nd-{n}", str(n)),
            "provider": "suwayomi",
            "source_name": "Niadd (EN)",
            "source_key": "suwayomi:niadd",
        }
        for n in range(1, 21)
    ]
    kept = {
        **chapter("wc-1", "1"),
        "provider": "suwayomi",
        "source_name": "Weeb Central (EN)",
        "source_key": "suwayomi:wc",
    }
    database.upsert_chapters("manga-1", rows + [kept])
    database.upsert_release_source(
        "manga-1",
        provider="suwayomi",
        provider_manga_id="niadd-42",
        title="Example",
        source_url=None,
        source_name="Niadd (EN)",
        language="en",
        match_confidence=1.0,
        match_reason="test",
        verified_by="test",
    )
    # One Niadd row already delivered a file: it stays, it is the library's.
    add_downloaded_chapter(database, tmp_path / "library", manga_data, {**rows[0]})

    class Silent:
        name = "suwayomi"

        async def list_chapters(self, manga_id, language):
            return []  # the source is gone

    service.providers["suwayomi"] = Silent()
    result = await service.remove_release_source("manga-1", "suwayomi", "niadd-42")

    assert result["releases_forgotten"] == 19
    remaining = {row["id"] for row in database.list_all_chapters("manga-1")}
    assert remaining == {"nd-1", "wc-1"}
    assert database.release_source_rejections("manga-1") == {
        ("suwayomi", "niadd-42"): "unmapped by the operator"
    }


@pytest.mark.asyncio
async def test_a_pack_imported_as_one_volume_does_not_make_chapters_duplicates(
    tmp_path: Path,
):
    """An oversized book cannot prove duplicates, including in manual previews."""

    from types import SimpleNamespace

    from tankarr.chapter_map import entries_from_releases
    from tankarr.monitor import ReleaseMonitor

    database, service, _komga = make_service(tmp_path)
    manga_data = manga()
    library = tmp_path / "library"
    volume_row = {
        **chapter("vol-1", "1"),
        "provider": "mangapill",
        "volume": None,
        "pages": 1456,
    }
    database.upsert_manga(manga_data, "en", "none")
    database.upsert_chapters("manga-1", [volume_row])
    assert database.reclassify_provider_releases_as_volumes("manga-1", "mangapill") == 1
    volume_path = library / "Example (Unknown Author)" / "Example - v001 [en].cbz"
    volume_path.parent.mkdir(parents=True, exist_ok=True)
    volume_path.write_bytes(b"cbz")
    database.mark_chapter_downloaded("vol-1", volume_path)
    paths = {}
    for number in ("1", "2"):
        row = {**chapter(f"c{number}", number), "volume": None}
        paths[number], _ = add_downloaded_chapter(database, library, manga_data, row)
    database.replace_chapter_map(
        "manga-1",
        "mangaupdates",
        entries_from_releases([{"volume": "1", "chapter": "1-2"}]),
    )
    assert service.duplicate_chapter_files("manga-1") == []
    assert service.suspect_covering_volumes("manga-1") == {
        "1": "1456 pages: not verified as one book"
    }

    monitor = SimpleNamespace(
        settings=Settings(data_dir=tmp_path / "data", library_dir=library),
        database=database,
        service=service,
    )
    outcome = await ReleaseMonitor.retire_duplicate_chapter_files(monitor)

    assert outcome == {"deleted": 0, "series": 0, "enabled": True}
    assert paths["1"].exists() and paths["2"].exists()


@pytest.mark.asyncio
async def test_once_every_book_is_on_disk_the_loose_chapter_files_are_retired(
    tmp_path: Path,
):
    """Billy Bat, 2026-09-05: twenty books and 147 chapter files side by
    side in the reader. The books are the edition; the chapters go."""

    database, service, _komga = make_service(tmp_path)
    manga_data = manga()
    library = tmp_path / "library"
    database.upsert_manga(manga_data, "en", "none")
    database.update_manga("manga-1", {"series_unit_override": "volumes"})
    with database.connect() as connection:
        connection.execute(
            "INSERT OR REPLACE INTO series_metadata (manga_id, data_json, source_status_json, last_enriched_at) "
            "VALUES (?, ?, '[]', '2026-09-05T00:00:00+00:00')",
            ("manga-1", '{"volume_count": 2, "chapter_count": 4}'),
        )
    paths = {}
    for volume in ("1", "2"):
        row = {**chapter(f"v{volume}", volume), "provider": "mangapill", "volume": None}
        database.upsert_chapters("manga-1", [row])
    assert database.reclassify_provider_releases_as_volumes("manga-1", "mangapill") == 2
    for volume in ("1", "2"):
        path = library / "Example (Unknown Author)" / f"Example - v00{volume} [en].cbz"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"cbz")
        database.mark_chapter_downloaded(f"v{volume}", path)
    for number in ("1", "2", "3", "4"):
        paths[number], _ = add_downloaded_chapter(
            database,
            library,
            manga_data,
            {**chapter(f"c{number}", number), "volume": None},
        )

    outcome = await service.retire_redundant_chapter_files("manga-1")

    assert outcome["deleted"] == 4 and outcome["errors"] == []
    assert not any(path.exists() for path in paths.values())
    assert (library / "Example (Unknown Author)" / "Example - v001 [en].cbz").exists()


def test_reference_guard_avoids_source_inventories_and_refreshes_filesystem(
    tmp_path, monkeypatch
):
    database, service, _ = make_service(tmp_path)
    source = manga()
    owned = chapter("owned", "1")
    path, _ = add_downloaded_chapter(database, tmp_path / "library", source, owned)
    target = path.with_name("incoming.cbz")

    def unrelated_inventory(*args, **kwargs):
        raise AssertionError("Safety checks must not rebuild source inventories")

    monkeypatch.setattr(database, "list_manga", unrelated_inventory)
    monkeypatch.setattr(database, "list_all_chapters", unrelated_inventory)
    service._assert_library_paths_not_shared("incoming-series", set(), [target])
    path.unlink()
    path.symlink_to(target)
    with pytest.raises(UnsafeLibraryPath, match="symlinked"):
        service._assert_library_paths_not_shared("incoming-series", set(), [target])


def test_file_reference_snapshot_confines_aliases_and_parent_components(tmp_path):
    from tankarr.service import _FileReferenceSnapshot

    root = tmp_path / "library"
    root.mkdir()
    snapshot = _FileReferenceSnapshot(root, [Path("/old-library")])
    assert snapshot.file("/old-library/Series/book.cbz") == root / "Series/book.cbz"
    with pytest.raises(UnsafeLibraryPath, match="outside"):
        snapshot.file(str(root) + "-other/book.cbz")
    with pytest.raises(UnsafeLibraryPath, match="outside"):
        snapshot.file(root / "../outside.cbz")
    (root / "linked").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(UnsafeLibraryPath, match="symlinked"):
        _FileReferenceSnapshot(root, []).file(root / "linked/book.cbz")
    (root / "directory.cbz").mkdir()
    with pytest.raises(UnsafeLibraryPath, match="non-file"):
        _FileReferenceSnapshot(root, []).file(root / "directory.cbz")


def _pages_cbz(path: Path, seeds: list[int]) -> Path:
    """A readable CBZ whose pages are distinct, deterministic drawings."""

    import io
    import random

    from PIL import Image, ImageDraw

    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        for index, seed in enumerate(seeds, start=1):
            rng = random.Random(seed)
            image = Image.new("L", (300, 450), 255)
            draw = ImageDraw.Draw(image)
            for _ in range(10):
                x0, y0 = rng.randrange(240), rng.randrange(380)
                draw.rectangle(
                    (x0, y0, x0 + rng.randrange(30, 120), y0 + rng.randrange(30, 120)),
                    fill=rng.randrange(0, 120),
                )
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
            archive.writestr(f"{index:03d}.png", buffer.getvalue())
    return path


@pytest.mark.asyncio
async def test_pages_not_inside_the_book_veto_automatic_retirement(tmp_path: Path):
    """Nana, 2026-09-17: the map said book 21 held 78–80 and the source
    tagged 81–84 "Vol.21"; the book's pages held none of 81–84. A map is a
    claim; the pages decide what the automation may delete."""

    from types import SimpleNamespace

    from tankarr.chapter_map import entries_from_releases
    from tankarr.monitor import ReleaseMonitor

    database, service, _komga = make_service(tmp_path)
    manga_data = manga()
    library = tmp_path / "library"
    volume_row = {**chapter("vol-1", "1"), "provider": "mangapill", "volume": None}
    database.upsert_manga(manga_data, "en", "none")
    database.upsert_chapters("manga-1", [volume_row])
    assert database.reclassify_provider_releases_as_volumes("manga-1", "mangapill") == 1
    volume_path = library / "Example (Unknown Author)" / "Example - v001 [en].cbz"
    _pages_cbz(volume_path, list(range(1, 31)))
    database.mark_chapter_downloaded("vol-1", volume_path)
    paths = {}
    for number in ("1", "2"):
        row = {**chapter(f"c{number}", number), "volume": None}
        paths[number], _ = add_downloaded_chapter(database, library, manga_data, row)
    # Chapter 1 really is in the book (its pages are the book's 5–12);
    # chapter 2 is another work's pages filed under this series.
    _pages_cbz(paths["1"], list(range(5, 13)))
    _pages_cbz(paths["2"], list(range(900, 908)))
    database.replace_chapter_map(
        "manga-1",
        "operator",
        entries_from_releases([{"volume": "1", "chapter": "1-2"}]),
    )
    assert [
        c["duplicate_of_volume"] for c in service.duplicate_chapter_files("manga-1")
    ] == ["1", "1"]

    monitor = SimpleNamespace(
        settings=Settings(data_dir=tmp_path / "data", library_dir=library),
        database=database,
        service=service,
    )
    outcome = await ReleaseMonitor.retire_duplicate_chapter_files(monitor)

    assert outcome == {"deleted": 1, "series": 1, "enabled": True}
    assert not paths["1"].exists()
    assert paths["2"].exists() and volume_path.exists()
    verdicts = database.content_alignment("manga-1")
    assert verdicts["c2"]["verdict"] == "outside"
    assert verdicts["c1"]["verdict"] == "inside" and verdicts["c1"]["volume"] == "1"
    # Verdicts are kept with the files' content keys: the next pass reads
    # nothing again while neither the chapter nor the book changed.
    assert service.align_chapter_contents("manga-1")["checked"] == 0

    # "Every book is on the shelf" does not make the foreign file redundant.
    outcome = await service.retire_redundant_chapter_files("manga-1")
    assert outcome["deleted"] == 0
    assert paths["2"].exists()
