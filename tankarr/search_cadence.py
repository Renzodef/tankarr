"""When the Wanted pass looks at a series again.

Every six hours the Wanted pass used to refresh, rediscover and search every
series with something missing, the same way whether the work ended twenty
years ago or is publishing this week. Sources for a finished work change
rarely, so each empty pass pushes the next look further away: six hours,
a day, three days, a week, a month. Anything that changes the odds resets
the ladder: a release was queued, a new source was discovered, or a person
asked. A running work is searched by the release monitor when a chapter is
due; its old gaps follow the same ladder as a finished work.

A pass also has a budget: the most overdue series go first, the rest wait
for the next pass, so one pass can never saturate a small host.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

LADDER = (
    timedelta(hours=6),
    timedelta(days=1),
    timedelta(days=3),
    timedelta(days=7),
    timedelta(days=30),
)


@dataclass
class SearchState:
    manga_id: str
    last_search_at: datetime | None = None
    next_search_at: datetime | None = None
    attempts: int = 0
    last_found_at: datetime | None = None

    def as_row(self) -> dict[str, Any]:
        return {
            "manga_id": self.manga_id,
            "last_search_at": _iso(self.last_search_at),
            "next_search_at": _iso(self.next_search_at),
            "attempts": int(self.attempts),
            "last_found_at": _iso(self.last_found_at),
        }

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> SearchState:
        return cls(
            manga_id=str(row["manga_id"]),
            last_search_at=_parse(row.get("last_search_at")),
            next_search_at=_parse(row.get("next_search_at")),
            attempts=int(row.get("attempts") or 0),
            last_found_at=_parse(row.get("last_found_at")),
        )


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat() if value else None


def _parse(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def interval_after(attempts: int) -> timedelta:
    """The wait after ``attempts`` consecutive passes that found nothing."""

    return LADDER[min(max(attempts, 0), len(LADDER) - 1)]


def is_due(state: SearchState | None, now: datetime) -> bool:
    if state is None or state.next_search_at is None:
        return True
    return state.next_search_at <= now


def after_pass(
    state: SearchState | None,
    manga_id: str,
    now: datetime,
    *,
    found: bool,
    discovered: bool,
) -> SearchState:
    """The state after a pass: found or discovered resets the ladder."""

    current = state or SearchState(manga_id=manga_id)
    attempts = 0 if (found or discovered) else current.attempts + 1
    return SearchState(
        manga_id=manga_id,
        last_search_at=now,
        next_search_at=now + interval_after(attempts),
        attempts=attempts,
        last_found_at=now if found else current.last_found_at,
    )


def plan_pass(
    entries: list[dict[str, Any]],
    states: dict[str, SearchState],
    now: datetime,
    *,
    budget: int,
    force: bool = False,
) -> tuple[list[dict[str, Any]], int]:
    """The entries to search this pass, most overdue first, within budget.

    Returns ``(planned, skipped)`` where ``skipped`` counts the series that
    are not due yet or did not fit the budget. ``force`` (a person asked)
    ignores the ladder but still honours the budget.
    """

    def overdue(entry: dict[str, Any]) -> float:
        state = states.get(str(entry["manga"]["id"]))
        if state is None or state.next_search_at is None:
            return float("inf")
        return (now - state.next_search_at).total_seconds()

    candidates = [
        entry
        for entry in entries
        if force or is_due(states.get(str(entry["manga"]["id"])), now)
    ]
    candidates.sort(key=overdue, reverse=True)
    limit = max(1, int(budget or 0)) if budget else len(candidates)
    planned = candidates[:limit]
    return planned, len(entries) - len(planned)
