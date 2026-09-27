from __future__ import annotations

import asyncio
import hashlib
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from tankarr.app import create_app
from tankarr.config import Settings
from tankarr.database import Database
from tankarr.importer import (
    LanguageReviewRequired,
    LibraryImporter,
    TorrentImportAmbiguous,
    infer_numbered_name,
)
from tankarr.service import ExternalImportConflict, TankarrService
from tankarr.torrent_utils import (
    release_number_hints,
    torrent_info_hash,
)
from tankarr.torrents import TorrentManager
from tests.test_importer import PNG, make_volume_zip, provision_library_identity


def release(*, info_hash: str = "d83a5fd49f4a638c93f4e90b085b1415cc7212fd"):
    return {
        "id": "2053711",
        "provider": "nyaa",
        "title": "The Legend of Kamui v01-02 (2025) (c2c) (Trite)",
        "language": "en",
        "category_id": "3_1",
        "category": "Literature - English-translated",
        "size": "1.8 GiB",
        "size_bytes": 1932735283,
        "seeders": 19,
        "leechers": 2,
        "downloads": 727,
        "comments": 1,
        "trusted": False,
        "remake": False,
        "info_hash": info_hash,
        "publish_at": "2025-12-13T18:15:00+00:00",
        "source_url": "https://nyaa.si/view/2053711",
        "torrent_url": "https://nyaa.si/download/2053711.torrent",
        "volume": "1-2",
        "chapter": None,
        "match_score": 100,
    }


def seed(database: Database) -> None:
    database.upsert_manga(
        {
            "id": "kamui",
            "provider": "local",
            "title": "The Legend of Kamui",
            "description": "",
            "cover_url": None,
            "authors": [],
            "original_language": "ja",
            "available_languages": ["en"],
        },
        "en",
        "none",
    )


def test_torrent_info_hash_uses_exact_bencoded_info_dictionary():
    info = b"d4:name4:teste"
    payload = b"d4:info" + info + b"e"
    expected = hashlib.sha1(info, usedforsecurity=False).hexdigest()
    assert torrent_info_hash(payload) == expected
    with pytest.raises(ValueError):
        torrent_info_hash(b"d4:infod4:name4:teste4:infod4:name5:otheree")


def test_release_numbering_does_not_treat_c2c_as_chapter_two():
    assert release_number_hints("Series v01-02 (c2c)") == ("1-2", None)
    assert infer_numbered_name("Series v01-02 (c2c)") == ("Series (c2c)", "1-2", None)


def test_torrent_api_does_not_expose_server_side_download_reference(tmp_path: Path):
    settings = Settings(
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "library",
        monitor_enabled=False,
        metadata_enabled=False,
    )
    provision_library_identity(settings)
    database = Database(settings.database_path)
    database.initialize()
    seed(database)
    item = release()
    item.update(
        {
            "id": f"10-{item['info_hash']}",
            "provider": "prowlarr",
            "provider_label": "Prowlarr · Nyaa.si",
            "indexer": "Nyaa.si",
            "download_ref": "/10/download?link=opaque",
        }
    )
    download = database.create_torrent_download("kamui", item)
    database.update_torrent_download(download["id"], status="imported")

    with TestClient(create_app(settings)) as client:
        response = client.get("/api/torrents")

    assert response.status_code == 200
    assert response.json()[0]["source"] == "prowlarr"
    assert response.json()[0]["indexer"] == "Nyaa.si"
    assert "torrent_url" not in response.json()[0]
    assert "download_ref" not in response.json()[0]


class FakeService:
    def __init__(self, download_dir: Path):
        self.settings = SimpleNamespace(
            torrent_poll_interval_seconds=2,
            torrent_auto_import=True,
            qbittorrent_category="tankarr",
            sabnzbd_category="tankarr",
            metadata_enabled=False,
            torrent_download_dir=download_dir,
            data_dir=download_dir / "data",
        )
        self.download_dir = download_dir
        self.komga_refreshes: list[str] = []
        self.reranked_series: list[str] = []

    def assert_mutations_allowed(self):
        return None

    async def refresh_komga_library(self, *, reason: str):
        self.komga_refreshes.append(reason)
        return {"ready": True}

    async def rerank_queued_jobs(self, manga_id: str) -> int:
        self.reranked_series.append(manga_id)
        return 0


class FakeProwlarr:
    enabled = False
    configured = False


class SearchableFakeProwlarr:
    enabled = True
    configured = True

    def __init__(self, item):
        self.item = item
        self.resolved = []

    async def search(self, query, language, limit=50):
        assert language == "en"
        return [dict(self.item)]

    async def resolve_download(self, download_ref, expected_hash):
        self.resolved.append((download_ref, expected_hash))
        return "magnet", download_ref


class FakeQBitTorrent:
    configured = True
    import_ready = True

    def __init__(self, content: Path):
        self.content = content
        self.current = None
        self.added = []
        self.deleted: list[tuple[str, bool]] = []

    async def torrent_info(self, info_hash):
        return self.current

    async def delete_torrent(self, info_hash, *, delete_files=True):
        self.deleted.append((info_hash, delete_files))
        self.current = None

    async def add_torrent(self, payload, *, release_id, expected_hash, paused=False):
        assert expected_hash == release()["info_hash"]
        self.added.append((payload, release_id, paused))
        self.current = {
            "hash": release()["info_hash"],
            "category": "tankarr",
            "state": "downloading",
            "progress": 0.25,
        }

    async def add_magnet(self, magnet_url, *, release_id, expected_hash, paused=False):
        self.added.append((magnet_url, release_id, paused))
        self.current = {
            "hash": expected_hash,
            "category": "tankarr",
            "state": "downloading",
            "progress": 0.1,
        }

    async def wait_for_torrent(self, info_hash):
        return self.current

    def local_content_path(self, current):
        return self.content


class FakeMetadata:
    pass


class ReviewingImporter:
    async def import_torrent_download(
        self,
        job,
        content,
        *,
        confirm_language=False,
        skip_unnumbered=False,
        selected_paths=None,
        assigned=None,
        automatic_decision=False,
    ):
        assert content.name == "download"
        raise LanguageReviewRequired(
            {"verdict": "review", "ocr": [{"verdict": "review"}]}
        )


class SuccessfulImporter:
    async def import_torrent_download(
        self,
        job,
        content,
        *,
        confirm_language=False,
        skip_unnumbered=False,
        selected_paths=None,
        assigned=None,
        automatic_decision=False,
    ):
        return {
            "paths": [str(content / f"{job['id']}.cbz")],
            "language_evidence": {"verdict": "confirmed"},
            "books": 1,
        }


@pytest.mark.asyncio
async def test_manager_searches_prowlarr_and_hands_magnet_to_qbittorrent(
    tmp_path: Path,
):
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    seed(database)
    info_hash = release()["info_hash"]
    magnet = f"magnet:?xt=urn:btih:{info_hash}&dn=Kamui"
    item = {
        **release(),
        "id": f"10-{info_hash}",
        "provider": "prowlarr",
        "provider_label": "Prowlarr · Nyaa.si",
        "indexer": "Nyaa.si",
        "indexer_id": 10,
        "download_ref": magnet,
    }
    prowlarr = SearchableFakeProwlarr(item)
    qbit = FakeQBitTorrent(tmp_path / "download")
    manager = TorrentManager(
        database,
        FakeService(tmp_path),
        ReviewingImporter(),
        FakeMetadata(),
        prowlarr,
        qbit,
    )

    found = await manager.search("kamui")
    assert found["providers"] == {"prowlarr": 1}
    assert found["results"][0]["provider_label"] == "Prowlarr · Nyaa.si"
    assert "download_ref" not in found["results"][0]
    job = await manager.grab("kamui", "prowlarr", item["id"])

    assert job["source"] == "prowlarr"
    assert job["indexer"] == "Nyaa.si"
    assert job["status"] == "downloading"
    assert prowlarr.resolved == [(magnet, info_hash)]
    assert qbit.added == [(magnet, item["id"], False)]


@pytest.mark.asyncio
async def test_add_new_discovers_prowlarr_then_binds_release_to_canonical_series(
    tmp_path: Path,
):
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    seed(database)
    info_hash = release()["info_hash"]
    magnet = f"magnet:?xt=urn:btih:{info_hash}&dn=Kamui"
    item = {
        **release(),
        "id": f"10-{info_hash}",
        "provider": "prowlarr",
        "provider_label": "Prowlarr · Nyaa.si",
        "indexer": "Nyaa.si",
        "indexer_id": 10,
        "download_ref": magnet,
    }
    prowlarr = SearchableFakeProwlarr(item)
    qbit = FakeQBitTorrent(tmp_path / "download")
    manager = TorrentManager(
        database,
        FakeService(tmp_path),
        ReviewingImporter(),
        FakeMetadata(),
        prowlarr,
        qbit,
    )

    with pytest.raises(ValueError, match="Search Add New again"):
        await manager.grab_discovered("kamui", "prowlarr", item["id"])
    discovered = await manager.discover("The Legend of Kamui", "en")
    job = await manager.grab_discovered("kamui", "prowlarr", item["id"])

    assert discovered["providers"] == {"prowlarr": 1}
    assert discovered["results"][0]["title"] == item["title"]
    assert "download_ref" not in discovered["results"][0]
    assert job["manga_id"] == "kamui"
    assert job["source"] == "prowlarr"
    assert qbit.added == [(magnet, item["id"], False)]

    with pytest.raises(ValueError, match="requires English"):
        await manager.discover("The Legend of Kamui", "it")


@pytest.mark.asyncio
async def test_rejected_language_recycles_payload_without_client_deletion(
    tmp_path: Path,
):
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    seed(database)
    job = database.create_torrent_download("kamui", release())
    content = tmp_path / "download"
    content.mkdir()
    qbit = FakeQBitTorrent(content)
    qbit.current = {
        "hash": job["info_hash"],
        "category": "tankarr",
        "state": "pausedUP",
        "progress": 1,
    }
    manager = TorrentManager(
        database,
        FakeService(tmp_path),
        ReviewingImporter(),
        FakeMetadata(),
        FakeProwlarr(),
        qbit,
    )

    result = await manager.poll_once()
    reviewed = database.get_torrent_download(job["id"])

    assert result == {"checked": 1, "imported": 0}
    assert reviewed["status"] == "failed"
    assert reviewed["progress"] == 1
    assert reviewed["language_evidence"]["verdict"] == "review"
    assert reviewed["content_path"] == str(content)
    assert not content.exists()
    assert Path(reviewed["language_evidence"]["retired_payload"]).exists()
    assert qbit.deleted == [(job["info_hash"], False)]
    assert await manager.poll_once() == {"checked": 0, "imported": 0}


@pytest.mark.asyncio
async def test_existing_review_is_reconsidered_with_current_automatic_rules(
    tmp_path: Path,
):
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    seed(database)
    job = database.create_torrent_download("kamui", release())
    content = tmp_path / "download"
    content.mkdir()
    payload = content / "unknown.cbz"
    payload.write_bytes(b"keep")
    database.update_torrent_download(
        job["id"],
        status="review",
        content_path=content,
        language_evidence={"verdict": "review"},
    )
    qbit = FakeQBitTorrent(content)
    qbit.current = {
        "hash": job["info_hash"],
        "category": "tankarr",
        "state": "pausedUP",
        "progress": 1,
    }
    manager = TorrentManager(
        database,
        FakeService(tmp_path),
        SuccessfulImporter(),
        FakeMetadata(),
        FakeProwlarr(),
        qbit,
    )
    assert await manager.poll_once() == {"checked": 1, "imported": 1}
    imported = database.get_torrent_download(job["id"])
    assert imported["status"] == "imported"
    assert payload.read_bytes() == b"keep"
    assert qbit.deleted == []
    assert await manager.poll_once() == {"checked": 0, "imported": 0}


@pytest.mark.asyncio
async def test_torrent_manager_refreshes_komga_after_successful_queue_drain(
    tmp_path: Path,
):
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    seed(database)
    job = database.create_torrent_download("kamui", release())
    content = tmp_path / "download"
    content.mkdir()
    qbit = FakeQBitTorrent(content)
    qbit.current = {
        "hash": job["info_hash"],
        "category": "tankarr",
        "state": "pausedUP",
        "progress": 1,
    }
    service = FakeService(tmp_path)
    manager = TorrentManager(
        database,
        service,
        SuccessfulImporter(),
        FakeMetadata(),
        FakeProwlarr(),
        qbit,
    )

    result = await manager.poll_once()

    assert result == {"checked": 1, "imported": 1}
    assert database.get_torrent_download(job["id"])["status"] == "imported"
    assert service.komga_refreshes == ["torrent_queue_drained"]
    assert service.reranked_series == ["kamui"]


class ImportProvider:
    name = "mangadex"


class ImportKomga:
    configured = False

    async def ensure_present(self, expected):
        return {
            "configured": False,
            "triggered": False,
            "expected_books": len(expected),
        }


@pytest.mark.asyncio
async def test_synthetic_nyaa_archive_is_normalized_and_published(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    settings = Settings(
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "library",
        monitor_enabled=False,
    )
    settings.ensure_directories()
    provision_library_identity(settings)
    database = Database(settings.database_path)
    database.initialize()
    seed(database)
    service = TankarrService(settings, database, ImportProvider(), ImportKomga())
    importer = LibraryImporter(settings, database, service)

    content = tmp_path / "download"
    archive = content / "The Legend of Kamui v01.cbz"
    archive.parent.mkdir()
    with zipfile.ZipFile(archive, "w") as output:
        for index in range(1, 4):
            output.writestr(f"page-{index:03d}.png", PNG)
    monkeypatch.setattr(
        "tankarr.importer.audit_english_pages",
        lambda _pages: {
            "verdict": "confirmed",
            "sampled_pages": 3,
            "word_count": 30,
            "common_word_count": 8,
            "long_word_count": 12,
            "reason": "synthetic English OCR evidence",
        },
    )
    job = database.create_torrent_download("kamui", release())

    result = await importer.import_torrent_download(job, content)

    assert result["books"] == 1
    assert result["language_evidence"]["verdict"] == "confirmed"
    destination = Path(result["paths"][0])
    assert destination.is_file()
    assert destination.name == "The Legend of Kamui - v001 [en].cbz"
    chapter = database.list_all_chapters("kamui")[0]
    assert chapter["provider"] == "nyaa"
    assert chapter["volume"] == "1"
    assert chapter["chapter"] is None
    assert chapter["downloaded"] is True


class RemovableFakeQBitTorrent(FakeQBitTorrent):
    def __init__(self, content: Path):
        super().__init__(content)
        self.deleted: list[tuple[str, bool]] = []
        self.category_torrents: list[dict] = []

    async def delete_torrent(self, info_hash, *, delete_files):
        self.deleted.append((info_hash, delete_files))
        if self.current and self.current["hash"] == info_hash:
            self.current = None

    async def list_category_torrents(self):
        return list(self.category_torrents)


def _seeded_manager(tmp_path: Path, action: str):
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    seed(database)
    job = database.create_torrent_download("kamui", release())
    content = tmp_path / "download"
    content.mkdir(exist_ok=True)
    qbit = RemovableFakeQBitTorrent(content)
    qbit.current = {
        "hash": job["info_hash"],
        "category": "tankarr",
        "state": "uploading",
        "progress": 1,
    }
    service = FakeService(tmp_path)
    service.settings.torrent_completed_action = action
    service.settings.torrent_orphan_grace_hours = 24
    manager = TorrentManager(
        database, service, SuccessfulImporter(), FakeMetadata(), FakeProwlarr(), qbit
    )
    return database, qbit, manager, job


@pytest.mark.asyncio
async def test_remove_after_import_deletes_the_torrent_and_its_files(tmp_path: Path):
    database, qbit, manager, job = _seeded_manager(tmp_path, "remove_after_import")
    await manager.poll_once()
    stored = database.get_torrent_download(job["id"])
    assert stored["status"] == "imported" and stored["qbit_state"] == "removed"
    assert qbit.deleted == [(job["info_hash"], True)]
    assert "removed from qBittorrent" in stored["message"]


@pytest.mark.asyncio
async def test_remove_when_seeded_waits_for_qbittorrent_to_pause_the_torrent(
    tmp_path: Path,
):
    database, qbit, manager, job = _seeded_manager(tmp_path, "remove_when_seeded")
    await manager.poll_once()  # imported, still uploading → kept
    assert qbit.deleted == []
    assert database.get_torrent_download(job["id"])["status"] == "imported"
    qbit.current["state"] = "pausedUP"
    result = await manager.poll_once()
    assert result.get("removed_after_seeding") == 1
    assert qbit.deleted == [(job["info_hash"], True)]
    assert database.get_torrent_download(job["id"])["qbit_state"] == "removed"
    # Keep seeding never touches the client.
    database2, qbit2, manager2, _job2 = _seeded_manager(tmp_path / "keep", "seed")
    qbit2.current["state"] = "pausedUP"
    await manager2.poll_once()
    assert qbit2.deleted == []


@pytest.mark.asyncio
async def test_externally_removed_torrent_fails_the_job_without_regrab(tmp_path: Path):
    database, qbit, manager, job = _seeded_manager(tmp_path, "seed")
    qbit.current = None
    await manager.poll_once()
    stored = database.get_torrent_download(job["id"])
    assert stored["status"] == "failed" and stored["qbit_state"] == "removed"
    assert "outside Tankarr" in stored["message"]


@pytest.mark.asyncio
async def test_orphan_sweep_removes_only_old_unreferenced_torrents_in_the_category(
    tmp_path: Path,
):
    import time

    database, qbit, manager, job = _seeded_manager(tmp_path, "seed")
    old = time.time() - 48 * 3600
    qbit.category_torrents = [
        {"hash": job["info_hash"], "name": "known", "added_on": old},
        {"hash": "a" * 40, "name": "old orphan", "added_on": old},
        {"hash": "b" * 40, "name": "fresh orphan", "added_on": time.time() - 600},
    ]
    result = await manager.sweep_orphans()
    assert result == {"seen": 3, "removed": 1}
    assert qbit.deleted == [("a" * 40, True)]
    assert manager.last_orphan_sweep["names"] == ["old orphan"]
    assert manager.status()["orphan_sweep"]["removed"] == 1


class FakeSAB:
    configured = True
    import_ready = True

    def __init__(self, content: Path):
        self.content = content
        self.added: list[tuple[str, str]] = []
        self.state: dict | None = None
        self.deleted: list[tuple[str, bool]] = []

    async def add_nzb_url(self, url, *, name):
        self.added.append((url, name))
        self.state = {
            "where": "queue",
            "status": "Downloading",
            "percentage": "12",
            "cat": "tankarr",
        }
        return "SABnzbd_nzo_1"

    async def job_state(self, nzo_id):
        return self.state

    async def delete(self, nzo_id, *, delete_files):
        self.deleted.append((nzo_id, delete_files))
        self.state = None

    def local_content_path(self, slot):
        return self.content


def usenet_release():
    return {
        **release(info_hash="a" * 40),
        "protocol": "usenet",
        "download_ref": "http://prowlarr:9696/7/download?link=abc&file=BECK+v01",
        "title": "BECK v01 (2018) (Kodansha Comics USA) (Digital) (1r0n)",
    }


@pytest.mark.asyncio
async def test_usenet_release_goes_through_sabnzbd_and_is_removed_after_import(
    tmp_path: Path,
):
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    seed(database)
    content = tmp_path / "download"
    content.mkdir()
    sab = FakeSAB(content)
    service = FakeService(tmp_path)
    service.settings.torrent_completed_action = "seed"  # usenet has nothing to seed
    manager = TorrentManager(
        database,
        service,
        SuccessfulImporter(),
        FakeMetadata(),
        FakeProwlarr(),
        FakeQBitTorrent(content),
        sabnzbd=sab,
    )
    manager._release_cache[("kamui", "prowlarr", usenet_release()["id"])] = (
        10**12,
        usenet_release(),
    )
    import asyncio

    loop = asyncio.get_running_loop()
    manager._release_cache[("kamui", "prowlarr", usenet_release()["id"])] = (
        loop.time(),
        usenet_release(),
    )
    job = await manager.grab("kamui", "prowlarr", usenet_release()["id"])
    assert (
        job["status"] == "queued"
        and job["protocol"] == "usenet"
        and job["client_id"] == "SABnzbd_nzo_1"
    )
    assert sab.added[0][0].startswith("http://prowlarr:9696/7/download")

    await manager.poll_once()
    assert database.get_torrent_download(job["id"])["status"] == "downloading"
    sab.state = {
        "where": "history",
        "status": "Completed",
        "cat": "tankarr",
        "storage": "/data/downloads/usenet/tankarr/BECK v01",
    }
    result = await manager.poll_once()
    stored = database.get_torrent_download(job["id"])
    assert result["imported"] == 1 and stored["status"] == "imported"
    # "seed" keeps nothing on Usenet: the history entry stays only when asked to seed.
    assert sab.deleted == []
    service.settings.torrent_completed_action = "remove_after_import"
    manager2 = TorrentManager(
        database,
        service,
        SuccessfulImporter(),
        FakeMetadata(),
        FakeProwlarr(),
        FakeQBitTorrent(content),
        sabnzbd=sab,
    )
    job2 = database.create_torrent_download(
        "kamui", {**usenet_release(), "id": "other", "info_hash": "b" * 40}
    )
    database.set_torrent_download_protocol(job2["id"], "usenet")
    database.update_torrent_download(job2["id"], client_id="SABnzbd_nzo_2")
    sab.state = {
        "where": "history",
        "status": "Completed",
        "category": "tankarr",
        "storage": "/data/downloads/usenet/tankarr/BECK v02",
    }
    await manager2.poll_once()
    assert sab.deleted == [("SABnzbd_nzo_2", True)]


def test_prowlarr_normalizes_usenet_releases_with_a_pseudo_hash():
    from tankarr.config import Settings
    from tankarr.prowlarr import ProwlarrClient

    settings = Settings(prowlarr_url="http://prowlarr:9696", prowlarr_api_key="k")
    client = ProwlarrClient(settings)
    raw = {
        "protocol": "usenet",
        "guid": "https://usenet-crawler.test/details/123",
        "downloadUrl": "http://prowlarr:9696/7/download?apikey=k&link=abc",
        "title": "BECK v03 (2018) (Kodansha Comics USA) (Digital) (1r0n)",
        "indexerId": 7,
        "indexer": "Usenet-Crawler",
        "categories": [{"id": 7030, "name": "Books/Comics"}],
        "size": 1000,
        "publishDate": "2026-01-01",
    }
    release = client._normalize_release(raw, "beck", "en")
    assert (
        release is not None
        and release["protocol"] == "usenet"
        and release["volume"] == "3"
    )
    assert len(release["info_hash"]) == 40 and release["download_ref"].startswith(
        "http://prowlarr:9696/7/download"
    )
    torrent = client._normalize_release(
        {**raw, "protocol": "torrent", "infoHash": "c" * 40}, "beck", "en"
    )
    assert torrent is not None and torrent["protocol"] == "torrent"


def test_create_torrent_download_refreshes_a_failed_row_instead_of_a_new_one(
    tmp_path: Path,
):
    """A grab that once failed (a stale or missing reference, a transient
    client error) must not permanently occupy its info hash: the next
    attempt with a working reference has to actually retry, not be handed
    back the same dead row."""

    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    seed(database)
    item = release()
    first = database.create_torrent_download("kamui", item)
    database.update_torrent_download(
        first["id"],
        status="failed",
        message="ValueError: Invalid Prowlarr download reference",
    )

    refreshed = item | {
        "torrent_url": "https://nyaa.si/download/2053711.torrent",
        "seeders": 42,
    }
    retried = database.create_torrent_download("kamui", refreshed)

    assert retried["id"] == first["id"]  # same row: no duplicate for one info hash
    assert retried["status"] == "adding"
    assert retried["seeders"] == 42


def test_create_torrent_download_leaves_an_active_row_alone(tmp_path: Path):
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    seed(database)
    item = release()
    first = database.create_torrent_download("kamui", item)
    database.update_torrent_download(first["id"], status="downloading")

    same = database.create_torrent_download("kamui", item)

    assert same == database.get_torrent_download(first["id"])
    assert same["status"] == "downloading"


@pytest.mark.asyncio
async def test_search_carries_the_download_reference_only_for_the_trusted_caller(
    tmp_path: Path,
):
    """volume_hunt.hunt_missing_volumes records a review a human may accept
    later; that acceptance has to be able to grab the same result, so the
    internal caller keeps the reference the public /torrent-search route
    strips before it ever reaches the browser."""

    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    seed(database)
    item = {**release(), "download_ref": "https://nyaa.si/download/2053711.torrent"}
    prowlarr = SearchableFakeProwlarr(item)
    manager = TorrentManager(
        database,
        FakeService(tmp_path),
        ReviewingImporter(),
        FakeMetadata(),
        prowlarr,
        FakeQBitTorrent(tmp_path / "download"),
    )

    public = await manager.search("kamui")
    assert "download_ref" not in public["results"][0]

    internal = await manager.search("kamui", _include_download_ref=True)
    assert internal["results"][0]["download_ref"] == item["download_ref"]


def test_torrent_api_exposes_a_browser_link_to_the_download_client(tmp_path: Path):
    """qbittorrent_url/sabnzbd_url are internal service addresses a browser
    cannot reach; the public counterparts drive the shortcut in Activity."""

    settings = Settings(
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "library",
        monitor_enabled=False,
        metadata_enabled=False,
        qbittorrent_url="http://qbittorrent:8080",
        qbittorrent_public_url="http://192.0.2.50:8080",
        sabnzbd_url="http://sabnzbd:8080",
        sabnzbd_public_url="http://192.0.2.51:8080",
    )
    provision_library_identity(settings)
    database = Database(settings.database_path)
    database.initialize()
    seed(database)

    torrent_item = release(info_hash="d83a5fd49f4a638c93f4e90b085b1415cc7212fd")
    database.create_torrent_download("kamui", torrent_item)

    usenet_item = {
        **release(info_hash="a83a5fd49f4a638c93f4e90b085b1415cc7212fd"),
        "id": "2053712",
    }
    usenet_job = database.create_torrent_download("kamui", usenet_item)
    database.set_torrent_download_protocol(usenet_job["id"], "usenet")

    with TestClient(create_app(settings)) as client:
        response = client.get("/api/torrents")

    assert response.status_code == 200
    by_hash = {item["info_hash"]: item for item in response.json()}
    assert by_hash[torrent_item["info_hash"]]["client_url"] == "http://192.0.2.50:8080"
    assert by_hash[usenet_item["info_hash"]]["client_url"] == "http://192.0.2.51:8080"


def test_torrent_api_hides_the_shortcut_when_no_public_url_is_configured(
    tmp_path: Path,
):
    settings = Settings(
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "library",
        monitor_enabled=False,
        metadata_enabled=False,
        qbittorrent_url="http://qbittorrent:8080",
    )
    provision_library_identity(settings)
    database = Database(settings.database_path)
    database.initialize()
    seed(database)
    database.create_torrent_download("kamui", release())

    with TestClient(create_app(settings)) as client:
        response = client.get("/api/torrents")

    assert response.json()[0]["client_url"] is None


def test_review_reason_separates_a_language_doubt_from_an_unnumbered_book(
    tmp_path: Path,
):
    """Confirming the language does nothing for a numbering problem: the
    numbering check runs first and would refuse the import again, so the two
    causes must not offer the same action."""

    from tankarr.app import _torrent_review_reason

    assert (
        _torrent_review_reason(
            {"status": "review", "language_evidence": {"verdict": "review"}}
        )
        == "language"
    )
    assert (
        _torrent_review_reason({"status": "review", "language_evidence": {}})
        == "content"
    )
    assert _torrent_review_reason({"status": "downloading"}) is None


@pytest.mark.asyncio
async def test_skip_unnumbered_imports_the_identified_books_only(tmp_path: Path):
    """A pack often bundles other works with no number of their own; the
    operator can take the books the release does identify."""

    settings = Settings(
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "library",
        monitor_enabled=False,
        metadata_enabled=False,
    )
    settings.ensure_directories()
    provision_library_identity(settings)
    database = Database(settings.database_path)
    database.initialize()
    seed(database)
    published: list[dict] = []

    class PublishingService(FakeService):
        async def publish_external_import(self, _manga_id, chapter, path, *_args):
            published.append(chapter)
            return {"path": str(path), "reused": False}

        async def reconcile_komga_library(self):
            return {"ready": True}

    importer = LibraryImporter(settings, database, PublishingService(tmp_path))  # type: ignore[arg-type]

    content = tmp_path / "pack"
    content.mkdir()
    make_volume_zip(content / "Example v01.cbz")
    make_volume_zip(content / "An Unrelated Short Story.cbz")
    job = database.create_torrent_download("kamui", release())

    with pytest.raises(TorrentImportAmbiguous, match="Cannot derive"):
        await importer.import_torrent_download(job, content)

    result = await importer.import_torrent_download(
        job, content, skip_unnumbered=True, confirm_language=True
    )
    assert result["books"] == 1
    # The numbered volume came in; the unrelated short story stayed out.
    assert [chapter["volume"] for chapter in published] == ["1"]


@pytest.mark.asyncio
async def test_a_multi_book_release_reports_each_book_as_it_lands(tmp_path: Path):
    """A ten-volume pack runs for minutes; one opaque bar cannot say how many
    books are already in the library."""

    settings = Settings(
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "library",
        monitor_enabled=False,
        metadata_enabled=False,
    )
    settings.ensure_directories()
    provision_library_identity(settings)
    database = Database(settings.database_path)
    database.initialize()
    seed(database)

    class PublishingService(FakeService):
        async def publish_external_import(self, _manga_id, chapter, path, *_args):
            return {"path": str(path), "reused": False}

        async def reconcile_komga_library(self):
            return {"ready": True}

    importer = LibraryImporter(settings, database, PublishingService(tmp_path))  # type: ignore[arg-type]
    content = tmp_path / "pack"
    content.mkdir()
    make_volume_zip(content / "Example v01.cbz")
    make_volume_zip(content / "Example v02.cbz")
    job = database.create_torrent_download("kamui", release())

    result = await importer.import_torrent_download(job, content, confirm_language=True)

    assert result["books"] == 2
    stored = database.get_torrent_download(job["id"])
    assert stored["message"] == "Imported 2 of 2 books"
    assert len(stored["imported_paths"]) == 2


@pytest.mark.asyncio
async def test_a_selected_owned_book_is_skipped_before_importing_the_rest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A stale picker selection must not put a pack back into the same review
    loop: occupied slots are immutable here, while missing slots still land."""

    async def run_inline(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr("tankarr.importer.asyncio.to_thread", run_inline)

    settings = Settings(
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "library",
        monitor_enabled=False,
        metadata_enabled=False,
    )
    settings.ensure_directories()
    provision_library_identity(settings)
    database = Database(settings.database_path)
    database.initialize()
    seed(database)
    database.upsert_chapters(
        "kamui",
        [
            {
                "id": "owned-volume-1",
                "volume": "1",
                "chapter": None,
                "title": "Volume 1",
                "language": "en",
                "provider": "nyaa",
                "groups": [],
                "publish_at": None,
                "source_url": "",
                "pages": 3,
                "version": 1,
            }
        ],
    )
    existing = settings.library_dir / "already-owned-v01.cbz"
    existing.write_bytes(b"already owned")
    database.mark_chapter_downloaded("owned-volume-1", existing)
    published: list[dict] = []

    class PublishingService(FakeService):
        async def publish_external_import(self, _manga_id, chapter, path, *_args):
            published.append(chapter)
            return {"path": str(path), "reused": False}

        async def reconcile_komga_library(self):
            return {"ready": True}

    monkeypatch.setattr(
        "tankarr.importer.audit_english_pages",
        lambda _pages, **_kwargs: {
            "verdict": "confirmed",
            "sampled_pages": 3,
            "reason": "English script detected",
            "script_evidence": {"latin": 1.0},
        },
    )
    importer = LibraryImporter(
        settings,
        database,
        PublishingService(tmp_path),  # type: ignore[arg-type]
    )
    content = tmp_path / "pack"
    content.mkdir()
    make_volume_zip(content / "Kamui v01.cbz")
    make_volume_zip(content / "Kamui v02.cbz")
    job = database.create_torrent_download("kamui", release())

    result = await importer.import_torrent_download(
        job,
        content,
        confirm_language=True,
        selected_paths={"Kamui v01.cbz", "Kamui v02.cbz"},
    )

    assert result["books"] == 1
    assert result["already_owned"] == 1
    assert result["already_owned_paths"] == ["Kamui v01.cbz"]
    assert [chapter["volume"] for chapter in published] == ["2"]
    evidence = result["language_evidence"]["ocr"]
    assert len(evidence) == 1 and evidence[0]["path"] == "Kamui v02.cbz"
    assert evidence[0]["verdict"] == "confirmed"
    assert evidence[0]["automatic_decision"]["verdict"] == "confirmed"


@pytest.mark.asyncio
async def test_wholly_conflicting_pack_is_recycled_after_importer_cannot_use_it(
    tmp_path: Path,
):
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    seed(database)
    job = database.create_torrent_download("kamui", release())
    database.update_torrent_download(job["id"], status="completed")
    content = tmp_path / "download"
    content.mkdir()
    make_volume_zip(content / "Kamui v01.cbz")
    make_volume_zip(content / "Kamui v02.cbz")

    class ConflictingImporter(SuccessfulImporter):
        def scan_torrent_content(self, path):
            return {
                "root": str(path),
                "items": [
                    {"path": "Kamui v01.cbz", "volume": "1"},
                    {"path": "Kamui v02.cbz", "volume": "2"},
                ],
                "skipped": [],
            }

        async def import_torrent_download(self, *args, **kwargs):
            raise ExternalImportConflict(
                "A different external book already occupies this language/volume/chapter"
            )

    qbit = FakeQBitTorrent(content)
    qbit.current = {
        "hash": job["info_hash"],
        "category": "tankarr",
        "state": "pausedUP",
        "progress": 1,
    }
    manager = TorrentManager(
        database,
        FakeService(tmp_path),
        ConflictingImporter(),
        FakeMetadata(),
        SearchableFakeProwlarr(release()),
        qbit,
    )

    updated = await manager.import_now(job["id"])

    assert updated["status"] == "failed"
    assert "No useful books" in updated["message"]
    assert qbit.deleted == [(job["info_hash"], False)]
    assert not content.exists()
    assert Path(updated["language_evidence"]["retired_payload"]).is_dir()


@pytest.mark.asyncio
async def test_grabbing_again_retries_a_release_whose_first_attempt_failed(
    tmp_path: Path,
):
    """A failed grab must not answer the next one: asking for the same release
    again is a request to try it, not to be handed the dead row."""

    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    seed(database)
    info_hash = release()["info_hash"]
    item = {
        **release(),
        "id": f"10-{info_hash}",
        "provider": "prowlarr",
        "indexer": "Nyaa.si",
        "indexer_id": 10,
        "download_ref": f"magnet:?xt=urn:btih:{info_hash}&dn=Kamui",
    }
    dead = database.create_torrent_download("kamui", item)
    database.update_torrent_download(
        dead["id"], status="failed", message="reference expired"
    )

    qbit = FakeQBitTorrent(tmp_path / "download")
    manager = TorrentManager(
        database,
        FakeService(tmp_path),
        SuccessfulImporter(),
        FakeMetadata(),
        SearchableFakeProwlarr(item),
        qbit,
    )
    await manager.search("kamui")

    retried = await manager.grab("kamui", "prowlarr", item["id"])

    assert retried["id"] == dead["id"]
    assert retried["status"] != "failed"
    assert qbit.added, "the release was handed to the download client again"


@pytest.mark.asyncio
async def test_naming_a_book_the_release_does_not_number_makes_it_importable(
    tmp_path: Path,
):
    """A one-book release whose name carries no number cannot be placed by
    inspection, and selecting it alone still failed: what the operator says it
    is, it is."""

    settings = Settings(
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "library",
        monitor_enabled=False,
        metadata_enabled=False,
    )
    settings.ensure_directories()
    provision_library_identity(settings)
    database = Database(settings.database_path)
    database.initialize()
    seed(database)
    published: list[dict] = []

    class PublishingService(FakeService):
        async def publish_external_import(self, _manga_id, chapter, path, *_args):
            published.append(chapter)
            return {"path": str(path), "reused": False}

        async def reconcile_komga_library(self):
            return {"ready": True}

    importer = LibraryImporter(settings, database, PublishingService(tmp_path))  # type: ignore[arg-type]
    content = tmp_path / "release"
    content.mkdir()
    make_volume_zip(content / "Bete Noire (Fantagraphics).cbz")
    # No number in the release name and none on the grab either: nothing in
    # the pipeline can place this book.
    job = database.create_torrent_download("kamui", {**release(), "volume": None})

    with pytest.raises(TorrentImportAmbiguous, match="Cannot derive"):
        await importer.import_torrent_download(job, content)

    result = await importer.import_torrent_download(
        job,
        content,
        confirm_language=True,
        selected_paths={"Bete Noire (Fantagraphics).cbz"},
        assigned={"Bete Noire (Fantagraphics).cbz": {"volume": "1"}},
    )

    assert result["books"] == 1
    assert [chapter["volume"] for chapter in published] == ["1"]


class FakeArchive:
    """Internet Archive stand-in: serves one file from a local source."""

    enabled = True
    configured = True

    def __init__(self, source: Path, releases: list[dict]) -> None:
        self.source = source
        self.releases = releases
        self.downloads: list[str] = []

    async def search(self, title, language, limit=50):
        assert language == "en"
        return [dict(item) for item in self.releases]

    async def download(self, url, destination, *, expected_size=0, progress=None):
        self.downloads.append(url)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(self.source.read_bytes())
        if progress is not None:
            await progress(destination.stat().st_size, destination.stat().st_size)
        return destination


def direct_release():
    return {
        **release(info_hash="c" * 40),
        "id": "ultra-heaven/Ultra Heaven v01.cbz",
        "provider": "internetarchive",
        "protocol": "http",
        "indexer": "Internet Archive",
        "seeders": 0,
        "download_ref": "https://archive.org/download/ultra-heaven/Ultra%20Heaven%20v01.cbz",
        "source_url": "https://archive.org/details/ultra-heaven",
        "title": "Ultra Heaven v01",
    }


@pytest.mark.asyncio
async def test_direct_release_is_downloaded_by_tankarr_and_imported_from_staging(
    tmp_path: Path,
):
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    seed(database)
    source = tmp_path / "source.cbz"
    source.write_bytes(b"PK" + b"\x00" * 100)
    service = FakeService(tmp_path / "downloads")
    service.settings.data_dir = tmp_path / "data"
    archive = FakeArchive(source, [direct_release()])
    manager = TorrentManager(
        database,
        service,  # type: ignore[arg-type]
        SuccessfulImporter(),  # type: ignore[arg-type]
        FakeMetadata(),  # type: ignore[arg-type]
        FakeProwlarr(),  # type: ignore[arg-type]
        FakeQBitTorrent(tmp_path / "download"),  # type: ignore[arg-type]
        internet_archive=archive,
    )

    found = await manager.search("kamui", "Kamui")
    assert found["providers"] == {"internetarchive": 1}
    assert found["results"][0]["protocol"] == "http"
    assert "download_ref" not in found["results"][0]

    job = await manager.grab("kamui", "internetarchive", direct_release()["id"])
    assert job["protocol"] == "http"
    # The download task runs on the loop; let it finish.
    await asyncio.gather(*manager._direct_tasks.values())
    job = database.get_torrent_download(job["id"])
    assert job["status"] == "completed"
    assert archive.downloads == [direct_release()["download_ref"]]
    staged = Path(job["content_path"])
    assert staged.exists()
    assert staged.name == "Ultra Heaven v01.cbz"
    assert staged.is_relative_to(tmp_path / "data" / "direct-downloads")

    result = await manager.poll_once()
    assert result["imported"] == 1
    job = database.get_torrent_download(job["id"])
    assert job["status"] == "imported"
    assert not staged.exists()  # the staging copy is gone once the library has it
    assert service.komga_refreshes  # the reader was told


@pytest.mark.asyncio
async def test_failed_direct_release_can_import_an_existing_staged_file(tmp_path: Path):
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    seed(database)
    service = FakeService(tmp_path / "downloads")
    service.settings.data_dir = tmp_path / "data"
    archive = FakeArchive(tmp_path / "unused.cbz", [direct_release()])
    manager = TorrentManager(
        database,
        service,  # type: ignore[arg-type]
        SuccessfulImporter(),  # type: ignore[arg-type]
        FakeMetadata(),  # type: ignore[arg-type]
        FakeProwlarr(),  # type: ignore[arg-type]
        FakeQBitTorrent(tmp_path / "download"),  # type: ignore[arg-type]
        internet_archive=archive,
    )
    job = database.create_torrent_download("kamui", direct_release())
    database.set_torrent_download_protocol(job["id"], "http")
    staged = manager._direct_destination(job)
    staged.parent.mkdir(parents=True)
    staged.write_bytes(b"PK" + b"\x00" * 100)
    database.update_torrent_download(
        job["id"],
        status="failed",
        content_path=staged,
        message="Previous import failed",
    )

    result = await manager.import_now(job["id"])

    assert result["status"] == "imported"
    assert archive.downloads == []
    assert not staged.exists()


@pytest.mark.asyncio
async def test_direct_download_is_restarted_after_a_restart_and_can_be_discarded(
    tmp_path: Path,
):
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    seed(database)
    source = tmp_path / "source.cbz"
    source.write_bytes(b"PK" + b"\x00" * 100)
    service = FakeService(tmp_path / "downloads")
    service.settings.data_dir = tmp_path / "data"
    service.settings.torrent_auto_import = False
    archive = FakeArchive(source, [direct_release()])
    manager = TorrentManager(
        database,
        service,  # type: ignore[arg-type]
        SuccessfulImporter(),  # type: ignore[arg-type]
        FakeMetadata(),  # type: ignore[arg-type]
        FakeProwlarr(),  # type: ignore[arg-type]
        FakeQBitTorrent(tmp_path / "download"),  # type: ignore[arg-type]
        internet_archive=archive,
    )
    # A job left "queued" by a process that died: no task knows about it.
    job = database.create_torrent_download("kamui", direct_release())
    database.set_torrent_download_protocol(job["id"], "http")
    database.update_torrent_download(job["id"], status="queued")

    await manager.poll_once()
    await asyncio.gather(*manager._direct_tasks.values())
    assert database.get_torrent_download(job["id"])["status"] == "completed"
    staged = Path(database.get_torrent_download(job["id"])["content_path"])
    assert staged.exists()

    await manager.discard(job["id"], delete_files=True)
    assert not staged.parent.exists()
    with pytest.raises(KeyError):
        database.get_torrent_download(job["id"])


def _downloaded(chapter: str | None, volume: str | None, unit: str) -> dict:
    return {
        "id": f"c-{chapter or volume}",
        "chapter": chapter,
        "volume": volume,
        "release_unit": unit,
        "downloaded": True,
    }


def test_books_can_fill_slots_currently_covered_only_by_chapters(tmp_path):
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    seed(database)
    manager = TorrentManager(
        database,
        FakeService(tmp_path),
        ReviewingImporter(),
        FakeMetadata(),
        FakeProwlarr(),
        FakeQBitTorrent(tmp_path),
    )
    job = {
        "manga_id": "kamui",
        "title": "The Legend of Kamui v01-02 (c2c)",
        "volume_hint": "1-2",
        "language": "en",
    }

    # Nothing on disk yet: the first release decides the unit, nothing to protect.
    assert manager._edition_conflict(job) is None

    database.list_chapters = lambda manga_id, language: [  # type: ignore[method-assign]
        _downloaded("1", None, "chapter"),
        _downloaded("2", None, "chapter"),
    ]
    assert manager._edition_conflict(job) is None

    # A chapter release for the same series is more of the same edition.
    assert (
        manager._edition_conflict(
            {"manga_id": "kamui", "title": "The Legend of Kamui c003", "language": "en"}
        )
        is None
    )


def test_edition_conflict_stops_a_chapter_for_a_series_kept_as_books(tmp_path):
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    seed(database)
    manager = TorrentManager(
        database,
        FakeService(tmp_path),
        ReviewingImporter(),
        FakeMetadata(),
        FakeProwlarr(),
        FakeQBitTorrent(tmp_path),
    )
    database.list_chapters = lambda manga_id, language: [  # type: ignore[method-assign]
        _downloaded(None, "1", "volume"),
        _downloaded(None, "2", "volume"),
    ]
    chapter = {
        "manga_id": "kamui",
        "title": "The Legend of Kamui c003 (2025)",
        "language": "en",
    }
    message = manager._edition_conflict(chapter)
    assert message is not None and "kept as books (2 on disk)" in message
    book = {
        "manga_id": "kamui",
        "title": "The Legend of Kamui v03 (c2c)",
        "volume_hint": "3",
        "language": "en",
    }
    assert manager._edition_conflict(book) is None

    # An already mixed series is not protected: the operator has chosen.
    database.list_chapters = lambda manga_id, language: [  # type: ignore[method-assign]
        _downloaded(None, "1", "volume"),
        _downloaded("9", None, "chapter"),
    ]
    assert manager._edition_conflict(chapter) is None


@pytest.mark.asyncio
async def test_completed_book_torrent_for_a_chapter_series_reaches_file_selection(
    tmp_path: Path,
):
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    seed(database)
    job = database.create_torrent_download("kamui", release())
    content = tmp_path / "download"
    content.mkdir()
    qbit = FakeQBitTorrent(content)
    qbit.current = {
        "hash": job["info_hash"],
        "category": "tankarr",
        "state": "pausedUP",
        "progress": 1,
    }
    database.list_chapters = lambda manga_id, language: [
        _downloaded("1", None, "chapter")
    ]  # type: ignore[method-assign]
    manager = TorrentManager(
        database,
        FakeService(tmp_path),
        SuccessfulImporter(),
        FakeMetadata(),
        FakeProwlarr(),
        qbit,
    )

    await manager.poll_once()
    reviewed = database.get_torrent_download(job["id"])

    assert reviewed["status"] == "imported"
    assert "Imported 1 book" in reviewed["message"]


@pytest.mark.asyncio
async def test_a_single_unnumbered_book_of_a_one_book_work_is_volume_one(
    tmp_path: Path,
):
    """Ding Dong Circus and Cinderalla, 2026-09-05: one file, no number, one
    book in the whole edition. There is nothing to ask."""

    settings = Settings(
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "library",
        monitor_enabled=False,
        metadata_enabled=False,
    )
    settings.ensure_directories()
    provision_library_identity(settings)
    database = Database(settings.database_path)
    database.initialize()
    seed(database)
    published: list[dict] = []

    class PublishingService(FakeService):
        async def publish_external_import(self, _manga_id, chapter, path, *_args):
            published.append(chapter)
            return {"path": str(path), "reused": False}

        async def reconcile_komga_library(self):
            return {"ready": True}

    importer = LibraryImporter(settings, database, PublishingService(tmp_path))  # type: ignore[arg-type]
    content = tmp_path / "single"
    content.mkdir()
    make_volume_zip(content / "Ding Dong Circus (Sasaki Maki).cbz")

    hintless = {**release(), "volume": None, "title": "Ding Dong Circus (2025) (Trite)"}
    # A multi-volume work still asks.
    job = database.create_torrent_download("kamui", hintless)
    with pytest.raises(TorrentImportAmbiguous, match="Cannot derive"):
        await importer.import_torrent_download(job, content, confirm_language=True)

    database.update_manga(
        "kamui",
        {"expected_count_override": 1, "expected_count_unit_override": "volume"},
    )
    job = database.create_torrent_download(
        "kamui", {**hintless, "id": "2053799", "info_hash": "e" * 40}
    )
    result = await importer.import_torrent_download(job, content, confirm_language=True)
    assert result["books"] == 1
    assert [chapter["volume"] for chapter in published] == ["1"]


@pytest.mark.asyncio
async def test_one_file_that_spans_several_volumes_is_not_volume_one(tmp_path: Path):
    settings = Settings(
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "library",
        monitor_enabled=False,
        metadata_enabled=False,
    )
    settings.ensure_directories()
    provision_library_identity(settings)
    database = Database(settings.database_path)
    database.initialize()
    seed(database)
    importer = LibraryImporter(settings, database, FakeService(tmp_path))  # type: ignore[arg-type]
    content = tmp_path / "pack"
    content.mkdir()
    make_volume_zip(content / "Pineapple Army 1-6 (ja).cbz")
    job = database.create_torrent_download(
        "kamui", {**release(), "volume": "1-6", "title": "Pineapple Army 1-6"}
    )

    with pytest.raises(TorrentImportAmbiguous, match="carries volumes 1-6"):
        await importer.import_torrent_download(job, content, confirm_language=True)


@pytest.mark.asyncio
async def test_a_magnet_without_metadata_is_abandoned_after_the_timeout(tmp_path: Path):
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    seed(database)
    job = database.create_torrent_download("kamui", release())
    with database.connect() as connection:
        connection.execute(
            "UPDATE torrent_download SET created_at = ? WHERE id = ?",
            ("2026-01-01T00:00:00+00:00", job["id"]),
        )
    qbit = FakeQBitTorrent(tmp_path / "download")
    qbit.current = {
        "hash": job["info_hash"],
        "category": "tankarr",
        "state": "metaDL",
        "progress": 0,
    }
    manager = TorrentManager(
        database,
        FakeService(tmp_path),
        ReviewingImporter(),
        FakeMetadata(),
        FakeProwlarr(),
        qbit,
    )

    await manager.poll_once()
    failed = database.get_torrent_download(job["id"])

    assert failed["status"] == "failed"
    assert failed["qbit_state"] == "metadata_timeout"
    assert "abandoned" in failed["message"]
    assert qbit.deleted == [(job["info_hash"], True)]


@pytest.mark.asyncio
async def test_a_fresh_magnet_still_fetching_metadata_is_left_alone(tmp_path: Path):
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    seed(database)
    job = database.create_torrent_download("kamui", release())
    qbit = FakeQBitTorrent(tmp_path / "download")
    qbit.current = {
        "hash": job["info_hash"],
        "category": "tankarr",
        "state": "metaDL",
        "progress": 0,
    }
    manager = TorrentManager(
        database,
        FakeService(tmp_path),
        ReviewingImporter(),
        FakeMetadata(),
        FakeProwlarr(),
        qbit,
    )

    await manager.poll_once()

    assert database.get_torrent_download(job["id"])["status"] == "queued"
    assert qbit.deleted == []


@pytest.mark.asyncio
async def test_rejected_payload_on_a_read_only_mount_is_kept_for_review(
    tmp_path: Path,
):
    root = tmp_path / "downloads"
    root.mkdir()
    content = root / "download"
    content.mkdir()
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    seed(database)
    job = database.create_torrent_download("kamui", release())
    qbit = FakeQBitTorrent(content)
    qbit.current = {
        "hash": job["info_hash"],
        "category": "tankarr",
        "state": "pausedUP",
        "progress": 1,
    }
    manager = TorrentManager(
        database,
        FakeService(root),
        ReviewingImporter(),
        FakeMetadata(),
        FakeProwlarr(),
        qbit,
    )
    root.chmod(0o555)
    try:
        await manager.poll_once()
        retired = database.get_torrent_download(job["id"])
    finally:
        root.chmod(0o755)

    assert retired["status"] == "failed"
    assert retired["qbit_state"] == "review_required"
    assert retired["language_evidence"]["retired_payload"] == str(content)
    assert qbit.deleted == []
    assert content.exists()
    # Nothing left to retry on the next poll.
    assert await manager.poll_once() == {"checked": 0, "imported": 0}
