from __future__ import annotations

from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from tankarr.app import create_app
from tankarr.config import Settings
from tankarr.naming import final_library_path
from tankarr.reader_links import komga_series_url


def manga() -> dict:
    return {
        "id": "manga-1",
        "title": "Example",
        "description": "",
        "cover_url": None,
        "authors": ["Author"],
        "original_language": "ja",
        "status": "ongoing",
        "last_volume": "1",
        "last_chapter": "1",
        "available_languages": ["en"],
        "source_url": "https://example.test/manga-1",
    }


def chapter() -> dict:
    return {
        "id": "chapter-1",
        "manga_id": "manga-1",
        "volume": "1",
        "chapter": "1",
        "title": "",
        "language": "en",
        "provider": "test",
        "groups": [],
        "publish_at": "2026-01-01T00:00:00+00:00",
        "source_url": "https://example.test/chapter-1",
        "pages": 10,
        "version": 1,
    }


def configured_settings(tmp_path) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "library",
        monitor_enabled=False,
        metadata_enabled=False,
        komga_link_enabled=True,
        komga_url="http://192.0.2.50:25600",
        komga_username="reader@example.test",
        komga_password="password",
    )


def test_enabled_komga_is_the_managed_library_and_metadata_reader(tmp_path):
    app = create_app(configured_settings(tmp_path))

    assert app.state.service.komga is app.state.komga_links
    assert app.state.metadata.komga is app.state.komga_links


def test_komga_series_url_uses_the_single_configured_url(tmp_path):
    settings = configured_settings(tmp_path)

    assert (
        komga_series_url(
            settings,
            "series-1",
        )
        == "http://192.0.2.50:25600/series/series-1"
    )
    assert (
        komga_series_url(
            settings,
            "series-1",
        )
        == "http://192.0.2.50:25600/series/series-1"
    )

    settings.komga_url = "https://reader.example.test"
    assert (
        komga_series_url(
            settings,
            "one shot",
            oneshot=True,
        )
        == "https://reader.example.test/oneshot/one%20shot"
    )


def test_reader_link_maps_the_exact_managed_book_to_komga(tmp_path):
    settings = configured_settings(tmp_path)
    app = create_app(settings)
    database = app.state.database
    manga_data = manga()
    chapter_data = chapter()
    database.upsert_manga(manga_data, "en", "none")
    database.upsert_chapters("manga-1", [chapter_data])
    output = final_library_path(settings.library_dir, manga_data, chapter_data)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(b"book")
    database.mark_chapter_downloaded("chapter-1", output)
    relative = output.relative_to(settings.library_dir).as_posix()
    app.state.komga_links.catalogue_for_paths = AsyncMock(
        return_value={
            "configured": True,
            "books": {
                relative: {
                    "id": "book-1",
                    "series_id": "series-1",
                    "url": f"/comics/{relative}",
                }
            },
            "series": {
                "series-1": {
                    "id": "series-1",
                    "name": "Example (Author)",
                    "oneshot": False,
                    "metadata": {"title": "Example"},
                }
            },
        }
    )

    with TestClient(app, base_url="http://100.64.0.10:8787") as client:
        response = client.get(
            "/api/manga/manga-1/reader-link",
            headers={"X-Forwarded-Proto": "https"},
        )

    assert response.status_code == 200
    assert response.json() == {
        "reader": "komga",
        "label": "Komga",
        "configured": True,
        "available": True,
        "url": "http://192.0.2.50:25600/series/series-1",
        "series_id": "series-1",
        "title": "Example",
        "matched_books": 1,
        "managed_books": 1,
        "books": [
            {
                "chapter_id": "chapter-1",
                "book_id": "book-1",
                "url": "http://192.0.2.50:25600/book/book-1/read",
            }
        ],
    }
    app.state.komga_links.catalogue_for_paths.assert_awaited_once_with({relative})


def test_reader_link_ignores_unrelated_komga_catalogue_entries(tmp_path):
    settings = configured_settings(tmp_path)
    app = create_app(settings)
    database = app.state.database
    manga_data = manga()
    chapter_data = chapter()
    database.upsert_manga(manga_data, "en", "none")
    database.upsert_chapters("manga-1", [chapter_data])
    output = final_library_path(settings.library_dir, manga_data, chapter_data)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(b"book")
    database.mark_chapter_downloaded("chapter-1", output)
    relative = output.relative_to(settings.library_dir).as_posix()
    app.state.komga_links.catalogue_for_paths = AsyncMock(
        return_value={
            "configured": True,
            "books": {
                relative: {"id": "book-1", "series_id": "series-1"},
                "another.cbz": {"id": "book-2", "series_id": "series-2"},
            },
            "series": {},
        }
    )

    with TestClient(app) as client:
        response = client.get("/api/manga/manga-1/reader-link")

    assert response.status_code == 200
    assert response.json()["available"] is True
    assert response.json()["series_id"] == "series-1"


def test_reader_link_fails_closed_when_managed_books_map_to_multiple_series(tmp_path):
    settings = configured_settings(tmp_path)
    app = create_app(settings)
    database = app.state.database
    manga_data = manga()
    first = chapter()
    second = {**chapter(), "id": "chapter-2", "chapter": "2"}
    database.upsert_manga(manga_data, "en", "none")
    database.upsert_chapters("manga-1", [first, second])
    paths = []
    for item in (first, second):
        output = final_library_path(settings.library_dir, manga_data, item)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(item["id"].encode())
        database.mark_chapter_downloaded(item["id"], output)
        paths.append(output.relative_to(settings.library_dir).as_posix())
    app.state.komga_links.catalogue_for_paths = AsyncMock(
        return_value={
            "configured": True,
            "books": {
                paths[0]: {"id": "book-1", "series_id": "series-1"},
                paths[1]: {"id": "book-2", "series_id": "series-2"},
            },
            "series": {},
        }
    )

    with TestClient(app) as client:
        response = client.get("/api/manga/manga-1/reader-link")

    assert response.status_code == 200
    assert response.json()["available"] is False
    assert "url" not in response.json()
    assert response.json()["reason"] == (
        "The managed books map to multiple Komga series"
    )
