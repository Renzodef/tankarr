from unittest.mock import AsyncMock

import pytest

from tankarr.chapter_map import MapEntry
from tankarr.chapter_recovery import ChapterRecovery
from tests.test_deletion import chapter, make_service, manga


@pytest.fixture
def recovery(tmp_path):
    database, service, _ = make_service(tmp_path)
    service.settings.internet_archive_enabled = False
    database.upsert_manga(manga(), "en", "all")
    database.update_manga("manga-1", {"series_unit_override": "volumes"})
    database.upsert_chapters("manga-1", [chapter("c1", "1"), chapter("c2", "2")])
    return ChapterRecovery(service, AsyncMock())


@pytest.mark.asyncio
async def test_explicit_recovery_without_map_is_reviewed_and_keeps_format(recovery):
    before = recovery.database.get_manga("manga-1")
    preview = recovery.preview("manga-1")
    assert len(preview["chapters"]) == 2
    recovery.worker.enqueue.assert_not_called()
    with pytest.raises(ValueError, match="Preview"):
        await recovery.queue("manga-1", None)
    result = await recovery.queue("manga-1", preview["confirmation_snapshot"])
    assert result["queued"] == 2
    assert recovery.worker.enqueue.await_count == 2
    after = recovery.database.get_manga("manga-1")
    for key in ("series_unit_override", "preferred_language", "monitor_mode"):
        assert after[key] == before[key]
    with pytest.raises(ValueError, match="Preview"):
        await recovery.queue("manga-1", preview["confirmation_snapshot"])
    assert recovery.preview("manga-1")["chapters"] == []


@pytest.mark.asyncio
async def test_recovery_revalidates_monitoring_and_safe_mode(recovery):
    preview = recovery.preview("manga-1")
    recovery.database.set_chapter_monitored("c1", False)
    with pytest.raises(ValueError, match="Preview"):
        await recovery.queue("manga-1", preview["confirmation_snapshot"])
    assert len(recovery.preview("manga-1")["chapters"]) == 1
    recovery.service.settings.restored_safe_mode = True
    from tankarr.service import RecoveryBlocked

    with pytest.raises(RecoveryBlocked):
        await recovery.queue("manga-1", preview["confirmation_snapshot"])


@pytest.mark.asyncio
async def test_automatic_fallback_requires_map_and_global_assembly(recovery):
    service, database = recovery.service, recovery.database
    service.settings.assemble_books_automatically = True
    assert await service.create_missing_download_jobs("manga-1") == []
    database.replace_chapter_map(
        "manga-1", "operator", [MapEntry(("1",), ("1", "2"), True, source="operator")]
    )
    service.settings.assemble_books_automatically = False
    assert await service.create_missing_download_jobs("manga-1") == []
    service.settings.assemble_books_automatically = True
    jobs = await service.create_missing_download_jobs("manga-1")
    assert len(jobs) == 2
    assert await service.create_missing_download_jobs("manga-1") == []


@pytest.mark.asyncio
async def test_whole_volume_precedes_chapter_fallback(recovery):
    service, database = recovery.service, recovery.database
    service.settings.assemble_books_automatically = True
    database.replace_chapter_map(
        "manga-1", "operator", [MapEntry(("1",), ("1", "2"), True, source="operator")]
    )
    database.upsert_chapters(
        "manga-1", [{**chapter("v1", ""), "chapter": None, "release_unit": "volume"}]
    )
    jobs = await service.create_missing_download_jobs("manga-1")
    assert [job["chapter_id"] for job in jobs] == ["v1"]
    assert await service.create_missing_download_jobs("manga-1") == []


@pytest.mark.asyncio
async def test_indexer_error_is_not_proof_a_volume_is_unavailable(recovery):
    service, database = recovery.service, recovery.database
    service.settings.assemble_books_automatically = True
    service.settings.prowlarr_enabled = True
    service.settings.prowlarr_api_key = "fixture-key"
    database.replace_chapter_map(
        "manga-1", "operator", [MapEntry(("1",), ("1", "2"), True, source="operator")]
    )
    assert await service.create_missing_download_jobs("manga-1") == []
    database.record_wanted_attempt(
        "manga-1", "volume:1", channel="indexer_book", outcome="error", detail="timeout"
    )
    assert await service.create_missing_download_jobs("manga-1") == []
    database.record_wanted_attempt(
        "manga-1",
        "volume:1",
        channel="indexer_book",
        outcome="not_offered",
        detail="no eligible book",
    )
    assert len(await service.create_missing_download_jobs("manga-1")) == 2


@pytest.mark.asyncio
async def test_book_arriving_later_does_not_duplicate_active_chapter_fallback(recovery):
    service, database = recovery.service, recovery.database
    service.settings.assemble_books_automatically = True
    database.replace_chapter_map(
        "manga-1", "operator", [MapEntry(("1",), ("1", "2"), True, source="operator")]
    )
    assert len(await service.create_missing_download_jobs("manga-1")) == 2
    database.upsert_chapters(
        "manga-1", [{**chapter("v1", ""), "chapter": None, "release_unit": "volume"}]
    )
    assert await service.create_missing_download_jobs("manga-1") == []


def test_explicit_recovery_waits_for_active_book(recovery):
    database = recovery.database
    database.upsert_chapters(
        "manga-1", [{**chapter("v1", ""), "chapter": None, "release_unit": "volume"}]
    )
    database.create_job("manga-1", "v1", "en")
    with pytest.raises(ValueError, match="whole-volume download"):
        recovery.preview("manga-1")


@pytest.mark.asyncio
async def test_map_change_requires_another_confirmation(recovery):
    plan = recovery.preview("manga-1")
    recovery.database.replace_chapter_map(
        "manga-1", "operator", [MapEntry(("1",), ("1", "2"), True, source="operator")]
    )
    with pytest.raises(ValueError, match="Preview"):
        await recovery.queue("manga-1", plan["confirmation_snapshot"])
    recovery.worker.enqueue.assert_not_called()


def test_recovery_http_contract(recovery):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from tankarr.chapter_recovery import register_chapter_recovery_routes

    app = FastAPI()
    register_chapter_recovery_routes(app, recovery.service, recovery.worker)
    with TestClient(app) as client:
        url = "/api/manga/manga-1/recover-chapters"
        response = client.post(url + "?dry_run=true")
        assert response.status_code == 200
        assert len(response.json()["chapters"]) == 2
        recovery.worker.enqueue.assert_not_called()
        assert client.post(url).status_code == 409
        assert client.post(url + "?confirmation_snapshot=invalid").status_code == 422
        args = {"confirmation_snapshot": response.json()["confirmation_snapshot"]}
        queued = client.post(url, params=args)
        assert queued.status_code == 200
        assert queued.json()["queued"] == 2
        assert client.post(url, params=args).status_code == 409
