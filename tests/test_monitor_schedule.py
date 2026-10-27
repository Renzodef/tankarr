from __future__ import annotations

from datetime import UTC, datetime, timedelta

from tankarr.monitor_schedule import (
    WEEKLY_SECONDS,
    effective_status,
    is_due,
    is_hot,
    series_interval_seconds,
    should_defer,
)

NOW = datetime(2026, 10, 22, tzinfo=UTC)
INTERVAL = 1800.0


def test_a_continuing_override_is_treated_as_ongoing():
    status = effective_status("completed", "continuing")
    assert status == "ongoing"
    assert series_interval_seconds(status, None, NOW, INTERVAL) == INTERVAL
    assert effective_status("ongoing", "hiatus") == "hiatus"


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


def test_every_unfinished_work_uses_the_global_timer_regardless_of_release_age():
    for status in ("ongoing", "publishing", "hiatus", "on_hiatus", "unknown", ""):
        for newest in (None, NOW - timedelta(days=1), NOW - timedelta(days=200)):
            every = series_interval_seconds(status, newest, NOW, INTERVAL)
            assert every == INTERVAL
            assert is_hot(every, INTERVAL)


def test_completed_works_switch_to_weekly_checks_even_with_recent_releases():
    for status in ("completed", "ended", "cancelled"):
        for newest in (None, NOW - timedelta(days=1), NOW - timedelta(days=200)):
            assert (
                series_interval_seconds(status, newest, NOW, INTERVAL) == WEEKLY_SECONDS
            )
