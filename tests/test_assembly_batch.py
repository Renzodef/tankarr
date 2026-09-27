from __future__ import annotations

import zipfile
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient

from tankarr.app import create_app
from tankarr.archive import sha256
from tankarr.assemble import AssemblyConflict
from tankarr.assembly_batch import BookAssemblyBatch
from tankarr.chapter_map import MapEntry
from tankarr.config import Settings
from tests.test_assemble import complete_book as complete_book
from tests.test_deletion import chapter, manga, provision_library_identity


@pytest.mark.asyncio
async def test_batch_requires_preview_and_returns_completed_books(complete_book):
    database, _service, assembler, _paths = complete_book
    batch = BookAssemblyBatch(assembler)
    with pytest.raises(AssemblyConflict, match="Preview"):
        await batch.assemble("manga-1", None)
    preview = batch.preview("manga-1")
    assert [book["volume"] for book in preview["books"]] == ["12"]
    result = await batch.assemble("manga-1", preview["confirmation_snapshot"])
    assert len(result["assembled"]) == 1
    assert result["errors"] == []
    assert result["remaining"] == []
    assert not database.get_chapter("chapter-55")["downloaded"]


@pytest.mark.asyncio
async def test_batch_reports_partial_failure_without_claiming_success(
    complete_book, monkeypatch
):
    _database, _service, assembler, _paths = complete_book
    batch = BookAssemblyBatch(assembler)
    preview = batch.preview("manga-1")
    monkeypatch.setattr(
        assembler, "assemble", AsyncMock(side_effect=AssemblyConflict("file changed"))
    )
    result = await batch.assemble("manga-1", preview["confirmation_snapshot"])
    assert result == {
        "assembled": [],
        "errors": [{"volume": "12", "message": "file changed"}],
        "remaining": ["12"],
    }


@pytest.mark.asyncio
async def test_automatic_assembly_is_opt_in_and_defers_when_busy(complete_book):
    database, _service, assembler, paths = complete_book
    batch = BookAssemblyBatch(assembler)
    assert await batch.run_automatic() == {"assembled": 0, "errors": []}
    assert all(path.exists() for path in paths)
    database.update_manga("manga-1", {"assemble_books_automatically": True})
    batch.busy = lambda: True
    assert await batch.run_automatic() == {"assembled": 0, "errors": []}
    batch.busy = lambda: False
    assert await batch.run_automatic() == {"assembled": 1, "errors": []}
    assert all(not path.exists() for path in paths)


@pytest.mark.asyncio
async def test_automatic_assembly_respects_safe_mode(complete_book):
    database, service, assembler, paths = complete_book
    database.update_manga("manga-1", {"assemble_books_automatically": True})
    service.settings.restored_safe_mode = True
    assert await BookAssemblyBatch(assembler).run_automatic() == {
        "assembled": 0,
        "errors": [],
    }
    assert all(path.exists() for path in paths)


def test_api_persists_automatic_opt_in_without_starting_downloads(tmp_path):
    settings = Settings(
        data_dir=tmp_path / "data", library_dir=tmp_path / "library", reader_kind="none"
    )
    provision_library_identity(settings)
    app = create_app(settings)
    database = app.state.database
    database.upsert_manga(manga(), "en", "none")
    client = TestClient(app)
    try:
        for enabled in (True, False):
            response = client.patch(
                "/api/manga/manga-1", json={"assemble_books_automatically": enabled}
            )
            assert response.status_code == 200, response.text
            assert response.json()["assemble_books_automatically"] is enabled
            assert (
                database.get_manga("manga-1")["assemble_books_automatically"] is enabled
            )
            assert response.json()["queued"] == 0
        assert (
            client.post("/api/manga/manga-1/assemble?dry_run=true").status_code == 409
        )
        assert (
            client.post(
                "/api/manga/manga-1/volumes/1/assemble?dry_run=true"
            ).status_code
            == 409
        )
    finally:
        client.close()


@pytest.mark.asyncio
async def test_one_batch_confirmation_assembles_two_books(complete_book):
    database, service, assembler, _paths = complete_book
    row = chapter("chapter-60", "60")
    database.upsert_chapters("manga-1", [row])
    path = service.settings.library_dir / "chapters" / "60.cbz"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("1.jpg", b"chapter60-first")
        archive.writestr("2.jpg", b"chapter60-last")
    database.mark_chapter_downloaded("chapter-60", path, sha256(path))
    entries = database.chapter_map("manga-1")
    database.replace_chapter_map(
        "manga-1",
        "operator",
        [*entries, MapEntry(("13",), ("60",), True, source="operator")],
    )
    batch = BookAssemblyBatch(assembler)
    preview = batch.preview("manga-1")
    assert [book["volume"] for book in preview["books"]] == ["12", "13"]
    result = await batch.assemble("manga-1", preview["confirmation_snapshot"])
    assert [book["volume"] for book in result["assembled"]] == ["12", "13"]
    assert result["errors"] == []
    assert result["remaining"] == []


@pytest.mark.asyncio
async def test_automatic_assembly_bounds_failed_attempts(complete_book, monkeypatch):
    database, _service, assembler, _paths = complete_book
    database.update_manga("manga-1", {"assemble_books_automatically": True})
    monkeypatch.setattr(
        "tankarr.assembly_batch.load_series_units",
        lambda *_: {
            "books": [
                {"volume": str(number), "can_assemble": True} for number in range(1, 10)
            ]
        },
    )
    preview = Mock(side_effect=AssemblyConflict("file changed"))
    monkeypatch.setattr(assembler, "preview", preview)
    result = await BookAssemblyBatch(assembler).run_automatic()
    assert preview.call_count == 4
    assert len(result["errors"]) == 4
    assert result["assembled"] == 0
