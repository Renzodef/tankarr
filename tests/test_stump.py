from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from httpx import Response

from tankarr.config import Settings
from tankarr.stump import StumpLibraryClient

STUMP = "https://stump.test"


class FakeStump:
    """Just enough of Stump's GraphQL surface for the client under test."""

    def __init__(self, paths: list[str]):
        self.paths = list(paths)
        self.series_paths = {path.rsplit("/", 1)[0] for path in paths}
        self.pending: list[str] = []
        self.jobs: list[dict] = [
            {
                "id": "job-old",
                "name": "library_scan",
                "description": "/data/comics",
                "status": "COMPLETED",
                "createdAt": "2026-08-29T15:36:29+00:00",
                "completedAt": "2026-08-29T15:43:48+00:00",
                "msElapsed": 438533,
            }
        ]
        self.scans = 0
        self.logins = 0
        self.polls = 0
        self.deleted: list[str] = []
        self.cleaned = 0
        self.cleaned_media_count = 0
        self.clean_missing = False
        self.missing_series_paths: set[str] = set()
        self.series_scans = 0
        self.missing_paths: set[str] = set()
        self.artwork_uploads: list[dict] = []

    def login(self, request):
        self.logins += 1
        return Response(200, json={"accessToken": "token-1"})

    def graphql(self, request):
        if request.headers.get("authorization") != "Bearer token-1":
            return Response(401, json={"error": "unauthorized"})
        body = json.loads(request.content)
        query = body["query"]
        variables = body.get("variables") or {}
        if "libraries {" in query:
            data = {
                "libraries": {
                    "nodes": [{"id": "lib-1", "name": "Comics", "path": "/data/comics"}]
                }
            }
        elif query.startswith("query($path") and "media(" in query:
            path = variables["path"]
            nodes = []
            if path in self.paths:
                nodes.append(
                    {
                        "id": f"id:{path}",
                        "path": path,
                        "seriesId": f"series:{path.rsplit('/', 1)[0]}",
                        "status": "MISSING" if path in self.missing_paths else "READY",
                        "deletedAt": None,
                    }
                )
            data = {"media": {"nodes": nodes}}
        elif query.startswith("query($path") and "series(" in query:
            path = variables["path"]
            nodes = []
            if path in self.series_paths:
                nodes.append(
                    {
                        "id": f"series:{path}",
                        "path": path,
                        "status": "MISSING"
                        if path in self.missing_series_paths
                        else "READY",
                    }
                )
            data = {"series": {"nodes": nodes}}
        elif query.startswith("query($page"):
            page = variables["page"]
            size = variables["size"]
            nodes = [
                {
                    "id": f"id:{path}",
                    "path": path,
                    "seriesId": f"series:{path.rsplit('/', 1)[0]}",
                    "status": "MISSING" if path in self.missing_paths else "READY",
                    "deletedAt": None,
                }
                for path in self.paths[(page - 1) * size : page * size]
            ]
            data = {"media": {"nodes": nodes}}
        elif "jobs(" in query:
            data = {"jobs": {"nodes": list(reversed(self.jobs))}}
        elif "jobById" in query:
            self.polls += 1
            job = next(j for j in self.jobs if j["id"] == variables["id"])
            if job["status"] == "RUNNING" and self.polls >= 2:
                job["status"] = "COMPLETED"
                job["completedAt"] = "2026-08-30T20:00:10+00:00"
                job["msElapsed"] = 10_000
                self.paths.extend(self.pending)
                self.series_paths.update(
                    path.rsplit("/", 1)[0] for path in self.pending
                )
                self.pending = []
            data = {"jobById": job}
        elif "scanLibrary" in query:
            self.scans += 1
            self.jobs.append(
                {
                    "id": f"job-{self.scans}",
                    "name": "library_scan",
                    "description": "/data/comics",
                    "status": "RUNNING",
                    "createdAt": "2026-08-30T20:00:00+00:00",
                    "completedAt": None,
                    "msElapsed": 0,
                }
            )
            data = {"scanLibrary": True}  # Stump answers only `true`
        elif "scanSeries" in query:
            self.series_scans += 1
            series_id = variables["id"]
            series_path = series_id.removeprefix("series:")
            self.jobs.append(
                {
                    "id": f"series-job-{self.series_scans}",
                    "name": "series_scan",
                    "description": series_path,
                    "status": "RUNNING",
                    "createdAt": datetime.now(UTC).isoformat(),
                    "completedAt": None,
                    "msElapsed": 0,
                }
            )
            data = {"scanSeries": True}
        elif "uploadSeriesThumbnailBase64" in query:
            self.artwork_uploads.append(
                {"kind": "series", "id": variables["id"], "image": variables["image"]}
            )
            data = {"uploadSeriesThumbnailBase64": {"id": variables["id"]}}
        elif "uploadMediaThumbnailBase64" in query:
            self.artwork_uploads.append(
                {"kind": "book", "id": variables["id"], "image": variables["image"]}
            )
            data = {"uploadMediaThumbnailBase64": {"id": variables["id"]}}
        elif "deleteMedia" in query:
            self.deleted.append(variables["id"])
            self.paths = [
                path for path in self.paths if f"id:{path}" != variables["id"]
            ]
            data = {"deleteMedia": {"id": variables["id"]}}
        elif "cleanLibrary" in query:
            self.cleaned += 1
            if self.clean_missing:
                self.paths = [
                    p
                    for p in self.paths
                    if p not in self.missing_paths
                    and p.rsplit("/", 1)[0] not in self.missing_series_paths
                ]
            data = {
                "cleanLibrary": {
                    "deletedMediaCount": self.cleaned_media_count,
                    "deletedSeriesCount": 1,
                }
            }
        elif "librariesStats" in query:
            data = {"librariesStats": {"bookCount": len(self.paths), "seriesCount": 2}}
        else:  # pragma: no cover - unexpected query
            return Response(400, json={"errors": [{"message": f"unknown {query}"}]})
        return Response(200, json={"data": data})


def settings() -> Settings:
    return Settings(
        reader_kind="stump",
        reader_url=STUMP,
        reader_username="fixture-user",
        reader_password="secret",
        reader_library_path="/data/comics",
        komga_reconcile_timeout_seconds=30,
    )


@pytest.fixture
def stump():
    fake = FakeStump(
        [
            "/data/comics/Series A (Author)/Series A - c001 [en].cbz",
            "/data/comics/Old Name (Author)/Old Name - c001 [en].cbz",
        ]
    )
    with respx.mock(assert_all_called=False) as mock:
        mock.post(f"{STUMP}/api/v2/auth/login").mock(side_effect=fake.login)
        mock.post(f"{STUMP}/api/graphql").mock(side_effect=fake.graphql)
        yield fake


def test_unconfigured_reader_is_inert():
    client = StumpLibraryClient(Settings(reader_kind="stump"))
    assert client.configured is False


@pytest.mark.asyncio
async def test_deleted_books_bypass_scan_cooldown_and_confirm_reader_cleanup(
    stump, monkeypatch
):
    monkeypatch.setattr("tankarr.stump.asyncio.sleep", _no_sleep)
    stump.jobs[-1]["completedAt"] = datetime.now(UTC).isoformat()
    target = stump.paths[0]
    stump.clean_missing = True
    client = StumpLibraryClient(settings())
    original = client._wait_for_job

    async def finish_scan(*args):
        result = await original(*args)
        stump.missing_paths.add(target)
        return result

    monkeypatch.setattr(client, "_wait_for_job", finish_scan)
    result = await client.reconcile_deleted([target.removeprefix("/data/comics/")])
    assert result["purged"] and not result["sync_pending"]
    assert stump.scans == 1 and stump.cleaned == 1
    assert target not in stump.paths and len(stump.paths) == 1
    await client.reconcile_deleted([target.removeprefix("/data/comics/")])
    assert stump.scans == 1


@pytest.mark.asyncio
async def test_running_deletion_scan_keeps_synchronization_pending(stump, monkeypatch):
    client = StumpLibraryClient(settings())

    async def running_scan():
        return {"configured": True, "scan_running": True}

    monkeypatch.setattr(client, "force_scan", running_scan)
    result = await client.reconcile_deleted(
        [stump.paths[0].removeprefix("/data/comics/")]
    )
    assert result["sync_pending"] and not result["purged"]
    assert stump.cleaned == 0


@pytest.mark.asyncio
async def test_missing_series_is_cleaned_even_when_its_books_still_say_ready(stump):
    target = stump.paths[0]
    stump.missing_series_paths.add(target.rsplit("/", 1)[0])
    stump.clean_missing = True
    result = await StumpLibraryClient(settings()).reconcile_deleted(
        [target.removeprefix("/data/comics/")]
    )
    assert result["purged"] and not result["sync_pending"]
    assert stump.scans == 0 and stump.cleaned == 1
    assert stump.paths == ["/data/comics/Old Name (Author)/Old Name - c001 [en].cbz"]


@pytest.mark.asyncio
async def test_ensure_present_only_scans_when_a_tracked_book_is_missing(
    stump, monkeypatch
):
    monkeypatch.setattr("tankarr.stump.asyncio.sleep", _no_sleep)
    client = StumpLibraryClient(settings())
    expected = ["Series A (Author)/Series A - c001 [en].cbz"]

    report = await client.ensure_present(expected)
    assert report["ready"] is True and report["triggered"] is False
    assert report["matched_expected_books"] == 1 and report["expected_books"] == 1
    assert report["stale_books"] == 1
    assert report["scan_job"]["id"] == "job-old"
    assert stump.scans == 0

    stump.pending = ["/data/comics/Series B (Author)/Series B - c002 [en].cbz"]
    expected.append("Series B (Author)/Series B - c002 [en].cbz")
    report = await client.ensure_present(expected)
    assert report["triggered"] is True and stump.scans == 1
    # The audit starts the scan and returns: readiness never waits for it.
    assert report["ready"] is True and report["scan_running"] is True
    assert report["scan_job"]["status"] == "RUNNING"
    assert report["matched_expected_books"] == 1 and report["missing_books"] == 1

    report = await client.scan(expected)
    assert stump.scans == 1  # the running job is reused, not duplicated
    assert report["matched_expected_books"] == 2 and report["missing_books"] == 0
    assert report["scan_job"]["status"] == "COMPLETED"
    assert report["scan_running"] is False
    assert stump.logins == 1


@pytest.mark.asyncio
async def test_ensure_present_defers_after_a_recent_expensive_scan(stump):
    stump.jobs[-1].update(
        {
            "completedAt": datetime.now(UTC).isoformat(),
            "msElapsed": 60_000,
        }
    )
    client = StumpLibraryClient(settings())

    report = await client.ensure_present(["Missing (Author)/Missing - c001 [en].cbz"])

    assert report["triggered"] is False
    assert report["scan_deferred"] is True
    assert report["retry_in_seconds"] > 0
    assert stump.scans == 0


@pytest.mark.asyncio
async def test_automatic_scan_defers_but_manual_scan_can_force(stump, monkeypatch):
    monkeypatch.setattr("tankarr.stump.asyncio.sleep", _no_sleep)
    stump.jobs[-1].update(
        {
            "completedAt": datetime.now(UTC).isoformat(),
            "msElapsed": 60_000,
        }
    )
    client = StumpLibraryClient(settings())

    automatic = await client.scan([])
    manual = await client.force_scan([])

    assert automatic["triggered"] is False
    assert automatic["scan_deferred"] is True
    assert automatic["retry_in_seconds"] > 0
    assert manual["triggered"] is True
    assert manual["scan_deferred"] is False
    assert stump.scans == 1


@pytest.mark.asyncio
async def test_import_into_known_series_uses_and_coalesces_series_scan(stump):
    client = StumpLibraryClient(settings())
    relative = "Series A (Author)/Series A - c002 [en].cbz"

    first = await client.sync_imported_path(relative)
    second = await client.sync_imported_path(relative)

    assert first["triggered"] is True
    assert first["scan_scope"] == "series"
    assert second["triggered"] is False
    assert second["scan_scope"] == "series"
    assert second["scan_running"] is True
    assert stump.series_scans == 1
    assert stump.scans == 0


@pytest.mark.asyncio
async def test_alignment_repairs_a_known_series_without_a_library_scan(stump):
    client = StumpLibraryClient(settings())

    report = await client.ensure_present(
        [
            "Series A (Author)/Series A - c001 [en].cbz",
            "Series A (Author)/Series A - c002 [en].cbz",
        ]
    )

    assert report["triggered"] is True
    assert report["scan_scope"] == "series"
    assert report["scan_running"] is True
    assert stump.series_scans == 1
    assert stump.scans == 0


@pytest.mark.asyncio
async def test_series_retry_does_not_inherit_the_periodic_library_interval(stump):
    completed = datetime.now(UTC) - timedelta(minutes=2)
    stump.jobs.append(
        {
            "id": "series-job-completed",
            "name": "series_scan",
            "description": "/data/comics/Series A (Author)",
            "status": "COMPLETED",
            "createdAt": (completed - timedelta(seconds=1)).isoformat(),
            "completedAt": completed.isoformat(),
            "msElapsed": 1_000,
        }
    )
    client = StumpLibraryClient(settings())

    report = await client.sync_imported_path(
        "Series A (Author)/Series A - c002 [en].cbz"
    )

    assert report["triggered"] is True
    assert report["scan_scope"] == "series"
    assert stump.series_scans == 1
    assert stump.scans == 0


@pytest.mark.asyncio
async def test_scan_reports_the_alignment_after_the_job_completes(stump, monkeypatch):
    monkeypatch.setattr("tankarr.stump.asyncio.sleep", _no_sleep)
    client = StumpLibraryClient(settings())
    stump.pending = ["/data/comics/Series B (Author)/Series B - c002 [en].cbz"]

    report = await client.scan(
        [
            "Series A (Author)/Series A - c001 [en].cbz",
            "Series B (Author)/Series B - c002 [en].cbz",
            "Missing (Author)/Missing - c001 [en].cbz",
        ]
    )
    assert report["triggered"] is True and stump.scans == 1
    assert report["expected_books"] == 3
    assert report["matched_expected_books"] == 2
    assert report["missing_books"] == 1
    assert report["missing_sample"] == [
        "/data/comics/Missing (Author)/Missing - c001 [en].cbz"
    ]
    assert "error" not in report


@pytest.mark.asyncio
async def test_probe_reports_counts_and_last_scan(stump):
    client = StumpLibraryClient(settings())
    status = await client.probe()
    assert status["book_count"] == 2 and status["series_count"] == 2
    assert status["scan_running"] is False
    assert status["last_scan"]["id"] == "job-old"
    assert status["reader_label"] == "Stump"


@pytest.mark.asyncio
async def test_expired_token_is_renewed_once(stump):
    client = StumpLibraryClient(settings())
    from time import monotonic

    client._token = "stale"
    client._token_acquired_at = monotonic()  # looks fresh; the 401 forces a login
    status = await client.probe()
    assert status["configured"] is True
    assert stump.logins == 1


async def _no_sleep(_seconds: float) -> None:
    return None


def test_target_paths_join_the_reader_root():
    assert StumpLibraryClient._target_paths("/data/comics/", ["A/b.cbz"]) == {
        "/data/comics/A/b.cbz"
    }


@pytest.mark.asyncio
async def test_catalogue_mapping_and_tankarr_artwork_upload(stump, tmp_path):
    configured = settings()
    configured.data_dir = tmp_path
    artwork = tmp_path / "metadata" / "artwork" / "cover.jpg"
    artwork.parent.mkdir(parents=True)
    artwork.write_bytes(b"\xff\xd8\xfftankarr-cover")
    client = StumpLibraryClient(configured)
    relative = "Series A (Author)/Series A - c001 [en].cbz"

    catalogue = await client.catalogue_for_paths([relative])
    book = catalogue["books"][relative]
    result = await client.apply_catalogue_metadata(
        [
            {
                "target_kind": "series",
                "target_id": book["series_id"],
                "payload": None,
                "payload_sha256": None,
                "artwork_path": "metadata/artwork/cover.jpg",
                "artwork_sha256": "artwork-hash",
            },
            {
                "target_kind": "book",
                "target_id": book["id"],
                "payload": None,
                "payload_sha256": None,
                "artwork_path": "metadata/artwork/cover.jpg",
                "artwork_sha256": "artwork-hash",
            },
        ]
    )

    assert catalogue["series"] == {book["series_id"]: {"id": book["series_id"]}}
    assert result["artwork_uploaded"] == 2
    assert all(receipt["ok"] for receipt in result["receipts"])
    assert [item["kind"] for item in stump.artwork_uploads] == ["series", "book"]
    assert all(
        base64.b64decode(item["image"]) == artwork.read_bytes()
        for item in stump.artwork_uploads
    )


@pytest.mark.asyncio
async def test_graphql_errors_surface_as_values(stump):
    client = StumpLibraryClient(settings())
    async with client._client() as http:
        with pytest.raises((ValueError, httpx.HTTPStatusError)):
            await client._graphql(http, "{ nonsense }")


@pytest.mark.asyncio
async def test_purge_cleans_records_after_scanning_without_soft_delete(
    stump, monkeypatch
):
    monkeypatch.setattr("tankarr.stump.asyncio.sleep", _no_sleep)
    client = StumpLibraryClient(settings())
    tracked = ["Series A (Author)/Series A - c001 [en].cbz"]
    on_disk = set(tracked) | {"Untracked (Author)/Untracked - c001 [en].cbz"}
    stump.paths.append("/data/comics/Untracked (Author)/Untracked - c001 [en].cbz")
    stump.cleaned_media_count = 1

    result = await client.purge_stale_trash(tracked, path_exists=on_disk.__contains__)

    # Stump scans the missing path before cleanup. It must never soft-delete a
    # READY media row because Stump cannot recover it if the path reappears.
    assert result["purged"] is True and result["removed_books"] == 1
    assert stump.scans == 1 and stump.deleted == []
    assert stump.cleaned == 1 and result["stale_series"] == 1


@pytest.mark.asyncio
async def test_purge_cleans_already_missing_records_without_another_scan(stump):
    client = StumpLibraryClient(settings())
    tracked = ["Series A (Author)/Series A - c001 [en].cbz"]
    stale = "/data/comics/Old Name (Author)/Old Name - c001 [en].cbz"
    stump.missing_paths.add(stale)
    stump.cleaned_media_count = 1

    result = await client.purge_stale_trash(
        tracked, path_exists=lambda path: path in tracked
    )

    assert result["purged"] is True and result["removed_books"] == 1
    assert stump.scans == 0 and stump.cleaned == 1


@pytest.mark.asyncio
async def test_purge_refuses_when_a_tracked_file_is_not_on_disk(stump):
    client = StumpLibraryClient(settings())
    result = await client.purge_stale_trash(
        ["Series A (Author)/Series A - c001 [en].cbz"], path_exists=lambda _p: False
    )
    assert result["purged"] is False and stump.deleted == []


@pytest.mark.asyncio
async def test_purge_filesystem_checks_run_outside_the_event_loop(stump, monkeypatch):
    import threading

    monkeypatch.setattr("tankarr.stump.asyncio.sleep", _no_sleep)
    loop_thread = threading.get_ident()
    checks = []
    tracked = ["Series A (Author)/Series A - c001 [en].cbz"]

    def exists(path):
        checks.append(path)
        assert threading.get_ident() != loop_thread
        return path in tracked

    client = StumpLibraryClient(settings())
    await client.purge_stale_trash(tracked, path_exists=exists)
    assert len(checks) > len(tracked)
