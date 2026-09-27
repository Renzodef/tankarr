"""What the catalogues agree on, and what only one of them claims.

MangaBaka is the spine: it names the work and hands over the identifiers of
the same work on MangaUpdates, AniList, Kitsu and MyAnimeList. Those
catalogues are then read by id - never searched - so every record here is
the same work by construction. What differs is their *numbers*: how many
chapters and volumes the work has, and whether it has ended.

A number is worth different things depending on who backs it. Junko Mizuno's
Hansel & Gretel carried ``chapter_count: 1`` from MangaBaka alone, and that
single unconfirmed digit closed the acquisition frontier of the whole series.
Corroborated by a second catalogue it would have been a fact; standing alone
it was a claim, and a claim must not be allowed to cap a library.

So each count comes out with a confidence:

``agreed``       two or more catalogues put it within a chapter or ten percent
``lone``         exactly one catalogue knows it
``conflict``     several know it and none of them agree
``unconfirmed``  nobody knows it

Only ``agreed`` is strong enough to set a frontier or to declare a finished
work complete by count. The value kept is MangaBaka's when it sits inside
the agreeing cluster - it remains the descriptive source of truth - and the
cluster's own value when MangaBaka is the odd one out.

The English edition is a separate question with a separate answer. MangaBaka
lists the work's publishers with the edition each represents, and annotates
the English one with what it released: "2 Vols - Complete" is Kodansha's
Queen Emeraldas omnibus against a four-volume original. That note is the
managed edition's own volume count, and it is what the volume unit should
expect - not the original's, which the operator can never own in English.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable
from typing import Any

SPINE = "mangabaka"
# Records that are not an independent opinion: the series' own origin record
# is MangaBaka's data under another label, and a local import knows nothing.
# Counting them would let the spine agree with itself.
NOT_A_VOTER = frozenset({"catalogue", "local"})
COUNT_TOLERANCE = 0.10
AGREED = "agreed"
LONE = "lone"
CONFLICT = "conflict"
UNCONFIRMED = "unconfirmed"

# "12 Volumes", "12 Vols", "12 Physical Volumes": one qualifier may sit
# between the count and the word (VIZ's Master Keaton note), never a second
# number (that is the omnibus form below).
_EDITION_VOLUMES = re.compile(
    r"(\d+)\s*(?:[A-Za-z]+\s+)?(?:vols?|volumes?)\b", re.IGNORECASE
)
# "5 3-in-1 Volumes - Complete": five omnibus books of three tankobon each.
# Read naively, the volume regex above matched the "1" of "3-in-1" and
# declared Yokohama Kaidashi Kikou a one-book work.
_EDITION_OMNIBUS = re.compile(
    r"(\d+)\s+(\d+)\s*-?\s*in\s*-?\s*1\s+(?:vols?|volumes?)\b", re.IGNORECASE
)
_ENGLISH = frozenset({"english", "en"})


def _positive_integer(value: object) -> int | None:
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _close(left: int, right: int) -> bool:
    return (
        abs(left - right) <= 1
        or abs(left - right) / max(left, right) <= COUNT_TOLERANCE
    )


def count_consensus(records: Iterable[dict[str, Any]], field: str) -> dict[str, Any]:
    """One count, its confidence, and every catalogue's vote."""

    votes: dict[str, int] = {}
    for record in records:
        source = str(record.get("source") or "")
        value = _positive_integer(record.get(field))
        if source in NOT_A_VOTER:
            continue
        if source and value is not None and source not in votes:
            votes[source] = value
    if not votes:
        return {"value": None, "confidence": UNCONFIRMED, "votes": {}, "agreeing": []}
    if len(votes) == 1:
        ((source, value),) = votes.items()
        return {
            "value": value,
            "confidence": LONE,
            "votes": votes,
            "agreeing": [source],
        }
    # Cluster by agreement; the spine's cluster wins ties, else the largest.
    best: list[str] = []
    for anchor, anchor_value in votes.items():
        cluster = [
            source for source, value in votes.items() if _close(value, anchor_value)
        ]
        if len(cluster) > len(best) or (
            len(cluster) == len(best) and SPINE in cluster and SPINE not in best
        ):
            best = cluster
    if len(best) >= 2:
        value = votes[SPINE] if SPINE in best else votes[best[0]]
        return {"value": value, "confidence": AGREED, "votes": votes, "agreeing": best}
    value = votes.get(SPINE, next(iter(votes.values())))
    return {"value": value, "confidence": CONFLICT, "votes": votes, "agreeing": []}


def status_consensus(records: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Whether the work has ended, by majority of the catalogues that say."""

    votes: dict[str, str] = {}
    for record in records:
        source = str(record.get("source") or "")
        status = str(record.get("status") or "").strip().casefold()
        if source in NOT_A_VOTER:
            continue
        if source and status and source not in votes:
            votes[source] = status
    if not votes:
        return {"value": None, "confidence": UNCONFIRMED, "votes": {}}
    tally = Counter(votes.values())
    value, support = tally.most_common(1)[0]
    if support >= 2:
        return {"value": value, "confidence": AGREED, "votes": votes}
    if len(votes) == 1:
        return {"value": value, "confidence": LONE, "votes": votes}
    return {"value": votes.get(SPINE, value), "confidence": CONFLICT, "votes": votes}


def english_edition(records: Iterable[dict[str, Any]]) -> dict[str, Any] | None:
    """The English edition MangaBaka describes, with its own volume count."""

    for record in records:
        if str(record.get("source") or "") != SPINE:
            continue
        for item in record.get("publishers") or []:
            if not isinstance(item, dict):
                continue
            if str(item.get("type") or "").strip().casefold() not in _ENGLISH:
                continue
            note = str(item.get("note") or "")
            omnibus = _EDITION_OMNIBUS.search(note)
            match = _EDITION_VOLUMES.search(note)
            if omnibus:
                volume_count: int | None = int(omnibus.group(1))
            else:
                volume_count = int(match.group(1)) if match else None
            edition = {
                "publisher": str(item.get("name") or "").strip(),
                "volume_count": volume_count,
                "omnibus": int(omnibus.group(2)) if omnibus else 1,
                "complete": "complete" in note.casefold(),
                "note": note.strip(),
                "source": SPINE,
            }
            if edition["volume_count"] or edition["publisher"]:
                return edition
    return None


def corroborate(records: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Everything the canonical record needs to know about its numbers."""

    items = list(records)
    return {
        "chapter_count": count_consensus(items, "chapter_count"),
        "volume_count": count_consensus(items, "volume_count"),
        "status": status_consensus(items),
        "english_edition": english_edition(items),
    }


def count_is_reliable(metadata: dict[str, Any] | None, field: str) -> bool:
    """Whether a canonical count may set a frontier or declare completeness.

    A count nobody corroborated is a claim, and a claim never caps a library.
    Records from before corroboration existed carry no confidence at all and
    keep their old standing, so a metadata refresh, not a deploy, is what
    changes a series' behaviour.
    """

    confidence = ((metadata or {}).get("count_confidence") or {}).get(field)
    return confidence in (None, AGREED)


def managed_volume_count(metadata: dict[str, Any] | None) -> int | None:
    """Volumes the managed (English) edition has, when the catalogue says.

    Only a *complete* edition is the work's extent. Kana's Billy Bat ("1
    Volume; Ongoing") and VIZ's Pineapple Army ("2 Volumes - Dropped") are
    partial: taking their size as the target declared a 20-volume work
    complete after one book and cancelled every chapter still queued. A
    partial edition names a publisher, never a total.
    """

    edition = (metadata or {}).get("managed_edition") or {}
    if not edition.get("complete"):
        return None
    # An omnibus ("5 3-in-1 Volumes") is a binding, not the work's extent:
    # the sources deliver chapters and tankobon, and five books would call a
    # fourteen-volume pack an overrun. The catalogue's own count stands.
    if _positive_integer(edition.get("omnibus")) not in (None, 1):
        return None
    return _positive_integer(edition.get("volume_count"))


__all__ = [
    "AGREED",
    "CONFLICT",
    "LONE",
    "UNCONFIRMED",
    "corroborate",
    "count_consensus",
    "count_is_reliable",
    "english_edition",
    "managed_volume_count",
    "status_consensus",
]
