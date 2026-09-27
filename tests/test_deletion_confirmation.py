from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tankarr.app import create_app
from tankarr.config import Settings
from tankarr.database import Database
from tankarr.naming import final_library_path
from tankarr.service import StaleMangaDeletionError, TankarrService


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
        "last_chapter": "2",
        "available_languages": ["en", "it"],
        "source_url": "https://example.test/manga-1",
    }


def chapter(
    chapter_id: str,
    number: str,
    language: str,
    *,
    downloaded: bool = False,
) -> dict:
    del downloaded
    return {
        "id": chapter_id,
        "chapter": number,
        "volume": "1",
        "title": f"Chapter {number}",
        "language": language,
        "provider": "mangadex",
        "groups": [],
        "publish_at": "2026-01-01T00:00:00Z",
        "source_url": f"https://example.test/{chapter_id}",
        "pages": 20,
        "version": 1,
    }


class UnusedProvider:
    pass


class ScanOnlyKomga:
    async def scan(self, _expected_relative_paths=()) -> dict:
        return {"configured": True, "triggered": True, "library_id": "manga"}


def provision(settings: Settings) -> None:
    identity = "0123456789abcdef0123456789abcdef"
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.library_dir.mkdir(parents=True, exist_ok=True)
    (settings.data_dir / ".tankarr-library-id").write_text(
        f"{identity}\n", encoding="utf-8"
    )
    (settings.library_dir / ".tankarr-library-id").write_text(
        f"{identity}\n", encoding="utf-8"
    )


def make_service(tmp_path: Path) -> tuple[Settings, Database, TankarrService]:
    settings = Settings(
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "library",
        monitor_enabled=False,
    )
    provision(settings)
    database = Database(settings.database_path)
    database.initialize()
    service = TankarrService(
        settings,
        database,
        UnusedProvider(),  # type: ignore[arg-type]
        ScanOnlyKomga(),  # type: ignore[arg-type]
    )
    return settings, database, service


def add_downloaded(
    settings: Settings,
    database: Database,
    manga_data: dict,
    chapter_data: dict,
) -> Path:
    output = final_library_path(settings.library_dir, manga_data, chapter_data)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(chapter_data["id"].encode())
    database.mark_chapter_downloaded(chapter_data["id"], output)
    return output


@pytest.mark.asyncio
async def test_series_delete_preview_counts_every_language_and_file(tmp_path: Path):
    settings, database, service = make_service(tmp_path)
    manga_data = manga()
    chapters = [
        chapter("en-1", "1", "en"),
        chapter("en-1-alt", "1", "en"),
        chapter("en-2", "2", "en"),
        chapter("it-1", "1", "it"),
    ]
    database.upsert_manga(manga_data, "en", "none")
    database.upsert_chapters("manga-1", chapters)
    english_path = add_downloaded(settings, database, manga_data, chapters[0])
    italian_path = add_downloaded(settings, database, manga_data, chapters[3])

    preview = await service.preview_manga_deletion("manga-1")

    assert preview["chapters"] == 3
    assert preview["chapter_releases"] == 4
    assert preview["downloaded_chapters"] == 2
    assert preview["files"] == 2
    assert preview["existing_files"] == 2
    assert preview["missing_files"] == 0
    assert len(preview["snapshot"]) == 64
    assert preview["languages"] == [
        {
            "language": "en",
            "chapters": 2,
            "chapter_releases": 3,
            "downloaded_chapters": 1,
            "files": 1,
            "existing_files": 1,
            "missing_files": 0,
        },
        {
            "language": "it",
            "chapters": 1,
            "chapter_releases": 1,
            "downloaded_chapters": 1,
            "files": 1,
            "existing_files": 1,
            "missing_files": 0,
        },
    ]
    assert english_path.exists()
    assert italian_path.exists()


@pytest.mark.asyncio
async def test_series_delete_preview_does_not_wait_for_long_mutation(tmp_path: Path):
    settings, database, service = make_service(tmp_path)
    manga_data = manga()
    first = chapter("en-1", "1", "en")
    database.upsert_manga(manga_data, "en", "none")
    database.upsert_chapters("manga-1", [first])
    add_downloaded(settings, database, manga_data, first)

    await service._mutation_lock.acquire()
    try:
        preview = await asyncio.wait_for(
            service.preview_manga_deletion("manga-1"), timeout=1
        )
    finally:
        service._mutation_lock.release()

    assert preview["manga_id"] == "manga-1"
    assert preview["existing_files"] == 1


@pytest.mark.asyncio
async def test_series_file_delete_requires_fresh_confirmation_snapshot(
    tmp_path: Path,
):
    settings, database, service = make_service(tmp_path)
    manga_data = manga()
    first = chapter("en-1", "1", "en")
    database.upsert_manga(manga_data, "en", "none")
    database.upsert_chapters("manga-1", [first])
    first_path = add_downloaded(settings, database, manga_data, first)
    preview = await service.preview_manga_deletion("manga-1")

    with pytest.raises(StaleMangaDeletionError, match="fresh server preview"):
        await service.delete_manga("manga-1", delete_files=True)

    second = chapter("en-2", "2", "en")
    database.upsert_chapters("manga-1", [second])
    with pytest.raises(StaleMangaDeletionError, match="changed since confirmation"):
        await service.delete_manga(
            "manga-1",
            delete_files=True,
            confirmation_snapshot=preview["snapshot"],
        )

    assert first_path.exists()
    assert database.get_manga("manga-1")["id"] == "manga-1"

    refreshed = await service.preview_manga_deletion("manga-1")
    first_path.write_bytes(b"changed after confirmation")
    with pytest.raises(StaleMangaDeletionError, match="changed since confirmation"):
        await service.delete_manga(
            "manga-1",
            delete_files=True,
            confirmation_snapshot=refreshed["snapshot"],
        )

    refreshed = await service.preview_manga_deletion("manga-1")
    result = await service.delete_manga(
        "manga-1",
        delete_files=True,
        confirmation_snapshot=refreshed["snapshot"],
    )

    assert result["deleted"] is True
    assert result["files_deleted"] == 1
    assert not first_path.exists()
    with pytest.raises(KeyError):
        database.get_manga("manga-1")


def test_delete_preview_endpoint_and_conditional_confirmation(tmp_path: Path):
    settings = Settings(
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "library",
        monitor_enabled=False,
    )
    provision(settings)
    app = create_app(settings)
    database: Database = app.state.database
    manga_data = manga()
    first = chapter("en-1", "1", "en")
    database.upsert_manga(manga_data, "en", "none")
    database.upsert_chapters("manga-1", [first])
    output = add_downloaded(settings, database, manga_data, first)

    with TestClient(app) as client:
        preview_response = client.get("/api/manga/manga-1/delete-preview")
        assert preview_response.status_code == 200
        preview = preview_response.json()
        assert preview["existing_files"] == 1

        rejected = client.delete(
            "/api/manga/manga-1", params={"delete_files": "true", "background": "false"}
        )
        assert rejected.status_code == 409
        assert "confirmation snapshot" in rejected.json()["detail"]
        assert output.exists()

        deleted = client.delete(
            "/api/manga/manga-1",
            params={
                "delete_files": "true",
                "confirmation_snapshot": preview["snapshot"],
            },
        )
        assert deleted.status_code == 200
        assert deleted.json()["deleted"] is True

    assert not output.exists()
