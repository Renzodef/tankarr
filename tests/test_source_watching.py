from datetime import UTC, datetime, timedelta

import pytest

from tankarr.app import create_app
from tankarr.config import Settings
from tankarr.monitor import ReleaseMonitor
from tankarr.numbering_reconciliation import reconcile_numbering
from tankarr.series_summary import publication_summary
from tests.test_deletion import chapter, make_service, manga
from tests.test_monitor import FakeService, RecordingWorker


def save_catalogue(database, status):
    database.save_series_metadata(
        "manga-1",
        {
            "status": status,
            "provenance": {"status": "mangabaka"},
            "chapter_count": 6 if status == "completed" else 900,
            "official_links": [
                {"url": "https://publisher.test/series", "language": "en"}
            ],
        },
        artwork_path=None,
        artwork_sha256=None,
        artwork_media_type=None,
        source_status=[],
    )
    database.save_metadata_source_record(
        "manga-1",
        entity_type="series",
        entity_key="manga-1",
        source="mangabaka",
        external_id="fixture-series",
        match_confidence=1.0,
        match_reason="fixture",
        data={"status": status},
        raw={},
    )


def source_chapter(source, number):
    return {
        **chapter(f"{source}-{number}", str(number)),
        "volume": None,
        "source_key": source,
        "source_name": source,
        "source_url": f"https://{source}.test/{number}",
    }


@pytest.mark.asyncio
async def test_watching_downloads_new_source_chapter_once_without_waiting_for_publisher(
    tmp_path,
):
    database, real_service, _reader = make_service(tmp_path)
    database.upsert_manga({**manga(), "status": "ongoing"}, "en", "future")
    save_catalogue(database, "ongoing")
    baseline = [source_chapter("publisher", n) for n in range(1, 6)]
    newest = source_chapter("community", 6)
    service = FakeService(
        database, [baseline, [*baseline, newest], [*baseline, newest]]
    )
    worker = RecordingWorker()
    monitor = ReleaseMonitor(real_service.settings, database, service, worker)
    assert (await monitor.refresh_one("manga-1"))["queued"] == 0
    owned = real_service.settings.library_dir / "existing.cbz"
    owned.write_bytes(b"existing fixture content")
    database.mark_chapter_downloaded("publisher-5", owned)
    assert (await monitor.refresh_one("manga-1"))["queued"] == 1
    assert (await monitor.refresh_one("manga-1"))["queued"] == 0
    assert [job["chapter_id"] for job in database.list_jobs()] == ["community-6"]
    assert owned.read_bytes() == b"existing fixture content"
    assert database.get_chapter("publisher-5")["downloaded"] is True


def test_mangabaka_completion_switches_the_watch_schedule_and_completed_total(tmp_path):
    database, service, _reader = make_service(tmp_path)
    database.upsert_manga(
        {**manga(), "provider": "catalogue", "status": "completed"}, "en", "all"
    )
    now = datetime.now(UTC)
    database.update_manga(
        "manga-1", {"last_checked_at": (now - timedelta(hours=1)).isoformat()}
    )
    monitor = ReleaseMonitor(service.settings, database, service, RecordingWorker())
    save_catalogue(database, "ongoing")
    assert [row["id"] for row in monitor._due_series(now)] == ["manga-1"]
    database.upsert_chapters(
        "manga-1", [source_chapter("community", n) for n in range(1, 8)]
    )
    assert "community-7" in {
        r["id"] for r in database.preferred_missing_releases("manga-1")
    }
    save_catalogue(database, "completed")
    database.record_monitor_result("manga-1")
    assert monitor._due_series(now) == []
    assert "community-7" not in {
        r["id"] for r in database.preferred_missing_releases("manga-1")
    }
    assert (
        publication_summary(
            database.get_manga("manga-1"),
            database.get_series_metadata("manga-1")["data"],
        )["status"]
        == "ended"
    )


def test_numbering_migration_preserves_owned_content_correspondence():
    owned = {
        **source_chapter("community", 20),
        "source_chapter": "20",
        "canonical_chapter": "10",
        "chapter": "10",
        "downloaded": True,
        "numbering_status": "mapped",
        "numbering_method": "content_sequence_alignment",
        "numbering_version": 6,
        "numbering_evidence": {"offset": 10},
    }
    result = reconcile_numbering([owned])[owned["id"]]
    assert result["canonical_chapter"] == "10"
    assert result["numbering_method"] == "content_sequence_alignment"


def test_calendar_and_official_alignment_endpoints_are_removed(tmp_path):
    app = create_app(
        Settings(
            _env_file=None,
            data_dir=tmp_path / "data",
            library_dir=tmp_path / "library",
            monitor_enabled=False,
            metadata_enabled=False,
            suwayomi_enabled=False,
            komga_link_enabled=False,
        )
    )
    try:
        paths = {route.path for route in app.routes}
        assert not any(
            "calendar" in path or "official-alignment" in path for path in paths
        )
        assert not hasattr(app.state, "render_calendar")
    finally:
        app.state.api_executor.shutdown(wait=True, cancel_futures=True)
        app.state.revision_executor.shutdown(wait=True, cancel_futures=True)


def test_numbering_offset_is_not_invented_beyond_the_content_evidence():
    owned = [source_chapter("owned", n) for n in range(1, 13)]
    drifted = [source_chapter("other", n + 10) for n in range(1, 13)]
    for n, (left, right) in enumerate(zip(owned, drifted), 1):
        left.update(downloaded=True, pages=20 + n, title="Episode")
        right.update(pages=20 + n, title="Episode")
    tail = {**source_chapter("other", 23), "title": "Episode", "pages": 99}
    result = reconcile_numbering([*owned, *drifted, tail])
    assert result["other-22"]["canonical_chapter"] == "12"
    assert result["other-23"]["numbering_status"] == "pending_evidence"
    assert result["other-23"]["numbering_method"] == "awaiting_content_alignment"


def test_a_legacy_download_source_cannot_close_a_mangabaka_running_work(tmp_path):
    database, service, _reader = make_service(tmp_path)
    source = {**manga(), "status": "completed", "last_chapter": "5"}
    database.upsert_manga(source, "en", "future")
    save_catalogue(database, "ongoing")
    allowed, _reason = service._future_monitoring_capability(
        source, [source_chapter("community", 5)]
    )
    assert allowed is True


@pytest.mark.parametrize(
    "number,title",
    [("1.5", "Chapter 1.5000000000000004"), ("1046.5", "Chapter 1046-5")],
)
def test_fractional_source_numbers_are_not_mistaken_for_reset_episode_indexes(
    number, title
):
    release = {**source_chapter("community", number), "title": title}
    result = reconcile_numbering([release])[release["id"]]
    assert result["canonical_chapter"] == number


def test_content_patterns_never_renumber_an_already_mapped_owned_file():
    reference = [source_chapter("reference", n) for n in range(1, 15)]
    other = [source_chapter("other", n + 20) for n in range(1, 13)]
    for n, item in enumerate(reference, 1):
        item.update(downloaded=True, pages=20 + n, title="Episode")
    for n, item in enumerate(other, 1):
        item.update(downloaded=True, pages=20 + n, title="Episode")
    result = reconcile_numbering([*reference, *other])
    assert result["other-32"]["canonical_chapter"] == "32"


def test_known_content_drift_does_not_reappear_as_a_new_unverified_tail():
    owned = [
        {**source_chapter("community", n), "downloaded": True} for n in range(1, 21)
    ]
    correspondence = {
        **source_chapter("offset", 25),
        "source_chapter": "25",
        "canonical_chapter": "15",
        "chapter": "15",
        "numbering_method": "content_sequence_alignment",
        "numbering_evidence": {"offset": 10, "anchors": 12},
        "numbering_status": "mapped",
    }
    ambiguous = {
        **source_chapter("offset", 35),
        "canonical_chapter": None,
        "numbering_status": "pending_evidence",
        "numbering_method": "awaiting_secondary_official_evidence",
        "downloaded": True,
        "numbering_version": 6,
    }
    mirror = source_chapter("unverified", 35)
    latest = source_chapter("community", 21)
    annotated = {
        **source_chapter("annotated", 21),
        "title": "Episode 21 (ch. 21)",
    }
    result = reconcile_numbering(
        [*owned, correspondence, ambiguous, mirror, latest, annotated]
    )
    assert result[ambiguous["id"]]["numbering_status"] == "pending_evidence"
    assert result[mirror["id"]]["numbering_status"] == "pending_evidence"
    assert result[latest["id"]]["canonical_chapter"] == "21"
    assert result[latest["id"]]["numbering_status"] == "mapped"
    assert result[annotated["id"]]["canonical_chapter"] == "21"
    assert result[annotated["id"]]["numbering_status"] == "mapped"
