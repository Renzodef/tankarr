"""Expected upcoming releases, Sonarr-style, from a work's release history.

MangaUpdates' release feed records when each chapter appeared. For a
running work that history has a rhythm — One Piece every Sunday with a
break every fourth or fifth week, a monthly seinen around the 25th — and
that rhythm is enough to put "chapter 1163 · expected Sunday" on the
calendar and to search for it on the right day instead of waiting for the
next six-hourly pass.

The prediction is deliberately conservative: it needs several dated
releases, a stable median interval, and a recent last release; otherwise
the work simply has no expected entry.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from statistics import median
from typing import Any

MIN_SAMPLES = 4
MIN_INTERVAL_DAYS = 3
MAX_INTERVAL_DAYS = 62
STALE_FACTOR = 3.0  # no release for 3× the cadence → the rhythm is broken
MAX_PREDICTIONS = 6


@dataclass(frozen=True)
class ExpectedRelease:
    chapter: str
    expected_at: date
    cadence_days: int
    cadence_label: str
    last_chapter: str
    last_release_at: date
    # Days since the first date this chapter was expected on and did not
    # appear; 0 for a chapter whose date has not passed. The number never
    # rolls forward: the next chapter of a work is always last + 1, whatever
    # the author's breaks did to the date.
    overdue_days: int = 0


def _parse_date(value: object) -> date | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None


_NUMBER = re.compile(r"^\s*(\d+(?:\.\d+)?)")
_RANGE = re.compile(r"^\s*(\d+)\s*[-–]\s*(\d+)\b")
MAX_RANGE_SPAN = 10


def _chapter_number(value: object) -> Decimal | None:
    """The chapter a release label names: ``"1191"``, ``"794 v2"`` → 794,
    ``"757-760"`` → 760 (short ranges only), ``"Extra"`` → ``None``."""

    raw = str(value or "").strip()
    if not raw:
        return None
    span = _RANGE.match(raw)
    if span:
        first, last = int(span.group(1)), int(span.group(2))
        if last < first or last - first > MAX_RANGE_SPAN:
            return None
        return Decimal(last)
    match = _NUMBER.match(raw)
    if not match:
        return None
    try:
        return Decimal(match.group(1))
    except InvalidOperation:
        return None


def cadence_label(days: int) -> str:
    if days <= 8:
        return "weekly"
    if days <= 16:
        return "biweekly"
    if days <= 35:
        return "monthly"
    return "bimonthly"


def release_timeline(history: Iterable[dict[str, Any]]) -> list[tuple[Decimal, date]]:
    """Dated chapter releases, one per chapter (its first appearance)."""

    first_seen: dict[Decimal, date] = {}
    for item in history:
        number = _chapter_number(item.get("chapter"))
        released = _parse_date(item.get("release_date"))
        if number is None or released is None:
            continue
        if number != number.to_integral_value():
            continue  # extras and split chapters do not carry the rhythm
        current = first_seen.get(number)
        if current is None or released < current:
            first_seen[number] = released
    return sorted(first_seen.items())


def expected_releases(
    history: Iterable[dict[str, Any]],
    *,
    today: date | None = None,
    horizon_days: int = 14,
    samples: int = 8,
) -> list[ExpectedRelease]:
    """Predict the next chapters inside the horizon, or nothing."""

    timeline = release_timeline(history)
    if len(timeline) < MIN_SAMPLES:
        return []
    recent = timeline[-samples:]
    intervals = [
        (later - earlier).days
        for (_n1, earlier), (_n2, later) in zip(recent, recent[1:])
        if (later - earlier).days > 0
    ]
    if len(intervals) < MIN_SAMPLES - 1:
        return []
    cadence = int(round(median(intervals)))
    if cadence < MIN_INTERVAL_DAYS or cadence > MAX_INTERVAL_DAYS:
        return []
    last_number, last_date = recent[-1]
    current = today or datetime.now(UTC).date()
    if (current - last_date).days > cadence * STALE_FACTOR:
        return []
    label = cadence_label(cadence)
    horizon = current + timedelta(days=horizon_days)
    results: list[ExpectedRelease] = []
    first_date = last_date + timedelta(days=cadence)
    next_date = first_date
    next_number = last_number + 1
    # A release that is overdue by less than one cadence still counts as
    # "expected now" rather than vanishing from the calendar. Its date moves
    # to the next slot of the rhythm; its number does not, because the
    # chapter that did not come out is still the one that comes next.
    while next_date < current - timedelta(days=cadence):
        next_date += timedelta(days=cadence)
    overdue = max(0, (current - first_date).days)
    while next_date <= horizon and len(results) < MAX_PREDICTIONS:
        results.append(
            ExpectedRelease(
                chapter=str(next_number),
                expected_at=next_date,
                cadence_days=cadence,
                cadence_label=label,
                last_chapter=str(last_number),
                last_release_at=last_date,
                overdue_days=overdue if next_number == last_number + 1 else 0,
            )
        )
        next_date += timedelta(days=cadence)
        next_number += 1
    return results


__all__ = ["ExpectedRelease", "cadence_label", "expected_releases", "release_timeline"]
