"""Perennial health ledger for download sources.

Tankarr never uninstalls a source for being unreliable: reliability is a
*ranking* problem, not a membership one. Every download outcome feeds a
per-source exponential moving average of success and speed; the resulting
score orders sources inside their class, drops chronic failers behind every
healthy source, and lets them climb back automatically when they start
working again. A source that is the only one carrying a release is still
tried: demotion is an ordering, never an exclusion.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

# Weight of the newest outcome in the success average: high enough that a
# broken source sinks within a handful of failures, low enough that one bad
# night does not erase months of good service.
ALPHA = 0.2
# An engine restart or a dropped connection says little about the source, so
# a transient failure moves the average by less than a hard one.
TRANSIENT_ALPHA = 0.08
# What a source nobody has measured yet is assumed to be: it starts in the
# middle of the table and earns its place in either direction.
NEUTRAL_SUCCESS = 0.5
# Speed that reads as "fast enough": at 1 MiB/s the speed factor is 0.5,
# saturating towards 1 as downloads get faster.
SPEED_REFERENCE_BPS = 1024.0 * 1024.0
# A record nobody refreshed slides back towards neutral: the site may have
# been fixed, or broken, since. Half the distance is forgotten per period.
STALE_HALF_LIFE_DAYS = 30.0
# Below this score, with enough attempts to mean it, the source is demoted
# behind every healthy one (still usable when it is the only carrier).
UNHEALTHY_SCORE = 0.25
MIN_ATTEMPTS = 5

SUCCESS_WEIGHT = 0.8
SPEED_WEIGHT = 0.2

# Sort component of a source without a record: the neutral score, negated.
NEUTRAL_COMPONENT = -500


def _parse(timestamp: object) -> datetime | None:
    raw = str(timestamp or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def decayed_success(
    ema: float, updated_at: object, *, now: datetime | None = None
) -> float:
    """The success average, slid towards neutral by the record's age."""

    updated = _parse(updated_at)
    if updated is None:
        return float(ema)
    current = now or datetime.now(UTC)
    days = max(0.0, (current - updated).total_seconds() / 86400.0)
    keep = 0.5 ** (days / STALE_HALF_LIFE_DAYS)
    return NEUTRAL_SUCCESS + (float(ema) - NEUTRAL_SUCCESS) * keep


def update(
    row: Mapping[str, Any] | None,
    *,
    ok: bool,
    bytes_downloaded: int = 0,
    seconds: float = 0.0,
    transient: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Fold one outcome into a health record, returning the new record."""

    current = now or datetime.now(UTC)
    previous = dict(row or {})
    ema = decayed_success(
        float(previous.get("ema_success", NEUTRAL_SUCCESS)),
        previous.get("updated_at"),
        now=current,
    )
    alpha = TRANSIENT_ALPHA if (transient and not ok) else ALPHA
    ema = ema + alpha * ((1.0 if ok else 0.0) - ema)
    speed = float(previous.get("ema_speed_bps") or 0.0)
    if ok and bytes_downloaded > 0 and seconds > 0:
        sample = bytes_downloaded / seconds
        speed = sample if speed <= 0 else speed + ALPHA * (sample - speed)
    return {
        "attempts": int(previous.get("attempts") or 0) + 1,
        "failures": int(previous.get("failures") or 0) + (0 if ok else 1),
        "ema_success": ema,
        "ema_speed_bps": speed,
        "updated_at": current.isoformat(timespec="seconds"),
    }


def score(row: Mapping[str, Any], *, now: datetime | None = None) -> float:
    """0..1: how much this source deserves to be tried before its peers."""

    success = decayed_success(
        float(row.get("ema_success", NEUTRAL_SUCCESS)),
        row.get("updated_at"),
        now=now,
    )
    speed = float(row.get("ema_speed_bps") or 0.0)
    speed_factor = (
        speed / (speed + SPEED_REFERENCE_BPS) if speed > 0 else NEUTRAL_SUCCESS
    )
    return SUCCESS_WEIGHT * success + SPEED_WEIGHT * speed_factor


def sort_component(value: float) -> int:
    """Lower sorts first: the score negated onto an integer scale."""

    return -round(max(0.0, min(1.0, value)) * 1000)


def is_unhealthy(row: Mapping[str, Any], *, now: datetime | None = None) -> bool:
    """Chronic failer: enough attempts to judge, score below the floor."""

    return (
        int(row.get("attempts") or 0) >= MIN_ATTEMPTS
        and score(row, now=now) < UNHEALTHY_SCORE
    )


def components(
    rows: Iterable[Mapping[str, Any]], *, now: datetime | None = None
) -> dict[str, int]:
    """source_key -> sort component (lower is better) for every record."""

    current = now or datetime.now(UTC)
    return {
        str(row["source_key"]): sort_component(score(row, now=current)) for row in rows
    }


def unhealthy_keys(
    rows: Iterable[Mapping[str, Any]], *, now: datetime | None = None
) -> frozenset[str]:
    current = now or datetime.now(UTC)
    return frozenset(
        str(row["source_key"]) for row in rows if is_unhealthy(row, now=current)
    )


__all__ = [
    "ALPHA",
    "MIN_ATTEMPTS",
    "NEUTRAL_COMPONENT",
    "NEUTRAL_SUCCESS",
    "SPEED_REFERENCE_BPS",
    "STALE_HALF_LIFE_DAYS",
    "TRANSIENT_ALPHA",
    "UNHEALTHY_SCORE",
    "components",
    "decayed_success",
    "is_unhealthy",
    "score",
    "sort_component",
    "unhealthy_keys",
    "update",
]
