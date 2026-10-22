"""Which series a monitor cycle should refresh, and when to stop.

A cycle used to walk the whole library. On a large library that holds the
managed Suwayomi server for longer than the pause between cycles, so the
server never idles and catalogue cleanup never gets a stopped database.
The schedule below is persisted only through ``last_checked_at``: a restart
continues with whatever is still due instead of starting the alphabet again.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

HOT_DAYS = 14
WARM_DAYS = 90
DAILY_SECONDS = 86400.0
WEEKLY_SECONDS = 7 * DAILY_SECONDS
# A timed-out or failed series stays out of the next cycles. Source-level
# cooldowns still apply on top of this once it is checked again.
ERROR_FLOOR_SECONDS = 6 * 3600.0
# Leave the tail of the budget for series that are not publishing, so a
# large set of active series cannot postpone the rest forever.
HOT_BUDGET_SHARE = 0.75

# Finished or paused works are not expected to publish. Hiatus is paused
# here even though the library still counts it as continuing: a chapter in
# the hot window puts it back on the frequent schedule anyway.
STALE_STATUSES = frozenset(
    {
        "completed",
        "complete",
        "ended",
        "finished",
        "cancelled",
        "canceled",
        "hiatus",
        "on_hiatus",
        "paused",
        "abandoned",
        "dropped",
        "discontinued",
    }
)


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
    """How long to wait after a successful check before refreshing again.

    A chapter from the last ``HOT_DAYS`` uses the monitor interval whatever
    the catalogue status says, so a series that just resumed or just ended
    is still seen promptly. An ongoing series with no dated chapter, and any
    series that is not explicitly finished or paused and published within
    ``WARM_DAYS``, waits a day. Everything else waits a week.
    """

    age = None
    if newest_publish_at is not None:
        age = (now - newest_publish_at).total_seconds()
    if age is not None and age <= HOT_DAYS * 86400:
        return float(monitor_interval)
    if status not in STALE_STATUSES and (age is None or age <= WARM_DAYS * 86400):
        return DAILY_SECONDS
    return WEEKLY_SECONDS


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
