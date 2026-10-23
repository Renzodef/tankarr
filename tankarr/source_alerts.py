"""Persistent outages across every usable source of a series."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

from tankarr.catalogue import NO_REMOTE_PROVIDERS
from tankarr.languages import normalize_language_code
from tankarr.monitor_schedule import parse_timestamp
from tankarr.suwayomi_runtime import CHALLENGE_PATTERN

# The monitor waits at most a week between checks. Two weeks is still a
# current verdict; a month-old error is not.
CHALLENGE_RECENCY = timedelta(days=14)
# One stale row must not speak for a source whose other checks succeeded.
CHALLENGE_FAILURE_SHARE = 0.5


def _language(value: object) -> str | None:
    try:
        return normalize_language_code(value)
    except ValueError:
        return None


def _mapping_enabled(mapping: dict[str, Any]) -> bool:
    enabled = mapping.get("enabled", 1)
    if isinstance(enabled, str):
        return enabled.strip().casefold() not in {"0", "false", "no", ""}
    return bool(enabled)


def _recent_check(mapping: dict[str, Any], now: datetime) -> datetime | None:
    checked = parse_timestamp(mapping.get("last_checked_at")) or parse_timestamp(
        mapping.get("error_since")
    )
    if checked is None or now - checked > CHALLENGE_RECENCY:
        return None
    return checked


def challenged_source_names(
    mappings: list[dict[str, Any]],
    *,
    probes: list[str] | None = None,
    now: datetime | None = None,
) -> list[str]:
    """Sources currently behind an anti-bot challenge.

    Probe names are the runtime's own measurement and are always reported.
    Saved mapping errors count only when the mapping is enabled, the check
    is recent, a later success of the same source has not superseded it, and
    challenge failures are at least half of that source's recent checks.
    """

    now = now or datetime.now(UTC)
    grouped: dict[str, list[tuple[datetime, str | None]]] = defaultdict(list)
    display: dict[str, str] = {}
    for mapping in mappings:
        if not _mapping_enabled(mapping):
            continue
        name = str(mapping.get("source_name") or "").strip()
        if not name:
            continue
        checked = _recent_check(mapping, now)
        if checked is None:
            continue
        key = name.casefold()
        display.setdefault(key, name)
        error = str(mapping.get("last_error") or "").strip() or None
        grouped[key].append((checked, error))

    names = [str(item).strip() for item in probes or [] if str(item).strip()]
    for key, rows in grouped.items():
        newest_success = max(
            (checked for checked, error in rows if not error),
            default=None,
        )
        considered = 0
        failures = 0
        for checked, error in rows:
            challenge = bool(error and CHALLENGE_PATTERN.search(error))
            if challenge and newest_success is not None and newest_success >= checked:
                continue
            considered += 1
            if challenge:
                failures += 1
        if considered and failures / considered >= CHALLENGE_FAILURE_SHARE:
            names.append(display[key])
    return sorted(set(names))


def _over_a_day(error: object, since: object, now: datetime) -> bool:
    if not error or not since:
        return False
    try:
        started = datetime.fromisoformat(str(since).replace("Z", "+00:00"))
    except ValueError:
        return False
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    return now - started > timedelta(hours=24)


def source_outages(
    manga_list: list[dict[str, Any]],
    mappings: list[dict[str, Any]],
    providers: dict[str, Any],
    *,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Series whose usable sources have all failed continuously for over 24h.

    The caller intersects this result with Wanted. A healthy, untried or recently
    failing source prevents an alert, regardless of its ranking or error text.
    """

    now = now or datetime.now(UTC)
    by_manga: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for mapping in mappings:
        by_manga[str(mapping["manga_id"])].append(mapping)
    outages = []
    for manga in manga_list:
        language = _language(manga.get("preferred_language"))
        if language is None:
            continue

        def usable(name: str) -> bool:
            provider = providers.get(name)
            return provider is not None and provider.supports_language(language)

        failed = []
        primary = str(manga.get("provider") or "")
        has_primary = primary not in NO_REMOTE_PROVIDERS and usable(primary)
        if has_primary:
            failed.append(
                _over_a_day(
                    manga.get("primary_source_error"),
                    manga.get("primary_source_error_since"),
                    now,
                )
            )
        for mapping in by_manga[str(manga["id"])]:
            provider_name = str(mapping["provider"])
            if (
                not mapping.get("enabled")
                or not usable(provider_name)
                or _language(mapping.get("language")) != language
            ):
                continue
            if (
                has_primary
                and provider_name == primary
                and str(mapping["provider_manga_id"]) == str(manga["id"])
            ):
                continue
            failed.append(
                _over_a_day(mapping.get("last_error"), mapping.get("error_since"), now)
            )
        if failed and all(failed):
            outages.append(manga)
    return outages
