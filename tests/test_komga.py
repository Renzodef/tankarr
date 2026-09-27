from __future__ import annotations

import json
import logging
from unittest.mock import AsyncMock, Mock

import pytest
import respx
from httpx import Request, Response

from tankarr.config import Settings
from tankarr.komga import KomgaClient, KomgaReconciliationError

BASE_URL = "https://komga.test"
LIBRARY_ID = "library-manga"
TARGET = "Example (Author)/Example - v001 c001 [en].cbz"


def settings() -> Settings:
    return Settings(
        komga_url=BASE_URL,
        komga_library_id=LIBRARY_ID,
        komga_api_key="test-key",
        komga_reconcile_timeout_seconds=5,
    )


def library(**updates: object) -> dict:
    result = {
        "id": LIBRARY_ID,
        "name": "Manga · English",
        "root": "/comics",
        "unavailable": False,
        "hashFiles": True,
        "emptyTrashAfterScan": False,
        "scanInterval": "DISABLED",
        "scanOnStartup": False,
    }
    result.update(updates)
    return result


def page(*items: dict) -> Response:
    return Response(
        200,
        json={
            "content": list(items),
            "totalElements": len(items),
        },
    )


def deleted_filter(request: Request) -> bool:
    payload = json.loads(request.content)
    conditions = payload["condition"]["allOf"]
    deleted = next(
        condition["deleted"] for condition in conditions if "deleted" in condition
    )
    return deleted["operator"] == "isTrue"


@pytest.mark.asyncio
@respx.mock
async def test_probe_catalogue_is_read_only_and_reports_selected_library_counts():
    respx.get(f"{BASE_URL}/api/v1/libraries").mock(
        return_value=Response(200, json=[library()])
    )
    respx.post(f"{BASE_URL}/api/v1/books/list").mock(
        return_value=page({"id": "book-1"}, {"id": "book-2"})
    )
    respx.post(f"{BASE_URL}/api/v1/series/list").mock(
        return_value=page({"id": "series-1"})
    )

    result = await KomgaClient(settings()).probe_catalogue()

    assert result == {
        "ok": True,
        "auth_method": "api_key",
        "library_id": LIBRARY_ID,
        "library_name": "Manga · English",
        "book_count": 2,
        "series_count": 1,
    }
    request = respx.calls[0].request
    assert request.headers["X-API-Key"] == "test-key"
    assert "Authorization" not in request.headers


@pytest.mark.asyncio
@respx.mock
async def test_basic_auth_is_used_exclusively_when_selected():
    configured = settings()
    configured.komga_auth_method = "basic"
    configured.komga_username = "reader@example.test"
    configured.komga_password = "reader-secret"
    respx.get(f"{BASE_URL}/api/v1/libraries").mock(
        return_value=Response(200, json=[library()])
    )
    respx.post(f"{BASE_URL}/api/v1/books/list").mock(return_value=page())
    respx.post(f"{BASE_URL}/api/v1/series/list").mock(return_value=page())

    result = await KomgaClient(configured).probe_catalogue()

    request = respx.calls[0].request
    assert result["auth_method"] == "basic"
    assert request.headers["Authorization"].startswith("Basic ")
    assert "X-API-Key" not in request.headers


@pytest.mark.asyncio
@respx.mock
async def test_catalogue_lookup_can_use_hidden_docker_route_with_one_public_url():
    configured = settings()
    configured.komga_url = "http://192.0.2.50:25600"
    configured.komga_internal_url = BASE_URL
    libraries = respx.get(f"{BASE_URL}/api/v1/libraries").mock(
        return_value=Response(200, json=[library()])
    )
    respx.post(f"{BASE_URL}/api/v1/books/list").mock(return_value=page())
    respx.post(f"{BASE_URL}/api/v1/series/list").mock(return_value=page())

    result = await KomgaClient(configured).probe_catalogue()

    assert result["ok"] is True
    assert libraries.called


@pytest.mark.asyncio
@respx.mock
async def test_scan_enforces_hashing_and_disables_automatic_empty_trash():
    respx.get(f"{BASE_URL}/api/v1/libraries").mock(
        return_value=Response(
            200,
            json=[
                library(
                    hashFiles=False,
                    emptyTrashAfterScan=True,
                    scanInterval="EVERY_6_HOURS",
                    scanOnStartup=True,
                )
            ],
        )
    )
    update = respx.patch(f"{BASE_URL}/api/v1/libraries/{LIBRARY_ID}").mock(
        return_value=Response(204)
    )
    scan = respx.post(f"{BASE_URL}/api/v1/libraries/{LIBRARY_ID}/scan").mock(
        return_value=Response(202)
    )
    respx.post(f"{BASE_URL}/api/v1/books/list").mock(return_value=page())

    result = await KomgaClient(settings()).scan()

    assert result["triggered"] is True
    assert result["policy_updated"] is True
    assert json.loads(update.calls[0].request.content) == {
        "hashFiles": True,
        "emptyTrashAfterScan": False,
        "scanInterval": "DISABLED",
        "scanOnStartup": False,
    }
    assert scan.called


@pytest.mark.asyncio
@respx.mock
async def test_scan_waits_until_the_expected_import_is_present_and_hashed():
    respx.get(f"{BASE_URL}/api/v1/libraries").mock(
        return_value=Response(200, json=[library()])
    )
    respx.post(f"{BASE_URL}/api/v1/libraries/{LIBRARY_ID}/scan").mock(
        return_value=Response(202)
    )
    respx.post(f"{BASE_URL}/api/v1/books/list").mock(
        return_value=page({"id": "book-1", "url": f"/comics/{TARGET}", "fileHash": "a"})
    )

    result = await KomgaClient(settings()).scan([TARGET])

    assert result["expected_books"] == 1
    assert result["matched_expected_books"] == 1


@pytest.mark.asyncio
@respx.mock
async def test_ensure_present_scans_only_when_a_tracked_book_is_missing():
    respx.get(f"{BASE_URL}/api/v1/libraries").mock(
        return_value=Response(200, json=[library()])
    )
    scan = respx.post(f"{BASE_URL}/api/v1/libraries/{LIBRARY_ID}/scan").mock(
        return_value=Response(202)
    )

    def books_response(_: Request) -> Response:
        # Automatic scans are disabled by policy: the book can only appear
        # after Tankarr requested a scan.
        if not scan.called:
            return page()
        return page({"id": "book-1", "url": f"/comics/{TARGET}", "fileHash": "a"})

    respx.post(f"{BASE_URL}/api/v1/books/list").mock(side_effect=books_response)

    result = await KomgaClient(settings()).ensure_present([TARGET])

    assert result["ready"] is True
    assert result["expected_books"] == 1
    assert result["matched_expected_books"] == 1
    assert scan.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_prepare_for_moves_waits_until_every_active_book_has_a_hash(
    monkeypatch,
):
    respx.get(f"{BASE_URL}/api/v1/libraries").mock(
        return_value=Response(200, json=[library()])
    )
    calls = 0

    def books_response(_: Request) -> Response:
        nonlocal calls
        calls += 1
        item = {"id": "book-1", "url": f"/comics/{TARGET}"}
        if calls > 1:
            item["fileHash"] = "0123456789abcdef0123456789abcdef"
        return page(item)

    respx.post(f"{BASE_URL}/api/v1/books/list").mock(side_effect=books_response)
    scan = respx.post(f"{BASE_URL}/api/v1/libraries/{LIBRARY_ID}/scan").mock(
        return_value=Response(202)
    )
    monkeypatch.setattr("tankarr.komga.asyncio.sleep", AsyncMock())

    result = await KomgaClient(settings()).prepare_for_moves()

    assert result["ready"] is True
    assert result["active_books"] == 1
    assert result["hashed_books"] == 1
    assert scan.called


@pytest.mark.asyncio
@respx.mock
async def test_reconcile_deleted_scans_then_purges_only_authorized_records():
    respx.get(f"{BASE_URL}/api/v1/libraries").mock(
        return_value=Response(200, json=[library()])
    )
    respx.post(f"{BASE_URL}/api/v1/libraries/{LIBRARY_ID}/scan").mock(
        return_value=Response(202)
    )
    empty = respx.post(f"{BASE_URL}/api/v1/libraries/{LIBRARY_ID}/empty-trash").mock(
        return_value=Response(202)
    )
    respx.get(f"{BASE_URL}/sse/v1/events").mock(
        return_value=Response(
            200,
            text='event: TaskQueueStatus\ndata: {"count": 0, "countByType": {}}\n\n',
            headers={"content-type": "text/event-stream"},
        )
    )
    deleted_book_calls = 0
    deleted_series_calls = 0

    def books_response(request: Request) -> Response:
        nonlocal deleted_book_calls
        if not deleted_filter(request):
            return page()
        deleted_book_calls += 1
        if deleted_book_calls <= 2:
            return page({"id": "book-1", "url": f"/comics/{TARGET}"})
        return page()

    def series_response(request: Request) -> Response:
        nonlocal deleted_series_calls
        assert deleted_filter(request)
        deleted_series_calls += 1
        if deleted_series_calls <= 2:
            return page({"id": "series-1", "url": "/comics/Example (Author)"})
        return page()

    respx.post(f"{BASE_URL}/api/v1/books/list").mock(side_effect=books_response)
    respx.post(f"{BASE_URL}/api/v1/series/list").mock(side_effect=series_response)
    safety_check = Mock()

    result = await KomgaClient(settings()).reconcile_deleted(
        [TARGET], safety_check=safety_check
    )

    assert result["purged"] is True
    assert result["matched_books"] == 1
    safety_check.assert_called_once_with()
    assert empty.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_reconcile_deleted_refuses_unrelated_trash_without_emptying_it():
    respx.get(f"{BASE_URL}/api/v1/libraries").mock(
        return_value=Response(200, json=[library()])
    )
    respx.post(f"{BASE_URL}/api/v1/libraries/{LIBRARY_ID}/scan").mock(
        return_value=Response(202)
    )

    def books_response(request: Request) -> Response:
        if deleted_filter(request):
            return page({"id": "other", "url": "/comics/Other/Other.cbz"})
        return page()

    respx.post(f"{BASE_URL}/api/v1/books/list").mock(side_effect=books_response)
    respx.post(f"{BASE_URL}/api/v1/series/list").mock(return_value=page())
    empty = respx.post(f"{BASE_URL}/api/v1/libraries/{LIBRARY_ID}/empty-trash").mock(
        return_value=Response(202)
    )

    with pytest.raises(KomgaReconciliationError, match="outside the authorized"):
        await KomgaClient(settings()).reconcile_deleted([TARGET], safety_check=Mock())

    assert empty.call_count == 0


@pytest.mark.asyncio
@respx.mock
async def test_reconcile_deleted_refuses_catalogue_records_without_a_safe_path():
    respx.get(f"{BASE_URL}/api/v1/libraries").mock(
        return_value=Response(200, json=[library()])
    )
    respx.post(f"{BASE_URL}/api/v1/libraries/{LIBRARY_ID}/scan").mock(
        return_value=Response(202)
    )

    def books_response(request: Request) -> Response:
        if deleted_filter(request):
            return page({"id": "malformed-book", "url": None})
        return page()

    respx.post(f"{BASE_URL}/api/v1/books/list").mock(side_effect=books_response)
    respx.post(f"{BASE_URL}/api/v1/series/list").mock(return_value=page())
    empty = respx.post(f"{BASE_URL}/api/v1/libraries/{LIBRARY_ID}/empty-trash").mock(
        return_value=Response(202)
    )

    with pytest.raises(KomgaReconciliationError, match="safe absolute path"):
        await KomgaClient(settings()).reconcile_deleted([TARGET], safety_check=Mock())

    assert empty.call_count == 0


@pytest.mark.asyncio
@respx.mock
async def test_destructive_reconciliation_refuses_an_unavailable_library():
    respx.get(f"{BASE_URL}/api/v1/libraries").mock(
        return_value=Response(200, json=[library(unavailable=True)])
    )

    with pytest.raises(KomgaReconciliationError, match="root unavailable"):
        await KomgaClient(settings()).reconcile_deleted([TARGET], safety_check=Mock())


@pytest.mark.asyncio
@respx.mock
async def test_alignment_never_rescans_while_komga_is_only_hashing(monkeypatch):
    """A rescan re-queues Komga's own analysis, so waiting for hashes must not scan."""

    import asyncio as asyncio_module

    async def instant_sleep(_: float) -> None:
        return None

    monkeypatch.setattr(asyncio_module, "sleep", instant_sleep)
    respx.get(f"{BASE_URL}/api/v1/libraries").mock(
        return_value=Response(200, json=[library()])
    )
    calls = 0

    def books_response(_: Request) -> Response:
        nonlocal calls
        calls += 1
        file_hash = "a" if calls >= 4 else None
        return page({"id": "book-1", "url": f"/comics/{TARGET}", "fileHash": file_hash})

    respx.post(f"{BASE_URL}/api/v1/books/list").mock(side_effect=books_response)
    scan = respx.post(f"{BASE_URL}/api/v1/libraries/{LIBRARY_ID}/scan").mock(
        return_value=Response(202)
    )

    result = await KomgaClient(settings()).ensure_present([TARGET])

    assert result["ready"] is True
    assert result["matched_expected_books"] == 1
    assert scan.call_count == 0
    assert calls >= 4


def _trash_fixture(deleted_books, deleted_series, active_books, active_series=()):
    respx.get(f"{BASE_URL}/api/v1/libraries").mock(
        return_value=Response(200, json=[library()])
    )

    def books_response(request: Request) -> Response:
        return page(*(deleted_books if deleted_filter(request) else active_books))

    def series_response(request: Request) -> Response:
        return page(*(deleted_series if deleted_filter(request) else active_series))

    respx.post(f"{BASE_URL}/api/v1/books/list").mock(side_effect=books_response)
    respx.post(f"{BASE_URL}/api/v1/series/list").mock(side_effect=series_response)
    return respx.post(f"{BASE_URL}/api/v1/libraries/{LIBRARY_ID}/empty-trash").mock(
        return_value=Response(202)
    )


@pytest.mark.asyncio
@respx.mock
async def test_purge_stale_trash_empties_rename_and_removal_debris():
    empty = _trash_fixture(
        deleted_books=[
            # Renamed: same content still active under the volume name.
            {
                "id": "old",
                "url": "/comics/BECK (A)/BECK - c001 [en].cbz",
                "fileHash": "h1",
            },
            # Removed by Tankarr: not tracked, gone from disk.
            {"id": "gone", "url": "/comics/Phoenix (B)/Phoenix - c012 [en].cbz"},
        ],
        deleted_series=[
            {"id": "s-old", "url": "/comics/BECK (A)"},  # dir still active
            {"id": "s-gone", "url": "/comics/Phoenix (B)"},  # dir removed
        ],
        active_books=[
            {
                "id": "new",
                "url": "/comics/BECK (A)/BECK - v001 [en].cbz",
                "fileHash": "h1",
            }
        ],
        active_series=[{"id": "s-new", "url": "/comics/BECK (A)"}],
    )
    on_disk = {"BECK (A)", "BECK (A)/BECK - v001 [en].cbz"}

    result = await KomgaClient(settings()).purge_stale_trash(
        ["BECK (A)/BECK - v001 [en].cbz"], path_exists=lambda rel: rel in on_disk
    )

    assert result["purged"] is True
    assert result["renamed_books"] == 1 and result["removed_books"] == 1
    assert result["stale_series"] == 2
    assert empty.called


@pytest.mark.asyncio
@respx.mock
async def test_purge_stale_trash_keeps_trash_when_a_record_still_exists_on_disk():
    empty = _trash_fixture(
        deleted_books=[
            {
                "id": "mid-scan",
                "url": "/comics/Example (Author)/Example - c001 [en].cbz",
            }
        ],
        deleted_series=[],
        active_books=[],
    )
    on_disk = {"Example (Author)/Example - c001 [en].cbz"}

    result = await KomgaClient(settings()).purge_stale_trash(
        [], path_exists=lambda rel: rel in on_disk
    )

    assert result["purged"] is False
    assert result["unexplained"] == ["Example (Author)/Example - c001 [en].cbz"]
    assert not empty.called


@pytest.mark.asyncio
@respx.mock
async def test_purge_stale_trash_is_a_no_op_without_trash():
    empty = _trash_fixture(deleted_books=[], deleted_series=[], active_books=[])
    result = await KomgaClient(settings()).purge_stale_trash(
        [TARGET], path_exists=lambda rel: False
    )
    assert result == {
        "configured": True,
        "triggered": False,
        "purged": False,
        "stale_books": 0,
        "stale_series": 0,
    }
    assert not empty.called


@pytest.mark.asyncio
async def test_a_reader_that_cannot_empty_its_trash_says_so(caplog, tmp_path):
    """A reader without such a call leaves a record for every renamed file.
    Nobody sees that until they open the reader, so it must reach the log."""

    from tankarr.service import TankarrService

    class RefusingReader:
        configured = True

        async def purge_stale_trash(self, expected, *, path_exists):
            raise ValueError("Validation error: Field 'cleanLibrary' is undefined")

    service = TankarrService.__new__(TankarrService)
    service.komga = RefusingReader()
    service._library_root = lambda: tmp_path

    with caplog.at_level(logging.WARNING, logger="tankarr.service"):
        result = await service._purge_stale_komga_trash(())

    assert result["purged"] is False
    assert "cleanLibrary" in result["error"]
    assert any(
        "Could not empty the reader's trash" in r.message for r in caplog.records
    )
