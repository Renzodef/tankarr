from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from tankarr.database import Database
from tankarr.torrents import TorrentManager
from tests.test_torrents import (
    FakeMetadata,
    FakeProwlarr,
    FakeQBitTorrent,
    FakeService,
    direct_release,
    seed,
)


@pytest.fixture
def manager(tmp_path):
    database = Database(tmp_path / "db.sqlite3")
    database.initialize()
    seed(database)
    service = FakeService(tmp_path)
    service.settings.torrent_auto_import = False
    value = TorrentManager(
        database,
        service,
        Mock(),
        FakeMetadata(),
        FakeProwlarr(),
        FakeQBitTorrent(tmp_path),
    )
    yield value
    database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("start_monitor", [False, True])
async def test_stop_waits_for_direct_download_cleanup_and_keeps_job_resumable(
    manager, start_monitor
):
    job = manager.database.create_torrent_download("kamui", direct_release())
    started, cleaning, finish = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def download(_job):
        manager.database.update_torrent_download(job["id"], status="downloading")
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await finish.wait()
            # The database must remain usable until stop returns.
            manager.database.get_torrent_download(job["id"])

    manager._direct_download = download
    manager._run = AsyncMock(side_effect=lambda: None)
    if start_monitor:
        await manager.start()
    manager._start_direct_download(job)
    await started.wait()
    child = manager._direct_tasks[job["id"]]
    stopping = asyncio.create_task(manager.stop())
    try:
        await asyncio.wait_for(cleaning.wait(), 2)
        assert not stopping.done()
        manager._start_direct_download({**job, "id": job["id"] + 1})
        assert len(manager._direct_tasks) == 1
    finally:
        finish.set()
        await stopping
    assert child.done()
    assert not manager._direct_tasks
    assert manager.database.get_torrent_download(job["id"])["status"] == "downloading"
    await manager.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("usenet", [False, True])
async def test_moved_category_is_quarantined_without_starving_other_downloads(
    manager, usenet
):
    from types import SimpleNamespace

    from tests.test_torrents import release

    database = manager.database
    good = database.create_torrent_download("kamui", release(info_hash="1" * 40))
    bad = database.create_torrent_download(
        "kamui", {**release(info_hash="2" * 40), "id": "second-release"}
    )
    calls = []

    async def state(identifier):
        calls.append(identifier)
        foreign = identifier == ("bad-nzo" if usenet else bad["info_hash"])
        return {
            "where": "queue",
            "category": "other" if foreign else "tankarr",
            "status": "Downloading",
            "state": "downloading",
            "percentage": "50",
            "progress": 0.5,
        }

    if usenet:
        for job, identifier in ((good, "good-nzo"), (bad, "bad-nzo")):
            database.set_torrent_download_protocol(job["id"], "usenet")
            database.update_torrent_download(job["id"], client_id=identifier)
        manager.sabnzbd = SimpleNamespace(
            configured=True, job_state=state, delete=AsyncMock()
        )
    else:
        manager.qbittorrent.torrent_info = state
    await manager.poll_once()
    assert database.get_torrent_download(good["id"])["status"] == "downloading"
    assert database.get_torrent_download(good["id"])["progress"] == 0.5
    quarantined = database.get_torrent_download(bad["id"])
    assert quarantined["status"] == "failed"
    assert quarantined["qbit_state"] == "outside_category"
    assert "outside Tankarr" in quarantined["message"]
    calls.clear()
    await manager.poll_once()
    assert calls == ["good-nzo" if usenet else good["info_hash"]]
    assert not manager.qbittorrent.deleted
    if usenet:
        manager.sabnzbd.delete.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["Failed", "Completed", "Downloading", None])
async def test_sab_retry_resubmits_failed_history_and_preserves_existing_success(
    manager, status
):
    from types import SimpleNamespace

    from tests.test_torrents import usenet_release

    job = manager.database.create_torrent_download("kamui", usenet_release())
    manager.database.set_torrent_download_protocol(job["id"], "usenet")
    manager.database.update_torrent_download(
        job["id"], status="failed", client_id="old-nzo"
    )
    state = (
        None
        if status is None
        else {
            "where": "queue" if status == "Downloading" else "history",
            "category": "tankarr",
            "status": status,
        }
    )
    manager.sabnzbd = SimpleNamespace(
        configured=True,
        job_state=AsyncMock(return_value=state),
        add_nzb_url=AsyncMock(return_value="new-nzo"),
    )
    retried = await manager.retry(job["id"])
    assert retried["status"] != "failed"
    if status in {"Failed", None}:
        manager.sabnzbd.add_nzb_url.assert_awaited_once()
        assert retried["client_id"] == "new-nzo"
        assert retried["status"] == "queued"
    else:
        manager.sabnzbd.add_nzb_url.assert_not_awaited()
        assert retried["client_id"] == "old-nzo"


@pytest.mark.asyncio
async def test_sab_retry_does_not_readd_a_foreign_category(manager):
    from types import SimpleNamespace

    from tankarr.sabnzbd import SABnzbdError
    from tests.test_torrents import usenet_release

    job = manager.database.create_torrent_download("kamui", usenet_release())
    manager.database.set_torrent_download_protocol(job["id"], "usenet")
    manager.database.update_torrent_download(
        job["id"], status="failed", client_id="old-nzo"
    )
    manager.sabnzbd = SimpleNamespace(
        configured=True,
        job_state=AsyncMock(
            return_value={"where": "history", "category": "other", "status": "Failed"}
        ),
        add_nzb_url=AsyncMock(),
    )
    with pytest.raises(SABnzbdError, match="outside Tankarr"):
        await manager.retry(job["id"])
    manager.sabnzbd.add_nzb_url.assert_not_awaited()
    assert manager.database.get_torrent_download(job["id"])["status"] == "failed"


def imported_history(database, *, manga_id="kamui", count=401):
    from tests.test_torrents import release

    rows = []
    for index in range(count):
        job = database.create_torrent_download(
            manga_id,
            {**release(info_hash=f"{index + 1:040x}"), "id": f"release-{index}"},
        )
        rows.append(
            database.update_torrent_download(
                job["id"], status="imported", qbit_state="uploading"
            )
        )
    return rows


@pytest.mark.asyncio
async def test_seeding_cleanup_visits_all_retained_pages_and_skips_removed_history(
    manager,
):
    manager.service.settings.torrent_completed_action = "remove_when_seeded"
    rows = imported_history(manager.database)
    for row in rows[200:]:
        manager.database.update_torrent_download(row["id"], qbit_state="removed")
    # Keep more than one page of eligible rows ahead of the oldest one too.
    for row in rows[201:]:
        manager.database.update_torrent_download(row["id"], qbit_state="uploading")
    calls = []

    async def state(info_hash):
        calls.append(info_hash)
        return {
            "category": "tankarr",
            "state": "stoppedUP" if info_hash == rows[0]["info_hash"] else "uploading",
        }

    manager.qbittorrent.torrent_info = state
    assert await manager._finish_seeded() == 1
    assert len(calls) == 400
    assert (
        manager.database.get_torrent_download(rows[0]["id"])["qbit_state"] == "removed"
    )
    assert rows[200]["info_hash"] not in calls


@pytest.mark.asyncio
async def test_seeding_cleanup_isolates_foreign_categories(manager):
    manager.service.settings.torrent_completed_action = "remove_when_seeded"
    good, foreign = imported_history(manager.database, count=2)

    async def state(info_hash):
        return {
            "category": "other" if info_hash == foreign["info_hash"] else "tankarr",
            "state": "stoppedUP",
        }

    manager.qbittorrent.torrent_info = state
    assert await manager._finish_seeded() == 1
    assert manager.qbittorrent.deleted == [(good["info_hash"], True)]
    assert manager.database.get_torrent_download(foreign["id"])["status"] == "imported"


def test_cleanup_pagination_does_not_skip_rows_removed_during_iteration(manager):
    rows = imported_history(manager.database)
    visited = []
    for row in manager.database.iter_torrent_cleanup():
        visited.append(row["id"])
        manager.database.update_torrent_download(row["id"], qbit_state="removed")
    assert visited == [row["id"] for row in reversed(rows)]
    assert list(manager.database.iter_torrent_cleanup()) == []
