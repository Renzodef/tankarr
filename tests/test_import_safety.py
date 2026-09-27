from __future__ import annotations

import asyncio
import errno
from pathlib import Path
from types import SimpleNamespace

import pytest

import tankarr.archive as archive_module
from tankarr.archive import install_atomically, package_cbz, publish_without_overwrite
from tankarr.config import Settings
from tankarr.importer import LibraryImporter
from tankarr.qbittorrent import QBitTorrentClient, QBitTorrentError
from tankarr.sabnzbd import SABnzbdClient, SABnzbdError
from tankarr.torrents import TorrentManager


def test_atomic_publication_refuses_an_existing_file(tmp_path: Path):
    source = tmp_path / "staging"
    destination = tmp_path / "book.cbz"
    source.write_bytes(b"new")
    destination.write_bytes(b"existing")

    with pytest.raises(FileExistsError):
        publish_without_overwrite(source, destination)

    assert source.read_bytes() == b"new"
    assert destination.read_bytes() == b"existing"


def test_atomic_publication_supports_link_fallback(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(archive_module, "_linux_renameat2", lambda: None)
    source = tmp_path / "staging"
    destination = tmp_path / "book.cbz"
    source.write_bytes(b"complete archive")

    publish_without_overwrite(source, destination)

    assert destination.read_bytes() == b"complete archive"
    assert not source.exists()


def test_atomic_publication_fails_closed_without_filesystem_support(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setattr(archive_module, "_linux_renameat2", lambda: None)

    def unsupported(*args, **kwargs):
        raise OSError(errno.EOPNOTSUPP, "unsupported")

    monkeypatch.setattr(archive_module.os, "link", unsupported)
    source = tmp_path / "staging"
    destination = tmp_path / "book.cbz"
    source.write_bytes(b"complete archive")

    with pytest.raises(OSError, match="safe no-overwrite publication"):
        publish_without_overwrite(source, destination)

    assert source.read_bytes() == b"complete archive"
    assert not destination.exists()


@pytest.mark.parametrize("identical", [False, True])
def test_install_handles_a_file_created_during_copy(
    tmp_path: Path, monkeypatch, identical: bool
):
    source = tmp_path / "source.cbz"
    destination = tmp_path / "library" / "book.cbz"
    source.write_bytes(b"staged book")
    concurrent = b"staged book" if identical else b"someone else's book"
    publish = archive_module.publish_without_overwrite

    def competing_publication(partial, final):
        final.write_bytes(concurrent)
        publish(partial, final)

    monkeypatch.setattr(
        archive_module, "publish_without_overwrite", competing_publication
    )
    if identical:
        assert install_atomically(source, destination) == destination
    else:
        with pytest.raises(FileExistsError, match="different library file"):
            install_atomically(source, destination)

    assert destination.read_bytes() == concurrent
    assert source.read_bytes() == b"staged book"
    assert list(destination.parent.glob("*.partial")) == []


def test_install_cleans_its_partial_after_io_failure(tmp_path: Path, monkeypatch):
    source = tmp_path / "source.cbz"
    destination = tmp_path / "library" / "book.cbz"
    source.write_bytes(b"staged book")

    def fail_fsync(descriptor):
        raise OSError("simulated disk failure")

    monkeypatch.setattr(archive_module.os, "fsync", fail_fsync)
    with pytest.raises(OSError, match="simulated disk failure"):
        install_atomically(source, destination)

    assert not destination.exists()
    assert list(destination.parent.iterdir()) == []


@pytest.mark.parametrize("broken", [False, True])
def test_install_never_replaces_a_symlink(tmp_path: Path, broken: bool):
    source = tmp_path / "source.cbz"
    target = tmp_path / "user-book.cbz"
    destination = tmp_path / "book.cbz"
    source.write_bytes(b"new")
    if not broken:
        target.write_bytes(b"new")
    destination.symlink_to(target)

    with pytest.raises(FileExistsError, match="symlink"):
        install_atomically(source, destination)

    assert destination.is_symlink()
    assert target.exists() is not broken


def test_failed_packaging_preserves_existing_archive_and_cleans_partial(tmp_path: Path):
    destination = tmp_path / "book.cbz"
    destination.write_bytes(b"previous archive")

    with pytest.raises(FileNotFoundError):
        package_cbz(
            destination,
            [tmp_path / "missing.jpg"],
            {"title": "Book"},
            {"language": "en", "provider": "test"},
        )

    assert destination.read_bytes() == b"previous archive"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["book.cbz"]


def make_manager(tmp_path: Path) -> TorrentManager:
    manager = TorrentManager.__new__(TorrentManager)
    manager.service = SimpleNamespace(
        settings=SimpleNamespace(data_dir=tmp_path, sabnzbd_category="tankarr")
    )
    return manager


@pytest.mark.parametrize(
    "name", ["%2Ftmp%2Fbook.cbz", "..%2Fbook.cbz", "%5Cbook.cbz", "book%00.cbz"]
)
def test_direct_download_rejects_decoded_path_components(tmp_path: Path, name: str):
    manager = make_manager(tmp_path)

    with pytest.raises(ValueError, match="unsafe filename"):
        manager._direct_destination(
            {"id": 1, "torrent_url": f"https://archive.org/download/item/{name}"}
        )


def test_direct_download_preserves_safe_unicode_filename(tmp_path: Path):
    destination = make_manager(tmp_path)._direct_destination(
        {
            "id": 7,
            "torrent_url": "https://archive.org/download/item/Caf%C3%A9%20Vol.1.cbz?x=1#anchor",
        }
    )
    assert destination == tmp_path / "direct-downloads" / "7" / "Café Vol.1.cbz"


def test_direct_download_rejects_symlinked_job_directory(tmp_path: Path):
    directory = tmp_path / "direct-downloads"
    directory.mkdir()
    (directory / "1").symlink_to(tmp_path, target_is_directory=True)

    with pytest.raises(ValueError, match="symlink"):
        make_manager(tmp_path)._direct_destination(
            {"id": 1, "torrent_url": "https://archive.org/download/item/book.cbz"}
        )


@pytest.mark.parametrize("relative", ["book/..", "book/../other", "alias"])
def test_qbittorrent_rejects_traversal_and_mount_root_alias(
    tmp_path: Path, relative: str
):
    (tmp_path / "alias").symlink_to(tmp_path, target_is_directory=True)
    client = QBitTorrentClient(
        Settings(
            _env_file=None,
            torrent_download_dir=tmp_path,
            qbittorrent_save_path="/downloads/tankarr",
        )
    )

    with pytest.raises(QBitTorrentError):
        client.local_content_path({"content_path": f"/downloads/tankarr/{relative}"})


@pytest.mark.parametrize("where", ["queue", "history"])
@pytest.mark.parametrize("category", ["other-app", ""])
@pytest.mark.asyncio
async def test_sabnzbd_never_deletes_outside_its_category(
    monkeypatch, where: str, category: str
):
    client = SABnzbdClient(Settings(_env_file=None, sabnzbd_category="tankarr"))
    called = []

    async def job_state(nzo_id):
        return {"where": where, "cat": category, "nzo_id": nzo_id}

    async def request(*args, **kwargs):
        called.append((args, kwargs))

    monkeypatch.setattr(client, "job_state", job_state)
    monkeypatch.setattr(client, "_call", request)

    with pytest.raises(SABnzbdError, match="outside Tankarr's"):
        await client.delete("selected-job", delete_files=True)

    assert called == []


def test_sabnzbd_monitor_rejects_category_change_before_import(tmp_path: Path):
    manager = make_manager(tmp_path)
    with pytest.raises(SABnzbdError, match="outside Tankarr's"):
        manager._record_sab_state(
            {"id": 1}, {"where": "history", "cat": "other-app", "status": "Completed"}
        )


def test_sabnzbd_rejects_storage_alias_to_entire_mount(tmp_path: Path):
    (tmp_path / "alias").symlink_to(tmp_path, target_is_directory=True)
    client = SABnzbdClient(
        Settings(
            _env_file=None,
            usenet_download_dir=tmp_path,
            sabnzbd_complete_path="/downloads/tankarr",
            sabnzbd_category="tankarr",
        )
    )

    with pytest.raises(SABnzbdError, match="escapes the download mount"):
        client.local_content_path(
            {"cat": "tankarr", "storage": "/downloads/tankarr/alias"}
        )


@pytest.mark.asyncio
async def test_concurrent_uploads_never_overwrite_the_first_complete_file(
    tmp_path: Path,
):
    settings = Settings(_env_file=None, data_dir=tmp_path / "data")
    importer = LibraryImporter(settings, object(), object())
    upload_id = importer.create_upload()["upload_id"]
    started = asyncio.Event()
    proceed = asyncio.Event()

    async def delayed_chunks():
        started.set()
        await proceed.wait()
        yield b"late upload"

    async def fast_chunks():
        yield b"first complete upload"

    slow = asyncio.create_task(
        importer.store_upload_file(upload_id, "book.cbz", delayed_chunks())
    )
    await started.wait()
    await importer.store_upload_file(upload_id, "book.cbz", fast_chunks())
    proceed.set()
    with pytest.raises(FileExistsError):
        await slow

    directory = importer.uploads_root / upload_id
    assert (directory / "book.cbz").read_bytes() == b"first complete upload"
    assert sorted(path.name for path in directory.iterdir()) == ["book.cbz"]
