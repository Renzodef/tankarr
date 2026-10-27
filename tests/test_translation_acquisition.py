from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from tankarr.torrents import TorrentManager
from tankarr.translation_acquisition import TranslationAcquisition, release_language
from tankarr.translation_policy import processor_languages
from tests.test_torrents import FakeArchive, FakeQBitTorrent, release
from tests.test_translation import output_archive
from tests.test_translation import translation as translation
from tests.test_translation_routes import source_cbz


def setup_acquisition(manager, monkeypatch):
    manager.settings.prowlarr_enabled = True
    monkeypatch.setattr(
        manager,
        "_missing",
        lambda _id: [{"volume": "2", "chapter": None, "expected": True}],
    )
    manager.release_sources._target = Mock(return_value={"alternate_titles": []})
    torrents = SimpleNamespace(
        prowlarr=SimpleNamespace(search=AsyncMock(return_value=[])),
        _release_cache={},
        grab=AsyncMock(return_value={}),
        completed_action="seed",
    )
    return TranslationAcquisition(manager, torrents)


def add_archive(acquisition, rows):
    acquisition.torrents.internet_archive = SimpleNamespace(
        enabled=True, search_translation=AsyncMock(return_value=rows)
    )


@pytest.mark.asyncio
async def test_foreign_archive_download_translation_and_import(
    translation, monkeypatch, tmp_path
):
    manager, database, service, _source = translation
    acquisition = setup_acquisition(manager, monkeypatch)
    manager.settings.prowlarr_enabled = False
    source = tmp_path / "source.cbz"
    source.write_bytes(source_cbz())
    item = {
        **release(),
        "id": "foreign-archive",
        "provider": "internetarchive",
        "protocol": "http",
        "title": "Example v02 [PT]",
        "volume": "2",
        "language": "pt",
        "seeders": 0,
        "download_ref": "https://archive.org/download/example/Example%20v02%20%5BPT%5D.cbz",
    }
    archive = FakeArchive(source, [item])
    archive.search_translation = AsyncMock(return_value=[item])
    torrents = TorrentManager(
        database,
        service,
        manager.importer,
        Mock(),
        acquisition.torrents.prowlarr,
        FakeQBitTorrent(tmp_path / "torrent"),
        internet_archive=archive,
    )
    acquisition.torrents = torrents
    torrents.translations = acquisition
    manager.acquisition = acquisition
    languages = processor_languages(database.get_manga("manga-1"), manager.settings)
    assert await acquisition.hunt("manga-1", languages) == 1
    await asyncio.gather(*torrents._direct_tasks.values())
    downloaded = database.list_torrent_downloads(manga_id="manga-1")[0]
    assert downloaded["protocol"] == "http"
    assert (
        downloaded["language_evidence"]["translation_request"]["source_language"]
        == "pt"
    )
    assert torrents.qbittorrent.added == []
    result = await torrents.import_now(downloaded["id"])
    assert result["imported_paths"] == []
    assert not any(
        chapter["downloaded"] for chapter in database.list_all_chapters("manga-1")
    )
    job = manager.store.list()[0]
    assert job["source"]["language"] == "pt" and job["target_language"] == "en"
    assert await acquisition.hunt("manga-1", languages) == 0
    output = output_archive(
        manager, job, tmp_path, source_language="pt", detected_source_language="pt"
    )
    await manager._publish(job, output, manager.directory(job) / "source.cbz")
    completed = manager.store.get(job["id"])
    assert completed["status"] == "completed"
    published = database.get_chapter(completed["result_chapter_id"])
    assert published["language"] == "en" and published["provider"] == "translated"
    assert published["volume"] == "2"


@pytest.mark.asyncio
async def test_archive_cannot_grab_after_translation_is_switched_off(
    translation, monkeypatch
):
    manager, _database, _service, _source = translation
    acquisition = setup_acquisition(manager, monkeypatch)
    manager.settings.prowlarr_enabled = False
    add_archive(acquisition, [])

    async def slow_search(*_args, **_kwargs):
        manager.settings.translation_enabled = False
        return [
            {
                **release(),
                "provider": "internetarchive",
                "protocol": "http",
                "title": "Example v02",
                "language": "ja",
                "seeders": 0,
            }
        ]

    acquisition.torrents.internet_archive.search_translation.side_effect = slow_search
    assert await acquisition.hunt("manga-1", ["ja"]) == 0
    acquisition.torrents.grab.assert_not_awaited()


@pytest.mark.asyncio
async def test_archive_fallback_accepts_unlisted_language_without_prowlarr(
    translation, monkeypatch
):
    manager, database, _service, _source = translation
    acquisition = setup_acquisition(manager, monkeypatch)
    manager.settings.prowlarr_enabled = False
    manager.settings.translation_source_languages = "ja"
    database.update_manga("manga-1", {"translation_source_languages": "ja"})
    add_archive(
        acquisition,
        [
            {
                **release(),
                "id": "archive-pt",
                "provider": "internetarchive",
                "protocol": "http",
                "title": "Example v02",
                "language": "pt",
                "seeders": 0,
            }
        ],
    )
    assert (
        await acquisition.hunt(
            "manga-1",
            processor_languages(database.get_manga("manga-1"), manager.settings),
        )
        == 1
    )
    acquisition.torrents.grab.assert_awaited_once_with(
        "manga-1",
        "internetarchive",
        "archive-pt",
        translation={
            "source_language": "pt",
            "target_language": "en",
            "slots": ["volume:2"],
        },
    )
    acquisition.torrents.prowlarr.search.assert_not_awaited()


@pytest.mark.asyncio
async def test_indexer_outage_does_not_hide_archive_candidates(
    translation, monkeypatch
):
    manager, _database, _service, _source = translation
    acquisition = setup_acquisition(manager, monkeypatch)
    acquisition.torrents.prowlarr.search.side_effect = RuntimeError("fixture outage")
    add_archive(
        acquisition,
        [
            {
                **release(),
                "id": "archive",
                "provider": "internetarchive",
                "protocol": "http",
                "title": "Example v02",
                "language": "ja",
                "seeders": 0,
            }
        ],
    )
    assert await acquisition.hunt("manga-1", ["ja"]) == 1
    assert acquisition.torrents.grab.await_args.args[1] == "internetarchive"


@pytest.mark.asyncio
@pytest.mark.parametrize("disabled", ["global", "archive"])
async def test_archive_fallback_respects_switches(translation, monkeypatch, disabled):
    manager, database, _service, _source = translation
    acquisition = setup_acquisition(manager, monkeypatch)
    manager.settings.prowlarr_enabled = False
    add_archive(acquisition, [])
    if disabled == "global":
        manager.settings.translation_enabled = False
    else:
        acquisition.torrents.internet_archive.enabled = False
    assert await acquisition.hunt("manga-1", ["ja"]) == 0
    acquisition.torrents.internet_archive.search_translation.assert_not_awaited()
    acquisition.torrents.grab.assert_not_awaited()


@pytest.mark.asyncio
async def test_archive_unknown_language_and_wrong_number_do_not_become_native(
    translation, monkeypatch
):
    manager, _database, _service, _source = translation
    acquisition = setup_acquisition(manager, monkeypatch)
    manager.settings.prowlarr_enabled = False
    base = {
        **release(),
        "provider": "internetarchive",
        "protocol": "http",
        "seeders": 0,
    }
    add_archive(
        acquisition,
        [
            {**base, "id": "unknown", "title": "Example v02", "language": "und"},
            {**base, "id": "native", "title": "Example v02", "language": "en"},
            {**base, "id": "other-book", "title": "Example v01", "language": "ja"},
            {
                **base,
                "id": "other-series",
                "title": "Other Series v02",
                "language": "ja",
            },
        ],
    )
    assert await acquisition.hunt("manga-1", ["ja", "pt"]) == 0
    acquisition.torrents.grab.assert_not_awaited()


@pytest.mark.parametrize(
    "name,language,expected",
    [
        ("Example v02 [JA]", "ja", True),
        ("Example v02 [raw]", "ja", True),
        ("Example v02 [FR]", "fr", True),
        ("Example v02 [EN]", "ja", False),
        ("Example v02", "ja", False),
    ],
)
def test_release_language_needs_evidence_in_the_name(name, language, expected):
    assert release_language(name, language) == expected


@pytest.mark.asyncio
async def test_only_missing_matching_books_are_grabbed(translation, monkeypatch):
    manager, _database, _service, _source = translation
    acquisition = setup_acquisition(manager, monkeypatch)
    acquisition.torrents.prowlarr.search.return_value = [
        {**release(), "id": "native", "title": "Example v02 [EN]"},
        {**release(), "id": "wrong", "title": "Example Other Series v02 [JA]"},
        {**release(), "id": "owned", "title": "Example v01 [JA]"},
        {
            **release(),
            "id": "needed",
            "provider": "prowlarr",
            "title": "Example v02 [JA]",
            "volume": "2",
        },
    ]
    assert await acquisition.hunt("manga-1", ["ja"]) == 1
    acquisition.torrents.grab.assert_awaited_once_with(
        "manga-1",
        "prowlarr",
        "needed",
        translation={
            "source_language": "ja",
            "target_language": "en",
            "slots": ["volume:2"],
        },
    )


@pytest.mark.asyncio
async def test_disabled_or_differently_sized_edition_is_not_grabbed(
    translation, monkeypatch
):
    manager, database, _service, _source = translation
    acquisition = setup_acquisition(manager, monkeypatch)
    database.update_manga("manga-1", {"edition_book_count": 12})
    assert await acquisition.hunt("manga-1", ["ja"]) == 0
    acquisition.torrents.prowlarr.search.assert_not_called()


@pytest.mark.asyncio
async def test_completed_foreign_torrent_is_staged_without_import(
    translation, tmp_path, monkeypatch
):
    manager, database, _service, _source = translation
    acquisition = setup_acquisition(manager, monkeypatch)
    item = {
        **release(),
        "provider": "prowlarr",
        "title": "Example v02 [JA]",
        "volume": "2",
        "language": "ja",
    }
    job = database.create_torrent_download("manga-1", item)
    request = {"source_language": "ja", "target_language": "en", "slots": ["volume:2"]}
    job = database.update_torrent_download(
        job["id"],
        status="completed",
        language_evidence={"translation_request": request},
    )
    content = tmp_path / "Example v02 [JA].cbz"
    content.write_bytes(source_cbz())
    result = await acquisition.stage_torrent(job, content)
    assert result["status"] == "imported"
    assert result["imported_paths"] == []
    assert not acquisition.can_finish_source(result)
    queued = manager.store.list()
    assert len(queued) == 1
    assert (manager.directory(queued[0]) / "source.cbz").exists()
    assert all(
        not chapter["downloaded"] for chapter in database.list_all_chapters("manga-1")
    )
    database.retry_torrent_download(
        database.update_torrent_download(job["id"], status="failed")["id"]
    )
    assert (
        database.get_torrent_download(job["id"])["language_evidence"][
            "translation_request"
        ]
        == request
    )


@pytest.mark.asyncio
async def test_paused_fallback_preserves_completed_download(
    translation, tmp_path, monkeypatch
):
    manager, database, _service, _source = translation
    acquisition = setup_acquisition(manager, monkeypatch)
    job = database.create_torrent_download("manga-1", release())
    job = database.update_torrent_download(
        job["id"],
        status="completed",
        language_evidence={
            "translation_request": {
                "target_language": "en",
                "source_language": "ja",
                "slots": ["volume:2"],
            }
        },
    )
    manager.settings.translation_enabled = False
    result = await acquisition.stage_torrent(job, tmp_path / "never-opened.cbz")
    assert result["status"] == "completed"
    assert manager.store.list() == []


@pytest.mark.asyncio
async def test_source_cleanup_reaches_old_completed_translation_after_multiple_pages(
    translation, monkeypatch
):
    from tests.test_torrent_lifecycle import imported_history

    manager, database, _service, source = translation
    acquisition = setup_acquisition(manager, monkeypatch)
    acquisition.torrents.completed_action = "remove_after_import"
    acquisition.torrents._remove_from_client = AsyncMock()
    translated = manager.store.create("manga-1", source, "en")
    manager.store.update(translated["id"], status="completed")
    rows = imported_history(database, manga_id="manga-1", count=501)
    old = rows[0]
    database.update_torrent_download(
        old["id"],
        language_evidence={
            "translation_request": {"target_language": "en"},
            "translation_jobs": [translated["id"]],
        },
    )
    await acquisition.finish_sources()
    acquisition.torrents._remove_from_client.assert_awaited_once()
    assert acquisition.torrents._remove_from_client.call_args.args[0]["id"] == old["id"]
