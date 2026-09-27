from __future__ import annotations

from datetime import UTC, datetime, timedelta

from tankarr.database import Database
from tankarr.search_cadence import (
    LADDER,
    SearchState,
    after_pass,
    interval_after,
    is_due,
    plan_pass,
)

NOW = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)


def entry(manga_id: str) -> dict:
    return {"manga": {"id": manga_id}, "chapters": []}


def test_empty_passes_climb_the_ladder_and_a_find_resets_it():
    state = None
    waits = []
    for _ in range(6):
        state = after_pass(state, "m", NOW, found=False, discovered=False)
        waits.append(state.next_search_at - NOW)
    assert waits == [LADDER[1], LADDER[2], LADDER[3], LADDER[4], LADDER[4], LADDER[4]]
    assert state.attempts == 6
    found = after_pass(state, "m", NOW, found=True, discovered=False)
    assert found.attempts == 0 and found.next_search_at - NOW == interval_after(0)
    assert found.last_found_at == NOW
    rediscovered = after_pass(state, "m", NOW, found=False, discovered=True)
    assert rediscovered.attempts == 0 and rediscovered.last_found_at is None


def test_a_pass_takes_the_most_overdue_series_within_budget_and_a_person_overrides():
    states = {
        "fresh": SearchState("fresh", next_search_at=NOW + timedelta(days=2)),
        "late": SearchState("late", next_search_at=NOW - timedelta(days=3)),
        "later": SearchState("later", next_search_at=NOW - timedelta(days=9)),
    }
    entries = [entry("fresh"), entry("late"), entry("never"), entry("later")]
    planned, skipped = plan_pass(entries, states, NOW, budget=2)
    assert [item["manga"]["id"] for item in planned] == ["never", "later"]
    assert skipped == 2
    planned, skipped = plan_pass(entries, states, NOW, budget=10)
    assert [item["manga"]["id"] for item in planned] == ["never", "later", "late"]
    assert skipped == 1
    forced, skipped = plan_pass(entries, states, NOW, budget=10, force=True)
    assert len(forced) == 4 and skipped == 0
    assert not is_due(states["fresh"], NOW) and is_due(None, NOW)


def test_states_survive_the_database(tmp_path):
    database = Database(tmp_path / "cadence.sqlite3")
    database.initialize()
    database.upsert_manga(
        {
            "id": "m",
            "provider": "catalogue",
            "title": "Series",
            "description": "",
            "cover_url": None,
            "authors": [],
            "status": "ended",
            "available_languages": ["en"],
        },
        "en",
        "all",
    )
    state = after_pass(None, "m", NOW, found=False, discovered=False)
    database.save_wanted_search_state(state)
    stored = database.wanted_search_states()["m"]
    assert stored.attempts == 1 and stored.next_search_at == state.next_search_at
    database.save_wanted_search_state(
        after_pass(stored, "m", NOW, found=True, discovered=False)
    )
    assert database.wanted_search_states()["m"].attempts == 0
    database.reset_wanted_search_state("m")
    assert database.wanted_search_states() == {}


def test_a_manual_search_forgets_the_indexers_last_answers_but_not_a_grab(tmp_path):
    from tankarr.database import Database

    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    database.upsert_manga(
        {
            "id": "m",
            "title": "Mars",
            "provider": "local",
            "source_id": "m",
            "source_url": "",
            "cover_url": None,
            "description": "",
            "status": "ended",
            "original_language": "ja",
            "authors": [],
        },
        "en",
        "all",
    )
    database.record_wanted_attempt(
        "m",
        "chapter:61",
        channel="indexer_book",
        outcome="ambiguous",
        detail="needs confirming",
    )
    database.record_wanted_attempt(
        "m",
        "chapter:61",
        channel="indexer_chapter",
        outcome="not_offered",
        detail="nothing",
    )
    database.record_wanted_attempt(
        "m",
        "chapter:62",
        channel="indexer_book",
        outcome="grabbed",
        detail="grabbed book 11",
    )
    database.record_wanted_attempt(
        "m", "chapter:63", channel="sources", outcome="not_offered", detail="no source"
    )

    assert database.forget_indexer_answers("m") == 2
    remaining = database.wanted_attempts("m")
    assert sorted(
        (key, item["channel"], item["outcome"])
        for key, items in remaining.items()
        for item in items
    ) == [
        ("chapter:62", "indexer_book", "grabbed"),
        ("chapter:63", "sources", "not_offered"),
    ]
