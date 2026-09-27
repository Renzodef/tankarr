from __future__ import annotations

from datetime import date, timedelta

from tankarr.release_cadence import expected_releases, release_timeline


def weekly_history(start: date, count: int, first_chapter: int = 1150) -> list[dict]:
    history = []
    for index in range(count):
        history.append(
            {
                "chapter": str(first_chapter + index),
                "release_date": (start + timedelta(days=7 * index)).isoformat(),
            }
        )
    return history


def test_timeline_keeps_the_first_dated_appearance_of_integer_chapters():
    history = [
        {"chapter": "12", "release_date": "2026-08-10"},
        {"chapter": "12", "release_date": "2026-08-09"},  # earlier re-release
        {"chapter": "12.5", "release_date": "2026-08-11"},  # extra: ignored
        {"chapter": "13-14", "release_date": "2026-08-17"},
        {"chapter": "Extra", "release_date": "2026-08-18"},
        {"chapter": "15", "release_date": None},
        {"chapter": "16 v2", "release_date": "2026-08-24"},  # re-release label
        {"chapter": "1-1000", "release_date": "2015-01-01"},  # bulk batch: ignored
    ]
    assert [(int(n), d.isoformat()) for n, d in release_timeline(history)] == [
        (12, "2026-08-09"),
        (14, "2026-08-17"),
        (16, "2026-08-24"),
    ]


def test_weekly_work_gets_the_next_chapters_inside_the_horizon():
    today = date(2026, 8, 29)  # Saturday
    history = weekly_history(date(2026, 6, 28), 9)  # Sundays, last on Aug 23
    expected = expected_releases(history, today=today, horizon_days=14)
    assert [(e.chapter, e.expected_at.isoformat()) for e in expected] == [
        ("1159", "2026-08-30"),
        ("1160", "2026-09-06"),
    ]
    assert expected[0].cadence_days == 7 and expected[0].cadence_label == "weekly"
    assert expected[0].last_chapter == "1158"


def test_overdue_release_keeps_its_number_and_reports_how_late_it_is():
    today = date(2026, 8, 29)
    history = weekly_history(date(2026, 6, 14), 9)  # last chapter 1158 on Aug 9
    expected = expected_releases(history, today=today, horizon_days=14)
    # Aug 16 is more than a cadence overdue and is skipped as a date; Aug 23
    # is less than one cadence late, so it is still "expected now", then Aug
    # 30. The chapter that did not come out is still the next one: 1159 is
    # 13 days late, it does not silently become 1160 (unORDINARY 394 stayed
    # 394 through the author's break).
    assert [(e.chapter, e.expected_at.isoformat()) for e in expected][:2] == [
        ("1159", "2026-08-23"),
        ("1160", "2026-08-30"),
    ]
    assert expected[0].overdue_days == 13
    assert expected[1].overdue_days == 0


def test_a_release_whose_date_has_not_come_is_not_overdue():
    today = date(2026, 8, 29)
    history = weekly_history(date(2026, 6, 28), 9)  # last on Aug 23
    expected = expected_releases(history, today=today, horizon_days=14)
    assert expected[0].chapter == "1159" and expected[0].overdue_days == 0


def test_no_prediction_without_rhythm_or_when_the_work_went_quiet():
    today = date(2026, 8, 29)
    assert expected_releases(weekly_history(date(2026, 8, 1), 3), today=today) == []
    stale = weekly_history(date(2026, 1, 4), 9)  # last in March
    assert expected_releases(stale, today=today) == []
    irregular = [
        {"chapter": "1", "release_date": "2026-01-01"},
        {"chapter": "2", "release_date": "2026-01-02"},
        {"chapter": "3", "release_date": "2026-01-03"},
        {"chapter": "4", "release_date": "2026-08-28"},
        {"chapter": "5", "release_date": "2026-08-29"},
    ]
    assert expected_releases(irregular, today=today) == []
