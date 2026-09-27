"""Persistent outages across every usable source of a series."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

from tankarr.catalogue import NO_REMOTE_PROVIDERS
from tankarr.languages import normalize_language_code


def _language(value: object) -> str | None:
    try:
        return normalize_language_code(value)
    except ValueError:
        return None


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
