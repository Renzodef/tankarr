"""Opt-in replacement by a strictly preferred configured source."""

from tankarr.chapter_mapping import canonical_number
from tankarr.source_numbering import is_not_yet_released, is_stub

MAX_UPGRADES_PER_SERIES = 20


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
