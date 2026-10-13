import time
import zipfile
from io import BytesIO
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from tankarr.app import create_app
from tankarr.config import Settings
from tankarr.importer import LibraryImporter
from tankarr.metadata.service import MetadataService


@pytest.fixture
def app(tmp_path: Path):
    settings = Settings(
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "library",
        monitor_enabled=False,
        metadata_enabled=False,
        komga_link_enabled=False,
    )
    for directory in (settings.data_dir, settings.library_dir):
        directory.mkdir(parents=True, exist_ok=True)
        (directory / ".tankarr-library-id").write_text(
            "0123456789abcdef0123456789abcdef\n"
        )
    return create_app(settings)


def payload(**overrides):
    return {
        "title": "An Unlisted Work",
        "authors": ["Example Creator"],
        "original_title": "Original Work Title",
        "alternate_titles": ["Another Edition Title"],
        "original_language": "ja",
        "work_type": "Manga",
        "language": "en",
        "year": 2009,
        "expected_book_count": 1,
        "status": "ended",
        **overrides,
    }


def test_empty_manual_work_has_real_identity_and_one_missing_book(app):
    with TestClient(app, raise_server_exceptions=True) as client:
        response = client.post("/api/manga/manual", json=payload())
        assert response.status_code == 201, response.text
        manga = response.json()
        assert manga["provider"] == "local"
        assert manga["id"].startswith("local-")
        assert manga["source_id"] is None
        assert manga["monitor_mode"] == "all"
        assert manga["manual_metadata"]["original_title"] == "Original Work Title"
        assert manga["manual_metadata"]["alternate_titles"] == ["Another Edition Title"]
        assert manga["year"] == 2009
        assert manga["downloaded_count"] == 0
        assert manga["library_count"]["missing_count"] == 1
        assert manga["library_count"]["expected_count"] == 1
        assert manga["library_count"]["downloaded_count"] == 0
        assert app.state.database.list_all_chapters(manga["id"]) == []
        assert app.state.database.list_jobs(manga_id=manga["id"]) == []
        wanted = app.state.service.list_wanted()
        assert len(wanted) == 1
        assert wanted[0]["manga"]["id"] == manga["id"]
        assert len(wanted[0]["chapters"]) == 1
        refresh = client.post(f"/api/manga/{manga['id']}/refresh")
        assert refresh.status_code == 200, refresh.text
        assert app.state.database.get_manga(manga["id"])["provider"] == "local"


@pytest.mark.parametrize(
    "changes",
    [
        {"title": "   "},
        {"authors": [" "]},
        {"expected_book_count": 0},
        {"expected_book_count": True},
        {"expected_book_count": 1.5},
        {"original_language": "zz"},
        {"language": "zz"},
        {"status": "typo"},
        {"year": True},
        {"title": "x" * 201},
        {"provider": "catalogue"},
    ],
)
def test_invalid_manual_identity_is_rejected_without_creating_rows(app, changes):
    response = TestClient(app).post("/api/manga/manual", json=payload(**changes))
    assert response.status_code == 422, response.text
    assert app.state.database.list_manga() == []


def test_duplicate_identity_is_rejected_but_namesakes_remain_distinct(app):
    client = TestClient(app)
    first = client.post("/api/manga/manual", json=payload())
    assert first.status_code == 201, first.text
    duplicate = client.post(
        "/api/manga/manual",
        json=payload(
            title="  AN UNLISTED WORK  ",
            authors=["example creator"],
            expected_book_count=2,
        ),
    )
    assert duplicate.status_code == 409, duplicate.text
    namesake = client.post("/api/manga/manual", json=payload(authors=["Other Creator"]))
    assert namesake.status_code == 201, namesake.text
    assert namesake.json()["id"] != first.json()["id"]
    assert (
        app.state.database.get_manga(first.json()["id"])["expected_count_override"] == 1
    )


@pytest.mark.asyncio
async def test_later_local_import_resolves_existing_work_and_keeps_monitoring(app):
    client = TestClient(app)
    response = client.post("/api/manga/manual", json=payload())
    assert response.status_code == 201, response.text
    manga = response.json()
    imported = await app.state.service.register_local_import_series(
        {
            "id": LibraryImporter._deterministic_manga_id(
                manga["title"], manga["authors"]
            ),
            "provider": "local",
            "title": manga["title"],
            "authors": manga["authors"],
        },
        "en",
    )
    assert imported["id"] == manga["id"]
    assert imported["monitor_mode"] == "all"
    assert imported["expected_count_override"] == 1
    assert imported["manual_metadata"] == manga["manual_metadata"]
    assert len(app.state.database.list_manga()) == 1


@pytest.mark.asyncio
async def test_metadata_refresh_cannot_match_or_adopt_a_namesake(app):
    response = TestClient(app).post("/api/manga/manual", json=payload())
    assert response.status_code == 201, response.text
    manga = response.json()
    source = type(
        "Source",
        (),
        {
            "name": "mangabaka",
            "label": "MangaBaka",
            "supports_series": True,
            "supports_volumes": False,
            "automatic_matching": True,
            "configured": True,
            "status": lambda self: {},
            "search_series": AsyncMock(),
        },
    )()
    metadata = MetadataService(
        app.state.settings, app.state.database, None, {}, [source]
    )
    await metadata.enrich_series(manga["id"])
    source.search_series.assert_not_called()
    saved = app.state.database.get_manga(manga["id"])
    assert saved["title"] == manga["title"]
    assert saved["year"] == 2009
    assert saved["authors"] == ["Example Creator"]
    assert saved["expected_count_override"] == 1
    assert saved["original_language"] == "ja"
    assert saved["metadata"]["work_type"] == "Manga"
    assert saved["metadata"]["year"] == 2009
    assert "Original Work Title" in saved["metadata"]["alternate_titles"]
    # Even stale catalogue evidence must not change the chosen local identity.
    app.state.database.save_series_metadata(
        manga["id"],
        {
            "external_ids": {"mangabaka": "123"},
        },
        artwork_path=None,
        artwork_sha256=None,
        artwork_media_type=None,
        source_status=[],
    )
    assert app.state.service.adopt_catalogue_identities()["adopted"] == []
    assert app.state.database.get_manga(manga["id"])["provider"] == "local"


def test_disabled_language_profile_is_rejected(app):
    response = TestClient(app).post("/api/manga/manual", json=payload(language="fr"))
    assert response.status_code == 400, response.text
    assert app.state.database.list_manga() == []


def test_real_book_upload_fills_manual_work_without_duplicate(app):
    picture = BytesIO()
    Image.new("RGB", (64, 96), "white").save(picture, format="PNG")
    book = BytesIO()
    with zipfile.ZipFile(book, "w") as archive:
        archive.writestr("001.png", picture.getvalue())
    with TestClient(app) as client:
        added = client.post("/api/manga/manual", json=payload())
        assert added.status_code == 201, added.text
        manga_id = added.json()["id"]
        upload_id = client.post("/api/import/uploads").json()["upload_id"]
        uploaded = client.put(
            f"/api/import/uploads/{upload_id}/files",
            params={"path": "An Unlisted Work v01.cbz"},
            content=book.getvalue(),
            headers={"Content-Type": "application/vnd.comicbook+zip"},
        )
        assert uploaded.status_code == 201, uploaded.text
        scan = client.get(f"/api/import/uploads/{upload_id}/scan")
        assert scan.status_code == 200, scan.text
        group = scan.json()["groups"][0]
        group.update(
            {
                "upload_id": upload_id,
                "target_manga_id": manga_id,
                "language": "en",
                "unit": "volumes",
            }
        )
        group["items"][0].update({"volume": "1", "chapter": None})
        started = client.post("/api/import", json={"groups": [group], "language": "en"})
        assert started.status_code == 202, started.text
        for _ in range(100):
            state = client.get("/api/import/status").json()
            if state.get("finished"):
                break
            time.sleep(0.05)
        assert state["finished"] and state["errors"] == [], state
        detail = client.get(f"/api/manga/{manga_id}").json()
        assert detail["library_count"]["missing_count"] == 0
        assert detail["library_count"]["downloaded_count"] == 1
        assert detail["monitor_mode"] == "all"
        assert detail["provider"] == "local"
        assert detail["manual_metadata"] == added.json()["manual_metadata"]
        assert Path(detail["chapters"][0]["library_path"]).is_file()
        assert len(app.state.database.list_manga()) == 1
