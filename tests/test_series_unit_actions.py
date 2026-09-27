from __future__ import annotations

import asyncio
import json
from threading import Event

import pytest
from fastapi.testclient import TestClient

from tankarr.app import create_app
from tankarr.chapter_map import MapEntry
from tankarr.config import Settings
from tankarr.naming import final_library_path
from tests.test_deletion import (
    add_downloaded_chapter,
    chapter,
    make_service,
    manga,
    provision_library_identity,
)


@pytest.fixture
def series_client(tmp_path):
    settings = Settings(
        data_dir=tmp_path / "data", library_dir=tmp_path / "library", reader_kind="none"
    )
    provision_library_identity(settings)
    app = create_app(settings)
    database = app.state.database
    paths = {}
    for number in ("1", "2"):
        paths[f"c{number}"], _ = add_downloaded_chapter(
            database, settings.library_dir, manga(), chapter(f"c{number}", number)
        )
        release = {
            **chapter(f"v{number}", "", volume=number),
            "chapter": None,
            "release_unit": "volume",
            "provider": "prowlarr",
        }
        path = final_library_path(settings.library_dir, manga(), release)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"cbz")
        database.publish_external_chapter("manga-1", release, path, "a" * 64, "b" * 64)
        paths[f"v{number}"] = path
    database.replace_chapter_map(
        "manga-1",
        "operator",
        [
            MapEntry(
                volumes=(number,), chapters=(number,), exact=True, source="operator"
            )
            for number in ("1", "2")
        ],
    )
    client = TestClient(app)
    try:
        yield client, database, settings, paths
    finally:
        client.close()


def test_units_and_duplicate_preview_are_book_scoped(series_client):
    client, _database, _settings, _paths = series_client
    result = client.get("/api/manga/manga-1/units")
    assert result.status_code == 200
    assert result.json()["mode"] == "grouped"
    assert [book["duplicate_release_ids"] for book in result.json()["books"]] == [
        ["c1"],
        ["c2"],
    ]
    preview = client.get(
        "/api/manga/manga-1/duplicates", params={"volume": "01"}
    ).json()
    assert [chapter["id"] for chapter in preview["chapters"]] == ["c1"]
    assert client.get("/api/manga/unknown/units").status_code == 404


def test_group_retirement_recycles_only_confirmed_book_files(series_client):
    client, database, settings, paths = series_client
    result = client.delete(
        "/api/manga/manga-1/duplicates/files",
        params={"volume": "1", "recycle": "true", "expected_chapter_id": "c1"},
    )
    assert result.status_code == 200, result.text
    assert result.json()["files_retired"] == 1
    assert result.json()["files_deleted"] == 0
    assert not paths["c1"].exists()
    assert all(paths[key].exists() for key in ("c2", "v1", "v2"))
    assert database.get_chapter("c1")["downloaded"] is False
    directory = next(settings.library_dir.glob(".tankarr-delete-*"))
    assert (
        json.loads((directory / "manifest.json").read_text())["disposition"] == "retain"
    )


@pytest.mark.parametrize(
    "parameters",
    [
        {"volume": "1", "recycle": "true"},
        {"volume": "1", "recycle": "true", "expected_chapter_id": "c2"},
        {"volume": "2", "recycle": "true", "expected_chapter_id": "c1"},
    ],
)
def test_group_retirement_refuses_missing_or_changed_confirmation(
    series_client, parameters
):
    client, _database, _settings, paths = series_client
    result = client.delete("/api/manga/manga-1/duplicates/files", params=parameters)
    assert result.status_code == 409
    assert all(path.exists() for path in paths.values())


@pytest.mark.asyncio
async def test_cancelled_retirement_holds_lock_until_file_thread_finishes(
    tmp_path, monkeypatch
):
    _database, service, _reader = make_service(tmp_path)
    entered, release = Event(), Event()

    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return {}

    monkeypatch.setattr(service, "_delete_duplicate_chapter_files_locked", blocked)
    task = asyncio.create_task(
        service.delete_duplicate_chapter_files("manga-1", recycle=True)
    )
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        assert service._mutation_lock.locked()
        assert not task.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not service._mutation_lock.locked()
