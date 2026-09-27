"""Replace scanlator files with the official release once it exists.

The acquisition policy decides which release to grab first. Independently,
this opt-in pass replaces a non-official file when the publisher later exposes
the same canonical chapter.
"""

from __future__ import annotations

from typing import Any

from tankarr.chapter_mapping import canonical_number
from tankarr.source_numbering import is_not_yet_released, is_stub
from tankarr.source_ranking import is_official_release

MAX_UPGRADES_PER_SERIES = 20


def upgrade_candidates(
    chapters: list[dict[str, Any]], official_hosts: frozenset[str] | set[str]
) -> list[dict[str, Any]]:
    """Official, not yet downloaded releases whose chapter is on disk from
    a non-official source. One per chapter, sorted by chapter number."""

    if not official_hosts:
        return []
    downloaded_from_scans: dict[str, dict[str, Any]] = {}
    official_by_number: dict[str, dict[str, Any]] = {}
    for release in chapters:
        number = canonical_number(release.get("chapter"))
        if number is None:
            continue
        is_official = is_official_release(release, frozenset(official_hosts))
        if release.get("downloaded"):
            if is_official:
                official_by_number.pop(number, None)
                downloaded_from_scans.pop(number, None)
                downloaded_from_scans[number] = {"official": True}
            elif number not in downloaded_from_scans:
                downloaded_from_scans[number] = release
        elif (
            is_official
            and not release.get("queue_status")
            and not release.get("blocked")
            and not is_not_yet_released(release)
            and not is_stub(release)
        ):
            official_by_number.setdefault(number, release)
    candidates = [
        official_by_number[number]
        for number, current in downloaded_from_scans.items()
        if not current.get("official") and number in official_by_number
    ]

    def order(item: dict[str, Any]):
        try:
            return float(canonical_number(item.get("chapter")) or 0)
        except ValueError:
            return 0.0

    return sorted(candidates, key=order)[:MAX_UPGRADES_PER_SERIES]


__all__ = ["MAX_UPGRADES_PER_SERIES", "upgrade_candidates"]


def preferred_source_upgrades(chapters, ranking, hosts=frozenset()):
    """Only strictly better stable preferences for the same canonical content.

    Ignore fluctuating health/age/size, unmonitored and ambiguous releases,
    mixed languages, and chapter-vs-book comparisons. Never redownload on a tie.
    """
    from collections import defaultdict

    from tankarr.chapter_mapping import logical_release_key, volume_scoped_numbering

    scoped = volume_scoped_numbering(chapters)
    slots = defaultdict(list)
    for release in chapters:
        if release.get("release_unit", "chapter") != "chapter":
            continue
        if release.get("numbering_status", "mapped") != "mapped":
            continue
        if canonical_number(release.get("chapter")) is None:
            continue
        key = (
            release.get("language"),
            logical_release_key(release, volume_scoped=scoped),
        )
        slots[key].append(release)
    result = []
    for releases in slots.values():
        owned = [r for r in releases if r.get("downloaded")]
        if not owned or any(r.get("queue_status") for r in releases):
            continue
        candidates = [
            r
            for r in releases
            if not r.get("downloaded")
            and not r.get("blocked")
            and r.get("monitored", True)
            and r.get("provider") != "manual"
            and not is_not_yet_released(r)
            and not is_stub(r)
        ]
        if not candidates:
            continue
        candidate = min(
            candidates, key=lambda r: (ranking.upgrade_key(r, hosts), str(r["id"]))
        )
        if ranking.upgrade_key(candidate, hosts) < min(
            ranking.upgrade_key(r, hosts) for r in owned
        ):
            result.append(candidate)
    return result[:MAX_UPGRADES_PER_SERIES]
