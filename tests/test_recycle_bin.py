from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tankarr.service import TankarrService
from tests.test_deletion import (
    UnusedProvider,
    add_downloaded_chapter,
    chapter,
    make_service,
    manga,
)


def seed(tmp_path: Path):
    database, service, reader = make_service(tmp_path)
    output, _ = add_downloaded_chapter(
        database, service.settings.library_dir, manga(), chapter("chapter-1", "1")
    )
    return database, service, reader, output


@pytest.mark.asyncio
async def test_retirement_survives_reader_ack_and_restart(tmp_path: Path):
    database, service, reader, output = seed(tmp_path)
    outcome = await service.delete_chapter_files("manga-1", ["chapter-1"], recycle=True)
    assert outcome["deleted"] == 1
    assert outcome["errors"] == []
    assert not output.exists()
    assert database.list_deletion_operations() == []
    directory = next(service.settings.library_dir.glob(".tankarr-delete-*"))
    manifest_before = (directory / "manifest.json").read_bytes()
    assert next(directory.glob("*.quarantined")).read_bytes() == b"cbz"
    assert json.loads(manifest_before)["disposition"] == "retain"
    restarted = TankarrService(service.settings, database, UnusedProvider(), reader)
    recovery = await restarted.recover_file_quarantines()
    assert recovery["recovery_blocked"] is False
    assert recovery["purged"] == 0
    assert recovery["scan_required"] is False
    assert reader.calls == 1
    assert (directory / "manifest.json").read_bytes() == manifest_before


@pytest.mark.asyncio
@pytest.mark.parametrize("db_committed", [False, True])
async def test_recovery_respects_database_commit_before_retirement_manifest(
    tmp_path: Path, db_committed: bool
):
    database, service, reader, output = seed(tmp_path)
    quarantine = service._stage_library_files([output], disposition="retain")
    assert (
        database.get_deletion_operation(quarantine.operation_id)["disposition"]
        == "retain"
    )
    if db_committed:
        database.reset_chapter_file("manga-1", "chapter-1", quarantine.operation_id)
    restarted = TankarrService(service.settings, database, UnusedProvider(), reader)
    recovery = await restarted.recover_file_quarantines()
    assert recovery["recovery_blocked"] is False
    assert output.exists() is not db_committed
    assert database.get_chapter("chapter-1")["downloaded"] is not db_committed
    if db_committed:
        manifest = json.loads(quarantine.manifest_path.read_text())
        assert manifest["state"] == "committed"
        assert manifest["retired_at"] is not None
        assert quarantine.moved[0][1].read_bytes() == b"cbz"
    else:
        assert not quarantine.directory.exists()


@pytest.mark.asyncio
async def test_expiry_preserves_pending_reader_receipt(tmp_path: Path):
    database, service, reader, output = seed(tmp_path)
    quarantine = service._stage_library_files([output], disposition="retain")
    database.reset_chapter_file("manga-1", "chapter-1", quarantine.operation_id)
    committed = service._commit_file_quarantine(quarantine)
    assert committed["files_retired"] == 1
    assert committed["files_deleted"] == 0
    retired = datetime.fromisoformat(quarantine.retired_at)
    early = await service.purge_recycle_bin(now=retired + timedelta(days=7, seconds=-1))
    assert early == {"files_deleted": 0, "purged": 0, "errors": []}
    expired = await service.purge_recycle_bin(now=retired + timedelta(days=7))
    assert expired == {"files_deleted": 1, "purged": 1, "errors": []}
    assert not quarantine.directory.exists()
    assert (
        database.get_deletion_operation(quarantine.operation_id)["state"] == "committed"
    )
    assert reader.calls == 0


@pytest.mark.asyncio
async def test_purge_failure_is_retryable_without_blocking_library(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    database, service, _reader, output = seed(tmp_path)
    quarantine = service._stage_library_files([output], disposition="retain")
    database.reset_chapter_file("manga-1", "chapter-1", quarantine.operation_id)
    service._commit_file_quarantine(quarantine)
    due = datetime.fromisoformat(quarantine.retired_at) + timedelta(days=8)
    original_unlink = Path.unlink

    def fail_staged(path, *args, **kwargs):
        if path.suffix == ".quarantined":
            raise OSError("Simulated recycle bin failure")
        return original_unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", fail_staged)
        failed = await service.purge_recycle_bin(now=due)
    assert failed["errors"]
    service.assert_mutations_allowed()
    assert quarantine.moved[0][1].exists()
    result = await service.purge_recycle_bin(now=due)
    assert result == {"files_deleted": 1, "purged": 1, "errors": []}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tamper", ["symlink", "unexpected", "timestamp", "disposition", "ambiguous"]
)
async def test_purge_preserves_unproven_or_unsafe_entries(tmp_path: Path, tamper: str):
    database, service, _reader, output = seed(tmp_path)
    quarantine = service._stage_library_files([output], disposition="retain")
    database.reset_chapter_file("manga-1", "chapter-1", quarantine.operation_id)
    service._commit_file_quarantine(quarantine)
    staged = quarantine.moved[0][1]
    external = tmp_path / "external.cbz"
    external.write_bytes(b"keep")
    if tamper == "symlink":
        staged.unlink()
        staged.symlink_to(external)
    elif tamper == "unexpected":
        (quarantine.directory / "keep.txt").write_text("keep")
    elif tamper == "ambiguous":
        (quarantine.directory / "AMBIGUOUS").write_text("unknown commit")
    else:
        payload = json.loads(quarantine.manifest_path.read_text())
        payload["retired_at" if tamper == "timestamp" else "disposition"] = "invalid"
        quarantine.manifest_path.write_text(json.dumps(payload))
    result = await service.purge_recycle_bin(now=datetime.now(UTC) + timedelta(days=8))
    assert result["files_deleted"] == 0
    assert quarantine.directory.exists()
    assert external.read_bytes() == b"keep"
    assert staged.exists()


@pytest.mark.asyncio
async def test_prepared_retirement_and_legacy_deletion_are_not_expired(tmp_path: Path):
    _database, service, _reader, output = seed(tmp_path)
    quarantine = service._stage_library_files([output], disposition="retain")
    result = await service.purge_recycle_bin(
        now=datetime.now(UTC) + timedelta(days=800)
    )
    assert result["files_deleted"] == 0
    assert quarantine.moved[0][1].exists()
    service._rollback_file_quarantine(quarantine)
    legacy = await service.delete_chapter_files("manga-1", ["chapter-1"])
    assert legacy["deleted"] == 1
    assert list(service.settings.library_dir.glob(".tankarr-delete-*")) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["recovery", "purge"])
async def test_temporary_manifest_symlink_never_overwrites_external_file(
    tmp_path: Path, phase: str
):
    database, service, _reader, output = seed(tmp_path)
    quarantine = service._stage_library_files([output], disposition="retain")
    database.reset_chapter_file("manga-1", "chapter-1", quarantine.operation_id)
    if phase == "purge":
        service._commit_file_quarantine(quarantine)
    sentinel = tmp_path / "outside.txt"
    sentinel.write_text("keep")
    (quarantine.directory / "manifest.json.tmp").symlink_to(sentinel)
    if phase == "purge":
        result = await service.purge_recycle_bin(
            now=datetime.now(UTC) + timedelta(days=8)
        )
        assert result["errors"]
    else:
        result = await service.recover_file_quarantines()
        assert result["recovery_blocked"]
    assert sentinel.read_text() == "keep"
    assert quarantine.moved[0][1].exists()


def test_retirement_does_not_acknowledge_a_replaced_staged_file(tmp_path: Path):
    database, service, _reader, output = seed(tmp_path)
    quarantine = service._stage_library_files([output], disposition="retain")
    database.reset_chapter_file("manga-1", "chapter-1", quarantine.operation_id)
    sentinel = tmp_path / "outside.cbz"
    sentinel.write_bytes(b"keep")
    staged = quarantine.moved[0][1]
    staged.unlink()
    staged.symlink_to(sentinel)
    outcome = service._commit_file_quarantine(quarantine)
    assert outcome["cleanup_errors"]
    assert outcome["files_deleted"] == 0
    assert "files_retired" not in outcome
    assert database.get_deletion_operation(quarantine.operation_id) is not None
    assert sentinel.read_bytes() == b"keep"
