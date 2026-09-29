"""Reconcile a source's own episode numbering with the work's chapters.

Webtoon platforms number *episodes*: specials, notices and season breaks take
a slot, so the last episode number drifts away from the chapter number the
work actually reached. The episode title still carries the real number
("250. Cylrit's Sword" as episode 293), which is evidence enough to renumber
the whole source deterministically.

Paid episodes are marked with a lock in their title. They cannot be fetched,
so they are not download candidates and must not inflate the chapter count.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from typing import Any

LOCK_MARKERS = ("\U0001f512", "\U0001f510", "\U0001f50f")
_TITLE_NUMBER = re.compile(r"^\s*(?:[^\w\s]\s*)*(\d{1,4}(?:\.\d{1,2})?)\s*[.)]\s+\S")
# "Chapter 45", "Ch. 45", "Chapter-151: Episode 148", "Ep. 45 - Title": the
# word names the unit, the number names the chapter. Aggregators keep their
# own running index that drifts from it whenever they insert a duplicate or
# a notice, so the title is the number the work actually uses.
_TITLE_UNIT_NUMBER = re.compile(
    r"^\s*(?:[^\w\s]\s*)*(?:(?:vol(?:ume)?\.?)\s*\d+(?:\.\d+)?\s+)?"
    r"(?:chapter|chap\.?|ch\.?|episode|ep\.?)[\s\-:]*"
    r"(\d{1,4}(?:\.\d{1,3})?)(?!\d)",
    re.IGNORECASE,
)
# Some mirrors expose both their own running index and the work's number as
# ``Chapter 280 - 237``.  The trailing number is accepted only for this exact
# numeric shape; a title such as ``Chapter 10 - 2 years later`` is not evidence.
_TITLE_DUAL_NUMBER = re.compile(
    r"^\s*(?:[^\w\s]\s*)*(?:chapter|chap\.?|ch\.?|episode|ep\.?)"
    r"[\s\-:]*\d{1,4}(?:\.\d{1,3})?\s*[-–:]\s*"
    r"(\d{1,4}(?:\.\d{1,3})?)\s*$",
    re.IGNORECASE,
)
# WEBTOON writes the chapter at the end: "Ep. 235 (Season 3 Finale) (ch. 650)".
_TITLE_CHAPTER_SUFFIX = re.compile(r"\(ch\.\s*(\d{1,4}(?:\.\d{1,5})?)\)\s*\S*\s*$")
# Enough of the source has to agree before its numbering is overridden.
_MIN_TITLED = 5
_MIN_SHARE = 0.8
# Within the titled episodes, the numbering must be one coherent run.
_MIN_RUN_SHARE = 0.9


def is_locked_title(value: object) -> bool:
    """True for an episode the source keeps behind its paywall."""

    title = str(value or "")
    return any(marker in title for marker in LOCK_MARKERS)


def is_not_yet_released(release: dict, *, now: datetime | None = None) -> bool:
    """True for an episode nobody can fetch today: the source marks it as
    locked, or it carries a publication date still in the future (Tapas
    lists next week's paid episode with its release date)."""

    if is_locked_title(release.get("title")):
        return True
    raw = str(release.get("publish_at") or "").strip()
    if not raw:
        return False
    try:
        published = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return False
    if published.tzinfo is None:
        published = published.replace(tzinfo=UTC)
    return published > (now or datetime.now(UTC))


#: A listed "chapter" of one or two pages is a placeholder (an announcement,
#: a cover, a "coming soon" card), never the chapter: Guyver 201, 204 and 205
#: exist only as such stubs on one aggregator and counted as three missing.
STUB_MAX_PAGES = 3


def is_stub(release: dict) -> bool:
    """True when the source's own page count says this entry is a placeholder."""

    if release.get("downloaded"):
        return False
    try:
        pages = int(release.get("pages") if release.get("pages") is not None else -1)
    except (TypeError, ValueError):
        return False
    return 0 < pages <= STUB_MAX_PAGES


def is_unpublished(release: dict, *, now: datetime | None = None) -> bool:
    """True for an episode the platform has not published yet.

    The publication date decides: a dated episode is out once its date has
    passed, even when the platform still sells it (Tapas locks every episode
    after the free ones for years). Only an undated locked episode is taken
    as unpublished, because nothing else says when it comes out. A stub of
    a page or two is unpublished too: the source lists it, nobody could
    read it.
    """

    if is_stub(release):
        return True
    raw = str(release.get("publish_at") or "").strip()
    if raw:
        try:
            published = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return is_locked_title(release.get("title"))
        if published.tzinfo is None:
            published = published.replace(tzinfo=UTC)
        return published > (now or datetime.now(UTC))
    return is_locked_title(release.get("title"))


def _decimal(value: object) -> Decimal | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        number = Decimal(raw)
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def title_number(value: object) -> Decimal | None:
    """The chapter number written at the start of an episode title."""

    return _title_number(str(value or ""))


@lru_cache(maxsize=16384)
def _title_number(text: str) -> Decimal | None:
    # Reconciliation and every render read each title several times, and
    # the same titles come back on every pass; Decimals are immutable.
    match = (
        _TITLE_CHAPTER_SUFFIX.search(text)
        or _TITLE_DUAL_NUMBER.match(text)
        or _TITLE_NUMBER.match(text)
        or _TITLE_UNIT_NUMBER.match(text)
    )
    if not match:
        return None
    raw = match.group(1)
    if re.fullmatch(r"\d+\.0+", raw):
        # "Chapter 45.0" is an aggregator's duplicate of chapter 45, and the
        # real chapter 45 follows it under the next index: an extra, never a
        # chapter of its own.
        return None
    return _decimal(raw)


def _initialize_numbering(chapters: list[dict[str, Any]]) -> None:
    """Preserve provider identity before assigning a canonical label."""

    for item in chapters:
        source = item.get("source_chapter", item.get("chapter"))
        item["source_chapter"] = (
            format(source.normalize(), "f") if isinstance(source, Decimal) else source
        )
        canonical = item.get("canonical_chapter", item.get("chapter"))
        item["canonical_chapter"] = canonical
        item.setdefault(
            "numbering_status", "mapped" if canonical is not None else "unmapped"
        )
        item.setdefault("numbering_method", "source_identity")
        item.setdefault("numbering_confidence", 0.6 if canonical is not None else 0.0)
        item.setdefault(
            "numbering_evidence",
            {"source_chapter": item.get("source_chapter")},
        )


def _set_canonical(
    item: dict[str, Any],
    number: Decimal | None,
    *,
    status: str,
    method: str,
    confidence: float,
) -> None:
    canonical = format(number.normalize(), "f") if number is not None else None
    item["canonical_chapter"] = canonical
    # ``chapter`` remains the compatibility view consumed by the rest of the
    # application; the immutable provider label is in ``source_chapter``.
    item["chapter"] = canonical
    item["numbering_status"] = status
    item["numbering_method"] = method
    item["numbering_confidence"] = confidence
    item["numbering_evidence"] = {
        "source_chapter": item.get("source_chapter"),
        "title": str(item.get("title") or ""),
        "canonical_chapter": canonical,
    }


def _longest_increasing_run(numbers: list[Decimal]) -> list[int]:
    """Indexes of the longest strictly increasing subsequence (patience sort).

    A platform posts the odd teaser or bonus out of order; the run is what the
    numbering actually is, the rest are extras.
    """

    tails: list[int] = []
    previous: list[int] = [-1] * len(numbers)
    for index, value in enumerate(numbers):
        low, high = 0, len(tails)
        while low < high:
            middle = (low + high) // 2
            if numbers[tails[middle]] < value:
                low = middle + 1
            else:
                high = middle
        previous[index] = tails[low - 1] if low else -1
        if low == len(tails):
            tails.append(index)
        else:
            tails[low] = index
    result: list[int] = []
    cursor = tails[-1] if tails else -1
    while cursor >= 0:
        result.append(cursor)
        cursor = previous[cursor]
    return list(reversed(result))


_SPECIAL_TITLE = re.compile(
    r"(?i)\b(recap|announcement|editor'?s? note|notice|q ?& ?a|hiatus|"
    r"season \d+ (?:end|finale)? ?(?:announcement|recap)|special announcement|"
    r"end announcement|trailer|teaser|preview|bonus art|wallpaper|contest|"
    r"omake|guidebook)\b"
)


_TITLE_VOLUME = re.compile(
    r"^\s*(?:[^\w\s]\s*)*vol(?:ume)?\.?\s*(\d{1,4})(?!\d)", re.IGNORECASE
)


def title_volume(value: object) -> int | None:
    """The book a source files an episode under, when its title says so.

    Aggregators prefix titles with the tankobon ("Vol.21 Chapter 81"). It is
    the source's own claim about the binding, worth exactly that: evidence
    to weigh against a map, never a reason to delete a file.
    """

    match = _TITLE_VOLUME.match(str(value or ""))
    return int(match.group(1)) if match else None


def is_side_story(value: object, chapter: object) -> bool:
    """A "chapter 0" filed under a later book is a side story, not a prologue.

    A prologue opens the work, so a source that tags it with a volume tags
    it with the first. Nana's "Vol.9 Chapter 0 : Naoki's Story" is an extra
    printed in book nine: not an expected chapter, never something to hunt.
    """

    number = _decimal(chapter)
    if number is None or number != 0:
        return False
    volume = title_volume(value)
    return volume is not None and volume > 1


def numbered_prologue(value: object) -> int | None:
    """A separately numbered prelude, not an ordinary chapter with this title."""
    match = re.match(r"^\s*prologue\s+(\d{1,4})\b", str(value or ""), re.I)
    return int(match.group(1)) if match else None


def is_special_title(value: object) -> bool:
    """An episode that is not a chapter of the work: a recap, a notice."""

    return _is_special_title(str(value or ""))


@lru_cache(maxsize=16384)
def _is_special_title(text: str) -> bool:
    # A word such as "Announcement" or "Contest" can be the legitimate title
    # of an explicitly numbered chapter.  In that case the number is stronger
    # evidence; cross-source reconciliation can still leave it for review.
    return _title_number(text) is None and bool(
        _SPECIAL_TITLE.search(text) or numbered_prologue(text) is not None
    )


def renumber_by_rank(chapters: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Number the real episodes in order when titles carry no numbers.

    Tapas titles are just names ("Opportunity", "IRL Meetup"), so nothing in
    them says which chapter they are; the episode index counts recaps and
    announcements too. Once those are set aside, the remaining episodes are
    the chapters, in the order the platform published them.
    """

    _initialize_numbering(chapters)
    ordered = sorted(
        (
            (number, item)
            for item in chapters
            if (number := _decimal(item.get("chapter"))) is not None
        ),
        key=lambda entry: entry[0],
    )
    specials = [
        item for _number, item in ordered if is_special_title(item.get("title"))
    ]
    if not specials:
        return chapters
    rank = 0
    for number, item in ordered:
        if item in specials:
            _set_canonical(
                item,
                None,
                status="unmapped",
                method="special_title",
                confidence=1.0,
            )
            continue
        if number != number.to_integral_value():
            continue  # a side story keeps its decimal: it is an extra already
        rank += 1
        _set_canonical(
            item,
            Decimal(rank),
            status="provisional",
            method="rank_after_specials",
            confidence=0.55,
        )
    return chapters


def _has_restarted_sequences(chapters: list[dict[str, Any]]) -> bool:
    """Detect long, repeated 1..N runs before sorting destroys source order.

    Different named scanlation groups and explicit volume numbers need their
    own edition reconciliation. Interleaved alternatives and a stray duplicate
    do not establish a restarted sequence.
    """
    if len(chapters) < 10 or any(item.get("volume") for item in chapters):
        return False
    groups = {tuple(item.get("groups") or ()) for item in chapters}
    if len(groups) != 1:
        return False
    numbers = [_decimal(item.get("chapter")) for item in chapters]
    if any(n is None or n < 1 or n != n.to_integral_value() for n in numbers):
        return False
    for sequence in (numbers, list(reversed(numbers))):
        runs: list[int] = []
        previous = 0
        for number in sequence:
            if number == 1:
                if previous and previous < 5:
                    break
                runs.append(1)
            elif not runs or number != previous + 1:
                break
            else:
                runs[-1] += 1
            previous = number
        else:
            if len(runs) >= 2 and min(runs) >= 5:
                return True
    return False


def renumber_from_titles(chapters: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rewrite ``chapter`` from the title when one source counts episodes.

    The override needs the source to agree with itself: most episodes carry a
    number and those numbers climb with the episode index. Episodes outside
    that run - specials, announcements, teasers - keep no chapter number at
    all, so they cannot collide with a real chapter.
    """

    _initialize_numbering(chapters)
    if _has_restarted_sequences(chapters):
        # The same printed numbers can belong to different arcs. Source order
        # alone cannot distinguish those arcs from separately uploaded editions;
        # neither deduplication by number nor invented offsets are safe.
        for item in chapters:
            _set_canonical(
                item,
                None,
                status="ambiguous",
                method="restarted_sequences",
                confidence=0.0,
            )
        return chapters
    ordered = [
        (number, title_number(item.get("title")), item)
        for item in chapters
        if (number := _decimal(item.get("chapter"))) is not None
    ]
    titled = sorted(
        (entry for entry in ordered if entry[1] is not None),
        key=lambda entry: entry[0],
    )
    if len(titled) < _MIN_TITLED or len(titled) < _MIN_SHARE * len(ordered):
        # No numbers in the titles: the order of the real episodes decides.
        return renumber_by_rank(chapters)
    # The chapters proper are the whole numbers; a side story ("Chapter 9.1")
    # is filed wherever the source put it and must not break the run.
    whole = [entry for entry in titled if entry[1] == entry[1].to_integral_value()]
    parts = [entry for entry in titled if entry[1] != entry[1].to_integral_value()]
    run = _longest_increasing_run([entry[1] for entry in whole])
    if len(whole) < _MIN_TITLED or len(run) < _MIN_RUN_SHARE * len(whole):
        return chapters
    numbered = [whole[index] for index in run]
    if all(source == number for source, number, _item in numbered):
        for _source_number, _number, item in ordered:
            if is_special_title(item.get("title")):
                _set_canonical(
                    item,
                    None,
                    status="unmapped",
                    method="special_title",
                    confidence=1.0,
                )
        return chapters
    seen_parts: set[Decimal] = set()
    for _source, number, item in parts:
        if number in seen_parts:
            continue
        seen_parts.add(number)
        numbered.append((_source, number, item))
    renumbered = {id(item) for _source, _number, item in numbered}
    for _source_number, number, item in numbered:
        _set_canonical(
            item,
            number,
            status="mapped",
            method="title_sequence",
            confidence=0.95,
        )
    for _source_number, _number, item in ordered:
        if id(item) not in renumbered:
            # An extra keeps its title but no number: it is not a chapter of
            # the work, and its episode index would collide with a real one.
            _set_canonical(
                item,
                None,
                status="unmapped",
                method="title_sequence_outlier",
                confidence=0.0,
            )
    return chapters


__all__ = [
    "LOCK_MARKERS",
    "is_locked_title",
    "is_side_story",
    "is_special_title",
    "renumber_by_rank",
    "renumber_from_titles",
    "title_number",
    "title_volume",
]
