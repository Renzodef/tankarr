from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path

import pytest

from tankarr.archive import package_cbz_validated, sha256
from tankarr.naming import final_library_path
from tankarr.service import (
    ExternalImportConflict,
    TankarrService,
    local_page_content_sha256,
)
from tests.test_assembly_provenance import assembled
from tests.test_deletion import (
    PageProvider,
    UnusedProvider,
    chapter,
    make_service,
    manga,
)


class ReplacementReader:
    configured = True
    standalone = False

    def __init__(self):
        self.synced = []
        self.purged = []

    async def sync_imported_path(self, path):
        self.synced.append(path)
        return {"configured": True, "triggered": True}

    async def reconcile_deleted(self, paths, *, safety_check):
        self.purged.append(list(paths))
        safety_check()
        return {"configured": True, "triggered": True, "purged": True}

    async def scan(self, _paths=()):
        return {"configured": True, "triggered": True}


def archive(tmp_path, release, contents):
    directory = tmp_path / contents
    directory.mkdir()
    page = directory / "0001.jpg"
    page.write_bytes(contents.encode())
    output, ledger = package_cbz_validated(
        directory / "book.cbz", [page], manga(), release
    )
    return output, ledger["sha256"], local_page_content_sha256([page])


@pytest.fixture
def upgrade(tmp_path):
    database, service, _ = make_service(tmp_path)
    database.upsert_manga(manga(), "en", "all")
    reader = ReplacementReader()
    service.komga = reader
    old = assembled()
    old["pages"] = 1
    staged, old_hash, content_hash = archive(tmp_path, old, "assembled-pages")
    canonical = final_library_path(service.settings.library_dir, manga(), old)
    canonical.parent.mkdir(parents=True, exist_ok=True)
    canonical.write_bytes(staged.read_bytes())
    database.publish_external_chapter("manga-1", old, canonical, old_hash, content_hash)
    replacement = {
        **chapter("external-book", ""),
        "chapter": None,
        "provider": "prowlarr",
        "release_unit": "volume",
        "pages": 1,
    }
    incoming = archive(tmp_path, replacement, "real-book-pages")
    return database, service, reader, canonical, old_hash, replacement, incoming


def retained(service):
    return list(service.settings.library_dir.glob(".tankarr-delete-*/*.quarantined"))


async def test_real_book_replaces_assembly_and_syncs_occupied_canonical(upgrade):
    database, service, reader, canonical, old_hash, replacement, incoming = upgrade
    result = await service.publish_external_import("manga-1", replacement, *incoming)

    current = database.get_chapter("assembled-1")
    assert current["provider"] == "prowlarr"
    assert current["assembled_from"] is None
    assert current["library_sha256"] == sha256(canonical) == incoming[1]
    assert current["library_path"] == result["path"] == str(canonical)
    assert result["assembly_retirement"]["files_retired"] == 1
    assert len(retained(service)) == 1
    assert sha256(retained(service)[0]) == old_hash
    assert reader.synced == [
        canonical.relative_to(service.settings.library_dir).as_posix()
    ]
    assert reader.purged == []
    assert database.list_deletion_operations() == []

    restarted = TankarrService(service.settings, database, UnusedProvider(), reader)
    recovered = await restarted.recover_file_quarantines()
    assert recovered["recovery_blocked"] is False
    assert sha256(canonical) == incoming[1]
    assert sha256(retained(service)[0]) == old_hash


async def test_ordinary_external_content_collision_stays_rejected(upgrade, tmp_path):
    database, service, _reader, canonical, _old_hash, replacement, incoming = upgrade
    await service.publish_external_import("manga-1", replacement, *incoming)
    other = archive(tmp_path, replacement, "different-external-pages")
    with pytest.raises(ExternalImportConflict, match="different external book"):
        await service.publish_external_import("manga-1", replacement, *other)
    assert sha256(canonical) == incoming[1]
    assert database.get_chapter("assembled-1")["provider"] == "prowlarr"


async def test_deleted_external_book_can_be_replaced_from_another_provider(
    upgrade, tmp_path
):
    database, service, _reader, canonical, _old_hash, replacement, incoming = upgrade
    await service.publish_external_import("manga-1", replacement, *incoming)
    canonical.unlink()
    database.reset_chapter_file("manga-1", "assembled-1")
    replacement = {
        **replacement,
        "id": "archive-book",
        "provider": "internetarchive",
    }
    incoming = archive(tmp_path, replacement, "archive-pages")

    result = await service.publish_external_import("manga-1", replacement, *incoming)

    current = database.get_chapter("assembled-1")
    assert current["provider"] == "internetarchive"
    assert current["downloaded"] is True
    assert current["library_path"] == result["path"] == str(canonical)
    assert sha256(canonical) == incoming[1]


async def test_assembly_never_replaces_an_external_book(upgrade, tmp_path):
    database, service, _reader, canonical, _old_hash, replacement, incoming = upgrade
    await service.publish_external_import("manga-1", replacement, *incoming)
    new_assembly = assembled()
    prepared = archive(tmp_path, new_assembly, "rebuilt-pages")
    with pytest.raises(ExternalImportConflict, match="different external book"):
        await service.publish_external_import("manga-1", new_assembly, *prepared)
    assert sha256(canonical) == incoming[1]
    assert database.get_chapter("assembled-1")["provider"] == "prowlarr"


@pytest.mark.parametrize("provider", ["assembled", "prowlarr"])
async def test_missing_retired_assembly_does_not_block_new_contents(
    upgrade, tmp_path, provider
):
    database, service, _reader, canonical, _old_hash, replacement, incoming = upgrade
    canonical.unlink()
    database.reset_chapter_file("manga-1", "assembled-1")
    if provider == "assembled":
        replacement = assembled()
        incoming = archive(tmp_path, replacement, "rebuilt-pages")
    result = await service.publish_external_import("manga-1", replacement, *incoming)
    assert result["chapter"]["provider"] == provider
    assert sha256(canonical) == incoming[1]
    assert database.list_deletion_operations() == []


async def test_database_publication_failure_restores_original(upgrade, monkeypatch):
    database, service, _reader, canonical, old_hash, replacement, incoming = upgrade
    before = database.get_chapter("assembled-1")
    publish = database.publish_external_chapter

    def fail_publication(*args, **kwargs):
        publish(*args, **kwargs)
        raise OSError("publication interrupted")

    monkeypatch.setattr(database, "publish_external_chapter", fail_publication)
    with pytest.raises(OSError, match="publication interrupted"):
        await service.publish_external_import("manga-1", replacement, *incoming)
    assert database.get_chapter("assembled-1") == before
    assert sha256(canonical) == old_hash
    assert retained(service) == []
    assert database.list_deletion_operations() == []
    assert list(canonical.parent.glob("tankarr-replacement-*")) == []


async def test_committed_database_exception_keeps_replacement_for_recovery(
    upgrade, monkeypatch
):
    database, service, reader, canonical, old_hash, replacement, incoming = upgrade
    transaction = database.write_snapshot

    @contextmanager
    def commit_then_fail():
        with transaction():
            yield
        raise OSError("commit completed but caller lost acknowledgement")

    monkeypatch.setattr(database, "write_snapshot", commit_then_fail)
    with pytest.raises(RuntimeError, match="committed but its caller failed"):
        await service.publish_external_import("manga-1", replacement, *incoming)
    published = database.get_chapter("assembled-1")
    assert published["provider"] == "prowlarr"
    assert sha256(Path(published["library_path"])) == incoming[1]
    assert sha256(retained(service)[0]) == old_hash
    assert not canonical.exists()
    monkeypatch.undo()
    restarted = TankarrService(service.settings, database, UnusedProvider(), reader)
    result = await restarted.recover_file_quarantines()
    assert result["recovery_blocked"] is False
    assert database.list_deletion_operations() == []
    assert sha256(canonical) == incoming[1]


async def test_changed_old_assembly_does_not_authorize_retirement(upgrade):
    database, service, reader, canonical, _old_hash, replacement, incoming = upgrade
    before = database.get_chapter("assembled-1")
    canonical.write_bytes(b"changed assembly")
    with pytest.raises(ExternalImportConflict, match="differs from its hash ledger"):
        await service.publish_external_import("manga-1", replacement, *incoming)
    assert database.get_chapter("assembled-1") == before
    assert canonical.read_bytes() == b"changed assembly"
    assert database.list_deletion_operations() == []
    assert retained(service) == reader.synced == reader.purged == []


@pytest.mark.parametrize("boundary", ["retention_manifest", "canonical_rename"])
async def test_restart_finishes_committed_replacement_before_reader_ack(
    upgrade, monkeypatch, boundary
):
    database, service, reader, canonical, old_hash, replacement, incoming = upgrade

    def crash(*_args, **_kwargs):
        raise OSError("simulated process interruption")

    monkeypatch.setattr(
        service,
        "_commit_file_quarantine"
        if boundary == "retention_manifest"
        else "_organize_imported_chapter_locked",
        crash,
    )
    with pytest.raises(OSError, match="simulated process interruption"):
        await service.publish_external_import("manga-1", replacement, *incoming)
    published = database.get_chapter("assembled-1")
    assert published["provider"] == "prowlarr"
    assert sha256(Path(published["library_path"])) == incoming[1]
    assert not canonical.exists()
    operation = database.list_deletion_operations()[0]
    assert operation["state"] == "committed"
    assert operation["replacement_chapter_id"] == "assembled-1"
    assert operation["replacement_sha256"] == incoming[1]

    restarted = TankarrService(service.settings, database, UnusedProvider(), reader)
    recovered = await restarted.recover_file_quarantines()
    assert recovered["recovery_blocked"] is False
    assert recovered["komga_scan"]["pending_operations"] == 0
    assert database.list_deletion_operations() == []
    assert database.get_chapter("assembled-1")["library_path"] == str(canonical)
    assert sha256(canonical) == incoming[1]
    assert sha256(retained(service)[0]) == old_hash
    manifest = json.loads((retained(service)[0].parent / "manifest.json").read_text())
    assert manifest["state"] == "committed"
    assert reader.synced
    assert reader.purged == []
    again = TankarrService(service.settings, database, UnusedProvider(), reader)
    assert (await again.recover_file_quarantines())["rolled_back"] == 0
    assert sha256(canonical) == incoming[1]


@pytest.mark.parametrize("tamper", ["file", "ledger"])
async def test_changed_replacement_proof_keeps_receipt_and_old_book(upgrade, tamper):
    database, service, reader, canonical, old_hash, replacement, incoming = upgrade
    service._publish_external_import_locked("manga-1", replacement, *incoming)
    if tamper == "file":
        canonical.write_bytes(b"unexpected book")
    else:
        with database.connect() as connection:
            connection.execute(
                "UPDATE chapter_release SET library_sha256=? WHERE id='assembled-1'",
                ("f" * 64,),
            )
    result = await service.retry_pending_komga_reconciliation()
    assert result["pending_operations"] == 1
    assert "Replacement book" in result["error"]
    assert len(database.list_deletion_operations()) == 1
    assert sha256(retained(service)[0]) == old_hash
    assert reader.synced == reader.purged == []


@pytest.mark.parametrize("volume,expected", [("1", "assembled-1"), ("2", None)])
async def test_manual_provider_volume_replacement_names_assembled_predecessor(
    upgrade, volume, expected
):
    database, service, _reader, _canonical, _old_hash, replacement, _incoming = upgrade
    release = {**replacement, "provider": "suwayomi", "volume": volume}
    database.upsert_chapters("manga-1", [release])
    database.set_release_book("external-book", volume)
    job = await service.create_manual_download_job("external-book", replace=True)
    assert job["supersedes_chapter_id"] == expected


async def test_provider_download_upgrade_retains_assembly_and_recovers_receipt(upgrade):
    database, service, reader, canonical, old_hash, replacement, _incoming = upgrade
    database.upsert_chapters("manga-1", [{**replacement, "provider": "suwayomi"}])
    database.set_release_book("external-book", "1")
    service.providers["suwayomi"] = PageProvider()
    job = await service.create_manual_download_job("external-book", replace=True)
    await service.process_download_job(job["id"])
    completed = database.get_job(job["id"])
    assert completed["status"] == "completed", completed["message"]
    assert database.get_chapter("assembled-1")["downloaded"] is False
    replacement = database.get_chapter("external-book")
    assert replacement["library_path"] == str(canonical)
    assert sha256(canonical) == replacement["library_sha256"] != old_hash
    assert sha256(retained(service)[0]) == old_hash
    operation = database.list_deletion_operations()[0]
    assert operation["replacement_chapter_id"] == "external-book"
    restarted = TankarrService(service.settings, database, UnusedProvider(), reader)
    recovered = await restarted.recover_file_quarantines()
    assert recovered["recovery_blocked"] is False
    assert database.list_deletion_operations() == []
    assert sha256(retained(service)[0]) == old_hash
    assert reader.purged == []
