from __future__ import annotations

import asyncio
import gzip
import threading
from pathlib import Path

from fastapi.testclient import TestClient

from tankarr.app import create_app
from tankarr.archive import install_atomically
from tankarr.config import Settings
from tankarr.database import Database
from tankarr.service import TankarrService


def provision_library_identity(settings: Settings) -> None:
    identity = "0123456789abcdef0123456789abcdef"
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.library_dir.mkdir(parents=True, exist_ok=True)
    (settings.data_dir / ".tankarr-library-id").write_text(f"{identity}\n")
    (settings.library_dir / ".tankarr-library-id").write_text(f"{identity}\n")


def manga() -> dict:
    return {
        "id": "performance-work",
        "provider": "local",
        "title": "Performance Work",
        "description": "",
        "authors": [],
        "original_language": "ja",
        "status": "ongoing",
        "available_languages": ["en"],
    }


def chapter() -> dict:
    return {
        "id": "performance-chapter",
        "manga_id": "performance-work",
        "volume": "1",
        "chapter": "1",
        "title": "Chapter 1",
        "language": "en",
        "provider": "local",
        "groups": [],
        "publish_at": "2026-08-30T00:00:00Z",
        "source_url": "https://example.test/chapter/1",
        "pages": 1,
        "version": 1,
    }


def test_series_detail_uses_a_scoped_conditional_snapshot(tmp_path: Path):
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
    database.upsert_manga(manga(), "en", "all")
    preferred = {
        **chapter(),
        "id": "preferred-release",
        "provider": "preferred",
        "version": 4,
    }
    redundant = {
        **chapter(),
        "id": "redundant-release",
        "provider": "redundant",
        "version": 2,
    }
    database.upsert_chapters("performance-work", [chapter(), preferred, redundant])
    database.mark_chapter_downloaded(
        "performance-chapter", settings.library_dir / "performance-chapter.cbz"
    )

    with TestClient(app) as client:
        first = client.get("/api/manga/performance-work")
        etag = first.headers["etag"]
        unchanged = client.get(
            "/api/manga/performance-work", headers={"If-None-Match": etag}
        )
        compact = client.get("/api/manga/performance-work?compact=true")
        compact_etag = compact.headers["etag"]
        database.set_chapter_monitored("preferred-release", False)
        stale_while_refreshing = client.get(
            "/api/manga/performance-work?compact=true",
            headers={"If-None-Match": compact_etag},
        )
        fresh_compact = client.get(
            "/api/manga/performance-work?compact=true&fresh=true",
            headers={"If-None-Match": compact_etag},
        )
        database.update_manga("performance-work", {"status_override": "ended"})
        changed = client.get(
            "/api/manga/performance-work", headers={"If-None-Match": etag}
        )

    assert first.status_code == 200
    assert first.headers["cache-control"] == "private, no-cache"
    assert unchanged.status_code == 304
    assert "chapters" not in compact.json()
    assert compact.json()["chapter_index"]["slots"]
    compact_slot = compact.json()["chapter_index"]["slots"][0]
    assert compact_slot["release_count"] == 3
    assert [release["id"] for release in compact_slot["releases"]] == [
        "preferred-release",
        "performance-chapter",
    ]
    assert "updated_at" not in compact_slot["releases"][0]
    assert stale_while_refreshing.status_code == 304
    assert fresh_compact.status_code == 200
    assert fresh_compact.headers["etag"] != compact_etag
    assert fresh_compact.json()["chapter_index"]["slots"][0]["monitored"] is False
    assert compact.headers["etag"] != etag
    assert changed.status_code == 200
    assert changed.headers["etag"] != etag
    assert changed.json()["status_override"] == "ended"


def test_library_snapshot_is_conditional_and_reuses_the_cached_payload(tmp_path: Path):
    settings = Settings(
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "library",
        monitor_enabled=False,
        metadata_enabled=False,
        komga_link_enabled=False,
    )
    provision_library_identity(settings)
    app = create_app(settings)
    app.state.database.upsert_manga(manga(), "en", "all")

    with TestClient(app) as client:
        first = client.get("/api/manga")
        unchanged = client.get(
            "/api/manga", headers={"If-None-Match": first.headers["etag"]}
        )

    assert first.status_code == 200
    assert first.headers["cache-control"] == "private, no-cache"
    assert unchanged.status_code == 304


def test_wanted_snapshot_reads_only_series_that_can_produce_rows(tmp_path: Path):
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    for number in range(40):
        record = {
            **manga(),
            "id": f"unmanaged-{number}",
            "title": f"Unmanaged {number}",
        }
        database.upsert_manga(record, "en", "none")
        database.upsert_chapters(
            record["id"],
            [
                {
                    **chapter(),
                    "id": f"unmanaged-chapter-{number}",
                    "monitored": False,
                }
            ],
        )
        database.configure_monitor_mode(record["id"], "none")
    database.upsert_manga(manga(), "en", "all")
    database.upsert_chapters("performance-work", [chapter()])

    inputs = database.wanted_inputs()

    assert [item["id"] for item in inputs["manga"]] == ["performance-work"]
    assert set(inputs["release_revisions"]) == {"performance-work"}


def test_versioned_assets_are_immutable_and_use_precompressed_files(tmp_path: Path):
    frontend = tmp_path / "frontend"
    assets = frontend / "assets"
    assets.mkdir(parents=True)
    (frontend / "index.html").write_text("Tankarr")
    javascript = b"console.log('precompressed tankarr');"
    asset = assets / "app-abcdef.js"
    asset.write_bytes(javascript)
    Path(f"{asset}.gz").write_bytes(gzip.compress(javascript, compresslevel=9))
    settings = Settings(
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "library",
        frontend_dir=frontend,
        monitor_enabled=False,
        metadata_enabled=False,
        komga_link_enabled=False,
    )
    provision_library_identity(settings)

    with TestClient(create_app(settings)) as client:
        response = client.get(
            "/assets/app-abcdef.js", headers={"Accept-Encoding": "gzip"}
        )
        unchanged = client.get(
            "/assets/app-abcdef.js",
            headers={
                "Accept-Encoding": "gzip",
                "If-None-Match": response.headers["etag"],
            },
        )

    assert response.status_code == 200
    assert response.content == javascript
    assert response.headers["content-encoding"] == "gzip"
    assert response.headers["cache-control"].endswith("immutable")
    assert unchanged.status_code == 304


def test_status_filesystem_checks_run_in_the_reserved_api_executor(
    tmp_path: Path, monkeypatch
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
    service = app.state.service
    calls: list[str] = []

    def observed_library_status() -> dict[str, object]:
        thread_name = threading.current_thread().name
        calls.append(thread_name)
        return {"available": True, "root": thread_name}

    with TestClient(app) as client:
        monkeypatch.setattr(service, "library_status", observed_library_status)
        health = client.get("/api/system/health")
        ready = client.get("/api/ready")
        system = client.get("/api/system/status")

    assert health.json()["library"]["root"].startswith("tankarr-api")
    assert ready.status_code in {200, 503}
    assert system.json()["library"]["root"].startswith("tankarr-api")
    assert calls
    assert all(name.startswith("tankarr-api") for name in calls)


class PageProvider:
    async def download_pages(
        self, _chapter_id: str, destination: Path, progress, *, concurrency: int
    ) -> list[Path]:
        del concurrency
        destination.mkdir(parents=True, exist_ok=True)
        page = destination / "0001.jpg"
        page.write_bytes(b"page")
        await progress(1, 1)
        return [page]


class StandaloneReader:
    standalone = True


async def test_atomic_nas_publication_does_not_block_the_event_loop(
    tmp_path: Path, monkeypatch
):
    settings = Settings(data_dir=tmp_path / "data", library_dir=tmp_path / "library")
    provision_library_identity(settings)
    database = Database(settings.database_path)
    database.initialize()
    database.upsert_manga(manga(), "en", "all")
    database.upsert_chapters("performance-work", [chapter()])
    job = database.create_job("performance-work", "performance-chapter", "en")
    service = TankarrService(
        settings,
        database,
        PageProvider(),  # type: ignore[arg-type]
        StandaloneReader(),  # type: ignore[arg-type]
    )
    release = threading.Event()
    released_while_waiting: list[bool] = []

    def slow_install(*args, **kwargs):
        released_while_waiting.append(release.wait(timeout=0.5))
        return install_atomically(*args, **kwargs)

    monkeypatch.setattr("tankarr.service.install_atomically", slow_install)

    async def advance_loop() -> None:
        await asyncio.sleep(0.05)
        release.set()

    release_task = asyncio.create_task(advance_loop())
    await service.process_download_job(job["id"])
    await release_task

    assert released_while_waiting == [True]
    assert database.get_job(job["id"])["status"] == "completed"
