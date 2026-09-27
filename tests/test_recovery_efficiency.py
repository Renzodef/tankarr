from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tankarr.chapter_map import entries_from_releases
from tankarr.config import Settings
from tankarr.database import Database
from tankarr.metadata.service import ArtworkStore
from tankarr.monitor import ReleaseMonitor
from tankarr.volume_hunt import hunt_volumes
from tankarr.wanted_recovery import (
    AMBIGUOUS,
    CHANNEL_INDEXER_BOOK,
    CHANNEL_INDEXER_CHAPTER,
    CHANNEL_SOURCES,
    ERROR,
    GRABBED,
    NOT_OFFERED,
    PENDING,
    UNAVAILABLE,
    UNSEARCHED,
    book_hunt_outcome,
    merge_book_hunts,
    slot_verdict,
)


@pytest.fixture
def monitor(tmp_path):
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "library",
    )
    result = ReleaseMonitor(settings, database, SimpleNamespace(), SimpleNamespace())
    result.torrents = SimpleNamespace(
        prowlarr=SimpleNamespace(enabled=True, configured=True),
        search=AsyncMock(return_value={"results": []}),
        grab=AsyncMock(side_effect=AssertionError("unexpected external mutation")),
    )
    return result


def seed_series(database, manga_id):
    database.upsert_manga(
        {
            "id": manga_id,
            "provider": "catalogue",
            "title": f"Example Work {manga_id}",
            "authors": [],
            "available_languages": ["en"],
        },
        "en",
        "all",
    )
    return database.get_manga(manga_id)


@pytest.mark.asyncio
async def test_book_probe_rotates_past_the_first_ten_series(monitor, monkeypatch):
    entries = [
        {
            "manga": seed_series(monitor.database, f"series-{index}"),
            "chapters": [{"chapter": "1"}],
        }
        for index in range(23)
    ]
    probe = AsyncMock(return_value=0)
    monkeypatch.setattr("tankarr.volume_hunt.probe_offers", probe)

    for _ in range(3):
        assert await monitor.probe_book_offers(entries) == 10

    visited = [call.kwargs["manga"]["id"] for call in probe.await_args_list]
    assert visited[:23] == [f"series-{index}" for index in range(23)]
    assert visited[23:] == [f"series-{index}" for index in range(7)]


@pytest.mark.asyncio
async def test_book_probe_skips_books_already_hunted_this_pass(monitor, monkeypatch):
    manga = seed_series(monitor.database, "series")
    monitor._last_book_hunt["series"] = {"searched_volumes": [1], "grabbed": []}
    probe = AsyncMock(return_value=0)
    monkeypatch.setattr("tankarr.volume_hunt.probe_offers", probe)

    assert (
        await monitor.probe_book_offers(
            [{"manga": manga, "chapters": [{"chapter": "1"}]}]
        )
        == 0
    )
    probe.assert_not_awaited()


@pytest.mark.asyncio
async def test_book_probe_moves_past_a_failing_series_on_the_next_pass(
    monitor, monkeypatch
):
    entries = [
        {"manga": seed_series(monitor.database, name), "chapters": [{"chapter": "1"}]}
        for name in ("first", "second")
    ]
    monitor.BOOK_PROBE_PER_CYCLE = 1
    probe = AsyncMock(side_effect=[RuntimeError("source failed"), 0])
    monkeypatch.setattr("tankarr.volume_hunt.probe_offers", probe)

    with pytest.raises(RuntimeError, match="source failed"):
        await monitor.probe_book_offers(entries)
    assert await monitor.probe_book_offers(entries) == 1
    assert [call.kwargs["manga"]["id"] for call in probe.await_args_list] == [
        "first",
        "second",
    ]


@pytest.mark.parametrize(
    ("hunted", "volume", "expected"),
    [
        ({"grabbed": [], "errors": ["timeout"]}, "1", ERROR),
        ({"grabbed": [], "needs_review": True}, "1", AMBIGUOUS),
        (
            {"grabbed": [], "searched_volumes": [], "pending_volumes": [1]},
            "1",
            PENDING,
        ),
        (
            {"grabbed": [{"volume": None, "volumes": [1, 2, 3]}]},
            "2.0",
            GRABBED,
        ),
        (
            {
                "grabbed": [{"volume": None, "volumes": [1, 2, 3]}],
                "searched_volumes": [1, 2, 3, 4],
            },
            "4",
            NOT_OFFERED,
        ),
        ({"grabbed": [], "errors": []}, "1", UNAVAILABLE),
        ({"grabbed": [], "errors": [], "searched_volumes": []}, "1", UNAVAILABLE),
        (None, "1", UNAVAILABLE),
    ],
)
def test_book_hunt_verdict_preserves_evidence(hunted, volume, expected):
    assert book_hunt_outcome(hunted, volumes=[volume])[0] == expected


def test_merged_book_hunts_keep_failures_scoped_to_the_book_that_failed():
    result = {"grabbed": [], "searched_volumes": [1], "errors": ["v1 timeout"]}
    for volume in (2, 3):
        result = merge_book_hunts(
            result, {"grabbed": [], "searched_volumes": [volume], "errors": []}
        )

    assert result["searched_volumes"] == [1, 2, 3]
    assert result["errors"] == ["v1 timeout"]
    assert book_hunt_outcome(result, volumes=[1])[0] == ERROR
    assert book_hunt_outcome(result, volumes=[2])[0] == NOT_OFFERED
    assert book_hunt_outcome(result, volumes=[3])[0] == NOT_OFFERED


@pytest.mark.asyncio
async def test_existing_pack_returned_for_a_new_book_does_not_count_as_a_new_download(
    monitor, monkeypatch
):
    manga = seed_series(monitor.database, "series")
    monitor.database.replace_chapter_map(
        "series",
        "operator",
        entries_from_releases([{"volume": "2", "chapter": "4"}], source="operator"),
    )
    monitor._last_book_hunt["series"] = {
        "searched_volumes": [1],
        "grabbed": [{"job_id": 42, "volume": 1, "volumes": [1]}],
        "errors": [],
    }
    hunt = AsyncMock(
        return_value={
            "searched_volumes": [2],
            "grabbed": [{"job_id": 42, "volume": 2, "volumes": [2]}],
            "errors": [],
        }
    )
    monkeypatch.setattr("tankarr.volume_hunt.hunt_volumes", hunt)

    assert await monitor._recover_one_chapter(manga, {"chapter": "4"}) == {"grabbed": 0}
    cached = monitor._last_book_hunt["series"]
    assert len(cached["grabbed"]) == 1
    assert cached["grabbed"][0]["volumes"] == [1, 2]
    assert book_hunt_outcome(cached, volumes=[1])[0] == GRABBED
    assert book_hunt_outcome(cached, volumes=[2])[0] == GRABBED


def test_pack_coverage_is_recorded_for_each_volume_not_as_an_empty_result(monitor):
    seed_series(monitor.database, "series")
    monitor._record_book_hunt(
        "series",
        [{"volume": "1"}, {"volume": "2"}],
        {"grabbed": [{"volume": None, "volumes": [1, 2]}], "errors": []},
        available=True,
    )
    ledger = monitor.database.wanted_attempts("series")
    assert ledger["volume:1"][0]["outcome"] == GRABBED
    assert ledger["volume:2"][0]["outcome"] == GRABBED


@pytest.mark.asyncio
async def test_book_already_in_download_is_pending_not_unobtainable(
    monitor, monkeypatch
):
    manga = seed_series(monitor.database, "series")
    monkeypatch.setattr(
        monitor.database,
        "pending_book_volumes",
        lambda _ids: {"series": {1}},
    )
    hunted = await monitor._hunt_missing_volumes(
        {"manga": manga, "chapters": [{"volume": "1"}]}
    )

    assert book_hunt_outcome(hunted, volumes=[1])[0] == PENDING
    monitor.torrents.search.assert_not_awaited()


def test_two_empty_indexer_rungs_do_not_claim_sources_were_also_searched():
    verdict = slot_verdict(
        [
            {"channel": CHANNEL_SOURCES, "outcome": ERROR},
            {"channel": CHANNEL_INDEXER_CHAPTER, "outcome": NOT_OFFERED},
            {"channel": CHANNEL_INDEXER_BOOK, "outcome": NOT_OFFERED},
        ]
    )
    assert verdict["verdict"] == UNSEARCHED


@pytest.mark.asyncio
async def test_partial_source_failure_survives_a_book_hunt(monitor):
    monitor.torrents.search.return_value = {
        "results": [],
        "errors": [{"provider": "prowlarr", "error": "timeout"}],
    }
    result = await hunt_volumes(
        monitor.torrents,
        manga={"id": "series", "title": "Example Work"},
        missing_volumes=[1],
        publisher=None,
    )

    assert result["searched_volumes"] == [1]
    assert "timeout" in result["errors"][0]
    assert book_hunt_outcome(result, volumes=[1])[0] == ERROR
    monitor.torrents.grab.assert_not_awaited()


@pytest.mark.asyncio
async def test_mapped_chapters_reuse_the_pass_book_search_without_counting_grabs_twice(
    monitor, monkeypatch
):
    manga = seed_series(monitor.database, "series")
    monitor.database.replace_chapter_map(
        "series",
        "mangaupdates",
        entries_from_releases([{"volume": "1", "chapter": "1-2"}]),
    )
    monitor._last_book_hunt["series"] = {
        "searched_volumes": [1],
        "grabbed": [{"volume": 1, "volumes": [1], "job_id": 42}],
        "errors": [],
    }
    hunt = AsyncMock(side_effect=AssertionError("book was already searched"))
    monkeypatch.setattr("tankarr.volume_hunt.hunt_volumes", hunt)

    for chapter in ("1", "2"):
        assert await monitor._recover_one_chapter(manga, {"chapter": chapter}) == {
            "grabbed": 0
        }

    hunt.assert_not_awaited()
    ledger = monitor.database.wanted_attempts("series")
    for chapter in ("1", "2"):
        by_channel = {item["channel"]: item for item in ledger[f"chapter:{chapter}"]}
        assert by_channel[CHANNEL_INDEXER_BOOK]["outcome"] == GRABBED


@pytest.mark.asyncio
async def test_new_confirmed_map_during_recovery_extends_book_cache_without_forgetting_old_books(
    monitor, monkeypatch
):
    manga = seed_series(monitor.database, "series")
    initial_map = [{"volume": "1", "chapter": "1-3"}]
    monitor.database.replace_chapter_map(
        "series", "operator", entries_from_releases(initial_map, source="operator")
    )
    monitor._last_book_hunt["series"] = {
        "searched_volumes": [1],
        "grabbed": [],
        "errors": [],
    }

    async def search_with_metadata_refresh(*args, **kwargs):
        # The user can confirm a new boundary while the indexer request
        # is in flight. Chapter 4 now has a book the initial hunt did not see.
        monitor.database.replace_chapter_map(
            "series",
            "operator",
            entries_from_releases(
                [*initial_map, {"volume": "2", "chapter": "4"}], source="operator"
            ),
        )
        return {"results": []}

    monitor.torrents.search.side_effect = search_with_metadata_refresh
    hunt = AsyncMock(
        return_value={
            "searched_volumes": [2],
            "grabbed": [],
            "errors": ["volume 2 source timed out"],
        }
    )
    monkeypatch.setattr("tankarr.volume_hunt.hunt_volumes", hunt)

    for chapter in ("4", "3", "2"):
        assert await monitor._recover_one_chapter(manga, {"chapter": chapter}) == {
            "grabbed": 0
        }

    assert [call.kwargs["missing_volumes"] for call in hunt.await_args_list] == [[2]]
    assert monitor._last_book_hunt["series"]["searched_volumes"] == [1, 2]
    ledger = monitor.database.wanted_attempts("series")
    for chapter, outcome in (("4", ERROR), ("3", NOT_OFFERED), ("2", NOT_OFFERED)):
        by_channel = {item["channel"]: item for item in ledger[f"chapter:{chapter}"]}
        assert by_channel[CHANNEL_INDEXER_BOOK]["outcome"] == outcome


@pytest.mark.asyncio
async def test_irrelevant_results_do_not_hide_a_partial_chapter_search_failure(monitor):
    manga = seed_series(monitor.database, "series")
    monitor.torrents.search.return_value = {
        "results": [{"id": "other", "title": "A Different Work c007"}],
        "errors": [{"provider": "prowlarr", "error": "timeout"}],
    }
    await monitor._recover_one_chapter(manga, {"chapter": "1"})

    ledger = monitor.database.wanted_attempts("series")["chapter:1"]
    by_channel = {item["channel"]: item for item in ledger}
    assert by_channel[CHANNEL_INDEXER_CHAPTER]["outcome"] == ERROR
    monitor.torrents.grab.assert_not_awaited()


@pytest.mark.asyncio
async def test_wanted_snapshot_does_not_run_on_the_event_loop(monitor, monkeypatch):
    event_loop_thread = threading.get_ident()
    threads = []

    def list_wanted():
        threads.append(threading.get_ident())
        return []

    monitor.service.list_wanted = list_wanted
    monitor._last_book_hunt["previous-pass"] = {"searched_volumes": [1]}
    for method in (
        "probe_book_offers",
        "retire_redundant_chapters",
        "retire_duplicate_chapter_files",
        "track_publication_pauses",
        "upgrade_to_official",
        "_notify_decisions",
    ):
        monkeypatch.setattr(monitor, method, AsyncMock(return_value=0))
    for method in ("align_official_editions", "audit_page_quality"):
        monkeypatch.setattr(monitor, method, AsyncMock(return_value={}))

    await monitor.search_wanted()
    assert threads and all(thread != event_loop_thread for thread in threads)
    assert monitor._last_book_hunt == {}


@pytest.mark.asyncio
async def test_remote_cover_normalization_does_not_run_on_the_event_loop(
    tmp_path, monkeypatch
):
    store = ArtworkStore(Settings(_env_file=None, data_dir=tmp_path))
    event_loop_thread = threading.get_ident()
    threads = []

    def prepare_bytes(content):
        threads.append(threading.get_ident())
        assert content == b"image bytes"
        return {"content": content}

    monkeypatch.setattr(store, "prepare_bytes", prepare_bytes)
    monkeypatch.setattr(
        store.http,
        "request",
        AsyncMock(
            return_value=SimpleNamespace(
                url="https://s4.anilist.co/cover.jpg", content=b"image bytes"
            )
        ),
    )
    try:
        assert await store.prepare_remote(
            "anilist", "https://s4.anilist.co/cover.jpg"
        ) == {"content": b"image bytes"}
    finally:
        await store.aclose()

    assert threads and all(thread != event_loop_thread for thread in threads)
