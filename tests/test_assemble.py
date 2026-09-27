from __future__ import annotations

import asyncio
import os
import threading
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tankarr.archive import sha256, validate_cbz
from tankarr.assemble import AssemblyConflict, BookAssembler, register_assemble_routes
from tankarr.chapter_map import MapEntry
from tankarr.comicinfo import build_comic_info
from tankarr.service import UnsafeLibraryPath
from tests.test_deletion import chapter, make_service, manga


@pytest.fixture
def complete_book(tmp_path, monkeypatch):
    database, service, _reader = make_service(tmp_path)
    database.upsert_manga(manga(), "en", "all")
    database.update_manga("manga-1", {"series_unit_override": "volumes"})
    database.save_series_metadata(
        "manga-1",
        {"volume_count": 13},
        artwork_path=None,
        artwork_sha256=None,
        artwork_media_type=None,
        source_status=[],
    )
    database.save_volume_metadata("manga-1", "12", {"title": "The Return"})
    releases = [
        {
            **chapter(f"chapter-{number}", number, volume="9"),
            "source_name": "Example Source (EN)",
        }
        for number in ("55", "55.5", "56")
    ]
    database.upsert_chapters("manga-1", releases)
    database.replace_chapter_map(
        "manga-1",
        "operator",
        [MapEntry(("12",), ("55", "55.5", "56"), True, source="operator")],
    )
    paths = []
    for release in releases:
        path = service.settings.library_dir / "chapters" / f"{release['id']}.cbz"
        path.parent.mkdir(exist_ok=True)
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("10.jpg", f"{release['chapter']}:second".encode())
            archive.writestr("2.png", f"{release['chapter']}:first".encode())
            archive.writestr("ComicInfo.xml", build_comic_info(manga(), release, 2))
        database.mark_chapter_downloaded(release["id"], path, sha256(path))
        paths.append(path)

    async def reader_call(*_args):
        return {"requested": True, "triggered": True}

    monkeypatch.setattr(service, "_request_komga_reconciliation", reader_call)
    monkeypatch.setattr(service, "_sync_imported_path_with_komga", reader_call)
    return database, service, BookAssembler(database, service), paths


def database_state(database):
    with database.connect() as connection:
        return tuple(connection.iterdump())


def test_preview_reads_real_files_without_writes(complete_book):
    database, _service, assembler, paths = complete_book
    before = database_state(database)
    originals = [path.read_bytes() for path in paths]

    preview = assembler.preview("manga-1", "12")

    assert preview["filename"] == "Example - v012 [en].cbz"
    assert preview["pages"] == 6
    assert [item["chapter"] for item in preview["chapters"]] == ["55", "55.5", "56"]
    assert [item["pages"] for item in preview["chapters"]] == [2, 2, 2]
    assert database_state(database) == before
    assert [path.read_bytes() for path in paths] == originals


@pytest.mark.parametrize("missing", ["not_downloaded", "file_gone"])
def test_dry_run_returns_409_with_missing_chapters(complete_book, missing):
    database, service, _assembler, paths = complete_book
    if missing == "file_gone":
        paths[1].unlink()
    else:
        with database.connect() as connection:
            connection.execute(
                "UPDATE chapter_release SET downloaded=0 WHERE id='chapter-55.5'"
            )
    app = FastAPI()
    register_assemble_routes(app, database, service)

    with TestClient(app) as client:
        response = client.post("/api/manga/manga-1/volumes/12/assemble?dry_run=true")

    assert response.status_code == 409
    assert response.json()["detail"]["missing_chapters"] == ["55.5"]


def test_hint_does_not_authorize_assembly(complete_book):
    database, _service, assembler, _paths = complete_book
    database.replace_chapter_map("manga-1", "operator", [])
    database.replace_chapter_map(
        "manga-1",
        "catalogue",
        [MapEntry(("12",), ("55", "55.5", "56"), False, source="catalogue")],
    )

    with pytest.raises(AssemblyConflict, match="exact chapter-to-book map"):
        assembler.preview("manga-1", "12")


def test_ambiguous_exact_map_does_not_authorize_assembly(complete_book):
    database, _service, assembler, _paths = complete_book
    database.replace_chapter_map(
        "manga-1",
        "operator",
        [
            MapEntry(("12",), ("55", "55.5", "56"), True, source="operator"),
            MapEntry(("13",), ("56", "57"), True, source="operator"),
        ],
    )
    with pytest.raises(AssemblyConflict, match="more than one book"):
        assembler.preview("manga-1", "12")


def add_part(database, service, number, *, source="split", downloaded=True):
    release = {
        **chapter(f"part-{number}", number),
        "source_key": source,
        "source_name": source,
    }
    database.upsert_chapters("manga-1", [release])
    if not downloaded:
        return None
    path = service.settings.library_dir / f"part-{number}.cbz"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("0001.jpg", number.encode())
        archive.writestr("ComicInfo.xml", build_comic_info(manga(), release, 1))
    database.mark_chapter_downloaded(release["id"], path, sha256(path))
    return path


def use_split_map(database):
    database.replace_chapter_map(
        "manga-1",
        "operator",
        [MapEntry(("12",), ("55", "56"), True, source="operator")],
    )
    with database.connect() as connection:
        connection.execute(
            "UPDATE chapter_release SET downloaded=0 WHERE id='chapter-55'"
        )


async def test_complete_parts_from_one_source_assemble_and_retire_together(
    complete_book,
):
    database, service, assembler, _paths = complete_book
    use_split_map(database)
    parts = [add_part(database, service, number) for number in ("55.1", "55.2")]

    preview = assembler.preview("manga-1", "12")
    assert [item["source_chapter"] for item in preview["chapters"]] == [
        "55.1",
        "55.2",
        "56",
    ]
    assert [item["chapter"] for item in preview["chapters"]] == ["55", "55", "56"]
    result = await assembler.assemble("manga-1", "12", preview["confirmation_snapshot"])

    assert result["chapter"]["assembled_from"]["chapters"] == ["55", "56"]
    assert result["chapter"]["assembled_from"]["chapter_ids"] == [
        "part-55.1",
        "part-55.2",
        "chapter-56",
    ]
    assert result["retirement"]["chapters_reset"] == 3
    assert all(not part.exists() for part in parts)


@pytest.mark.parametrize("incomplete", ["missing_last", "mixed_sources"])
def test_partial_or_mixed_source_parts_do_not_cover_a_chapter(
    complete_book, incomplete
):
    database, service, assembler, _paths = complete_book
    use_split_map(database)
    add_part(database, service, "55.1")
    add_part(
        database,
        service,
        "55.2",
        source="another" if incomplete == "mixed_sources" else "split",
    )
    if incomplete == "missing_last":
        add_part(database, service, "55.3", downloaded=False)

    with pytest.raises(AssemblyConflict) as caught:
        assembler.preview("manga-1", "12")

    assert caught.value.detail["missing_chapters"] == ["55"]


def test_an_owned_suspect_book_still_prevents_assembly(complete_book):
    database, _service, assembler, _paths = complete_book
    database.upsert_chapters(
        "manga-1",
        [
            {
                **chapter("manual-book", "", volume="12"),
                "chapter": None,
                "release_unit": "volume",
                "provider": "manual",
            }
        ],
    )
    database.mark_chapter_downloaded(
        "manual-book", database.path.parent / "missing.cbz"
    )

    with pytest.raises(AssemblyConflict, match="already owned"):
        assembler.preview("manga-1", "12")


@pytest.mark.parametrize("unsafe", ["symlink", "symlink_parent", "outside"])
def test_unsafe_library_sources_are_not_followed(complete_book, unsafe, tmp_path):
    database, service, assembler, paths = complete_book
    if unsafe == "outside":
        target = tmp_path / "outside.cbz"
        target.write_bytes(paths[0].read_bytes())
    elif unsafe == "symlink":
        target = service.settings.library_dir / "linked.cbz"
        target.symlink_to(paths[0])
    else:
        link = service.settings.library_dir / "linked"
        link.symlink_to(paths[0].parent, target_is_directory=True)
        target = link / paths[0].name
    database.mark_chapter_downloaded("chapter-55", target, sha256(paths[0]))

    with pytest.raises(AssemblyConflict) as caught:
        assembler.preview("manga-1", "12")

    assert caught.value.detail["missing_chapters"] == ["55"]


@pytest.mark.parametrize("changed", ["map", "file", "metadata"])
async def test_changed_snapshot_preserves_all_chapters(complete_book, changed):
    database, _service, assembler, paths = complete_book
    preview = assembler.preview("manga-1", "12")
    if changed == "map":
        database.replace_chapter_map(
            "manga-1",
            "operator",
            [MapEntry(("12",), ("55", "56"), True, source="operator")],
        )
    elif changed == "file":
        original_stat = paths[0].stat()
        payload = paths[0].read_bytes().replace(b"55:second", b"55:alterd")
        paths[0].write_bytes(payload)
        os.utime(paths[0], ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    else:
        database.save_volume_metadata("manga-1", "12", {"title": "Changed title"})
    before = database_state(database)

    with pytest.raises(AssemblyConflict):
        await assembler.assemble("manga-1", "12", preview["confirmation_snapshot"])

    assert database_state(database) == before
    assert all(path.exists() for path in paths)


async def test_packaged_pages_are_in_chapter_and_natural_page_order(
    complete_book, tmp_path
):
    _database, _service, assembler, _paths = complete_book
    with assembler.database.read_snapshot():
        plan = assembler._plan("manga-1", "12")
    workspace = tmp_path / "package"
    workspace.mkdir()

    path, info, _content_hash = assembler._package(plan, workspace)

    assert validate_cbz(path) == info
    with zipfile.ZipFile(path) as archive:
        assert archive.namelist() == [
            "ComicInfo.xml",
            "000001.png",
            "000002.jpg",
            "000003.png",
            "000004.jpg",
            "000005.png",
            "000006.jpg",
        ]
        assert [archive.read(name) for name in archive.namelist()[1:]] == [
            b"55:first",
            b"55:second",
            b"55.5:first",
            b"55.5:second",
            b"56:first",
            b"56:second",
        ]
        comic = ET.fromstring(archive.read("ComicInfo.xml"))
    assert comic.findtext("Number") == "12"
    assert comic.findtext("Count") == "13"
    assert comic.findtext("Title") == "The Return"
    assert comic.findtext("PageCount") == "6"
    assert comic.findtext("Notes") == (
        "Assembled from chapters 55, 55.5, 56; sources: Example Source (EN)"
    )


async def test_assembly_commits_provenance_before_recycling_sources(
    complete_book, monkeypatch
):
    database, service, assembler, paths = complete_book
    preview = assembler.preview("manga-1", "12")
    original_bytes = [path.read_bytes() for path in paths]
    retire = service._delete_duplicate_chapter_files_locked
    proofs = []

    def checked_retirement(*args, **kwargs):
        book = next(
            row
            for row in database.list_all_chapters("manga-1")
            if row["provider"] == "assembled"
        )
        proofs.append(book["assembled_from"])
        assert book["library_sha256"] == sha256(Path(book["library_path"]))
        assert all(path.exists() for path in paths)
        return retire(*args, **kwargs)

    monkeypatch.setattr(
        service, "_delete_duplicate_chapter_files_locked", checked_retirement
    )

    result = await assembler.assemble("manga-1", "12", preview["confirmation_snapshot"])

    assert proofs == [result["chapter"]["assembled_from"]]
    assert proofs[0]["chapter_ids"] == ["chapter-55", "chapter-55.5", "chapter-56"]
    assert proofs[0]["chapters"] == ["55", "55.5", "56"]
    assert result["retirement"]["chapters_reset"] == 3
    assert not any(path.exists() for path in paths)
    assert all(
        not database.get_chapter(f"chapter-{n}")["downloaded"]
        for n in ("55", "55.5", "56")
    )
    retained = [path for path in service.settings.library_dir.rglob("*.quarantined")]
    assert sorted(path.read_bytes() for path in retained) == sorted(original_bytes)


async def test_import_failure_does_not_retire_source_files(complete_book, monkeypatch):
    database, _service, assembler, paths = complete_book
    preview = assembler.preview("manga-1", "12")

    def fail_publication(*_args, **_kwargs):
        raise RuntimeError("injected database failure")

    monkeypatch.setattr(database, "publish_external_chapter", fail_publication)
    with pytest.raises(RuntimeError, match="injected database failure"):
        await assembler.assemble("manga-1", "12", preview["confirmation_snapshot"])

    assert all(path.exists() for path in paths)
    assert not any(
        row["provider"] == "assembled" for row in database.list_all_chapters("manga-1")
    )
    assert not list(
        assembler.service.settings.library_dir.rglob("Example - v012 [en].cbz")
    )


async def test_recovered_identical_orphan_is_adopted_before_retirement(
    complete_book, tmp_path
):
    database, _service, assembler, paths = complete_book
    with database.read_snapshot():
        plan = assembler._plan("manga-1", "12")
    workspace = tmp_path / "orphan"
    workspace.mkdir()
    packaged, _info, _content_hash = assembler._package(plan, workspace)
    destination = Path(plan["destination"])
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(packaged.read_bytes())
    with zipfile.ZipFile(destination, "a") as archive:
        archive.comment = b"Recovered after interruption before database commit"
    orphan_sha256 = sha256(destination)
    preview = assembler.preview("manga-1", "12")

    result = await assembler.assemble("manga-1", "12", preview["confirmation_snapshot"])

    assert result["reused"] is True
    assert result["chapter"]["library_sha256"] == orphan_sha256
    assert result["retirement"]["chapters_reset"] == 3
    assert not any(path.exists() for path in paths)


async def test_new_destination_file_invalidates_the_reviewed_snapshot(complete_book):
    _database, _service, assembler, paths = complete_book
    preview = assembler.preview("manga-1", "12")
    with assembler.database.read_snapshot():
        destination = Path(assembler._plan("manga-1", "12")["destination"])
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(paths[0].read_bytes())

    with pytest.raises(AssemblyConflict, match="Preview and confirm again"):
        await assembler.assemble("manga-1", "12", preview["confirmation_snapshot"])

    assert all(path.exists() for path in paths)


async def test_proven_assembled_book_over_600_pages_retires_its_chapters(complete_book):
    database, service, assembler, paths = complete_book
    with zipfile.ZipFile(paths[0], "a") as archive:
        for number in range(3, 602):
            archive.writestr(f"extra-{number:04d}.jpg", f"55:{number}".encode())
    database.mark_chapter_downloaded("chapter-55", paths[0], sha256(paths[0]))
    preview = assembler.preview("manga-1", "12")
    assert preview["pages"] == 605

    result = await assembler.assemble("manga-1", "12", preview["confirmation_snapshot"])

    assert result["chapter"]["pages"] == 605
    assert "12" not in service.suspect_covering_volumes("manga-1")
    assert result["retirement"]["chapters_reset"] == 3
    assert not any(path.exists() for path in paths)


async def test_retirement_failure_leaves_committed_book_and_originals(
    complete_book, monkeypatch
):
    database, service, assembler, paths = complete_book
    preview = assembler.preview("manga-1", "12")

    def fail_retirement(*_args, **_kwargs):
        raise RuntimeError("injected interruption after import")

    monkeypatch.setattr(
        service, "_delete_duplicate_chapter_files_locked", fail_retirement
    )
    with pytest.raises(RuntimeError, match="interruption after import"):
        await assembler.assemble("manga-1", "12", preview["confirmation_snapshot"])

    assert all(path.exists() for path in paths)
    book = next(
        row
        for row in database.list_all_chapters("manga-1")
        if row["provider"] == "assembled"
    )
    assert book["downloaded"] is True
    assert len(book["assembled_from"]["chapter_ids"]) == 3
    assert Path(book["library_path"]).exists()


async def test_changed_bytes_at_retirement_are_restored_without_resetting_sources(
    complete_book, monkeypatch
):
    database, service, assembler, paths = complete_book
    preview = assembler.preview("manga-1", "12")
    retire = service._delete_duplicate_chapter_files_locked

    def change_before_staging(*args, **kwargs):
        paths[0].write_bytes(paths[0].read_bytes() + b"external edit")
        return retire(*args, **kwargs)

    monkeypatch.setattr(
        service, "_delete_duplicate_chapter_files_locked", change_before_staging
    )
    with pytest.raises(UnsafeLibraryPath, match="changed before retirement"):
        await assembler.assemble("manga-1", "12", preview["confirmation_snapshot"])

    assert all(path.exists() for path in paths)
    assert paths[0].read_bytes().endswith(b"external edit")
    assert all(
        database.get_chapter(f"chapter-{n}")["downloaded"] for n in ("55", "55.5", "56")
    )
    book = next(
        row
        for row in database.list_all_chapters("manga-1")
        if row["provider"] == "assembled"
    )
    assert book["downloaded"] is True
    assert book["assembled_from"]["chapter_ids"] == [
        "chapter-55",
        "chapter-55.5",
        "chapter-56",
    ]


def test_another_book_import_does_not_invalidate_this_book_preview(complete_book):
    database, service, assembler, _paths = complete_book
    preview = assembler.preview("manga-1", "12")
    database.upsert_chapters(
        "manga-1",
        [
            {
                **chapter("other-book", "", volume="13"),
                "chapter": None,
                "release_unit": "volume",
            }
        ],
    )
    database.mark_chapter_downloaded(
        "other-book", service.settings.library_dir / "other.cbz"
    )

    assert (
        assembler.preview("manga-1", "12")["confirmation_snapshot"]
        == preview["confirmation_snapshot"]
    )


async def test_another_release_in_this_book_invalidates_confirmation(complete_book):
    database, _service, assembler, paths = complete_book
    preview = assembler.preview("manga-1", "12")
    database.upsert_chapters("manga-1", [chapter("another-release", "55", volume="1")])

    with pytest.raises(AssemblyConflict, match="Preview and confirm again"):
        await assembler.assemble("manga-1", "12", preview["confirmation_snapshot"])

    assert all(path.exists() for path in paths)


async def test_source_change_during_packaging_is_rechecked_before_import(
    complete_book, monkeypatch
):
    database, _service, assembler, paths = complete_book
    preview = assembler.preview("manga-1", "12")
    original = assembler._package

    def changed_after_copy(plan, workspace):
        packaged = original(plan, workspace)
        paths[0].write_bytes(paths[0].read_bytes() + b"changed")
        return packaged

    monkeypatch.setattr(assembler, "_package", changed_after_copy)
    with pytest.raises(AssemblyConflict):
        await assembler.assemble("manga-1", "12", preview["confirmation_snapshot"])

    assert all(path.exists() for path in paths)
    assert not any(
        row["provider"] == "assembled" for row in database.list_all_chapters("manga-1")
    )


async def test_cancelled_packaging_keeps_lock_and_workspace_until_thread_finishes(
    complete_book, monkeypatch
):
    database, service, assembler, paths = complete_book
    preview = assembler.preview("manga-1", "12")
    started = threading.Event()
    finish = threading.Event()
    workspace = []
    original = assembler._package

    def slow_package(plan, directory):
        workspace.append(directory)
        started.set()
        assert finish.wait(3)
        return original(plan, directory)

    monkeypatch.setattr(assembler, "_package", slow_package)
    task = asyncio.create_task(
        assembler.assemble("manga-1", "12", preview["confirmation_snapshot"])
    )
    assert await asyncio.to_thread(started.wait, 3)
    task.cancel()
    await asyncio.sleep(0)
    try:
        assert service._mutation_lock.locked()
        assert workspace[0].exists()
    finally:
        finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not service._mutation_lock.locked()
    assert not workspace[0].exists()
    assert all(path.exists() for path in paths)
    assert not any(
        row["provider"] == "assembled" for row in database.list_all_chapters("manga-1")
    )


def use_split_map_with_whole_and_parts(database):
    # Adekan's volume 18 maps 65 beside 65.1 and 65.2: the whole was never
    # published on its own, yet the map lists it next to the parts that are.
    database.replace_chapter_map(
        "manga-1",
        "operator",
        [MapEntry(("12",), ("55", "55.1", "55.2", "56"), True, source="operator")],
    )
    with database.connect() as connection:
        connection.execute(
            "UPDATE chapter_release SET downloaded=0 WHERE id='chapter-55'"
        )


def test_a_whole_mapped_beside_its_own_parts_is_covered_by_them(complete_book):
    database, service, assembler, _paths = complete_book
    use_split_map_with_whole_and_parts(database)
    add_part(database, service, "55.1")
    add_part(database, service, "55.2")

    preview = assembler.preview("manga-1", "12")

    assert [item["source_chapter"] for item in preview["chapters"]] == [
        "55.1",
        "55.2",
        "56",
    ]
