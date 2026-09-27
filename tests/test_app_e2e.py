from __future__ import annotations

import time
import zipfile
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import respx
from fastapi.testclient import TestClient
from httpx import Response
from PIL import Image

from tankarr.app import create_app
from tankarr.archive import validate_cbz
from tankarr.config import Settings
from tankarr.naming import final_library_path

MANGA_ID = "11111111-1111-1111-1111-111111111111"
CHAPTER_ID = "22222222-2222-2222-2222-222222222222"


def provision_library_identity(settings: Settings) -> None:
    identity = "0123456789abcdef0123456789abcdef"
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.library_dir.mkdir(parents=True, exist_ok=True)
    (settings.data_dir / ".tankarr-library-id").write_text(
        f"{identity}\n", encoding="utf-8"
    )
    (settings.library_dir / ".tankarr-library-id").write_text(
        f"{identity}\n", encoding="utf-8"
    )


def jpeg_bytes(color: str) -> bytes:
    output = BytesIO()
    Image.new("RGB", (1000, 1500), color=color).save(output, format="JPEG")
    return output.getvalue()


def png_bytes(size: tuple[int, int], color: str) -> bytes:
    output = BytesIO()
    Image.new("RGB", size, color=color).save(output, format="PNG")
    return output.getvalue()


def test_artwork_candidate_api_persists_choice_and_serves_versioned_image(
    tmp_path: Path,
):
    settings = Settings(
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "library",
        monitor_enabled=False,
        metadata_enabled=False,
        komga_link_enabled=False,
    )
    provision_library_identity(settings)
    app = create_app(settings)
    database = app.state.database
    manga_id = "artwork-choice"
    database.upsert_manga(
        {
            "id": manga_id,
            "provider": "local",
            "title": "Artwork Choice",
            "description": "",
            "authors": [],
            "original_language": "ja",
            "status": "completed",
            "available_languages": ["en"],
        },
        "en",
        "none",
    )
    candidates = []
    for candidate_id, source, color in (
        ("automatic-cover", "myanimelist", "blue"),
        ("manual-cover", "mangaupdates", "green"),
    ):
        content = jpeg_bytes(color)
        relative = Path("metadata/artwork") / manga_id / f"{candidate_id}.jpg"
        path = settings.data_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        candidates.append(
            {
                "candidate_id": candidate_id,
                "source": source,
                "source_url": f"https://example.test/{candidate_id}",
                "path": relative.as_posix(),
                "sha256": sha256(content).hexdigest(),
                "media_type": "image/jpeg",
                "source_width": 1000,
                "source_height": 1500,
                "score": 100 if candidate_id == "automatic-cover" else 90,
                "source_priority": 34 if candidate_id == "automatic-cover" else 30,
            }
        )
    database.replace_series_artwork_candidates(
        manga_id, candidates, automatic_candidate_id="automatic-cover"
    )
    automatic = candidates[0]
    database.save_series_metadata(
        manga_id,
        {
            "title": "Artwork Choice",
            "authors": [],
            "artwork_source": automatic["source"],
            "artwork_candidate_id": automatic["candidate_id"],
            "artwork_automatic_candidate_id": automatic["candidate_id"],
            "artwork_selection_mode": "automatic",
        },
        artwork_path=str(automatic["path"]),
        artwork_sha256=str(automatic["sha256"]),
        artwork_media_type="image/jpeg",
        source_status=[],
    )

    with TestClient(app) as client:
        choices = client.get(f"/api/manga/{manga_id}/artwork-candidates").json()
        assert choices["selection_mode"] == "automatic"
        assert len(choices["candidates"]) == 2
        image = client.get(choices["candidates"][0]["image_url"])
        assert image.status_code == 200
        assert image.headers["cache-control"].endswith("immutable")

        digest = str(automatic["sha256"])
        thumbnail = client.get(
            f"/api/metadata/artwork/{manga_id}/series/thumbnail/320?v={digest}"
        )
        assert thumbnail.status_code == 200
        assert thumbnail.headers["content-type"].startswith("image/webp")
        assert thumbnail.headers["cache-control"].endswith("immutable")
        assert thumbnail.headers["etag"].endswith('thumb-w320-v1"')
        with Image.open(BytesIO(thumbnail.content)) as resized:
            assert resized.format == "WEBP"
            assert resized.size == (320, 480)
        assert (
            client.get(
                f"/api/metadata/artwork/{manga_id}/series/thumbnail/123?v={digest}"
            ).status_code
            == 404
        )

        selected = client.put(
            f"/api/manga/{manga_id}/artwork-selection",
            json={"candidate_id": "manual-cover"},
        )

    assert selected.status_code == 200
    assert selected.json()["selection"]["selection_mode"] == "manual"
    metadata = database.get_series_metadata(manga_id)
    assert metadata is not None
    assert metadata["data"]["artwork_candidate_id"] == "manual-cover"
    assert metadata["artwork_sha256"] == candidates[1]["sha256"]


def test_artwork_upload_is_normalized_selected_and_served(tmp_path: Path):
    settings = Settings(
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "library",
        monitor_enabled=False,
        metadata_enabled=False,
        komga_link_enabled=False,
    )
    provision_library_identity(settings)
    app = create_app(settings)
    database = app.state.database
    manga_id = "artwork-upload"
    database.upsert_manga(
        {
            "id": manga_id,
            "provider": "local",
            "title": "Artwork Upload",
            "description": "",
            "authors": [],
            "original_language": "ja",
            "status": "completed",
            "available_languages": ["en"],
        },
        "en",
        "none",
    )
    upload = png_bytes((640, 360), "purple")

    with TestClient(app) as client:
        response = client.post(
            f"/api/manga/{manga_id}/artwork-upload",
            content=upload,
            headers={"Content-Type": "image/png"},
        )
        assert response.status_code == 200
        result = response.json()
        assert result["selection"]["selection_mode"] == "manual"
        candidate_id = result["selection"]["selected_candidate_id"]
        assert candidate_id.startswith("upload-")
        candidate = next(
            item
            for item in result["selection"]["candidates"]
            if item["candidate_id"] == candidate_id
        )
        assert candidate["source"] == "upload"
        assert candidate["source_width"] == 640
        assert candidate["source_height"] == 360
        assert candidate["normalized_width"] == 1000
        assert candidate["normalized_height"] == 1500

        image = client.get(candidate["image_url"])
        assert image.status_code == 200
        assert image.headers["content-type"].startswith("image/jpeg")
        assert image.headers["cache-control"].endswith("immutable")
        with Image.open(BytesIO(image.content)) as normalized:
            assert normalized.format == "JPEG"
            assert normalized.mode == "RGB"
            assert normalized.size == (1000, 1500)

    metadata = database.get_series_metadata(manga_id)
    assert metadata is not None
    assert metadata["data"]["artwork_source"] == "upload"
    assert metadata["data"]["artwork_candidate_id"] == candidate_id
    assert database.get_series_artwork_preference(manga_id) == candidate_id


def test_artwork_hash_resolver_is_shared_by_library_series_and_volumes(
    tmp_path: Path,
):
    settings = Settings(
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "library",
        monitor_enabled=False,
        metadata_enabled=False,
    )
    provision_library_identity(settings)
    app = create_app(settings)
    database = app.state.database
    manga_id = "artwork-series"
    database.upsert_manga(
        {
            "id": manga_id,
            "provider": "local",
            "title": "Artwork Series",
            "description": "",
            "authors": [],
            "status": "completed",
            "available_languages": ["en"],
        },
        "en",
        "none",
    )
    series_relative = Path("metadata/artwork/artwork-series/series.jpg")
    series_path = settings.data_dir / series_relative
    series_path.parent.mkdir(parents=True, exist_ok=True)
    series_content = b"canonical-series-artwork-v1"
    series_path.write_bytes(series_content)
    series_digest = sha256(series_content).hexdigest()
    database.save_series_metadata(
        manga_id,
        {
            "title": "Artwork Series",
            "authors": [],
            "alternate_titles": ["Artwork Alias"],
            "description": "Detail-only metadata",
            "work": {"source_payload": "not needed by Library cards"},
            "cover_url": f"/api/metadata/artwork/{manga_id}/series",
        },
        artwork_path=str(series_relative),
        artwork_sha256=series_digest,
        artwork_media_type="image/jpeg",
        source_status=[],
    )
    volume_relative = Path("metadata/artwork/artwork-series/volume-1.jpg")
    volume_path = settings.data_dir / volume_relative
    volume_content = b"canonical-volume-artwork-v1"
    volume_path.write_bytes(volume_content)
    volume_digest = sha256(volume_content).hexdigest()
    database.save_volume_metadata(
        manga_id,
        "1",
        {
            "number": "1",
            "title": "Volume 1",
            "cover_url": f"/api/metadata/artwork/{manga_id}/volumes/1",
        },
        artwork_path=str(volume_relative),
        artwork_sha256=volume_digest,
        artwork_media_type="image/jpeg",
    )

    expected_series_url = f"/api/metadata/artwork/{manga_id}/series?v={series_digest}"
    expected_volume_url = (
        f"/api/metadata/artwork/{manga_id}/volumes/1?v={volume_digest}"
    )
    with TestClient(app) as client:
        library_record = next(
            item for item in client.get("/api/manga").json() if item["id"] == manga_id
        )
        series_record = client.get(f"/api/manga/{manga_id}").json()

        assert library_record["artwork_url"] == expected_series_url
        assert series_record["artwork_url"] == expected_series_url
        assert library_record["metadata"]["cover_url"] == expected_series_url
        assert library_record["metadata"]["alternate_titles"] == ["Artwork Alias"]
        assert "description" not in library_record["metadata"]
        assert "work" not in library_record["metadata"]
        assert series_record["metadata"]["cover_url"] == expected_series_url
        assert series_record["metadata"]["description"] == "Detail-only metadata"
        assert series_record["artwork_sha256"] == series_digest
        assert series_record["volume_metadata"][0]["artwork_url"] == expected_volume_url

        series_artwork = client.get(expected_series_url)
        assert series_artwork.content == series_content
        assert (
            series_artwork.headers["cache-control"]
            == "public, max-age=31536000, immutable"
        )
        assert series_artwork.headers["etag"] == f'"sha256-{series_digest}"'
        assert (
            client.get(f"/api/metadata/artwork/{manga_id}/series").headers[
                "cache-control"
            ]
            == "no-cache, max-age=0"
        )

        changed_content = b"canonical-series-artwork-v2"
        changed_digest = sha256(changed_content).hexdigest()
        series_path.write_bytes(changed_content)
        database.save_series_metadata(
            manga_id,
            {
                "title": "Artwork Series",
                "authors": [],
                "cover_url": expected_series_url,
            },
            artwork_path=str(series_relative),
            artwork_sha256=changed_digest,
            artwork_media_type="image/jpeg",
            source_status=[],
        )
        changed_url = client.get(f"/api/manga/{manga_id}").json()["artwork_url"]
        assert changed_url == (
            f"/api/metadata/artwork/{manga_id}/series?v={changed_digest}"
        )
        changed_library_url = next(
            item
            for item in client.get("/api/manga?fresh=true").json()
            if item["id"] == manga_id
        )["artwork_url"]
        assert changed_library_url == changed_url
        assert changed_url != expected_series_url
        assert client.get(changed_url).content == changed_content


def test_series_details_expose_expected_slots_without_provider_rows(tmp_path: Path):
    settings = Settings(
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "library",
        monitor_enabled=False,
        metadata_enabled=False,
    )
    provision_library_identity(settings)
    app = create_app(settings)
    database = app.state.database
    database.upsert_manga(
        {
            "id": "ended-work",
            "provider": "mangadex",
            "title": "Ended Work",
            "description": "",
            "authors": [],
            "status": "completed",
            "last_chapter": "3",
            "available_languages": ["en"],
        },
        "en",
        "all",
    )
    database.upsert_chapters(
        "ended-work",
        [
            {
                "id": f"ended-{number}",
                "provider": "mangadex",
                "volume": "1",
                "chapter": str(number),
                "title": "",
                "language": "en",
                "groups": [],
                "publish_at": None,
                "source_url": f"https://example.test/{number}",
            }
            for number in (1, 3)
        ],
    )
    for number in (1, 3):
        database.mark_chapter_downloaded(
            f"ended-{number}",
            tmp_path / "library" / f"ended-{number}.cbz",
            str(number) * 64,
        )

    with TestClient(app) as client:
        response = client.get("/api/manga/ended-work")

    assert response.status_code == 200
    index = response.json()["chapter_index"]
    missing = next(slot for slot in index["slots"] if slot["key"] == "chapter:2")
    assert missing == {
        "key": "chapter:2",
        "volume": None,
        "chapter": "2",
        "expected": True,
        "special": False,
        "evidence": "provider_final",
        "volume_inferred": False,
        "split_parts": [],
        "releases": [],
        "available": False,
        "downloaded": False,
        "monitored": True,
        "covered_by_volume": None,
        "covered_by_chapters": False,
        "coverage_exact": False,
        "covered_unmapped": False,
        "duplicate_of_volume": None,
        "volume_monitor_state": "automatic",
        "ignored": False,
        "queue_status": None,
        "searchable": True,
    }


def test_automatic_chapter_search_queues_only_the_requested_slot(tmp_path: Path):
    settings = Settings(
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "library",
        monitor_enabled=False,
        metadata_enabled=False,
    )
    provision_library_identity(settings)
    app = create_app(settings)
    database = app.state.database
    database.upsert_manga(
        {
            "id": "automatic-search",
            "provider": "local",
            "title": "Automatic Search",
            "description": "",
            "authors": [],
            "status": "ongoing",
            "available_languages": ["en"],
        },
        "en",
        "all",
    )
    database.upsert_chapters(
        "automatic-search",
        [
            {
                "id": f"automatic-{number}",
                "provider": "local",
                "volume": None,
                "chapter": str(number),
                "title": "",
                "language": "en",
                "groups": [],
                "publish_at": None,
                "source_url": f"https://example.test/{number}",
            }
            for number in (1, 2)
        ],
    )
    app.state.worker.enqueue = AsyncMock()

    with TestClient(app) as client:
        response = client.post(
            "/api/manga/automatic-search/chapters/search/automatic",
            json={"chapter": "2", "volume": None},
        )
        repeated = client.post(
            "/api/manga/automatic-search/chapters/search/automatic",
            json={"chapter": "2", "volume": None},
        )

    assert response.status_code == 200
    assert response.json()["state"] == "queued"
    assert response.json()["queued"] == 1
    assert repeated.status_code == 200
    assert repeated.json()["state"] == "already_queued"
    jobs = database.list_jobs(limit=10)
    assert [job["chapter_id"] for job in jobs] == ["automatic-2"]
    app.state.worker.enqueue.assert_awaited_once()


def test_wanted_compact_snapshot_paints_then_refreshes_queue_state(tmp_path: Path):
    settings = Settings(
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "library",
        monitor_enabled=False,
        metadata_enabled=False,
        komga_link_enabled=False,
    )
    provision_library_identity(settings)
    app = create_app(settings)
    database = app.state.database
    database.upsert_manga(
        {
            "id": "wanted-snapshot",
            "provider": "local",
            "title": "Wanted Snapshot",
            "description": "",
            "authors": [],
            "status": "ongoing",
            "available_languages": ["en"],
        },
        "en",
        "all",
    )
    database.upsert_chapters(
        "wanted-snapshot",
        [
            {
                "id": "wanted-snapshot-1",
                "provider": "local",
                "source_name": "Local Archive",
                "volume": "1",
                "chapter": "1",
                "title": "Snapshot Chapter",
                "language": "en",
                "groups": [],
                "publish_at": "2026-01-01T00:00:00+00:00",
                "source_url": "https://example.test/wanted-snapshot-1",
            }
        ],
    )

    with TestClient(app) as client:
        initial = client.get("/api/wanted?compact=true&fresh=true")
        assert initial.status_code == 200
        assert initial.headers["cache-control"] == "private, no-cache"
        unchanged = client.get(
            "/api/wanted?compact=true&fresh=true",
            headers={"If-None-Match": initial.headers["etag"]},
        )
        assert unchanged.status_code == 304
        initial_chapter = initial.json()[0]["chapters"][0]
        assert initial_chapter["id"] == "wanted-snapshot-1"
        assert initial_chapter["title"] == "Snapshot Chapter"
        assert initial_chapter["provider"] == "local"
        assert initial_chapter["source_name"] == "Local Archive"
        assert initial_chapter["publish_at"] == "2026-01-01T00:00:00+00:00"
        assert "source_url" not in initial_chapter

        job = database.create_job("wanted-snapshot", "wanted-snapshot-1", "en")
        database.update_job(job["id"], status="packaging")

        painted = client.get("/api/wanted?compact=true")
        assert painted.json()[0]["chapters"][0].get("queue_status") is None

        refreshed = client.get("/api/wanted?compact=true&fresh=true")
        assert refreshed.status_code == 200
        assert refreshed.json()[0]["chapters"][0]["queue_status"] == "packaging"


MB_URL = "https://mb.test"


def catalogue_payload(**overrides):
    payload = {
        "id": 377,
        "title": "Tankarr Test",
        "type": "manga",
        "status": "releasing",
        "year": 2001,
        "authors": ["Test Author"],
        "description": "A test work.",
        "cover": {"raw": {"url": "https://cdn.test/cover.jpg"}},
        "final_volume": 1,
        "total_chapters": 2,
    }
    payload.update(overrides)
    return payload


class FakeSourceProvider:
    """A Suwayomi-like release source with an in-memory chapter list."""

    name = "suwayomi"
    label = "Suwayomi"
    search_mode = "title"

    def __init__(self, series: dict, chapters: list[dict]):
        self.series = series
        self.chapters = chapters
        self.downloaded: list[str] = []

    async def aclose(self) -> None:
        return None

    def supports_language(self, language: str) -> bool:
        return language == "en"

    async def search_with_diagnostics(self, query: str, language: str, limit: int = 20):
        return [dict(self.series)], []

    async def get_manga(self, manga_id: str) -> dict:
        return dict(self.series)

    async def list_chapters(self, manga_id: str, language: str) -> list[dict]:
        return [dict(item) for item in self.chapters]

    async def download_pages(self, chapter_id, target_dir, progress, concurrency=4):
        self.downloaded.append(chapter_id)
        Path(target_dir).mkdir(parents=True, exist_ok=True)
        paths = []
        for index, color in enumerate(("red", "blue"), start=1):
            path = Path(target_dir) / f"{index:03d}.jpg"
            path.write_bytes(jpeg_bytes(color))
            paths.append(path)
            await progress(index, 2)
        return paths


def source_series():
    return {
        "id": "suwayomi-abcd1234-7",
        "provider": "suwayomi",
        "title": "Tankarr Test",
        "alternate_titles": [],
        "authors": ["Test Author"],
        "year": 2001,
        "source_url": "https://source.test/tankarr-test",
        "source_name": "Weeb Central (EN)",
    }


def source_chapter(number: str, identifier: str):
    return {
        "id": identifier,
        "provider": "suwayomi",
        "volume": "1",
        "chapter": number,
        "title": "Hello" if number == "1" else "",
        "language": "en",
        "groups": ["Test Group"],
        "publish_at": "2026-01-01T00:00:00+00:00",
        "source_url": f"https://source.test/{identifier}",
        "pages": 2,
        "version": 1,
        "source_key": "suwayomi:3",
        "source_name": "Weeb Central (EN)",
    }


def wait_ready(client) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if client.get("/api/ready").status_code == 200:
            return
        time.sleep(0.05)


@respx.mock
def test_catalogue_add_maps_a_source_downloads_and_imports(tmp_path: Path):
    respx.get(f"{MB_URL}/series/search").mock(
        return_value=Response(200, json={"data": [catalogue_payload()]})
    )
    respx.get(f"{MB_URL}/series/377").mock(
        return_value=Response(200, json={"data": catalogue_payload()})
    )
    frontend_dir = tmp_path / "frontend"
    assets_dir = frontend_dir / "assets"
    assets_dir.mkdir(parents=True)
    (frontend_dir / "index.html").write_text(
        "<!doctype html><title>Tankarr test UI</title>", encoding="utf-8"
    )
    (assets_dir / "app.js").write_text("console.log('tankarr')", encoding="utf-8")
    settings = Settings(
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "library",
        frontend_dir=frontend_dir,
        mangabaka_api_url=MB_URL,
        metadata_enabled=False,
        monitor_enabled=False,
    )
    provision_library_identity(settings)
    app = create_app(settings)
    source = FakeSourceProvider(
        source_series(), [source_chapter("1", "suwayomi-abcd1234-70")]
    )

    with TestClient(app) as client:
        homepage = client.get("/")
        assert homepage.status_code == 200
        assert "Tankarr test UI" in homepage.text
        assert homepage.headers["cache-control"] == "no-store, max-age=0"
        assert client.get("/series/test-series").status_code == 200
        assert client.get("/assets/app.js").status_code == 200
        health = client.get("/api/system/health")
        assert health.status_code == 200
        assert health.json()["library_organization"]["naming_version"] == 2
        wait_ready(client)
        app.state.providers.clear()
        app.state.providers["suwayomi"] = source

        search = client.get("/api/search", params={"q": "Tankarr", "language": "en"})
        assert search.status_code == 200, search.text
        work = search.json()["works"][0]
        assert work["id"] == "mb:377" and work["provider"] == "catalogue"
        assert work["title"] == "Tankarr Test" and work["status"] == "ongoing"
        assert work["cover_url"] == "https://cdn.test/cover.jpg"
        assert work["volume_count"] == 1 and work["latest_release_chapter"] == 2

        preview = client.get("/api/manga/mb:377/preview", params={"language": "en"})
        assert preview.status_code == 200, preview.text
        assert preview.json()["future_monitoring_allowed"] is True
        assert preview.json()["chapter_count"] == 2

        added = client.post(
            "/api/manga",
            json={
                "manga_id": "mb:377",
                "provider": "catalogue",
                "language": "en",
                "monitor_mode": "all",
            },
        )
        assert added.status_code == 201, added.text
        manga_id = added.json()["id"]
        assert added.json()["provider"] == "catalogue"
        assert added.json()["title"] == "Tankarr Test"

        duplicate = client.post(
            "/api/manga",
            json={
                "manga_id": "mb:377",
                "provider": "catalogue",
                "language": "en",
                "monitor_mode": "all",
            },
        )
        assert duplicate.status_code == 409

        database = app.state.database
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if database.list_release_sources(manga_id):
                break
            time.sleep(0.05)
        mappings = database.list_release_sources(manga_id)
        assert [(m["provider"], m["provider_manga_id"]) for m in mappings] == [
            ("suwayomi", "suwayomi-abcd1234-7")
        ]

        deadline = time.monotonic() + 10
        job = None
        while time.monotonic() < deadline:
            jobs = client.get("/api/jobs").json()
            if jobs:
                job = client.get(f"/api/jobs/{jobs[0]['id']}").json()
                if job["status"] in {"completed", "failed"}:
                    break
            time.sleep(0.05)
        assert job is not None and job["status"] == "completed", (
            job.get("message"),
            job.get("language_evidence"),
        )
        assert job["manga_title"] == "Tankarr Test"
        assert job["chapter_volume"] == "1" and job["chapter_number"] == "1"
        assert job["chapter_groups"] == ["Test Group"]
        output = Path(job["result_path"])
        assert output.exists()
        assert output == final_library_path(
            settings.library_dir,
            {"title": "Tankarr Test", "authors": ["Test Author"]},
            {
                "id": "suwayomi-abcd1234-70",
                "volume": "1",
                "chapter": "1",
                "language": "en",
            },
        )
        assert validate_cbz(output)["page_count"] == 2
        assert source.downloaded == ["suwayomi-abcd1234-70"]

        details = client.get(f"/api/manga/{manga_id}").json()
        assert details["preferred_language"] == "en"
        assert details["monitor_mode"] == "all"
        assert details["status"] == "ongoing"
        assert [c["source_name"] for c in details["chapters"]] == ["Weeb Central (EN)"]
        # Running work without an official source: count the available chapters.
        assert details["chapter_index"]["expected_count"] == 1
        assert details["chapter_index"]["reference_kind"] == "available"
        assert details["library_count"]["reference_unknown"] is False
        assert details["library_count"]["latest_chapter"] == "1"

        renamed = client.post(
            f"/api/manga/{manga_id}/rename", json={"title": "Tankarr Renamed"}
        )
        assert renamed.status_code == 200, renamed.text
        assert renamed.json()["manga"]["title_overridden"] is True
        assert renamed.json()["organization"]["moved"] == 1
        renamed_output = Path(
            client.get(f"/api/jobs/{job['id']}").json()["result_path"]
        )
        assert renamed_output.exists() and not output.exists()
        with zipfile.ZipFile(renamed_output) as archive:
            comic_info = archive.read("ComicInfo.xml").decode("utf-8")
        assert "<Series>Tankarr Renamed</Series>" in comic_info

        organization = client.post("/api/library/organize", params={"dry_run": "true"})
        assert organization.status_code == 200
        assert organization.json()["planned_moves"] == 0


@respx.mock
def test_ended_catalogue_work_stays_monitorable_like_a_sonarr_series(tmp_path: Path):
    payload = catalogue_payload(status="completed", total_chapters=1)
    respx.get(f"{MB_URL}/series/377").mock(
        return_value=Response(200, json={"data": payload})
    )
    settings = Settings(
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "library",
        mangabaka_api_url=MB_URL,
        metadata_enabled=False,
        monitor_enabled=False,
    )
    provision_library_identity(settings)

    with TestClient(create_app(settings)) as client:
        preview = client.get("/api/manga/mb:377/preview", params={"language": "en"})
        assert preview.status_code == 200, preview.text
        assert preview.json()["status"] == "ended"
        assert preview.json()["chapter_count"] == 1
        # Ended does not freeze the work: late specials still get picked up.
        assert preview.json()["future_monitoring_allowed"] is True
        assert "refreshed less often" in preview.json()["future_monitoring_reason"]

        added = client.post(
            "/api/manga",
            json={
                "manga_id": "mb:377",
                "provider": "catalogue",
                "language": "en",
                "monitor_mode": "future",
                "series_unit": "chapters",
                "search_now": False,
            },
        )
        assert added.status_code == 201, added.text
        assert added.json()["monitor_mode"] == "future"
        assert added.json()["series_unit_override"] == "chapters"
        assert added.json()["last_chapter"] == "1"


def test_add_manga_immediately_discovers_other_release_sources(tmp_path: Path):
    settings = Settings(
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "library",
        monitor_enabled=False,
        metadata_enabled=False,
    )
    provision_library_identity(settings)
    app = create_app(settings)

    class OriginProvider:
        name = "mangadex"
        label = "MangaDex"
        search_mode = "title"

        async def aclose(self) -> None:
            return None

        def supports_language(self, language: str) -> bool:
            return True

        async def search_with_diagnostics(
            self, query: str, language: str, limit: int = 20
        ):
            return [], []

        async def get_manga(self, manga_id: str) -> dict:
            return {
                "id": "origin-id",
                "provider": "mangadex",
                "title": "V.B. Rose",
                "description": "",
                "cover_url": None,
                "authors": ["Banri Hidaka"],
                "original_language": "ja",
                "status": "completed",
                "year": 2004,
                "last_chapter": "83",
                "last_volume": "14",
                "available_languages": ["en"],
                "source_url": "https://example.test/origin",
            }

        async def list_chapters(self, manga_id: str, language: str) -> list[dict]:
            return [
                {
                    "id": "origin-1",
                    "provider": "mangadex",
                    "volume": "1",
                    "chapter": "1",
                    "title": "",
                    "language": "en",
                    "groups": [],
                    "publish_at": None,
                    "source_url": "https://example.test/origin/1",
                    "pages": 20,
                    "version": 1,
                }
            ]

    alternate_series = {
        "id": "alt-series",
        "provider": "alternate",
        "title": "V.B. Rose",
        "alternate_titles": [],
        "authors": ["Banri Hidaka"],
        "year": 2004,
        "source_url": "https://example.test/alt-series",
        "source_name": "Alternate",
    }

    class AlternateProvider:
        name = "alternate"
        label = "Alternate source"
        search_mode = "title"

        async def aclose(self) -> None:
            return None

        def supports_language(self, language: str) -> bool:
            return language == "en"

        async def search_with_diagnostics(
            self, query: str, language: str, limit: int = 20
        ):
            return [dict(alternate_series)], []

        async def get_manga(self, manga_id: str) -> dict:
            return dict(alternate_series)

        async def list_chapters(self, manga_id: str, language: str) -> list[dict]:
            return [
                {
                    "id": "alt-2",
                    "provider": "alternate",
                    "volume": "1",
                    "chapter": "2",
                    "title": "",
                    "language": "en",
                    "groups": ["Group"],
                    "publish_at": None,
                    "source_url": "https://example.test/alt/2",
                    "pages": 20,
                    "version": 1,
                }
            ]

    with TestClient(app) as client:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if client.get("/api/ready").status_code == 200:
                break
            time.sleep(0.05)
        providers = app.state.providers
        providers.clear()
        providers.update(
            {"mangadex": OriginProvider(), "alternate": AlternateProvider()}
        )

        response = client.post(
            "/api/manga",
            json={
                "manga_id": "origin-id",
                "provider": "mangadex",
                "language": "en",
                "monitor_mode": "existing",
            },
        )
        assert response.status_code == 201, response.text

        database = app.state.database
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if database.list_release_sources("origin-id"):
                break
            time.sleep(0.05)

        mappings = database.list_release_sources("origin-id")
        assert [(item["provider"], item["provider_manga_id"]) for item in mappings] == [
            ("alternate", "alt-series")
        ]

        # The discovered chapter joins the canonical index and, because the
        # series was added in a backlog mode, gets its download job without
        # waiting for the next scheduled Wanted pass. The live worker may have
        # already claimed (and failed) the job, so assert existence, not state.
        deadline = time.monotonic() + 10
        chapters_with_jobs: set[str] = set()
        while time.monotonic() < deadline:
            chapters_with_jobs = {
                str(job["chapter_id"]) for job in database.list_jobs()
            }
            if {"origin-1", "alt-2"} <= chapters_with_jobs:
                break
            time.sleep(0.05)
        assert {"origin-1", "alt-2"} <= chapters_with_jobs


def test_manual_release_source_mapping_merges_and_queues(tmp_path: Path):
    settings = Settings(
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "library",
        monitor_enabled=False,
        metadata_enabled=False,
    )
    provision_library_identity(settings)
    app = create_app(settings)

    class StubProvider:
        search_mode = "title"

        def __init__(self, name: str, series: dict, chapters: list[dict]):
            self.name = name
            self.label = name.title()
            self.series = series
            self.chapters = chapters

        async def aclose(self) -> None:
            return None

        def supports_language(self, language: str) -> bool:
            return True

        async def search_with_diagnostics(
            self, query: str, language: str, limit: int = 20
        ):
            return [], []

        async def get_manga(self, manga_id: str) -> dict:
            if manga_id != self.series["id"]:
                raise KeyError(manga_id)
            return dict(self.series)

        async def list_chapters(self, manga_id: str, language: str) -> list[dict]:
            return [dict(item) for item in self.chapters]

    origin = StubProvider(
        "mangadex",
        {
            "id": "origin-id",
            "provider": "mangadex",
            "title": "V.B. Rose",
            "description": "",
            "cover_url": None,
            "authors": ["Banri Hidaka"],
            "original_language": "ja",
            "status": "completed",
            "available_languages": ["en"],
            "source_url": "https://example.test/origin",
        },
        [
            {
                "id": "origin-1",
                "provider": "mangadex",
                "volume": "1",
                "chapter": "1",
                "title": "",
                "language": "en",
                "groups": [],
                "publish_at": None,
                "source_url": "https://example.test/origin/1",
                "pages": 20,
                "version": 1,
            }
        ],
    )
    alternate = StubProvider(
        "alternate",
        {
            "id": "alt-series",
            "provider": "alternate",
            "title": "V.B. Rose",
            "source_name": "Alternate",
            "source_url": "https://example.test/alt-series",
        },
        [
            {
                "id": "alt-2",
                "provider": "alternate",
                "volume": "1",
                "chapter": "2",
                "title": "",
                "language": "en",
                "groups": [],
                "publish_at": None,
                "source_url": "https://example.test/alt/2",
                "pages": 20,
                "version": 1,
            }
        ],
    )

    with TestClient(app) as client:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if client.get("/api/ready").status_code == 200:
                break
            time.sleep(0.05)
        providers = app.state.providers
        providers.clear()
        providers.update({"mangadex": origin, "alternate": alternate})

        added = client.post(
            "/api/manga",
            json={
                "manga_id": "origin-id",
                "provider": "mangadex",
                "language": "en",
                "monitor_mode": "existing",
            },
        )
        assert added.status_code == 201, added.text

        # Own provider and unknown providers are rejected before any mapping.
        rejected = client.post(
            "/api/manga/origin-id/release-sources",
            json={"provider": "mangadex", "provider_manga_id": "origin-id"},
        )
        assert rejected.status_code == 409
        unknown = client.post(
            "/api/manga/origin-id/release-sources",
            json={"provider": "nope", "provider_manga_id": "x"},
        )
        assert unknown.status_code == 404

        response = client.post(
            "/api/manga/origin-id/release-sources",
            json={"provider": "alternate", "provider_manga_id": "alt-series"},
        )
        assert response.status_code == 201, response.text
        payload = response.json()
        assert payload["mapping"]["verified_by"] == "manual"
        assert payload["releases"] == 1
        assert payload["new"] == 1
        assert payload["queued"] >= 1

        database = app.state.database
        mappings = database.list_release_sources("origin-id")
        assert [(m["provider"], m["provider_manga_id"]) for m in mappings] == [
            ("alternate", "alt-series")
        ]
        chapters = {str(j["chapter_id"]) for j in database.list_jobs()}
        assert "alt-2" in chapters


def test_calendar_lists_expected_releases_from_the_official_platform(tmp_path: Path):
    from datetime import UTC, datetime, timedelta

    settings = Settings(
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "library",
        monitor_enabled=False,
        metadata_enabled=False,
    )
    provision_library_identity(settings)
    app = create_app(settings)
    database = app.state.database
    database.upsert_manga(
        {
            "id": "weekly",
            "provider": "catalogue",
            "title": "Weekly Work",
            "description": "",
            "cover_url": None,
            "authors": [],
            "status": "ongoing",
            "available_languages": ["en"],
        },
        "en",
        "future",
    )
    database.save_series_metadata(
        "weekly",
        {
            "title": "Weekly Work",
            "status": "ongoing",
            "official_links": [
                {
                    "name": "MANGA Plus",
                    "language": "en",
                    "url": "https://mangaplus.shueisha.co.jp/titles/100020",
                }
            ],
        },
        artwork_path=None,
        artwork_sha256=None,
        artwork_media_type=None,
        source_status=[],
    )
    last_sunday = datetime.now(UTC).date() - timedelta(days=3)
    # Scanlation dates never make a calendar; the official platform's do.
    database.upsert_chapters(
        "weekly",
        [
            {
                "id": f"scan-{100 + index}",
                "provider": "suwayomi",
                "volume": None,
                "chapter": str(100 + index),
                "title": "",
                "language": "en",
                "groups": [],
                "source_url": f"https://weebcentral.com/c/{index}",
                "publish_at": (
                    last_sunday - timedelta(days=7 * (7 - index) - 1)
                ).isoformat(),
            }
            for index in range(8)
        ],
    )
    with TestClient(app) as client:
        wait_ready(client)
        without_official = client.get(
            "/api/calendar", params={"days": 7, "ahead": 14}
        ).json()
        assert without_official["expected"] == []
        database.upsert_chapters(
            "weekly",
            [
                {
                    "id": f"official-{100 + index}",
                    "provider": "suwayomi",
                    "volume": None,
                    "chapter": str(100 + index),
                    "title": "",
                    "language": "en",
                    "groups": [],
                    "source_url": f"https://mangaplus.shueisha.co.jp/viewer/{index}",
                    "publish_at": (
                        last_sunday - timedelta(days=7 * (7 - index))
                    ).isoformat(),
                }
                for index in range(8)
            ],
        )
        database.upsert_chapters(
            "weekly",
            [
                {
                    "id": "official-108",
                    "provider": "suwayomi",
                    "volume": None,
                    "chapter": "108",
                    "title": "\U0001f512 Chapter 108",
                    "language": "en",
                    "groups": [],
                    "source_url": "https://mangaplus.shueisha.co.jp/viewer/108",
                    "publish_at": (last_sunday + timedelta(days=7)).isoformat(),
                }
            ],
        )
        calendar_response = client.get("/api/calendar", params={"days": 7, "ahead": 14})
        payload = calendar_response.json()
        etag = calendar_response.headers["etag"]
        not_modified = client.get(
            "/api/calendar",
            params={"days": 7, "ahead": 14},
            headers={"If-None-Match": etag},
        )
        assert not_modified.status_code == 304
        assert not_modified.headers["etag"] == etag
        assert calendar_response.headers["cache-control"] == "private, no-cache"
        next_release = last_sunday + timedelta(days=7)
        ranged = client.get(
            "/api/calendar",
            params={"start": next_release.isoformat(), "end": next_release.isoformat()},
        ).json()
        release_day = client.get(
            "/api/calendar",
            params={"start": last_sunday.isoformat(), "end": last_sunday.isoformat()},
        ).json()
        assert (
            client.get(
                "/api/calendar", params={"start": next_release.isoformat()}
            ).status_code
            == 422
        )
        assert (
            client.get(
                "/api/calendar",
                params={
                    "start": next_release.isoformat(),
                    "end": (next_release - timedelta(days=1)).isoformat(),
                },
            ).status_code
            == 422
        )

    assert [item["chapter"] for item in payload["expected"]] == ["109"]
    assert ranged["expected"] == []
    assert [item["id"] for item in ranged["releases"]] == ["official-108"]
    assert ranged["releases"][0]["availability_status"] == "expected"
    assert [item["id"] for item in release_day["releases"]] == ["official-107"]
    assert release_day["releases"][0]["manga_title"] == "Weekly Work"
    first = payload["expected"][0]
    assert first["manga_title"] == "Weekly Work"
    assert first["cadence_label"] == "weekly" and first["cadence_days"] == 7
    assert first["expected_at"] == (last_sunday + timedelta(days=14)).isoformat()


def test_reader_connection_test_answers_without_a_reader(tmp_path: Path):
    settings = Settings(
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "library",
        monitor_enabled=False,
        metadata_enabled=False,
    )
    provision_library_identity(settings)
    with TestClient(create_app(settings)) as client:
        wait_ready(client)
        none = client.post("/api/settings/test/reader", json={"reader_kind": "none"})
        assert none.status_code == 200 and none.json()["ok"] is True
        url = client.post(
            "/api/settings/test/reader",
            json={
                "reader_kind": "url",
                "reader_series_url_template": "https://r.test/{title}",
            },
        )
        assert url.status_code == 200 and url.json()["ok"] is True
        stump = client.post("/api/settings/test/reader", json={"reader_kind": "stump"})
        assert stump.status_code == 200 and stump.json()["ok"] is False


def test_metadata_test_reports_the_provider_status_and_message(tmp_path: Path):
    """A spent daily quota and a wrong key both used to read "request failed"."""

    from tankarr.app import _metadata_api_message

    response = httpx.Response(
        429,
        json={
            "error": {
                "code": 429,
                "message": "Quota exceeded for quota metric 'Queries'",
            }
        },
        request=httpx.Request("GET", "https://example.test"),
    )
    error = httpx.HTTPStatusError("429", request=response.request, response=response)

    assert (
        _metadata_api_message(error) == " — Quota exceeded for quota metric 'Queries'"
    )
    assert (
        _metadata_api_message(
            httpx.HTTPStatusError(
                "500",
                request=response.request,
                response=httpx.Response(500, text="<html>", request=response.request),
            )
        )
        == ""
    )


def test_dismissed_alert_returns_when_its_content_changes(tmp_path: Path):
    """Acknowledging "12 failed downloads" must not hide the thirteenth."""

    from tankarr.database import Database

    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()

    database.dismiss_alert("failed_jobs", "12 failed downloads")

    assert database.dismissed_alerts() == {"failed_jobs": "12 failed downloads"}
    assert database.dismissed_alerts().get("failed_jobs") != "13 failed downloads"

    database.dismiss_alert("failed_jobs", "13 failed downloads")
    assert database.dismissed_alerts() == {"failed_jobs": "13 failed downloads"}


@respx.mock
def test_author_page_is_persisted_from_mangabaka_and_lists_addable_works(
    tmp_path: Path,
):
    def catalogue_search(request):
        assert request.url.params.get("staff") == "Test Author"
        return Response(
            200,
            json={
                "data": [
                    catalogue_payload(),
                    catalogue_payload(id=378, title="Second Work", year=2005),
                    # A namesake's work must not be attributed to this author.
                    catalogue_payload(
                        id=379, title="Other Person", authors=["Test Authorson"]
                    ),
                ],
                "pagination": {"count": 3, "next": None, "page": 1, "limit": 50},
            },
        )

    respx.get(f"{MB_URL}/series/377").mock(
        return_value=Response(200, json={"data": catalogue_payload()})
    )
    respx.get(f"{MB_URL}/series/search").mock(side_effect=catalogue_search)
    settings = Settings(
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "library",
        mangabaka_api_url=MB_URL,
        metadata_enabled=False,
        monitor_enabled=False,
    )
    provision_library_identity(settings)
    with TestClient(create_app(settings)) as client:
        wait_ready(client)
        added = client.post(
            "/api/manga",
            json={
                "manga_id": "mb:377",
                "provider": "catalogue",
                "language": "en",
                "monitor_mode": "none",
                "search_now": False,
            },
        )
        assert added.status_code == 201, added.text
        credits = added.json()["author_entities"]
        assert len(credits) == 1
        assert credits[0]["name"] == "Test Author"

        author_id = credits[0]["id"]
        assert author_id.startswith("author-")
        page = client.get(f"/api/authors/{author_id}")
        assert page.status_code == 200, page.text
        body = page.json()
        assert body["source"] == "mangabaka"
        assert body["author"] == "Test Author"
        assert body["aliases"] == ["Test Author"]
        assert "profile" not in body
        assert [work["id"] for work in body["works"]] == ["mb:377", "mb:378"]
        assert body["works"][0]["in_library"] is True
        assert body["works"][0]["library_manga_id"] == added.json()["id"]
        assert body["works"][0]["source_url"] == "https://mangabaka.org/377"
        assert body["works"][1]["in_library"] is False
        assert body["last_refreshed_at"] is not None
        assert body["source_url"] is None
        assert body["source_pages"] == []


def test_public_match_review_hides_the_download_reference():
    """A release review's payload keeps a Prowlarr download reference so
    accepting it later can grab the same result; the browser must never
    see it."""

    from tankarr.app import _public_match_review

    review = {
        "id": 1,
        "kind": "release",
        "payload": {
            "title": "Example",
            "download_ref": "https://nyaa.si/download/1.torrent",
        },
    }
    public = _public_match_review(review)
    assert "download_ref" not in public["payload"]
    assert public["payload"]["title"] == "Example"

    source_review = {"id": 2, "kind": "source", "payload": {"provider_manga_id": "x"}}
    assert _public_match_review(source_review) == source_review
