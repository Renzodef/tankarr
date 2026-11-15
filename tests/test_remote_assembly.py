from __future__ import annotations

import importlib.util
import shutil
import zipfile
from pathlib import Path

import pytest

from tankarr.assemble import AssemblyConflict
from tankarr.service import UnsafeLibraryPath
from tests import test_assemble

complete_book = test_assemble.complete_book

SPEC = importlib.util.spec_from_file_location(
    "assembly_worker", Path(__file__).resolve().parents[1] / "tools/assemble_volume.py"
)
worker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(worker)


def prepared(complete_book, tmp_path):
    _database, service, assembler, paths = complete_book
    preview = assembler.preview("manga-1", "12", worker_manifest=True)
    manifest = preview["worker_manifest"]
    inputs = tmp_path / "worker-input"
    inputs.mkdir()
    for item, path in zip(manifest["sources"], paths, strict=True):
        shutil.copyfile(path, inputs / item["file"])
    session = "a" * 32
    target = service.settings.data_dir / "import-uploads" / session / "assembled.cbz"
    target.parent.mkdir(parents=True)
    report = worker.package(manifest, inputs, target, reserve_bytes=0)
    assert report["pages"] == 6
    return preview, session, target


def test_portable_manifest_has_no_host_paths(complete_book):
    _database, _service, assembler, paths = complete_book
    preview = assembler.preview("manga-1", "12", worker_manifest=True)
    manifest = preview["worker_manifest"]
    assert [x["file"] for x in manifest["sources"]] == [
        "000001.cbz",
        "000002.cbz",
        "000003.cbz",
    ]
    assert all(str(path) not in str(manifest) for path in paths)
    assert "worker_manifest" not in assembler.preview("manga-1", "12")


async def test_remote_book_uses_normal_commit_and_recycle_without_local_packaging(
    complete_book, tmp_path, monkeypatch
):
    database, _service, assembler, paths = complete_book
    preview, session, _target = prepared(complete_book, tmp_path)

    def no_local_packaging(*_args):
        pytest.fail("remote assembly must not repackage on the library host")

    monkeypatch.setattr(assembler, "_package", no_local_packaging)
    result = await assembler.assemble(
        "manga-1", "12", preview["confirmation_snapshot"], prepared_upload_id=session
    )
    assert result["retirement"]["chapters_reset"] == 3
    assert result["retirement"]["files_deleted"] == 0
    assert all(not p.exists() for p in paths)
    book = database.get_chapter(result["chapter"]["id"])
    assert book["provider"] == "assembled"
    assert book["assembled_from"]["chapters"] == ["55", "55.5", "56"]
    assert Path(result["path"]).is_file()
    with pytest.raises(AssemblyConflict, match="already owned"):
        await assembler.assemble(
            "manga-1",
            "12",
            preview["confirmation_snapshot"],
            prepared_upload_id=session,
        )


@pytest.mark.parametrize(
    "tamper", ["payload", "order", "extra", "missing", "metadata", "compressed"]
)
async def test_remote_output_cannot_forge_source_coverage(
    complete_book, tmp_path, tamper
):
    database, _service, assembler, paths = complete_book
    preview, session, target = prepared(complete_book, tmp_path)
    with zipfile.ZipFile(target) as archive:
        entries = [(x.filename, archive.read(x)) for x in archive.infolist()]
    if tamper == "payload":
        name, data = entries[1]
        entries[1] = name, b"x" * len(data)
    elif tamper == "order":
        # Same names/sizes, different page payload sequence.
        entries[1], entries[2] = entries[2], entries[1]
    elif tamper == "extra":
        entries.append(("extra.txt", b"unexpected"))
    elif tamper == "missing":
        entries.pop()
    elif tamper == "metadata":
        entries[0] = "ComicInfo.xml", b"<ComicInfo/>"
    with zipfile.ZipFile(
        target,
        "w",
        compression=zipfile.ZIP_DEFLATED
        if tamper == "compressed"
        else zipfile.ZIP_STORED,
    ) as archive:
        for name, data in entries:
            archive.writestr(name, data)
    with pytest.raises(AssemblyConflict):
        await assembler.assemble(
            "manga-1",
            "12",
            preview["confirmation_snapshot"],
            prepared_upload_id=session,
        )
    assert all(p.exists() for p in paths)
    assert not any(
        r["provider"] == "assembled" for r in database.list_chapters("manga-1", "en")
    )


async def test_changed_sources_invalidate_prepared_book(complete_book, tmp_path):
    _database, _service, assembler, paths = complete_book
    preview, session, _target = prepared(complete_book, tmp_path)
    with zipfile.ZipFile(paths[0], "a") as archive:
        archive.writestr("another.jpg", b"new page")
    with pytest.raises(AssemblyConflict, match="Preview|preview|hash ledger"):
        await assembler.assemble(
            "manga-1",
            "12",
            preview["confirmation_snapshot"],
            prepared_upload_id=session,
        )
    assert all(p.exists() for p in paths)


async def test_upload_symlink_is_not_followed(complete_book, tmp_path):
    _database, _service, assembler, paths = complete_book
    preview, session, target = prepared(complete_book, tmp_path)
    outside = tmp_path / "outside.cbz"
    target.rename(outside)
    target.symlink_to(outside)
    with pytest.raises((OSError, UnsafeLibraryPath)):
        await assembler.assemble(
            "manga-1",
            "12",
            preview["confirmation_snapshot"],
            prepared_upload_id=session,
        )
    assert all(p.exists() for p in paths)


def test_worker_refuses_missing_disk_reserve(complete_book, tmp_path, monkeypatch):
    _database, _service, assembler, paths = complete_book
    manifest = assembler.preview("manga-1", "12", worker_manifest=True)[
        "worker_manifest"
    ]
    monkeypatch.setattr(
        worker.shutil, "disk_usage", lambda _: type("Disk", (), {"free": 1})()
    )
    target = tmp_path / "output.cbz"
    with pytest.raises(ValueError, match="reserve"):
        worker.package(manifest, paths[0].parent, target)
    assert not target.exists()


def test_worker_rejects_source_different_from_manifest(complete_book, tmp_path):
    _database, _service, assembler, paths = complete_book
    manifest = assembler.preview("manga-1", "12", worker_manifest=True)[
        "worker_manifest"
    ]
    for item, path in zip(manifest["sources"], paths, strict=True):
        shutil.copyfile(path, tmp_path / item["file"])
    (tmp_path / "000001.cbz").write_bytes(b"changed")
    with pytest.raises(ValueError, match="snapshot"):
        worker.package(manifest, tmp_path, tmp_path / "output.cbz", reserve_bytes=0)
    assert not (tmp_path / "output.cbz").exists()
