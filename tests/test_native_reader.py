from __future__ import annotations

import zipfile
from io import BytesIO
from pathlib import Path
from threading import Event, Lock
from time import sleep
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from tankarr.app import create_app
from tankarr.config import Settings


def image_bytes(size: tuple[int, int], color: str) -> bytes:
    output = BytesIO()
    Image.new("RGB", size, color=color).save(output, format="JPEG")
    return output.getvalue()


def seed_reader(
    tmp_path: Path,
    *,
    sizes=((800, 1200), (800, 1200)),
    original_language: str | None = None,
):
    settings = Settings(
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "library",
        reader_kind="tankarr",
        monitor_enabled=False,
        metadata_enabled=False,
        suwayomi_enabled=False,
    )
    identity = "0123456789abcdef0123456789abcdef"
    settings.data_dir.mkdir(parents=True)
    settings.library_dir.mkdir(parents=True)
    (settings.data_dir / ".tankarr-library-id").write_text(identity + "\n")
    (settings.library_dir / ".tankarr-library-id").write_text(identity + "\n")
    app = create_app(settings)
    database = app.state.database
    database.upsert_manga(
        {
            "id": "series-1",
            "provider": "local",
            "title": "Reader Example",
            "description": "",
            "authors": [],
            "original_language": original_language,
            "available_languages": ["en"],
        },
        "en",
        "none",
    )
    chapters = []
    for number in (1, 2):
        chapters.append(
            {
                "id": f"book-{number}",
                "manga_id": "series-1",
                "volume": str(number),
                "chapter": None,
                "release_unit": "volume",
                "title": "",
                "language": "en",
                "provider": "local",
                "groups": [],
                "publish_at": f"2026-01-0{number}T00:00:00+00:00",
                "source_url": "",
                "pages": len(sizes),
                "version": 1,
            }
        )
    database.upsert_chapters("series-1", chapters)
    for number in (1, 2):
        path = (
            settings.library_dir / "Reader Example" / f"Reader Example - v{number}.cbz"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(path, "w") as archive:
            # Deliberately lexical-hostile names prove natural page ordering.
            archive.writestr("page-10.jpg", image_bytes(sizes[1], "blue"))
            archive.writestr("page-2.jpg", image_bytes(sizes[0], "red"))
            archive.writestr("ComicInfo.xml", "<ComicInfo />")
        database.mark_chapter_downloaded(f"book-{number}", path)
    return app, database


def test_native_reader_serves_cbz_without_saving_implicit_progress(
    tmp_path: Path, monkeypatch
):
    app, database = seed_reader(tmp_path)
    with TestClient(app) as client:
        # Opening a local book must not decode its remote release inventory.
        monkeypatch.setattr(
            database,
            "list_all_chapters",
            Mock(side_effect=AssertionError("remote inventory read")),
        )
        link = client.get("/api/manga/series-1/reader-link").json()
        manifest = client.get("/api/reader/books/book-1").json()
        first = client.get("/api/reader/books/book-1/pages/0")
        cached = client.get(
            "/api/reader/books/book-1/pages/0",
            headers={"If-None-Match": first.headers["etag"]},
        )

    assert link["reader"] == "tankarr"
    assert link["url"] == "#/reader/book-1"
    assert [item["chapter_id"] for item in link["books"]] == ["book-1", "book-2"]
    assert manifest["page_index"] == 0
    assert (
        manifest["page_version"]
        == database.get_chapter("book-1")["library_sha256"][:16]
    )
    assert manifest["display_mode"] == "manga"
    assert manifest["reading_direction"] == "rtl"
    assert manifest["next_release_id"] == "book-2"
    assert first.status_code == 200
    assert first.content == image_bytes((800, 1200), "red")
    assert cached.status_code == 304
    assert database.reader_bookmark("series-1") is None


@pytest.mark.asyncio
async def test_reader_serves_pages_concurrently_without_unbounded_decompression(
    tmp_path: Path, monkeypatch
):
    import asyncio

    import httpx

    app, _database = seed_reader(tmp_path)
    original_read = zipfile.ZipFile.read
    guard = Lock()
    active = 0
    peak = 0

    def slow_read(self, *args, **kwargs):
        nonlocal active, peak
        with guard:
            active += 1
            peak = max(peak, active)
        try:
            sleep(0.05)
            return original_read(self, *args, **kwargs)
        finally:
            with guard:
                active -= 1

    monkeypatch.setattr(zipfile.ZipFile, "read", slow_read)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        responses = await asyncio.gather(
            *(
                client.get(f"/api/reader/books/book-{book}/pages/{page}")
                for book in (1, 2)
                for page in (0, 1)
            )
        )

    assert all(response.status_code == 200 for response in responses)
    assert peak == 2


@pytest.mark.asyncio
async def test_reader_finishes_an_open_page_when_the_book_moves(tmp_path, monkeypatch):
    import asyncio

    import httpx

    app, database = seed_reader(tmp_path)
    source = Path(database.get_chapter("book-1")["library_path"])
    destination = source.with_name("moved.cbz")
    entered = Event()
    resume = Event()
    original_read = zipfile.ZipFile.read

    def paused_read(self, *args, **kwargs):
        entered.set()
        if not resume.wait(timeout=5):
            raise TimeoutError("Page read was not resumed")
        return original_read(self, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, "read", paused_read)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        request = asyncio.create_task(client.get("/api/reader/books/book-1/pages/0"))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            async with app.state.service._mutation_lock:
                source.rename(destination)
                database.mark_chapter_downloaded("book-1", destination)
        finally:
            resume.set()
        assert (await request).status_code == 200
        assert (await client.get("/api/reader/books/book-1/pages/0")).status_code == 200


def test_bookmark_is_the_only_resume_state_and_can_be_removed(tmp_path: Path):
    app, database = seed_reader(tmp_path)
    with TestClient(app) as client:
        saved = client.put(
            "/api/reader/series/series-1/bookmark",
            json={"release_id": "book-2", "page_index": 1},
        )
        link = client.get("/api/manga/series-1/reader-link").json()
        manifest = client.get("/api/reader/books/book-2").json()
        removed = client.delete("/api/reader/series/series-1/bookmark")

    assert saved.status_code == 200
    assert link["url"] == "#/reader/book-2?page=2"
    assert link["bookmark"]["label"] == "Volume 2"
    assert manifest["page_index"] == 1
    assert manifest["bookmarked"] is True
    assert removed.status_code == 204
    assert database.reader_bookmark("series-1") is None


def test_bookmarks_list_collects_series_and_tracks_removed_books(tmp_path: Path):
    app, database = seed_reader(tmp_path)
    database.upsert_manga(
        {
            "id": "series-2",
            "provider": "local",
            "title": "Another Series",
            "description": "",
            "authors": [],
            "available_languages": ["en"],
        },
        "en",
        "none",
    )
    database.upsert_chapters(
        "series-2",
        [
            {
                "id": "other-book",
                "manga_id": "series-2",
                "volume": "3",
                "chapter": None,
                "release_unit": "volume",
                "title": "",
                "language": "en",
                "provider": "local",
                "groups": [],
                "publish_at": "2026-01-03T00:00:00+00:00",
                "source_url": "",
                "pages": 2,
                "version": 1,
            }
        ],
    )
    database.mark_chapter_downloaded(
        "other-book", Path(database.get_chapter("book-1")["library_path"])
    )
    with TestClient(app) as client:
        assert client.get("/api/reader/bookmarks").json() == []
        assert (
            client.put(
                "/api/reader/series/series-1/bookmark",
                json={
                    "release_id": "book-2",
                    "page_index": 1,
                },
            ).status_code
            == 200
        )
        assert (
            client.put(
                "/api/reader/series/series-2/bookmark",
                json={
                    "release_id": "other-book",
                    "page_index": 0,
                },
            ).status_code
            == 200
        )
        bookmarks = client.get("/api/reader/bookmarks").json()
        assert {item["manga_id"] for item in bookmarks} == {"series-1", "series-2"}
        by_series = {item["manga_id"]: item for item in bookmarks}
        assert by_series["series-1"]["label"] == "Volume 2"
        assert by_series["series-1"]["url"] == "#/reader/book-2?page=2"
        assert by_series["series-2"]["manga_title"] == "Another Series"
        assert by_series["series-2"]["url"] == "#/reader/other-book?page=1"
        assert all(item["updated_at"] for item in bookmarks)

        assert client.delete("/api/reader/series/series-1/bookmark").status_code == 204
        assert [
            item["manga_id"] for item in client.get("/api/reader/bookmarks").json()
        ] == ["series-2"]
        with database.connect() as connection:
            connection.execute(
                "UPDATE chapter_release SET downloaded=0 WHERE id='other-book'"
            )
        assert client.get("/api/reader/bookmarks").json() == []


def test_reader_mode_uses_page_shape_global_and_series_precedence(tmp_path: Path):
    app, database = seed_reader(tmp_path, sizes=((600, 2400), (600, 2600)))
    with TestClient(app) as client:
        automatic = client.get("/api/reader/books/book-1").json()
        app.state.settings.reader_display_mode = "manga"
        global_choice = client.get("/api/reader/books/book-1").json()
        database.update_manga("series-1", {"reader_mode_override": "webtoon"})
        series_choice = client.get("/api/reader/books/book-1").json()

    assert (automatic["display_mode"], automatic["display_mode_source"]) == (
        "webtoon",
        "page_shape",
    )
    assert (global_choice["display_mode"], global_choice["display_mode_source"]) == (
        "manga",
        "global",
    )
    assert (series_choice["display_mode"], series_choice["display_mode_source"]) == (
        "webtoon",
        "series",
    )


def test_chinese_books_read_left_to_right_by_default(tmp_path: Path):
    app, _database = seed_reader(tmp_path, original_language="zh")
    with TestClient(app) as client:
        manifest = client.get("/api/reader/books/book-1").json()

    assert manifest["display_mode"] == "manga"
    assert manifest["reading_direction"] == "ltr"


def test_series_can_override_paged_reading_direction_and_reset_it(tmp_path: Path):
    app, database = seed_reader(tmp_path, original_language="ja")
    with TestClient(app) as client:
        automatic = client.get("/api/reader/books/book-1").json()
        updated = client.patch("/api/manga/series-1", json={"reader_direction": "ltr"})
        left_to_right = client.get("/api/reader/books/book-1").json()
        reset = client.patch(
            "/api/manga/series-1", json={"reader_direction": "automatic"}
        )
        restored = client.get("/api/reader/books/book-1").json()

    assert updated.status_code == reset.status_code == 200
    assert automatic["reading_direction"] == "rtl"
    assert left_to_right["reading_direction"] == "ltr"
    assert restored["reading_direction"] == "rtl"
    assert database.get_manga("series-1")["reader_direction_override"] is None


def test_manhua_catalogue_family_does_not_force_vertical_reader(tmp_path: Path):
    app, database = seed_reader(tmp_path, original_language="zh")
    database.save_series_metadata(
        "series-1",
        {
            "content_kind": "webtoon",
            "classification": {"kind": "webtoon", "subtype": "Manhua"},
            "reading_direction": "LEFT_TO_RIGHT",
        },
        artwork_path=None,
        artwork_sha256=None,
        artwork_media_type=None,
        source_status=[],
    )
    with TestClient(app) as client:
        paged = client.get("/api/reader/books/book-1").json()
        metadata = database.get_series_metadata("series-1") or {}
        explicit = dict(metadata.get("data") or {})
        explicit["official_links"] = [
            {"url": "https://www.webtoons.com/en/example/list"}
        ]
        database.save_series_metadata(
            "series-1",
            explicit,
            artwork_path=None,
            artwork_sha256=None,
            artwork_media_type=None,
            source_status=[],
        )
        vertical = client.get("/api/reader/books/book-1").json()

    assert (paged["display_mode"], paged["reading_direction"]) == ("manga", "ltr")
    assert (vertical["display_mode"], vertical["reading_direction"]) == (
        "webtoon",
        "ltr",
    )


def test_reader_modes_can_be_saved_globally_and_per_series(tmp_path: Path):
    app, database = seed_reader(tmp_path)
    with TestClient(app) as client:
        global_update = client.put(
            "/api/settings", json={"reader_display_mode": "webtoon"}
        )
        series_update = client.patch(
            "/api/manga/series-1", json={"reader_mode": "manga"}
        )

    assert global_update.status_code == 200
    assert app.state.settings.reader_display_mode == "webtoon"
    assert series_update.status_code == 200
    assert database.get_manga("series-1")["reader_mode_override"] == "manga"


def test_reader_rejects_paths_outside_the_managed_library(tmp_path: Path):
    app, database = seed_reader(tmp_path)
    outside = tmp_path / "outside.cbz"
    with zipfile.ZipFile(outside, "w") as archive:
        archive.writestr("page.jpg", image_bytes((10, 10), "black"))
    database.mark_chapter_downloaded("book-1", outside)

    with TestClient(app) as client:
        response = client.get("/api/reader/books/book-1")

    assert response.status_code == 403
    assert response.json()["detail"] == "Unsafe library path"


@pytest.mark.asyncio
async def test_native_reader_reconciliation_does_not_inventory_the_library(
    tmp_path: Path,
):
    app, _database = seed_reader(tmp_path)
    inventory = Mock(side_effect=AssertionError("library inventory was requested"))
    app.state.service._tracked_library_relative_paths = inventory

    result = await app.state.service.reconcile_komga_library()

    assert result == {
        "configured": True,
        "reader_independent": True,
        "ready": True,
        "triggered": False,
    }
    inventory.assert_not_called()


def test_bookmark_survives_reading_and_moves_only_on_an_explicit_save(tmp_path: Path):
    app, database = seed_reader(tmp_path)
    with TestClient(app) as client:
        assert (
            client.put(
                "/api/reader/series/series-1/bookmark",
                json={"release_id": "book-1", "page_index": 1},
            ).status_code
            == 200
        )
        saved = database.reader_bookmark("series-1")
        for book in ("book-1", "book-2"):
            assert client.get(f"/api/reader/books/{book}").status_code == 200
            assert client.get(f"/api/reader/books/{book}/pages/1").status_code == 200
        assert database.reader_bookmark("series-1") == saved
        assert (
            client.put(
                "/api/reader/series/series-1/bookmark",
                json={"release_id": "book-2", "page_index": 0},
            ).status_code
            == 200
        )
        assert client.get("/api/reader/books/book-1").json()["bookmarked"] is False
        link = client.get("/api/manga/series-1/reader-link").json()
        assert link["bookmark"] == {
            "chapter_id": "book-2",
            "page_index": 0,
            "label": "Volume 2",
            "url": "#/reader/book-2?page=1",
        }
        assert client.delete("/api/reader/series/series-1/bookmark").status_code == 204
        assert client.get("/api/manga/series-1/reader-link").json()["bookmark"] is None
        assert client.get("/api/reader/books/book-2").json()["page_index"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["manifest", "page", "bookmark"])
async def test_reader_waits_for_atomic_library_relocation(tmp_path, operation):
    import asyncio

    import httpx

    app, database = seed_reader(tmp_path)
    original = Path(database.get_chapter("book-1")["library_path"])
    destination = original.with_name("organized.cbz")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        async with app.state.service._mutation_lock:
            original.rename(destination)
            if operation == "bookmark":
                request = client.put(
                    "/api/reader/series/series-1/bookmark",
                    json={"release_id": "book-1", "page_index": 1},
                )
            else:
                suffix = "/pages/0" if operation == "page" else ""
                request = client.get("/api/reader/books/book-1" + suffix)
            task = asyncio.create_task(request)
            await asyncio.sleep(0.03)
            assert not task.done()
            database.mark_chapter_downloaded("book-1", destination)
        response = await task
    assert response.status_code == 200
    if operation != "bookmark":
        assert database.reader_bookmark("series-1") is None


def test_partial_volume_tags_do_not_reorder_chapter_reader(tmp_path):
    from tankarr.native_reader import native_reader_link

    app, database = seed_reader(tmp_path)
    with database.connect() as connection:
        for number, chapter_number, volume in [(1, "1", None), (2, "15", "3")]:
            connection.execute(
                "UPDATE chapter_release SET release_unit='chapter', chapter=?, "
                "canonical_chapter=?, source_chapter=?, volume=? WHERE id=?",
                (
                    chapter_number,
                    chapter_number,
                    chapter_number,
                    volume,
                    f"book-{number}",
                ),
            )
    assert native_reader_link(database, "series-1")["url"] == "#/reader/book-1"
    with TestClient(app) as client:
        manifest = client.get("/api/reader/books/book-1").json()
    assert manifest["next_release_id"] == "book-2"
