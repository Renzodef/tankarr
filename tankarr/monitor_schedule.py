"""Which series a monitor cycle should refresh, and when to stop.

A cycle used to walk the whole library. On a large library that holds the
managed Suwayomi server for longer than the pause between cycles, so the
server never idles and catalogue cleanup never gets a stopped database.
The schedule below is persisted only through ``last_checked_at``: a restart
continues with whatever is still due instead of starting the alphabet again.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

DAILY_SECONDS = 86400.0
WEEKLY_SECONDS = 7 * DAILY_SECONDS
ERROR_FLOOR_SECONDS = 6 * 3600.0
HOT_BUDGET_SHARE = 0.75


def parse_timestamp(value: object) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def effective_status(status: object, status_override: object) -> str:
    """The publication state the schedule follows.

    ``ended`` is not returned for scheduling: the monitor already skips a
    manual ended override. ``continuing`` forces the frequent ongoing path.
    """

    override = str(status_override or "").strip().casefold()
    if override == "continuing":
        return "ongoing"
    if override == "hiatus":
        return "hiatus"
    raw = str(status or "").strip().casefold().replace("-", " ").replace("_", " ")
    return "_".join(raw.split())


def series_interval_seconds(
    status: str,
    newest_publish_at: datetime | None,
    now: datetime,
    monitor_interval: float,
) -> float:
    """Use the global watch interval until the catalogue marks a work ended."""

    from tankarr.series_summary import TERMINAL_STATUSES

    return WEEKLY_SECONDS if status in TERMINAL_STATUSES else float(monitor_interval)


def is_hot(interval: float, monitor_interval: float) -> bool:
    return float(interval) <= float(monitor_interval) + 1e-9


def is_due(
    last_checked_at: datetime | None,
    interval: float,
    now: datetime,
    *,
    error: bool = False,
) -> bool:
    """A series that has never been checked is due, including after a restart."""

    if last_checked_at is None:
        return True
    wait = max(float(interval), ERROR_FLOOR_SECONDS) if error else float(interval)
    return now >= last_checked_at + timedelta(seconds=wait)


def should_defer(
    elapsed: float,
    budget: float,
    *,
    hot: bool,
    started: bool,
    reserve_for_cold: bool = True,
) -> bool:
    """Whether this series waits for a later cycle.

    The first series always starts, so a budget smaller than one refresh
    still makes progress. Further hot series stop at ``HOT_BUDGET_SHARE``
    when colder series are waiting; once nothing colder is waiting they
    may use the rest. Anything stops once the budget is spent.
    """

    if not started:
        return False
    if elapsed >= budget:
        return True
    if hot and reserve_for_cold and elapsed >= HOT_BUDGET_SHARE * budget:
        return True
    return False
