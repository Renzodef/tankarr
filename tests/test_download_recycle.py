from __future__ import annotations

import errno
import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tankarr import download_recycle
from tankarr.config import Settings
from tankarr.download_recycle import (
    BIN_NAME,
    DownloadRecycleConflict,
    purge_download_recycle,
    recycle_payload,
)


@pytest.fixture
def downloads(tmp_path):
    root = tmp_path / "downloads"
    root.mkdir()
    settings = Settings(data_dir=tmp_path / "data", torrent_download_dir=root)
    content = root / "Book.cbz"
    content.write_bytes(b"original download")
    return root, content, settings


def receipt(root, result):
    path = root / BIN_NAME / f"{result['operation_id']}.json"
    return path, json.loads(path.read_text())


def due(root, result, days=7):
    return datetime.fromisoformat(receipt(root, result)[1]["retired_at"]) + timedelta(
        days=days
    )


def test_file_moves_atomically_and_retry_is_idempotent(downloads):
    root, content, _settings = downloads
    inode = content.stat().st_ino
    result = recycle_payload(root, content, 17)
    assert result["files_retired"] == 1
    assert result["already_retired"] is False
    assert not content.exists()
    retired = Path(result["path"])
    assert retired.read_bytes() == b"original download"
    assert retired.stat().st_ino == inode
    retried = recycle_payload(root, content, 17)
    assert retried["path"] == result["path"]
    assert retried["already_retired"] is True
    assert receipt(root, result)[1]["state"] == "committed"


def test_retry_never_retires_recreated_original_even_after_expiry(downloads):
    root, content, settings = downloads
    result = recycle_payload(root, content, 17)
    content.write_bytes(b"new independent download")
    retried = recycle_payload(root, content, 17)
    assert retried["already_retired"] is True
    assert retried["source_recreated"] is True
    assert content.read_bytes() == b"new independent download"
    assert Path(result["path"]).read_bytes() == b"original download"

    assert purge_download_recycle(settings, now=due(root, result))["purged"] == 1
    assert not Path(result["path"]).exists()
    late = recycle_payload(root, content, 17)
    assert late["already_retired"] is late["purged"] is True
    assert late["path"] is None
    assert content.read_bytes() == b"new independent download"
    assert receipt(root, result)[1]["state"] == "purged"


def test_new_attempt_retires_new_payload_while_previous_retry_preserves_it(downloads):
    root, content, settings = downloads
    first_key, next_key = "a" * 32, "b" * 32
    first = recycle_payload(root, content, 17, attempt_key=first_key)
    content.write_bytes(b"second download attempt")

    retried = recycle_payload(root, content, 17, attempt_key=first_key)
    assert retried["already_retired"] is True
    assert retried["source_recreated"] is True
    assert content.read_bytes() == b"second download attempt"
    second = recycle_payload(root, content, 17, attempt_key=next_key)
    assert second["already_retired"] is False
    assert not content.exists()
    assert first["operation_id"] != second["operation_id"]
    assert receipt(root, first)[1]["attempt_key"] == first_key
    assert receipt(root, second)[1]["attempt_key"] == next_key
    assert Path(first["path"]).read_bytes() == b"original download"
    assert Path(second["path"]).read_bytes() == b"second download attempt"
    assert (
        recycle_payload(root, content, 17, attempt_key=first_key)["already_retired"]
        is True
    )
    expired = purge_download_recycle(settings, now=due(root, second))
    assert expired == {"purged": 2, "files_deleted": 2, "errors": []}


def test_legacy_receipt_without_attempt_key_remains_readable(downloads):
    root, content, settings = downloads
    result = recycle_payload(root, content, 17)
    journal, value = receipt(root, result)
    value.pop("attempt_key")
    journal.write_text(json.dumps(value))
    assert recycle_payload(root, content, 17)["already_retired"] is True
    assert purge_download_recycle(settings, now=due(root, result))["purged"] == 1


@pytest.mark.parametrize(
    "attempt_key", ["", "A" * 32, "a" * 31, "../escape", 123, True]
)
def test_invalid_attempt_keys_are_rejected_without_filesystem_changes(
    downloads, attempt_key
):
    root, content, _settings = downloads
    with pytest.raises(
        DownloadRecycleConflict, match="Invalid download retirement attempt key"
    ):
        recycle_payload(root, content, 17, attempt_key=attempt_key)
    assert content.read_bytes() == b"original download"
    assert not (root / BIN_NAME).exists()


@pytest.mark.parametrize("changed_key", ["invalid", "b" * 32])
def test_changed_attempt_key_invalidates_purge_receipt(downloads, changed_key):
    root, content, settings = downloads
    result = recycle_payload(root, content, 17, attempt_key="a" * 32)
    deadline = due(root, result)
    journal, value = receipt(root, result)
    value["attempt_key"] = changed_key
    journal.write_text(json.dumps(value))
    purged = purge_download_recycle(settings, now=deadline)
    assert purged["errors"]
    assert purged["purged"] == purged["files_deleted"] == 0
    assert Path(result["path"]).read_bytes() == b"original download"


def test_retention_uses_configured_duration_and_includes_exact_deadline(downloads):
    root, content, settings = downloads
    settings.recycle_bin_retention_days = 2
    result = recycle_payload(root, content, 17)
    deadline = due(root, result, 2)
    early = purge_download_recycle(settings, now=deadline - timedelta(microseconds=1))
    assert early == {"purged": 0, "files_deleted": 0, "errors": []}
    assert Path(result["path"]).exists()
    expired = purge_download_recycle(settings, now=deadline)
    assert expired == {"purged": 1, "files_deleted": 1, "errors": []}


def test_directory_retirement_and_purge_never_follow_nested_symlinks(
    downloads, tmp_path
):
    root, _content, settings = downloads
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep me")
    pack = root / "Pack"
    (pack / "pages").mkdir(parents=True)
    (pack / "pages" / "1.jpg").write_bytes(b"page")
    (pack / "external-directory").symlink_to(outside, target_is_directory=True)
    (pack / "external-file").symlink_to(outside / "keep.txt")
    result = recycle_payload(root, pack, 17)
    assert result["files_retired"] == 3
    assert not pack.exists()
    purged = purge_download_recycle(settings, now=due(root, result))
    assert purged == {"purged": 1, "files_deleted": 3, "errors": []}
    assert (outside / "keep.txt").read_text() == "keep me"


def test_empty_directory_can_be_retired(downloads):
    root, _content, settings = downloads
    empty = root / "empty"
    empty.mkdir()
    result = recycle_payload(root, empty, 17)
    assert result["files_retired"] == 0
    assert purge_download_recycle(settings, now=due(root, result)) == {
        "purged": 1,
        "files_deleted": 0,
        "errors": [],
    }


def test_retry_survives_removal_of_original_parent_directory(downloads):
    root, _content, _settings = downloads
    parent = root / "nested"
    parent.mkdir()
    content = parent / "Book.cbz"
    content.write_bytes(b"book")
    first = recycle_payload(root, content, 17)
    parent.rmdir()
    repeated = recycle_payload(root, content, 17)
    assert repeated["path"] == first["path"]
    assert repeated["already_retired"] is True


def test_prepared_rename_failure_keeps_source_and_is_retryable(downloads, monkeypatch):
    root, content, settings = downloads

    def unavailable(*_args):
        raise OSError(errno.EXDEV, "simulated rename refusal")

    with monkeypatch.context() as patch:
        patch.setattr(download_recycle, "_rename_payload", unavailable)
        with pytest.raises(OSError, match="simulated rename refusal"):
            recycle_payload(root, content, 17)
    assert content.read_bytes() == b"original download"
    far_future = datetime.now(UTC) + timedelta(days=800)
    assert purge_download_recycle(settings, now=far_future)["purged"] == 0
    assert content.read_bytes() == b"original download"
    assert recycle_payload(root, content, 17)["already_retired"] is False


@pytest.mark.parametrize("recovery", ["retry", "maintenance"])
def test_crash_after_rename_recovers_without_capturing_recreated_source(
    downloads, monkeypatch, recovery
):
    root, content, settings = downloads

    def interrupted(*_args):
        raise OSError("simulated interruption before commit receipt")

    with monkeypatch.context() as patch:
        patch.setattr(download_recycle, "_commit_receipt", interrupted)
        with pytest.raises(OSError, match="simulated interruption"):
            recycle_payload(root, content, 17)
    assert not content.exists()
    content.write_bytes(b"another download")
    if recovery == "retry":
        result = recycle_payload(root, content, 17)
        assert result["already_retired"] is True
        assert receipt(root, result)[1]["state"] == "committed"
    else:
        result = purge_download_recycle(
            settings, now=datetime.now(UTC) + timedelta(days=800)
        )
        assert result == {"purged": 0, "files_deleted": 0, "errors": []}
        journal = next(
            path
            for path in (root / BIN_NAME).glob("*.json")
            if path.name != "owner.json"
        )
        assert json.loads(journal.read_text())["state"] == "committed"
    assert content.read_bytes() == b"another download"
    assert (
        next((root / BIN_NAME).glob("*.payload")).read_bytes() == b"original download"
    )


def test_prepared_receipt_rejects_replaced_source(downloads, monkeypatch):
    root, content, _settings = downloads

    def interrupted(*_args):
        raise OSError("before rename")

    with monkeypatch.context() as patch:
        patch.setattr(download_recycle, "_rename_payload", interrupted)
        with pytest.raises(OSError):
            recycle_payload(root, content, 17)
    content.unlink()
    content.write_bytes(b"different download")
    with pytest.raises(DownloadRecycleConflict, match="changed before retirement"):
        recycle_payload(root, content, 17)
    assert content.read_bytes() == b"different download"


def test_partial_directory_purge_retries_with_committed_inode(downloads, monkeypatch):
    root, _content, settings = downloads
    pack = root / "Pack"
    pack.mkdir()
    for name in ("1.cbz", "2.cbz"):
        (pack / name).write_bytes(name.encode())
    result = recycle_payload(root, pack, 17)
    original = os.unlink
    removed = 0

    def fail_second(name, *args, **kwargs):
        nonlocal removed
        if name in ("1.cbz", "2.cbz"):
            removed += 1
            if removed == 2:
                raise OSError("simulated purge interruption")
        return original(name, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(download_recycle.os, "unlink", fail_second)
        failed = purge_download_recycle(settings, now=due(root, result))
    assert failed["errors"]
    assert failed["files_deleted"] == 1
    assert receipt(root, result)[1]["state"] == "purging"
    repeated = purge_download_recycle(settings, now=due(root, result))
    assert repeated == {"purged": 1, "files_deleted": 1, "errors": []}


@pytest.mark.parametrize(
    "unsafe", ["root", "outside", "bin", "symlink", "parent_symlink", "hard_link"]
)
def test_unsafe_or_shared_sources_are_never_moved(downloads, tmp_path, unsafe):
    root, content, _settings = downloads
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "Book.cbz"
    sentinel.write_bytes(b"outside")
    if unsafe == "root":
        candidate = root
    elif unsafe == "outside":
        candidate = sentinel
    elif unsafe == "bin":
        candidate = root / BIN_NAME
        candidate.mkdir()
    elif unsafe == "symlink":
        candidate = root / "linked.cbz"
        candidate.symlink_to(sentinel)
    elif unsafe == "parent_symlink":
        (root / "linked").symlink_to(outside, target_is_directory=True)
        candidate = root / "linked" / "Book.cbz"
    else:
        candidate = content
        os.link(content, root / "alias.cbz")
    with pytest.raises((OSError, DownloadRecycleConflict)):
        recycle_payload(root, candidate, 17)
    assert content.read_bytes() == b"original download"
    assert sentinel.read_bytes() == b"outside"


@pytest.mark.parametrize(
    "tamper",
    [
        "path",
        "inode",
        "state",
        "timestamp",
        "payload_symlink",
        "journal_symlink",
        "marker",
    ],
)
def test_purge_preserves_unproven_journals_and_payloads(downloads, tmp_path, tamper):
    root, content, settings = downloads
    result = recycle_payload(root, content, 17)
    deadline = due(root, result)
    journal, value = receipt(root, result)
    sentinel = tmp_path / "sentinel"
    sentinel.write_text("outside")
    if tamper == "payload_symlink":
        Path(result["path"]).unlink()
        Path(result["path"]).symlink_to(sentinel)
    elif tamper == "journal_symlink":
        journal.unlink()
        journal.symlink_to(sentinel)
    elif tamper == "marker":
        (root / BIN_NAME / "owner.json").write_text("{}")
    else:
        if tamper == "path":
            value["original"] = "../sentinel"
        elif tamper == "inode":
            value["fingerprint"]["inode"] += 1
        elif tamper == "state":
            value["state"] = "invented"
        else:
            value["retired_at"] = "2020-01-01"
        journal.write_text(json.dumps(value))
    purged = purge_download_recycle(settings, now=deadline)
    assert purged["errors"]
    assert purged["purged"] == purged["files_deleted"] == 0
    assert sentinel.read_text() == "outside"
    assert Path(result["path"]).exists()


def test_bin_symlink_and_unowned_directory_are_rejected(downloads, tmp_path):
    root, content, settings = downloads
    outside = tmp_path / "outside"
    outside.mkdir()
    recycle = root / BIN_NAME
    recycle.symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError):
        recycle_payload(root, content, 17)
    assert purge_download_recycle(settings)["errors"]
    recycle.unlink()
    recycle.mkdir()
    (recycle / "keep.txt").write_text("keep")
    with pytest.raises(DownloadRecycleConflict, match="no ownership marker"):
        recycle_payload(root, content, 17)
    assert (recycle / "keep.txt").read_text() == "keep"


def test_concurrent_retries_only_move_one_payload(downloads):
    root, content, _settings = downloads
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(lambda _: recycle_payload(root, content, 17), range(2))
        )
    assert sorted(result["already_retired"] for result in results) == [False, True]
    assert len(list((root / BIN_NAME).glob("*.payload"))) == 1


def test_purge_covers_all_configured_download_roots(tmp_path):
    settings = Settings(
        data_dir=tmp_path / "data",
        torrent_download_dir=tmp_path / "torrent",
        usenet_download_dir=tmp_path / "usenet",
    )
    for root in (
        settings.torrent_download_dir,
        settings.usenet_download_dir,
        settings.data_dir / "direct-downloads",
    ):
        root.mkdir(parents=True)
        content = root / "book.cbz"
        content.write_bytes(b"book")
        recycle_payload(root, content, 17)
    result = purge_download_recycle(settings, now=datetime.now(UTC) + timedelta(days=8))
    assert result == {"purged": 3, "files_deleted": 3, "errors": []}
