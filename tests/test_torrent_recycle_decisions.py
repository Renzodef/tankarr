from pathlib import Path

import pytest

from tankarr.database import Database
from tankarr.torrents import TorrentManager
from tests.test_torrents import (
    FakeMetadata,
    FakeProwlarr,
    FakeQBitTorrent,
    FakeService,
    SuccessfulImporter,
    release,
    seed,
)


def context(tmp_path):
    database = Database(tmp_path / "db.sqlite3")
    database.initialize()
    seed(database)
    job = database.create_torrent_download("kamui", release())
    content = tmp_path / "download"
    content.mkdir()
    (content / "v1.cbz").write_bytes(b"retained bytes")
    qbit = FakeQBitTorrent(content)
    manager = TorrentManager(
        database,
        FakeService(tmp_path),
        SuccessfulImporter(),
        FakeMetadata(),
        FakeProwlarr(),
        qbit,
    )
    return database, manager, qbit, job, content


@pytest.mark.asyncio
async def test_client_failure_preserves_payload_and_next_poll_retries(
    tmp_path, monkeypatch
):
    database, manager, qbit, job, content = context(tmp_path)
    original = qbit.delete_torrent

    async def unavailable(*args, **kwargs):
        raise OSError("client temporarily unavailable")

    monkeypatch.setattr(qbit, "delete_torrent", unavailable)
    failed = await manager._refuse_import(job, content, "No useful books")
    assert failed["qbit_state"] == "recycle_pending"
    assert (content / "v1.cbz").read_bytes() == b"retained bytes"
    monkeypatch.setattr(qbit, "delete_torrent", original)
    await manager.poll_once()
    completed = database.get_torrent_download(job["id"])
    assert completed["qbit_state"] == "removed"
    retired = Path(completed["language_evidence"]["retired_payload"])
    assert (retired / "v1.cbz").read_bytes() == b"retained bytes"
    assert not content.exists()
    assert qbit.deleted == [(job["info_hash"], False)]


@pytest.mark.asyncio
async def test_shared_payload_is_neither_detached_nor_moved(tmp_path, monkeypatch):
    database, manager, qbit, job, content = context(tmp_path)
    second = database.create_torrent_download(
        "kamui", {**release(info_hash="a" * 40), "id": "another-pack"}
    )
    database.update_torrent_download(second["id"], content_path=content / "v1.cbz")
    # UI pagination can omit the older sharing job; safety checks use the ledger.
    monkeypatch.setattr(database, "list_torrent_downloads", lambda **kwargs: [])
    failed = await manager._refuse_import(job, content, "No useful books")
    assert failed["qbit_state"] == "recycle_pending"
    assert content.exists() and not qbit.deleted


@pytest.mark.asyncio
async def test_successful_partial_pack_persists_import_and_skip_decisions(tmp_path):
    database, manager, qbit, job, content = context(tmp_path)

    class PartialImporter:
        async def import_torrent_download(self, job, content, **options):
            assert options["automatic_decision"] and options["skip_unnumbered"]
            assert not options["confirm_language"]
            return {
                "books": 1,
                "paths": ["library/v12.cbz"],
                "already_owned": 14,
                "already_owned_paths": ["v01.cbz"],
                "skipped_paths": ["extras.cbz"],
                "skip_decisions": [{"path": "extras.cbz", "reason": "unnumbered"}],
                "language_evidence": {"verdict": "confirmed"},
            }

    manager.importer = PartialImporter()
    result = await manager._import(job, {}, confirm_language=False)
    assert result["status"] == "imported"
    assert "Imported 1" in result["message"] and "skipped 14" in result["message"]
    assert result["language_evidence"]["import_decisions"] == {
        "imported_paths": ["library/v12.cbz"],
        "already_owned_paths": ["v01.cbz"],
        "skipped": [{"path": "extras.cbz", "reason": "unnumbered"}],
    }
    assert content.exists() and not qbit.deleted


@pytest.mark.asyncio
async def test_new_attempt_of_same_job_recycles_its_own_payload(tmp_path):
    database, manager, qbit, job, content = context(tmp_path)
    first = await manager._refuse_import(job, content, "First refusal")
    first_path = Path(first["language_evidence"]["retired_payload"])
    database.retry_torrent_download(job["id"])
    content.mkdir()
    (content / "v1.cbz").write_bytes(b"second attempt")
    next_job = database.get_torrent_download(job["id"])
    second = await manager._refuse_import(next_job, content, "Second refusal")
    second_path = Path(second["language_evidence"]["retired_payload"])
    assert first_path != second_path
    assert (first_path / "v1.cbz").read_bytes() == b"retained bytes"
    assert (second_path / "v1.cbz").read_bytes() == b"second attempt"
    assert second["qbit_state"] == "removed" and not content.exists()


@pytest.mark.asyncio
async def test_misconfigured_download_root_cannot_retire_library(tmp_path):
    database, manager, qbit, job, content = context(tmp_path)
    manager.settings.library_dir = content
    failed = await manager._refuse_import(job, content, "No useful books")
    assert failed["qbit_state"] == "recycle_pending"
    assert (content / "v1.cbz").read_bytes() == b"retained bytes"
    assert not qbit.deleted
