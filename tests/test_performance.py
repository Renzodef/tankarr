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


def _seed_series(database: Database, count: int, *, downloaded: bool = False) -> None:
    for number in range(count):
        record = {**manga(), "id": f"bulk-{number}", "title": f"Bulk {number}"}
        database.upsert_manga(record, "en", "all")
        database.upsert_chapters(
            record["id"],
            [
                {
                    **chapter(),
                    "id": f"bulk-{number}-chapter-{index}",
                    "chapter": str(index),
                    "volume": None,
                    "pages": 20,
                    "publish_at": "2026-09-20T00:00:00Z",
                }
                for index in range(1, 4)
            ],
        )
        if downloaded:
            database.mark_chapter_downloaded(
                f"bulk-{number}-chapter-1", f"/library/bulk-{number}/c001.cbz"
            )


def test_calendar_resolves_every_unit_from_its_own_snapshot(
    tmp_path: Path, monkeypatch
):
    """Three queries per series made the unit choice a third of a cold render."""

    from tankarr import series_unit

    settings = Settings(
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "library",
        monitor_enabled=False,
        metadata_enabled=False,
        komga_link_enabled=False,
    )
    provision_library_identity(settings)
    app = create_app(settings)
    _seed_series(app.state.database, 5)
    installed = series_unit.unit_context
    assert installed is not None
    per_series_lookups: list[str] = []

    def counting(manga_id: str):
        per_series_lookups.append(manga_id)
        return installed(manga_id)

    monkeypatch.setattr(series_unit, "unit_context", counting)
    with TestClient(app) as client:
        response = client.get("/api/calendar?days=30&ahead=30")
    assert response.status_code == 200
    assert per_series_lookups == []


def test_library_orphans_read_tracked_paths_in_one_query(tmp_path: Path, monkeypatch):
    """The System page scanned every release of every series for one column."""

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
    _seed_series(database, 3)
    root = settings.library_dir
    tracked = root / "Bulk 0" / "Bulk 0 - c001 [en].cbz"
    tracked.parent.mkdir(parents=True)
    tracked.write_bytes(b"PK\x05\x06" + b"\x00" * 18)
    database.mark_chapter_downloaded("bulk-0-chapter-1", str(tracked))
    orphan = root / "Gone Work" / "Gone Work - c001 [en].cbz"
    orphan.parent.mkdir(parents=True)
    orphan.write_bytes(b"PK\x05\x06" + b"\x00" * 18)

    def never(*_args, **_kwargs):
        raise AssertionError("the orphan scan must not decode releases per series")

    monkeypatch.setattr(database, "list_all_chapters", never)
    monkeypatch.setattr(database, "list_manga", never)
    report = app.state.service.library_orphans()

    assert report["count"] == 1
    assert [item["folder"] for item in report["folders"]] == ["Gone Work"]


def test_compact_wanted_omits_the_boilerplate_verdict_of_unsearched_slots(
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
    _seed_series(app.state.database, 1)
    with TestClient(app) as client:
        full = client.get("/api/wanted?fresh=true").json()
        compact = client.get("/api/wanted?compact=true&fresh=true").json()
    full_chapters = full[0]["chapters"]
    compact_chapters = compact[0]["chapters"]
    assert len(full_chapters) == len(compact_chapters) == 3
    assert full_chapters[0]["recovery"]["verdict"] == "unsearched"
    assert all("recovery" not in item for item in compact_chapters)
    # The page's row model still gets every field it renders.
    assert set(compact_chapters[0]) >= {"id", "chapter", "volume", "title", "slot_key"}


def test_status_reuses_the_orphan_summary_and_the_series_rows(
    tmp_path: Path, monkeypatch
):
    """The System page polls the status; at 1,500 series the orphan scan
    (a stat per tracked file) and the release aggregate behind the series
    rows cost two seconds per call. Both are reused until something changes."""

    settings = Settings(
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "library",
        monitor_enabled=False,
        auth_required=False,
    )
    provision_library_identity(settings)
    (settings.library_dir / "Stray").mkdir()
    (settings.library_dir / "Stray" / "Stray 001.cbz").write_bytes(b"PK")
    app = create_app(settings)
    with TestClient(app) as client:
        service = app.state.service
        database = app.state.database
        scans = {"count": 0}
        original_scan = service.library_orphans

        def counted_scan(**kwargs):
            scans["count"] += 1
            return original_scan(**kwargs)

        monkeypatch.setattr(service, "library_orphans", counted_scan)
        listings = {"count": 0}
        original_list = database.list_manga

        def counted_list(*args, **kwargs):
            listings["count"] += 1
            return original_list(*args, **kwargs)

        monkeypatch.setattr(database, "list_manga", counted_list)

        first = client.get("/api/system/status").json()
        second = client.get("/api/system/status").json()
        assert [
            alert["key"]
            for alert in first["alerts"]
            if alert["key"] == "library_orphans"
        ]
        assert second["alerts"] == first["alerts"]
        assert scans["count"] == 1, "the alert must reuse the cached orphan summary"
        assert listings["count"] == 1, (
            "the rows must be reused while the library revision is unchanged"
        )

        # An explicit scan from the System page is always fresh and refreshes the summary.
        assert client.get("/api/library/orphans").json()["count"] == 1
        assert scans["count"] == 2
        client.get("/api/system/status")
        assert scans["count"] == 2

        # A library change invalidates the rows; the orphan summary follows the
        # next organization or deletion, not the row revision.
        database.upsert_manga(manga(), "en", "all")
        client.get("/api/system/status")
        assert listings["count"] == 2
