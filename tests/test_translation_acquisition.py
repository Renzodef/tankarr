from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from tankarr.translation_acquisition import TranslationAcquisition, release_language
from tests.test_torrents import release
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
