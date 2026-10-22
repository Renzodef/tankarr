from __future__ import annotations

from datetime import UTC, datetime, timedelta

from tankarr.monitor_schedule import (
    DAILY_SECONDS,
    WEEKLY_SECONDS,
    effective_status,
    is_due,
    is_hot,
    series_interval_seconds,
    should_defer,
)

NOW = datetime(2026, 10, 22, tzinfo=UTC)
INTERVAL = 1800.0


def test_a_recent_chapter_uses_the_monitor_interval_even_when_completed():
    recent = NOW - timedelta(days=3)
    ongoing = series_interval_seconds("ongoing", recent, NOW, INTERVAL)
    finished = series_interval_seconds("completed", recent, NOW, INTERVAL)
    assert ongoing == INTERVAL
    assert finished == INTERVAL
    assert is_hot(ongoing, INTERVAL)


def test_ongoing_series_without_a_fresh_chapter_are_daily_then_weekly():
    assert (
        series_interval_seconds("ongoing", NOW - timedelta(days=30), NOW, INTERVAL)
        == DAILY_SECONDS
    )
    assert series_interval_seconds("ongoing", None, NOW, INTERVAL) == DAILY_SECONDS
    assert (
        series_interval_seconds("publishing", NOW - timedelta(days=120), NOW, INTERVAL)
        == WEEKLY_SECONDS
    )


def test_finished_and_paused_series_are_weekly_until_a_chapter_returns():
    old = NOW - timedelta(days=40)
    assert series_interval_seconds("completed", old, NOW, INTERVAL) == WEEKLY_SECONDS
    assert series_interval_seconds("hiatus", old, NOW, INTERVAL) == WEEKLY_SECONDS
    assert (
        series_interval_seconds("on_hiatus", NOW - timedelta(days=200), NOW, INTERVAL)
        == WEEKLY_SECONDS
    )
    assert series_interval_seconds("cancelled", None, NOW, INTERVAL) == WEEKLY_SECONDS


def test_a_continuing_override_is_treated_as_ongoing():
    status = effective_status("completed", "continuing")
    assert status == "ongoing"
    assert series_interval_seconds(status, None, NOW, INTERVAL) == DAILY_SECONDS
    assert effective_status("ongoing", "hiatus") == "hiatus"


def test_an_unknown_status_stays_daily_until_the_chapters_go_stale():
    assert series_interval_seconds("", None, NOW, INTERVAL) == DAILY_SECONDS
    assert (
        series_interval_seconds("", NOW - timedelta(days=100), NOW, INTERVAL)
        == WEEKLY_SECONDS
    )


def test_never_checked_is_due_and_an_error_waits_at_least_six_hours():
    assert is_due(None, WEEKLY_SECONDS, NOW) is True
    assert is_due(NOW - timedelta(seconds=10), INTERVAL, NOW) is False
    assert is_due(NOW - timedelta(hours=2), INTERVAL, NOW, error=True) is False
    assert is_due(NOW - timedelta(hours=7), INTERVAL, NOW, error=True) is True
    assert is_due(NOW - timedelta(hours=2), INTERVAL, NOW, error=False) is True


def test_the_budget_always_starts_one_series_and_reserves_room_for_cold_ones():
    assert should_defer(0, 0, hot=True, started=False) is False
    assert should_defer(0, 0, hot=False, started=True) is True
    assert should_defer(80, 100, hot=True, started=True) is True
    assert should_defer(80, 100, hot=False, started=True) is False
    assert (
        should_defer(80, 100, hot=True, started=True, reserve_for_cold=False) is False
    )
    assert should_defer(100, 100, hot=False, started=True) is True
