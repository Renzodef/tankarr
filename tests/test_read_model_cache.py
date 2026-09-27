from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from tankarr import read_model_cache, source_numbering
from tankarr.app import create_app
from tankarr.config import Settings
from tankarr.database import Database
from tankarr.library_snapshot import changed_inputs, manga_revisions


@pytest.fixture
def cache_app(tmp_path: Path):
    app = create_app(
        Settings(
            data_dir=tmp_path / "data",
            library_dir=tmp_path / "library",
            monitor_enabled=False,
            metadata_enabled=False,
            komga_link_enabled=False,
            suwayomi_enabled=False,
        )
    )
    # Read APIs do not require workers, provider calls or library maintenance.
    yield app
    app.state.api_executor.shutdown(wait=True, cancel_futures=True)
    app.state.revision_executor.shutdown(wait=True, cancel_futures=True)


def add_series(database: Database, manga_id: str, *, status: str = "ongoing"):
    database.upsert_manga(
        {
            "id": manga_id,
            "provider": "local",
            "title": manga_id,
            "description": "",
            "authors": [],
            "status": status,
            "available_languages": ["en"],
        },
        "en",
        "all",
    )


def release(manga_id: str, number: int, *, provider: str = "a", **extra):
    return {
        "id": f"{manga_id}-{provider}-{number}",
        "provider": provider,
        "chapter": str(number),
        "volume": None,
        "language": "en",
        "title": f"Chapter {number}",
        "groups": [],
        "source_url": f"https://{provider}.example.test/{number}",
        "publish_at": "2020-01-01T00:00:00Z",
        **extra,
    }


@pytest.mark.parametrize("endpoint", ["manga", "wanted"])
def test_initial_cached_list_does_not_wait_for_source_refresh(
    cache_app, monkeypatch, endpoint
):
    database = cache_app.state.database
    add_series(database, "cached-list")
    database.upsert_chapters("cached-list", [release("cached-list", 1)])
    started = threading.Event()
    finish = threading.Event()
    name = "list_manga" if endpoint == "manga" else "list_chapter_summaries"
    original = getattr(database, name)

    def blocked_render(*args, **kwargs):
        started.set()
        assert finish.wait(5), "cached navigation waited for a full rebuild"
        return original(*args, **kwargs)

    with TestClient(cache_app) as client:
        before = client.get(f"/api/{endpoint}?fresh=true")
        database.upsert_chapters("cached-list", [release("cached-list", 2)])
        monkeypatch.setattr(database, name, blocked_render)
        try:
            cached = client.get(
                f"/api/{endpoint}?cached=true",
                headers={"If-None-Match": before.headers["etag"]},
            )
            assert cached.status_code == 304
            assert started.wait(2)
        finally:
            finish.set()
        fresh = client.get(f"/api/{endpoint}?fresh=true")
        assert fresh.status_code == 200
        assert fresh.headers["etag"] != before.headers["etag"]


def advance_clock(monkeypatch, now: datetime):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz)

    monkeypatch.setattr(source_numbering, "datetime", Clock)
    monkeypatch.setattr(read_model_cache, "cache_now", lambda: now.timestamp())
    monkeypatch.setattr("tankarr.app.cache_now", lambda: now.timestamp())
    monkeypatch.setattr("tankarr.service.cache_now", lambda: now.timestamp())


def test_publication_date_expires_every_layer_without_a_database_write(
    cache_app, monkeypatch
):
    database = cache_app.state.database
    now = datetime(2030, 1, 1, tzinfo=UTC)
    advance_clock(monkeypatch, now)
    add_series(database, "future")
    database.upsert_chapters(
        "future",
        [
            release("future", 1),
            release("future", 2, publish_at=(now + timedelta(days=1)).isoformat()),
        ],
    )
    client = TestClient(cache_app)
    wanted = client.get("/api/wanted?compact=true&fresh=true")
    library = client.get("/api/manga?fresh=true")
    series = client.get("/api/manga/future?compact=true&fresh=true")
    assert [row["chapter"] for row in wanted.json()[0]["chapters"]] == ["1"]
    revision = database.wanted_revision()

    advance_clock(monkeypatch, now + timedelta(days=2))
    assert database.wanted_revision() == revision
    updated = client.get(
        "/api/wanted?compact=true&fresh=true",
        headers={"If-None-Match": wanted.headers["etag"]},
    )
    assert updated.status_code == 200
    assert [row["chapter"] for row in updated.json()[0]["chapters"]] == ["1", "2"]
    for url, previous in (
        ("/api/manga?fresh=true", library),
        ("/api/manga/future?compact=true&fresh=true", series),
    ):
        current = client.get(url, headers={"If-None-Match": previous.headers["etag"]})
        assert current.status_code == 200
        card = current.json()[0] if isinstance(current.json(), list) else current.json()
        assert card["library_count"]["available_count"] == 2


def test_recovery_changes_the_unit_in_library_wanted_and_series(cache_app):
    database = cache_app.state.database
    add_series(database, "recovery", status="ended")
    database.save_series_metadata(
        "recovery",
        {"status": "ended", "volume_count": 1, "chapter_count": 1},
        artwork_path=None,
        artwork_sha256=None,
        artwork_media_type=None,
        source_status=[],
    )
    database.upsert_chapters("recovery", [release("recovery", n) for n in range(1, 23)])
    database.record_indexer_offers("recovery", [{"volume": "1", "title": "Wrong work"}])
    client = TestClient(cache_app)
    assert (
        client.get("/api/manga?fresh=true").json()[0]["effective_series_unit"]
        == "volumes"
    )
    client.get("/api/manga/recovery?compact=true&fresh=true")
    before = client.get("/api/wanted?fresh=true").json()
    assert before[0]["chapters"][0]["slot_key"] == "volume:1"

    database.record_wanted_attempt(
        "recovery", "volume:1", channel="indexer_book", outcome="ambiguous"
    )
    wanted = client.get("/api/wanted?fresh=true").json()
    assert wanted[0]["chapters"][0]["slot_key"] == "chapter:1"
    assert (
        client.get("/api/manga?fresh=true").json()[0]["effective_series_unit"]
        == "chapters"
    )
    series = client.get("/api/manga/recovery?compact=true&fresh=true").json()
    assert series["chapter_index"]["series_unit"] == "chapters"


def test_source_settings_and_health_invalidate_wanted_without_release_changes(
    cache_app,
):
    database = cache_app.state.database
    add_series(database, "ranking")
    database.upsert_chapters(
        "ranking", [release("ranking", 1, provider=provider) for provider in ("a", "b")]
    )
    client = TestClient(cache_app)

    def chosen():
        response = client.get("/api/wanted?compact=true&fresh=true")
        assert response.status_code == 200
        return response.json()[0]["chapters"][0]["provider"]

    assert chosen() == "a"
    changed = client.put("/api/settings", json={"source_priority_backfill": "b,a"})
    assert changed.status_code == 200
    assert chosen() == "b"
    client.put("/api/settings", json={"source_priority_backfill": ""})
    assert chosen() == "a"
    release_revision = database.wanted_inputs()["release_revisions"]
    for _ in range(8):
        database.record_source_health("a", ok=False, reason="download failed")
    assert database.wanted_inputs()["release_revisions"] == release_revision
    assert chosen() == "b"


def test_library_only_rebuilds_the_changed_card_and_batch_reads_cold_inputs(cache_app):
    import tankarr.app as app_module

    database = cache_app.state.database
    for manga_id in ("first", "second", "third"):
        add_series(database, manga_id)
        database.upsert_chapters(manga_id, [release(manga_id, n) for n in range(1, 4)])
    client = TestClient(cache_app)
    built: list[str] = []
    build = app_module.build_chapter_index

    def counted(manga, *args, **kwargs):
        built.append(str(manga["id"]))
        return build(manga, *args, **kwargs)

    with (
        patch.object(app_module, "build_chapter_index", counted),
        patch.object(
            database, "list_chapters", side_effect=AssertionError("N+1 release read")
        ),
    ):
        first = client.get("/api/manga?fresh=true")
        assert first.status_code == 200
        assert sorted(built) == ["first", "second", "third"]
        built.clear()
        unchanged = client.get(
            "/api/manga?fresh=true", headers={"If-None-Match": first.headers["etag"]}
        )
        assert unchanged.status_code == 304
        assert built == []
        database.set_manga_title_override("second", "Renamed")
        changed = client.get("/api/manga?fresh=true")
        assert changed.status_code == 200
        assert built == ["second"]
        assert (
            next(row for row in changed.json() if row["id"] == "second")["title"]
            == "Renamed"
        )


def test_wanted_releases_and_their_revision_share_one_sqlite_snapshot(cache_app):
    database = cache_app.state.database
    add_series(database, "snapshot")
    database.upsert_chapters("snapshot", [release("snapshot", 1)])
    writer = Database(database.path)
    original = database.wanted_inputs
    changed = False

    def concurrent_import():
        nonlocal changed
        inputs = original()
        if not changed:
            changed = True
            writer.upsert_chapters("snapshot", [release("snapshot", 2)])
        return inputs

    with patch.object(database, "wanted_inputs", concurrent_import):
        first = cache_app.state.service.list_wanted()
    assert [row["chapter"] for row in first[0]["chapters"]] == ["1"]
    second = cache_app.state.service.list_wanted()
    assert [row["chapter"] for row in second[0]["chapters"]] == ["1", "2"]


def test_wanted_cache_notices_download_hidden_behind_newer_release(cache_app):
    database = cache_app.state.database
    add_series(database, "downloaded")
    database.upsert_chapters(
        "downloaded", [release("downloaded", 1), release("downloaded", 2)]
    )
    with database.connect() as connection:
        connection.execute(
            "UPDATE chapter_release SET updated_at='9999-01-01T00:00:00Z' "
            "WHERE id='downloaded-a-2'"
        )

    service = cache_app.state.service
    assert [row["chapter"] for row in service.list_wanted()[0]["chapters"]] == [
        "1",
        "2",
    ]
    revision = database.wanted_inputs()["release_revisions"]["downloaded"]

    database.mark_chapter_downloaded(
        "downloaded-a-1", Path("/library/chapter-1.cbz"), "0" * 64
    )

    changed = database.wanted_inputs()["release_revisions"]["downloaded"]
    assert changed != revision
    assert [row["chapter"] for row in service.list_wanted()[0]["chapters"]] == ["2"]


def test_library_snapshot_keeps_revision_and_inputs_together(cache_app):
    database = cache_app.state.database
    add_series(database, "snapshot")
    database.upsert_chapters("snapshot", [release("snapshot", 1)])
    writer = Database(database.path)
    with database.read_snapshot():
        before = manga_revisions(database)
        writer.upsert_chapters("snapshot", [release("snapshot", 2)])
        inputs = changed_inputs(database, ["snapshot"])
        assert len(inputs["snapshot"]["releases"]) == 1
        assert manga_revisions(database) == before
    assert manga_revisions(database) != before


def test_a_release_published_during_library_build_is_not_cached_forever(
    cache_app, monkeypatch
):
    import tankarr.app as app_module

    database = cache_app.state.database
    now = datetime(2030, 1, 1, tzinfo=UTC)
    advance_clock(monkeypatch, now)
    add_series(database, "boundary")
    database.upsert_chapters(
        "boundary",
        [
            release("boundary", 1),
            release(
                "boundary",
                2,
                publish_at=f" {(now + timedelta(seconds=1)).isoformat()} ",
            ),
        ],
    )
    build = app_module.build_chapter_index

    def crosses_publication(*args, **kwargs):
        result = build(*args, **kwargs)
        advance_clock(monkeypatch, now + timedelta(seconds=31))
        return result

    client = TestClient(cache_app)
    with patch.object(app_module, "build_chapter_index", crosses_publication):
        first = client.get("/api/manga?fresh=true")
    assert first.json()[0]["library_count"]["available_count"] == 1
    second = client.get("/api/manga?fresh=true")
    assert second.json()[0]["library_count"]["available_count"] == 2


def test_health_changes_only_rebuild_wanted_series_using_that_source(cache_app):
    import tankarr.service as service_module

    database = cache_app.state.database
    for manga_id, provider in (("first", "a"), ("second", "b")):
        add_series(database, manga_id)
        database.upsert_chapters(manga_id, [release(manga_id, 1, provider=provider)])
    service = cache_app.state.service
    service.list_wanted()
    database.record_source_health("a", ok=False, reason="download failed")
    build = service_module.build_chapter_index
    built: list[str] = []

    def counted(manga, *args, **kwargs):
        built.append(str(manga["id"]))
        return build(manga, *args, **kwargs)

    with patch.object(service_module, "build_chapter_index", counted):
        service.list_wanted()
    assert built == ["first"]


def test_repeated_transient_failures_demote_wanted_like_the_downloader(cache_app):
    database = cache_app.state.database
    add_series(database, "transient")
    database.upsert_chapters("transient", [release("transient", 1)])
    for _ in range(database.TRANSIENT_DEMOTION_FAILURES):
        database.record_source_failure(
            "transient", "a", transient=True, reason="timeout"
        )
    assert "a" in database.demoted_sources("transient")
    assert "a" in database.wanted_inputs()["demoted_sources"]["transient"]


def test_expected_book_does_not_also_get_a_duplicate_unknown_series_row(cache_app):
    database = cache_app.state.database
    add_series(database, "book", status="ended")
    database.save_series_metadata(
        "book",
        {"status": "ended", "volume_count": 1},
        artwork_path=None,
        artwork_sha256=None,
        artwork_media_type=None,
        source_status=[],
    )
    add_series(database, "unknown", status="unknown")
    wanted = {row["manga"]["id"]: row for row in cache_app.state.service.list_wanted()}
    assert wanted["book"]["chapters"][0]["slot_key"] == "volume:1"
    assert wanted["book"]["no_sources"] is False
    assert wanted["unknown"]["no_sources"] is True
    assert wanted["unknown"]["chapters"] == []


def test_wanted_hydrates_complete_candidates_after_slim_selection(cache_app):
    database = cache_app.state.database
    add_series(database, "full")
    database.upsert_chapters(
        "full", [release("full", 1, groups=["Group"], pages=20, version=2)]
    )
    original = database.get_chapter("full-a-1")
    selected = cache_app.state.service.list_wanted()[0]["chapters"][0]
    assert {key: selected[key] for key in original} == original


@pytest.mark.parametrize("endpoint", ["manga", "wanted", "wanted?compact=true"])
def test_first_cached_read_after_restart_returns_saved_snapshot(
    tmp_path, monkeypatch, endpoint
):
    settings = Settings(
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "library",
        monitor_enabled=False,
        metadata_enabled=False,
        komga_link_enabled=False,
        suwayomi_enabled=False,
    )
    separator = "&" if "?" in endpoint else "?"
    first = create_app(settings)
    add_series(first.state.database, "restart")
    first.state.database.upsert_chapters("restart", [release("restart", 1)])
    with TestClient(first) as client:
        before = client.get(f"/api/{endpoint}{separator}fresh=true")
        assert before.status_code == 200
    first.state.database.upsert_chapters("restart", [release("restart", 2)])
    second = create_app(settings)
    database = second.state.database
    name = "list_manga" if endpoint == "manga" else "list_chapter_summaries"
    original = getattr(database, name)
    started, finish = threading.Event(), threading.Event()

    def blocked(*args, **kwargs):
        started.set()
        assert finish.wait(5), "first paint waited for reconciliation after restart"
        return original(*args, **kwargs)

    monkeypatch.setattr(database, name, blocked)
    with TestClient(second) as client:
        try:
            response = client.get(f"/api/{endpoint}{separator}cached=true")
            assert response.status_code == 200
            assert response.content == before.content
            if endpoint == "manga":
                assert started.wait(2)
        finally:
            finish.set()
        fresh = client.get(f"/api/{endpoint}{separator}fresh=true")
        assert fresh.status_code == 200
        assert fresh.content != before.content


def test_verified_publication_api_keeps_future_wanted_chapters(cache_app, monkeypatch):
    monkeypatch.setattr(
        cache_app.state.service, "assert_mutations_allowed", lambda: None
    )
    database = cache_app.state.database
    add_series(database, "published")
    database.update_manga("published", {"series_unit_override": "chapters"})
    database.upsert_chapters("published", [release("published", 1)])
    with TestClient(cache_app) as client:
        response = client.patch(
            "/api/manga/published",
            json={
                "verified_chapter_count": 2,
                "verified_chapter_source": "https://publisher.example/series",
            },
        )
        assert response.status_code == 200, response.text
        manga = database.get_manga("published")
        assert manga["verified_chapter_count"] == 2
        assert manga["library_status_override"] is None
        assert manga["expected_count_override"] is None
        wanted = client.get("/api/wanted?fresh=true").json()
        entry = next(item for item in wanted if item["manga"]["id"] == "published")
        assert entry["expected_count"] == 2
        assert {c["chapter"] for c in entry["chapters"]} == {"1", "2"}
        database.upsert_chapters("published", [release("published", 3)])
        wanted = client.get("/api/wanted?fresh=true").json()
        entry = next(item for item in wanted if item["manga"]["id"] == "published")
        assert entry["expected_count"] == 3
        assert {c["chapter"] for c in entry["chapters"]} == {"1", "2", "3"}
