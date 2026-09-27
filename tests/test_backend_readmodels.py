from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from tankarr.app import create_app
from tankarr.config import Settings
from tankarr.database import Database
from tankarr.library_snapshot import manga_revisions
from tankarr.monitor import ReleaseMonitor
from tankarr.wanted_recovery import slot_verdict


@pytest.fixture
def audit_app(tmp_path):
    app = create_app(
        Settings(
            _env_file=None,
            host="127.0.0.1",
            data_dir=tmp_path / "data",
            library_dir=tmp_path / "library",
            monitor_enabled=False,
            metadata_enabled=False,
            suwayomi_enabled=False,
            komga_link_enabled=False,
        )
    )
    yield app
    app.state.api_executor.shutdown(wait=True, cancel_futures=True)
    app.state.revision_executor.shutdown(wait=True, cancel_futures=True)


def seed(database, manga_id="series", mode="all"):
    database.upsert_manga(
        {
            "id": manga_id,
            "provider": "local",
            "title": manga_id,
            "authors": [],
            "status": "ongoing",
            "available_languages": ["en"],
        },
        "en",
        mode,
    )


def chapter(number, *, publish_at="2020-01-01T00:00:00Z"):
    return {
        "id": f"chapter-{number}",
        "provider": "suwayomi",
        "language": "en",
        "chapter": str(number),
        "volume": None,
        "title": f"Chapter {number}",
        "groups": [],
        "publish_at": publish_at,
        "source_name": "Webtoons.com (EN)",
        "source_key": "suwayomi:webtoons",
        "source_url": f"https://www.webtoons.com/en/example/{number}",
    }


def test_settings_bundle_rolls_back_when_secret_installation_fails(audit_app):
    database = audit_app.state.database
    database.save_setting("first", "old")
    database.save_setting("legacy", "keep")
    original = database.get_setting_overrides()

    def fail():
        raise OSError("staged credentials could not be installed")

    with pytest.raises(OSError):
        database.apply_setting_changes(
            {"first": "new", "second": "new"},
            delete_keys=("legacy",),
            before_commit=fail,
        )
    assert database.get_setting_overrides() == original
    database.apply_setting_changes(
        {"first": "new", "second": "new"}, delete_keys=("legacy",)
    )
    original.pop("legacy")
    assert database.get_setting_overrides() == {
        **original,
        "first": "new",
        "second": "new",
    }


def test_revision_clocks_are_transactional_and_observe_other_connections(audit_app):
    database = audit_app.state.database
    seed(database)
    before = database.library_revision()
    per_series = manga_revisions(database)
    with pytest.raises(RuntimeError), database.connect() as connection:
        connection.execute("UPDATE manga SET title='rolled back' WHERE id='series'")
        raise RuntimeError("rollback")
    assert database.library_revision() == before
    assert manga_revisions(database) == per_series
    writer = Database(database.path)
    with writer.connect() as connection:
        # Even an unchanged timestamp is not a revision collision.
        connection.execute("UPDATE manga SET title='external change' WHERE id='series'")
    assert database.library_revision() != before
    assert manga_revisions(database) != per_series


def test_revision_query_work_does_not_grow_with_release_count(audit_app):
    database = audit_app.state.database
    seed(database)

    def instructions():
        steps = 0

        def progress():
            nonlocal steps
            steps += 1
            return 0

        with database.read_snapshot(), database.connect() as connection:
            connection.set_progress_handler(progress, 1)
            database.library_revision()
            database.wanted_revision()
            database.calendar_revision()
            manga_revisions(database)
            connection.set_progress_handler(None, 0)
        return steps

    small = instructions()
    with database.connect() as connection:
        connection.executemany(
            "INSERT INTO chapter_release(id,manga_id,language,provider,source_url,first_seen_at,updated_at) "
            "VALUES (?,'series','en','local','local','2020','2020')",
            ((f"bulk-{number}",) for number in range(4000)),
        )
    large = instructions()
    assert large <= small + 25, (small, large)


def test_progress_updates_do_not_invalidate_wanted_but_state_changes_do(audit_app):
    database = audit_app.state.database
    seed(database)
    database.upsert_chapters("series", [chapter(1)])
    job = database.create_job("series", "chapter-1", "en")
    before = database.wanted_revision()
    with database.connect() as connection:
        connection.execute(
            "UPDATE download_job SET progress=42, message='progress' WHERE id=?",
            (job["id"],),
        )
    assert database.wanted_revision() == before
    with database.connect() as connection:
        connection.execute(
            "UPDATE download_job SET status='downloading' WHERE id=?", (job["id"],)
        )
    assert database.wanted_revision() != before


def test_wanted_indexes_each_active_job_once_even_with_many_series(
    audit_app, monkeypatch
):
    database = audit_app.state.database
    for number in range(30):
        seed(database, f"series-{number}")
    inputs = database.wanted_inputs()

    class CountedJobs(dict):
        visits = 0

        def values(self):
            for item in super().values():
                self.visits += 1
                yield item

    jobs = CountedJobs(
        {
            f"release-{number}": {
                "manga_id": f"series-{number % 30}",
                "chapter_id": f"release-{number}",
                "chapter": str(number),
                "volume": None,
                "status": "queued",
                "job_id": number,
            }
            for number in range(3000)
        }
    )
    inputs["active_jobs"] = jobs
    monkeypatch.setattr(database, "wanted_inputs", lambda: inputs)
    audit_app.state.service.list_wanted()
    assert jobs.visits == 3000
    jobs.visits = 0
    audit_app.state.service.list_wanted()
    assert jobs.visits == 3000


@pytest.mark.asyncio
async def test_monitor_stop_waits_for_detached_wanted_even_without_refresh_task():
    monitor = ReleaseMonitor(
        Settings(_env_file=None),
        SimpleNamespace(get_setting_overrides=lambda: {}),
        SimpleNamespace(),
        SimpleNamespace(),
    )
    entered, gate, cleaned = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def recover(**kwargs):
        entered.set()
        try:
            await gate.wait()
        finally:
            cleaned.set()

    monitor._search_wanted_once = recover
    caller = asyncio.create_task(monitor.search_wanted())
    await entered.wait()
    await monitor.stop()
    assert cleaned.is_set()
    assert monitor._wanted_search_task is None
    with pytest.raises(asyncio.CancelledError):
        await caller


@pytest.mark.asyncio
async def test_wanted_scheduler_does_not_wait_for_a_slow_refresh_cycle():
    monitor = ReleaseMonitor(
        Settings(_env_file=None, monitor_enabled=True),
        SimpleNamespace(get_setting_overrides=lambda: {}),
        SimpleNamespace(),
        SimpleNamespace(),
    )
    refreshing, recovered, gate = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def refresh(**kwargs):
        assert kwargs == {"include_wanted": False}
        refreshing.set()
        await gate.wait()

    async def wanted(**kwargs):
        recovered.set()
        monitor.last_wanted_search_at = datetime.now(UTC).isoformat()
        return {}

    monitor.run_cycle = refresh
    monitor._search_wanted_once = wanted
    try:
        await monitor.start()
        await asyncio.wait_for(refreshing.wait(), 1)
        await asyncio.wait_for(recovered.wait(), 1)
        assert not gate.is_set()
    finally:
        await monitor.stop()
    assert monitor._wanted_scheduler_task is None


def test_recovery_timing_is_derived_from_the_recorded_answer():
    verdict = slot_verdict(
        [
            {
                "channel": "sources",
                "outcome": "not_offered",
                "attempted_at": "2030-01-01T10:00:00Z",
            },
            {
                "channel": "indexer_book",
                "outcome": "not_offered",
                "attempted_at": "2030-01-02T10:00:00Z",
            },
        ]
    )
    assert verdict["checked_at"] == "2030-01-02T10:00:00+00:00"
    assert verdict["next_eligible_at"] == "2030-01-09T10:00:00+00:00"
    assert verdict["channels"][0]["next_eligible_at"] is None
    assert slot_verdict([])["checked_at"] is None


def test_acquisition_explanations_share_the_real_gates_without_changing_selection(
    audit_app,
):
    database = audit_app.state.database
    seed(database)
    database.save_series_metadata(
        "series",
        {
            "official_links": [
                {"url": "https://www.webtoons.com/en/example/list", "language": "en"}
            ]
        },
        source_status=[],
        artwork_path=None,
        artwork_sha256=None,
        artwork_media_type=None,
    )
    releases = [chapter(number) for number in range(1, 8)]
    releases[6]["volume"] = "7"
    releases.extend(
        [
            {
                **chapter(1),
                "id": "mirror-1",
                "source_name": "Mirror",
                "source_key": "mirror",
                "source_url": "https://mirror.test/1",
            },
            {
                **chapter(9),
                "id": "ahead-9",
                "source_name": "Mirror",
                "source_key": "mirror",
                "source_url": "https://mirror.test/9",
            },
            {**chapter(8), "id": "italian-8", "language": "it"},
        ]
    )
    database.upsert_chapters("series", releases)
    database.mark_chapter_downloaded(
        "chapter-2", audit_app.state.settings.library_dir / "owned.cbz"
    )
    database.set_chapter_monitored("chapter-3", False)
    with database.connect() as connection:
        connection.execute(
            "UPDATE chapter_release SET numbering_status='unmapped', canonical_chapter=NULL WHERE id='chapter-4'"
        )
        connection.execute(
            "UPDATE chapter_release SET numbering_status='mapped', canonical_chapter='9' WHERE id='ahead-9'"
        )
    database.block_release("chapter-5", reason="test block")
    database.create_job("series", "chapter-6", "en")
    database.set_volume_monitor_override("series", "7", "ignored")
    expected = database.preferred_missing_releases("series")
    reasons = {}
    assert database.preferred_missing_releases("series", explain=reasons) == expected
    for release_id, fragment in (
        ("mirror-1", "preferred"),
        ("chapter-2", "owned"),
        ("chapter-3", "not monitored"),
        ("chapter-4", "numbering"),
        ("chapter-5", "blocked"),
        ("chapter-6", "job"),
        ("chapter-7", "ignored"),
        ("ahead-9", "frontier"),
        ("italian-8", "language"),
    ):
        assert any(fragment in reason for reason in reasons.get(release_id, [])), (
            release_id,
            reasons,
        )
    assert "chapter-1" not in reasons
    database.update_manga("series", {"library_status_override": "up_to_date"})
    reasons = {}
    assert database.preferred_missing_releases("series", explain=reasons) == []
    assert "up to date" in reasons["chapter-1"][0]


def test_calendar_signals_are_batched_and_invalidate_the_http_cache(
    audit_app, monkeypatch
):
    database = audit_app.state.database
    seed(database, mode="future")
    database.save_series_metadata(
        "series",
        {
            "official_links": [
                {"url": "https://www.webtoons.com/en/example/list", "language": "en"}
            ]
        },
        source_status=[],
        artwork_path=None,
        artwork_sha256=None,
        artwork_media_type=None,
    )
    now = datetime.now(UTC)
    database.upsert_chapters(
        "series",
        [
            chapter(
                number, publish_at=(now - timedelta(days=(5 - number) * 7)).isoformat()
            )
            for number in range(1, 5)
        ],
    )
    database.upsert_release_source(
        "series",
        provider="suwayomi",
        provider_manga_id="official",
        title="series",
        source_url="https://www.webtoons.com/en/example/list",
        source_name="Webtoons.com (EN)",
        language="en",
        match_confidence=1,
        match_reason="test",
    )
    with database.connect() as connection:
        connection.execute(
            "UPDATE manga_release_source SET source_role='primary_official' WHERE manga_id='series'"
        )
    monkeypatch.setattr(
        database,
        "publication_signals",
        lambda *args: pytest.fail("N+1 publication lookup"),
    )
    client = TestClient(audit_app)
    first = client.get("/api/calendar")
    assert first.status_code == 200
    assert first.json()["expected"]
    database.record_release_source_result(
        "series", "suwayomi", "official", publication_status="hiatus"
    )
    after = client.get(
        "/api/calendar", headers={"If-None-Match": first.headers["etag"]}
    )
    assert after.status_code == 200
    assert after.json()["expected"] == []


def test_calendar_reads_one_snapshot_across_a_concurrent_writer(audit_app, monkeypatch):
    database = audit_app.state.database
    seed(database)
    database.upsert_chapters("series", [chapter(1)])
    decode = database._decode_manga
    changed = False

    def concurrent_write(row):
        nonlocal changed
        if not changed:
            changed = True
            writer = Database(database.path)
            with writer.connect() as connection:
                connection.execute(
                    "UPDATE manga SET preferred_language='it' WHERE id='series'"
                )
                connection.execute(
                    "DELETE FROM chapter_release WHERE manga_id='series'"
                )
        return decode(row)

    monkeypatch.setattr(database, "_decode_manga", concurrent_write)
    inputs = database.list_calendar_inputs()
    assert changed
    assert inputs[0]["manga"]["preferred_language"] == "en"
    assert [release["id"] for release in inputs[0]["chapters"]] == ["chapter-1"]
    assert database.list_all_chapters("series") == []


def test_calendar_expires_at_a_publication_without_a_database_write(
    audit_app, monkeypatch
):
    import tankarr.app as app_module
    import tankarr.source_numbering as numbering

    now = datetime(2030, 1, 1, 12, tzinfo=UTC)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz)

    monkeypatch.setattr(app_module, "datetime", Clock)
    monkeypatch.setattr(numbering, "datetime", Clock)
    database = audit_app.state.database
    seed(database, mode="future")
    database.save_series_metadata(
        "series",
        {
            "official_links": [
                {"url": "https://www.webtoons.com/en/example/list", "language": "en"}
            ]
        },
        source_status=[],
        artwork_path=None,
        artwork_sha256=None,
        artwork_media_type=None,
    )
    database.upsert_chapters(
        "series",
        [
            chapter(
                number, publish_at=(now - timedelta(days=(5 - number) * 7)).isoformat()
            )
            for number in range(1, 5)
        ]
        + [chapter(5, publish_at=(now + timedelta(seconds=10)).isoformat())],
    )
    client = TestClient(audit_app)
    first = client.get("/api/calendar")
    assert first.status_code == 200
    revision = database.calendar_revision()
    now += timedelta(seconds=11)
    after = client.get(
        "/api/calendar", headers={"If-None-Match": first.headers["etag"]}
    )
    assert database.calendar_revision() == revision
    assert after.status_code == 200
    assert after.json() != first.json()
