"""Try every channel for a wanted release, and say what each one answered.

Wanted used to be a list of slots with nothing behind them. A pass refreshed
the mapped sources, created jobs for the releases those sources happened to
carry, and hunted books for series followed by volume. A slot no source
lists - the majority of a long Wanted list - went through that untouched and
came out the other side identical, so the pass reported ``queued: 0`` with no
errors and the list never moved. Nothing distinguished "nobody has ever
scanned this chapter" from "it is one search away".

This module makes the attempt explicit. Each wanted slot is taken down a
ladder of channels, in increasing cost:

1. ``sources``          - the mapped sources and the Suwayomi catalogue, which
                          the discovery pass has just refreshed: if a release
                          exists now, queue it;
2. ``indexer_chapter``  - ask Prowlarr for that chapter by name;
3. ``indexer_book``     - ask Prowlarr for the book that contains it, because
                          a chapter nobody scanned is often inside a volume
                          somebody released.

Every rung records what it answered, so the ledger can distinguish a slot
that is one retry away from one that no channel on earth carries. That is the
question Wanted has to answer before it asks for anyone's attention.

A grab from an indexer is only automatic when the result is unambiguous: the
work's title as a contiguous phrase, the chapter number the slot asks for, a
downloadable release, and no rival naming a different work. Everything else
is recorded as a match to confirm. This is deliberately the rule that would
have stopped Yuuichi Yokoyama's *Garden* being filled with another comic of
the same name.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from tankarr.release_kind import classify_release, title_matches_release
from tankarr.torrent_utils import release_match_score
from tankarr.volume_hunt import _identity_review_reason, rank_release, series_queries

logger = logging.getLogger(__name__)

# Channels, in the order the ladder walks them.
CHANNEL_SOURCES = "sources"
# The ledger key of a series nobody lists: there is no slot to ask for, so
# the whole work is the slot.
SERIES_SLOT = "series"
CHANNEL_INDEXER_CHAPTER = "indexer_chapter"
CHANNEL_INDEXER_BOOK = "indexer_book"

# What a channel answered.
QUEUED = "queued"  # a download job now exists for this slot
GRABBED = "grabbed"  # an indexer release was sent to the download client
PENDING = "pending"  # already queued or downloading; nothing to do
BLOCKED = "blocked"  # every release for this slot is blocked
NOT_OFFERED = "not_offered"  # the channel was asked and has nothing
AMBIGUOUS = "ambiguous"  # results exist but none is safe to take alone
UNAVAILABLE = "unavailable"  # the channel is off or not configured
ERROR = "error"

# One chapter costs the indexers at most this many searches per pass: two
# numbered forms plus the bare series query the book rung needs. Measured
# live, five or six forms per chapter was what made a pass over a backlog
# run into the request timeout before it reached the second series.
MAX_CHAPTER_QUERIES = 3
# A channel that answered "nobody has it" is not asked again for this long:
# indexer catalogues change over days, not minutes, and every repeated
# question is a search the newer slots of other series are waiting on.
RETRY_AFTER_DAYS = 7

# An outcome that ends the ladder: the slot is on its way.
SETTLED = frozenset({QUEUED, GRABBED, PENDING})

# Channels that must all have come back empty before a slot may be called
# unobtainable: the mapped sources, and at least one indexer rung.
MIN_EXHAUSTIVE_CHANNELS = 2

# How a whole slot reads once the ladder has run.
RECOVERING = "recovering"
NEEDS_REVIEW = "needs_review"
EXHAUSTED = "exhausted"
UNSEARCHED = "unsearched"


def book_hunt_outcome(
    hunted: dict[str, Any] | None, *, volumes: list[int | str] | None = None
) -> tuple[str, str]:
    """Translate actual book-search evidence, including packs and failures.

    A timeout is not evidence that a book does not exist. A pack's explicit
    coverage, not its nullable single-volume hint, tells which slots it fills.
    """

    from tankarr.chapter_mapping import canonical_number

    result = hunted or {}
    wanted = {
        number
        for volume in volumes or []
        if (number := canonical_number(volume)) is not None
    }
    if wanted and (by_volume := result.get("volume_hunts")):
        answers = [
            book_hunt_outcome(by_volume.get(volume), volumes=[volume])
            for volume in sorted(wanted)
        ]
        # Each mapped book can contain the chapter. Preserve a real success,
        # otherwise expose review/error evidence before an empty result.
        for outcome in (GRABBED, PENDING, AMBIGUOUS, ERROR, UNAVAILABLE, NOT_OFFERED):
            for answer in answers:
                if answer[0] == outcome:
                    return answer
    for book in result.get("grabbed") or []:
        coverage = {
            number
            for volume in book.get("volumes") or [book.get("volume")]
            if (number := canonical_number(volume)) is not None
        }
        if not wanted or coverage & wanted:
            label = ", ".join(sorted(coverage)) or str(book.get("title") or "pack")
            return GRABBED, f"grabbed book(s) {label}"[:200]
    pending = {
        number
        for volume in result.get("pending_volumes") or []
        if (number := canonical_number(volume)) is not None
    }
    if pending and (not wanted or wanted <= pending):
        return PENDING, "book(s) already queued or downloading"
    if result.get("needs_review"):
        return AMBIGUOUS, "book search found a release that needs confirming"
    if result.get("errors"):
        return ERROR, "; ".join(str(error) for error in result["errors"][:2])[:200]
    searched = {
        number
        for volume in result.get("searched_volumes") or []
        if (number := canonical_number(volume)) is not None
    }
    if not searched or (wanted and not wanted <= searched):
        return UNAVAILABLE, "no mapped book was searched"
    label = ", ".join(str(volume) for volume in volumes or [])
    return NOT_OFFERED, (
        f"no indexer or archive offers book {label}"
        if label
        else "no indexer or archive offers a book of this work"
    )


def merge_book_hunts(
    previous: dict[str, Any], current: dict[str, Any]
) -> dict[str, Any]:
    """Extend one pass's evidence without leaking a new book's error to old ones."""

    from tankarr.chapter_mapping import canonical_number

    by_volume: dict[str, dict[str, Any]] = {}
    grabbed: list[dict[str, Any]] = []
    jobs: dict[Any, dict[str, Any]] = {}
    for result in (previous, current):
        by_volume.update(result.get("volume_hunts") or {})
        evidence = {
            key: value for key, value in result.items() if key != "volume_hunts"
        }
        for volume in [
            *(result.get("searched_volumes") or []),
            *(result.get("pending_volumes") or []),
        ]:
            if (number := canonical_number(volume)) is not None:
                if number not in (result.get("volume_hunts") or {}):
                    by_volume[number] = evidence
        for book in result.get("grabbed") or []:
            job_id = book.get("job_id")
            if job_id is not None and job_id in jobs:
                existing = jobs[job_id]
                covered = set(existing.get("volumes") or [existing.get("volume")])
                covered.update(book.get("volumes") or [book.get("volume")])
                existing["volumes"] = sorted(
                    volume for volume in covered if volume is not None
                )
                existing["volume"] = (
                    existing["volumes"][0] if len(existing["volumes"]) == 1 else None
                )
            else:
                copied = dict(book)
                grabbed.append(copied)
                if job_id is not None:
                    jobs[job_id] = copied
    return {
        "grabbed": grabbed,
        "errors": [*(previous.get("errors") or []), *(current.get("errors") or [])],
        "searched_volumes": sorted(
            set(previous.get("searched_volumes") or [])
            | set(current.get("searched_volumes") or [])
        ),
        "pending_volumes": sorted(
            set(previous.get("pending_volumes") or [])
            | set(current.get("pending_volumes") or [])
        ),
        "needs_review": bool(
            previous.get("needs_review") or current.get("needs_review")
        ),
        "volume_hunts": by_volume,
    }


def _decimal(value: object) -> Decimal | None:
    text = str(value if value is not None else "").strip()
    if not text:
        return None
    try:
        number = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def slot_key_for(row: dict[str, Any]) -> str:
    """The canonical slot a wanted row stands for.

    Recovery is recorded against the slot, never against the release that
    happens to be the current best candidate for it: the ledger has to survive
    a source being remapped, demoted or replaced by a better one. This is the
    same key the chapter index gives its slots, so the two always line up.
    """

    from tankarr.chapter_mapping import canonical_number

    chapter = canonical_number(row.get("chapter"))
    if chapter is not None:
        return f"chapter:{chapter}"
    volume = canonical_number(row.get("volume"))
    if volume is not None:
        return f"volume:{volume}"
    return f"release:{row.get('id')}"


def chapter_queries(
    title: str, chapter: str, publisher: str | list[str] | None = None
) -> list[str]:
    """Indexer queries for one chapter, cheapest and most specific first.

    Indexers tokenise badly and a chapter is named a dozen ways ("c012",
    "Chapter 12", "Ch.12"), so the numbered forms are asked first and the
    bare series query last: it pools everything the indexers have for the
    work, which is also what the book rung needs.
    """

    base = " ".join(str(title or "").split()).strip()
    number = _decimal(chapter)
    queries: list[str] = []
    if base and number is not None:
        whole = number == number.to_integral_value()
        label = format(number.normalize(), "f")
        forms = [f"{base} chapter {label}", f"{base} c{label}"]
        if whole:
            forms.append(f"{base} c{int(number):03d}")
        for form in forms[: MAX_CHAPTER_QUERIES - 1]:
            if form not in queries:
                queries.append(form)
    for query in series_queries(base, publisher):
        if query and query not in queries:
            queries.append(query)
    return queries[:MAX_CHAPTER_QUERIES]


def recently_answered(
    attempts: list[dict[str, Any]],
    channel: str,
    *,
    now: datetime | None = None,
    days: int = RETRY_AFTER_DAYS,
) -> bool:
    """Whether ``channel`` gave this slot a real answer within the window.

    Only informative answers count - "nobody has it" or "needs a human" - so
    a slot that errored out, or that no channel has reached yet, is asked
    again at once.
    """

    moment = now or datetime.now(UTC)
    for item in attempts:
        if str(item.get("channel")) != channel:
            continue
        if str(item.get("outcome")) not in {NOT_OFFERED, AMBIGUOUS}:
            continue
        stamp = _timestamp(item.get("attempted_at"))
        if stamp is not None and moment - stamp < timedelta(days=days):
            return True
    return False


def _timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _release_chapter(release: dict[str, Any], kind: Any) -> Decimal | None:
    return _decimal(kind.chapter) or _decimal(release.get("chapter"))


def pick_chapter_release(
    results: list[dict[str, Any]],
    *,
    title: str,
    chapter: str,
    publisher: str | list[str] | None = None,
) -> tuple[dict[str, Any] | None, str]:
    """The one indexer result that is unmistakably this chapter of this work.

    Several releases of the same chapter are a choice, not an ambiguity: the
    ranking picks among them exactly as the volume hunt does. What is never
    resolved here is a *different work* - a rival result whose name is not
    this work's title - and a chapter number that has to be guessed.
    """

    wanted = _decimal(chapter)
    if wanted is None:
        return None, "the slot has no chapter number to search for"
    publishers = [publisher] if isinstance(publisher, str) else list(publisher or [])
    candidates: list[tuple[tuple[int, ...], dict[str, Any]]] = []
    reviewed = 0
    for release in results:
        if release.get("download"):
            continue  # already grabbed or being grabbed
        name = str(release.get("title") or "")
        kind = classify_release(
            title=name,
            pages=release.get("pages"),
            size_bytes=release.get("size_bytes"),
        )
        if kind.kind != "chapter":
            continue
        if _release_chapter(release, kind) != wanted:
            continue
        if release_match_score(title, name) < 100 or not title_matches_release(
            title, name
        ):
            continue
        identity_review = _identity_review_reason(title, name, publishers)
        if identity_review:
            release["_review"] = identity_review
            reviewed += 1
            continue
        score = max(
            (rank_release(release, name) for name in publishers),
            default=rank_release(release, None),
        )
        if score[0] < 0:
            continue  # a dead torrent is not a candidate
        candidates.append((score, release))
    if not candidates:
        if reviewed:
            return None, f"{reviewed} result(s) name the work too loosely to take"
        return None, "no indexer result is this chapter of this work"
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1], ""


_VOLUME_OF_CHAPTER = re.compile(r"^\s*(\d+)")


def containing_volumes(chapter: str, chapter_map: list[Any] | None) -> list[int]:
    """The book(s) the catalogue says this chapter is in, if it says so."""

    from tankarr.chapter_map import chapters_by_volume

    wanted = _decimal(chapter)
    if wanted is None:
        return []
    volumes: list[int] = []
    for volume, chapters in chapters_by_volume(list(chapter_map or [])).items():
        if any(_decimal(item) == wanted for item in chapters):
            match = _VOLUME_OF_CHAPTER.match(str(volume))
            if match:
                volumes.append(int(match.group(1)))
    return sorted(set(volumes))


def slot_verdict(attempts: list[dict[str, Any]]) -> dict[str, Any]:
    """Attach observed checks and retry eligibility, never a promised run time."""

    result = _slot_verdict(attempts)
    checked: list[datetime] = []
    eligible: list[datetime] = []
    for channel, attempt in zip(result["channels"], attempts, strict=True):
        stamp = _timestamp(attempt.get("attempted_at"))
        if stamp is not None:
            checked.append(stamp)
        retry_at = (
            stamp + timedelta(days=RETRY_AFTER_DAYS)
            if stamp is not None
            and channel["channel"] in {CHANNEL_INDEXER_CHAPTER, CHANNEL_INDEXER_BOOK}
            and channel["outcome"] in {NOT_OFFERED, AMBIGUOUS}
            else None
        )
        if retry_at is not None:
            eligible.append(retry_at)
        channel["checked_at"] = stamp.isoformat() if stamp is not None else None
        channel["next_eligible_at"] = (
            retry_at.isoformat() if retry_at is not None else None
        )
        channel["actionable_reason"] = str(channel["detail"] or result["summary"])
    result["checked_at"] = max(checked).isoformat() if checked else None
    result["next_eligible_at"] = (
        min(eligible).isoformat()
        if eligible and result["verdict"] in {EXHAUSTED, NEEDS_REVIEW}
        else None
    )
    result["actionable_reason"] = {
        RECOVERING: "A download is already in progress; inspect Activity for its status.",
        NEEDS_REVIEW: "Review the matching or blocked release before changing acquisition rules.",
        EXHAUSTED: "Configured channels returned no usable release; retry after cooldown or map another verified source.",
        UNSEARCHED: "Run recovery or check the unavailable channel before declaring this release unobtainable.",
    }[result["verdict"]]
    return result


def _slot_verdict(attempts: list[dict[str, Any]]) -> dict[str, Any]:
    """How the whole slot reads, from what the channels answered.

    ``unsearched`` is not a failure: it means no channel has run yet, and a
    slot in that state must not be presented as unobtainable.
    """

    if not attempts:
        return {
            "verdict": UNSEARCHED,
            "summary": "not searched yet",
            "channels": [],
        }
    outcomes = {str(item.get("channel")): str(item.get("outcome")) for item in attempts}
    channels = [
        {
            "channel": str(item.get("channel")),
            "outcome": str(item.get("outcome")),
            "detail": str(item.get("detail") or ""),
            "at": str(item.get("attempted_at") or ""),
        }
        for item in attempts
    ]
    if any(outcome in SETTLED for outcome in outcomes.values()):
        return {
            "verdict": RECOVERING,
            "summary": "a release is on its way",
            "channels": channels,
        }
    if AMBIGUOUS in outcomes.values():
        return {
            "verdict": NEEDS_REVIEW,
            "summary": "the indexers offer something that needs confirming",
            "channels": channels,
        }
    if BLOCKED in outcomes.values():
        return {
            "verdict": NEEDS_REVIEW,
            "summary": "every release for this chapter is blocked",
            "channels": channels,
        }
    searched = [
        channel
        for channel, outcome in outcomes.items()
        if outcome in {NOT_OFFERED, AMBIGUOUS}
    ]
    unavailable = sorted(
        channel
        for channel, outcome in outcomes.items()
        if outcome in {UNAVAILABLE, ERROR}
    )
    # "Nobody has it" is a strong claim and one channel cannot support it:
    # the sources rung answers ``not_offered`` for every slot no mapped
    # extension lists, which is exactly the population the indexers exist to
    # cover. It takes a source channel *and* an indexer channel coming back
    # empty before Wanted may tell the operator to stop waiting. This is
    # checked before ``unavailable``: two channels that already answered
    # empty settle the slot even if a third rung errored out, so one flaky
    # indexer request does not hide an otherwise exhausted verdict.
    if (
        len(searched) >= MIN_EXHAUSTIVE_CHANNELS
        and CHANNEL_SOURCES in searched
        and any(
            channel in searched
            for channel in (CHANNEL_INDEXER_CHAPTER, CHANNEL_INDEXER_BOOK)
        )
    ):
        return {
            "verdict": EXHAUSTED,
            "summary": "no source and no indexer carries this release",
            "channels": channels,
        }
    if unavailable:
        return {
            "verdict": UNSEARCHED,
            "summary": (
                "not fully searched: " + ", ".join(unavailable) + " unavailable"
            ),
            "channels": channels,
        }
    return {
        "verdict": UNSEARCHED,
        "summary": "not searched on every channel yet",
        "channels": channels,
    }


__all__ = [
    "AMBIGUOUS",
    "BLOCKED",
    "CHANNEL_INDEXER_BOOK",
    "CHANNEL_INDEXER_CHAPTER",
    "CHANNEL_SOURCES",
    "ERROR",
    "EXHAUSTED",
    "GRABBED",
    "MIN_EXHAUSTIVE_CHANNELS",
    "NEEDS_REVIEW",
    "NOT_OFFERED",
    "PENDING",
    "QUEUED",
    "RECOVERING",
    "SETTLED",
    "UNAVAILABLE",
    "UNSEARCHED",
    "chapter_queries",
    "containing_volumes",
    "pick_chapter_release",
    "slot_key_for",
    "slot_verdict",
]
