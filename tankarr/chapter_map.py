"""Chapter↔volume map: which chapters a volume covers, and what that implies.

Confirmed boundaries turn "I own volume 12" into "chapters 35-37 are not
missing". Catalogue release feeds and provider tags are stored as suggestions;
only operator boundaries and explicitly submitted book readings are used by
the library. ``edition_entries`` and ``resolved_map`` enforce that policy.

Two kinds of entries exist:

* **exact** source entries name one volume and its chapters (``v20 c58``);
* **coarse** entries span several volumes (``v1-20 c1-60``) and remain hints;
  even owning the whole span does not prove any chapter is a duplicate.

An exact source claim alone is not confirmation. Statistical layouts remain
outside the stored map and never establish coverage or duplicate files.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

RANGE_PATTERN = re.compile(r"^\s*0*(\d+(?:\.\d+)?)\s*[-–~]\s*0*(\d+(?:\.\d+)?)\s*$")
NUMBER_PATTERN = re.compile(r"^\s*0*(\d+(?:\.\d+)?)\s*$")
# Quality/edition suffixes groups append to numbers ("73 LQ", "73-75 HQ",
# "101-102 (end)", "12 v2"). They never change which chapters are covered.
DECORATION_PATTERN = re.compile(
    r"\s*(?:\((?:end|fin|final|complete|extra|omake|v\d+)[^)]*\)|"
    r"\b(?:lq|hq|mq|end|fin|final|fixed|v\d+)\b)\s*",
    re.IGNORECASE,
)
MAX_SPAN = 500


@dataclass(frozen=True)
class MapEntry:
    volumes: tuple[str, ...]
    chapters: tuple[str, ...]
    exact: bool
    source: str = "mangaupdates"
    release_date: str | None = None


@dataclass
class Coverage:
    """Coverage verdict for one chapter given the volumes that are owned."""

    volume: str | None = None
    exact: bool = False
    unmapped: bool = False
    candidates: tuple[str, ...] = field(default_factory=tuple)

    @property
    def covered(self) -> bool:
        return self.volume is not None


def edition_entries(
    entries: Iterable[MapEntry], metadata: dict[str, Any] | None
) -> list[MapEntry]:
    """Only explicitly supplied boundaries describe the managed edition.

    Catalogue and provider volume tags remain suggestions, even when dense
    and ordered. ``ocr`` is the explicit book-reading API, not an automatic
    catalogue source; the operator can still override those supplied ranges.
    """
    return [entry for entry in entries if entry.source in {"operator", "ocr"}]


def canonical_label(value: object) -> str | None:
    raw = str(value if value is not None else "").strip()
    match = NUMBER_PATTERN.fullmatch(raw)
    if not match:
        return None
    try:
        number = Decimal(match.group(1))
    except InvalidOperation:
        return None
    text = format(number, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text if text else None


def entries_from_boundaries(
    boundaries: Iterable[dict[str, Any]],
    *,
    last_chapter: object,
    known_chapters: Iterable[object] = (),
) -> list[MapEntry]:
    """Expand fixed operator boundaries without inventing fractional chapters.

    Every book owns [first, next_first); the last book ends at the inclusive
    last known canonical chapter supplied by the caller at save time. Stored
    entries are a snapshot and do not grow when a catalogue refreshes.
    """

    def decimal(value: object, name: str, minimum: int) -> Decimal:
        try:
            number = Decimal(str(value))
        except (InvalidOperation, ValueError):
            raise ValueError(f"{name} must be a finite number") from None
        if not number.is_finite() or number < minimum:
            raise ValueError(f"{name} must be finite and at least {minimum}")
        # Avoid pathological exponent expansion or output larger than a whole
        # supported canonical sequence. Precision itself is never rounded.
        if number.adjusted() > 8 or number.as_tuple().exponent < -32:
            raise ValueError(f"{name} exceeds the supported numbering precision")
        return number

    rows: list[tuple[Decimal, Decimal]] = []
    for boundary in boundaries:
        volume = decimal(boundary.get("volume"), "Volume", 1)
        first = decimal(boundary.get("first_chapter"), "First chapter", 0)
        if rows and (volume <= rows[-1][0] or first <= rows[-1][1]):
            raise ValueError("Volumes and first chapters must be strictly increasing")
        rows.append((volume, first))
        if len(rows) > 10_000:
            raise ValueError("At most 10000 book boundaries can be stored")
    if not rows:
        return []
    last = decimal(last_chapter, "Last known chapter", 0)
    if rows[-1][1] > last:
        raise ValueError("A book boundary is beyond the last known chapter")
    known: set[Decimal] = {first for _volume, first in rows}
    known.add(last)
    for label in known_chapters:
        try:
            number = decimal(label, "Known chapter", 0)
        except ValueError:
            continue
        if rows[0][1] <= number <= last:
            known.add(number)
    first_integer = int(rows[0][1].to_integral_value(rounding="ROUND_CEILING"))
    last_integer = int(last.to_integral_value(rounding="ROUND_FLOOR"))
    if last_integer - first_integer >= 10_000 or len(known) > 10_000:
        raise ValueError("The map may contain at most 10000 canonical chapters")
    known.update(Decimal(number) for number in range(first_integer, last_integer + 1))
    if len(known) > 10_000:
        raise ValueError("The map may contain at most 10000 canonical chapters")

    def label(number: Decimal) -> str:
        return canonical_label(format(number, "f")) or "0"

    groups: list[list[str]] = [[] for _row in rows]
    index = 0
    for number in sorted(known):
        while index + 1 < len(rows) and number >= rows[index + 1][1]:
            index += 1
        groups[index].append(label(number))
    return [
        MapEntry((label(volume),), tuple(groups[index]), True, source="operator")
        for index, (volume, _first) in enumerate(rows)
    ]


def resolved_map(entries: Iterable[MapEntry], *, raw: bool = False) -> list[MapEntry]:
    """The stored rows as every reader must see them.

    Raw reads retain catalogue suggestions. Operational reads use only
    explicitly supplied boundaries, with operator boundaries taking priority.
    Dense or ordered catalogue logs do not establish edition membership.
    """

    entries = downgrade_sparse_maps(list(entries))
    return sorted(
        entries if raw else effective_entries(edition_entries(entries, None)),
        key=lambda entry: (not entry.exact, entry.volumes, entry.chapters),
    )


def effective_entries(entries: Iterable[MapEntry]) -> list[MapEntry]:
    """Resolve operator ownership without changing any source's stored rows."""
    items = [
        MapEntry(
            tuple(canonical_label(volume) or volume for volume in entry.volumes),
            tuple(
                canonical_label(chapter) or chapter
                for chapter in entry.chapters
                if str(chapter).strip()
            ),
            entry.exact,
            source=entry.source,
            release_date=entry.release_date,
        )
        for entry in entries
        if entry.source != "estimate"
    ]
    # Two sources own what they describe: the operator first, then a reading
    # of the books themselves ("ocr", pages read by an external reader). A
    # catalogue's entry for a book they describe gives way to them.
    for authority in ("operator", "ocr"):
        items = _resolve_authority(items, authority)
    return items


def _resolve_authority(items: list[MapEntry], authority: str) -> list[MapEntry]:
    operator = [
        entry
        for entry in items
        if entry.source == authority and entry.exact and len(entry.volumes) == 1
    ]
    if not operator:
        return items
    volumes = {volume for entry in operator for volume in entry.volumes}
    chapters = {chapter for entry in operator for chapter in entry.chapters}
    resolved = list(operator)
    for entry in items:
        if entry.source == authority and entry.exact and len(entry.volumes) == 1:
            continue
        if entry.source == "operator" and authority == "ocr":
            resolved.append(entry)  # the operator's word is never trimmed by a reading
            continue
        remaining_volumes = tuple(
            volume for volume in entry.volumes if volume not in volumes
        )
        remaining_chapters = tuple(
            chapter for chapter in entry.chapters if chapter not in chapters
        )
        if remaining_volumes and remaining_chapters:
            resolved.append(
                MapEntry(
                    remaining_volumes,
                    remaining_chapters,
                    entry.exact,
                    source=entry.source,
                    release_date=entry.release_date,
                )
            )
    return resolved


def expand_numbers(value: object) -> list[str]:
    """Expand ``"12"``, ``"12.5"`` or ``"99-100"`` into canonical labels.

    Ranges expand only across integers; a decimal endpoint keeps the range
    literal because ``12.1-12.4`` is a split chapter, not a span.
    """

    raw = DECORATION_PATTERN.sub(" ", str(value if value is not None else "")).strip()
    raw = " ".join(raw.split())
    if not raw:
        return []
    single = canonical_label(raw)
    if single is not None:
        return [single]
    match = RANGE_PATTERN.fullmatch(raw)
    if not match:
        return []
    start, end = match.group(1), match.group(2)
    if "." in start or "." in end:
        return [
            label for label in (canonical_label(start), canonical_label(end)) if label
        ]
    first, last = int(start), int(end)
    if first > last or last - first > MAX_SPAN:
        return []
    return [str(number) for number in range(first, last + 1)]


def entries_from_releases(
    releases: Iterable[dict[str, Any]], *, source: str = "mangaupdates"
) -> list[MapEntry]:
    """Build map entries from release rows carrying ``volume`` and ``chapter``."""

    output: list[MapEntry] = []
    seen: set[tuple[tuple[str, ...], tuple[str, ...]]] = set()
    for release in releases:
        volumes = tuple(expand_numbers(release.get("volume")))
        chapters = tuple(expand_numbers(release.get("chapter")))
        if not volumes or not chapters:
            continue
        key = (volumes, chapters)
        if key in seen:
            continue
        seen.add(key)
        output.append(
            MapEntry(
                volumes=volumes,
                chapters=chapters,
                exact=len(volumes) == 1,
                source=source,
                release_date=str(release.get("release_date") or "") or None,
            )
        )
    return output


def _misplaced_pairs(pairs: list[tuple[Decimal, int]]) -> list[tuple[Decimal, int]]:
    """The pairs outside the longest run of non-decreasing books.

    A single stray entry breaks the order in one direction or the other:
    "c74 v5" among the book-7 chapters, or "c72 v8" before six more book-7
    chapters. Counting the pairs that lag behind a running maximum blames the
    six good ones in the second case; the complement of the longest ordered
    subsequence blames the stray one in both.
    """

    import bisect

    tails: list[int] = []  # smallest ending book of a run of each length
    tail_index: list[int] = []
    previous: list[int] = [-1] * len(pairs)
    for index, (_number, volume) in enumerate(pairs):
        position = bisect.bisect_right(tails, volume)
        if position == len(tails):
            tails.append(volume)
            tail_index.append(index)
        else:
            tails[position] = volume
            tail_index[position] = index
        previous[index] = tail_index[position - 1] if position else -1
    kept: set[int] = set()
    cursor = tail_index[-1] if tail_index else -1
    while cursor != -1:
        kept.add(cursor)
        cursor = previous[cursor]
    return [pair for index, pair in enumerate(pairs) if index not in kept]


def downgrade_sparse_maps(entries: Iterable[MapEntry]) -> list[MapEntry]:
    """A release log is not a structure: keep only dense, ordered maps exact.

    MangaUpdates' feed for Blade of the Phantom Master said "v3 c6", "v5
    c8-10", "v8 c14-15", "v10 c17": a handful of group labels, several of
    them wrong, and Tankarr called chapter 17 "in v10". Per source, the
    exact entries stay exact only when the volumes they name are in step
    with the chapters (a later chapter never sits in an earlier book) and
    the span they describe is filled: at most a third of the books in it
    without chapters, and at least half of the chapters in it mapped.
    Anything else is a hint that may still guide a search, never a claim
    that a chapter file is a duplicate or that a book is covered.
    """

    items = list(entries)
    demoted_entries: set[int] = set()
    by_source: dict[str, list[MapEntry]] = {}
    for entry in items:
        by_source.setdefault(entry.source, []).append(entry)
    weak_sources: set[str] = set()
    for source, group in by_source.items():
        if source in ("operator", "ocr", "estimate"):
            continue
        pairs: list[tuple[Decimal, int]] = []
        for entry in group:
            if not entry.exact or len(entry.volumes) != 1:
                continue
            try:
                volume = int(str(entry.volumes[0]).strip())
            except ValueError:
                continue
            for label in entry.chapters:
                try:
                    number = Decimal(str(label).strip())
                except InvalidOperation:
                    continue
                if not number.is_finite():
                    continue
                pairs.append((number, volume))
        if len(pairs) < 2:
            continue
        pairs.sort()
        # One mistyped release ("c74 v7" after "c72 v8") must not throw away
        # ninety good entries: a few inversions are dropped from the map,
        # many inversions mean the log has no order at all.
        inverted = {number for number, _volume in _misplaced_pairs(pairs)}
        ordered = len(inverted) <= max(1, len(pairs) // 20)
        if ordered and inverted:
            # Only an entry made entirely of misplaced chapters is the typo;
            # the book that legitimately holds the chapter keeps its claim.
            for entry in group:
                numbers = [_number_or_none(label) for label in entry.chapters]
                numbers = [number for number in numbers if number is not None]
                if entry.exact and numbers and all(n in inverted for n in numbers):
                    demoted_entries.add(id(entry))
        volumes = {volume for _number, volume in pairs}
        span_volumes = range(min(volumes), max(volumes) + 1)
        empty_books = sum(1 for volume in span_volumes if volume not in volumes)
        whole = {int(number) for number, _volume in pairs if number == int(number)}
        chapter_span = max(whole) - min(whole) + 1 if whole else 0
        filled = len(whole) / chapter_span if chapter_span else 1.0
        sparse = empty_books * 3 > len(span_volumes) or filled < 0.5
        if not ordered or sparse:
            weak_sources.add(source)
    if not weak_sources and not demoted_entries:
        return items
    return [
        MapEntry(
            volumes=entry.volumes,
            chapters=entry.chapters,
            exact=False
            if entry.source in weak_sources or id(entry) in demoted_entries
            else entry.exact,
            source=entry.source,
            release_date=entry.release_date,
        )
        for entry in items
    ]


def _number_or_none(label: object) -> Decimal | None:
    try:
        value = Decimal(str(label).strip())
        return value if value.is_finite() else None
    except InvalidOperation:
        return None


def chapters_by_volume(entries: Iterable[MapEntry]) -> dict[str, set[str]]:
    """Exact chapter sets per volume, ignoring coarse multi-volume spans.

    Retain conflicting claims so consumers can detect ambiguity, including
    contradictions inside one catalogue. Choosing the later volume would invent
    a boundary; dropping both claims would let an assembler build a partial book.
    """

    result: dict[str, set[str]] = {}
    for entry in effective_entries(entries):
        if entry.exact and len(entry.volumes) == 1:
            volume = entry.volumes[0]
            result.setdefault(volume, set()).update(entry.chapters)
    return {volume: labels for volume, labels in result.items() if labels}


def coverage_for_chapter(
    chapter: object, entries: Iterable[MapEntry], owned_volumes: set[str]
) -> Coverage:
    """Decide whether one chapter is covered by the volumes that are owned."""

    label = canonical_label(chapter)
    if label is None or not owned_volumes:
        return Coverage()
    entry_list = effective_entries(entries)
    exact_volumes = tuple(
        dict.fromkeys(
            entry.volumes[0]
            for entry in entry_list
            if entry.exact and len(entry.volumes) == 1 and label in entry.chapters
        )
    )
    if len(exact_volumes) > 1:
        return Coverage(
            unmapped=bool(set(exact_volumes) & owned_volumes), candidates=exact_volumes
        )
    for volume in exact_volumes:
        if volume in owned_volumes:
            return Coverage(volume=volume, exact=True, candidates=exact_volumes)
    if exact_volumes:
        # The map knows the volume and it is not owned: genuinely missing.
        return Coverage(candidates=exact_volumes)
    for entry in entry_list:
        if entry.exact or label not in entry.chapters:
            continue
        if set(entry.volumes) & owned_volumes:
            # Part of a coarse span is owned: cannot tell which volume holds
            # this chapter, so it must neither be missing nor downloaded twice.
            return Coverage(unmapped=True, candidates=entry.volumes)
    return Coverage()


def coverage_by_chapter(
    entries: Iterable[MapEntry], owned_volumes: set[str]
) -> dict[str, Coverage]:
    """Resolve a whole series once, without rebuilding its map per slot."""
    if not owned_volumes:
        return {}
    entry_list = effective_entries(entries)
    candidates: dict[str, list[str]] = {}
    for entry in entry_list:
        if entry.exact and len(entry.volumes) == 1:
            for chapter in entry.chapters:
                candidates.setdefault(chapter, []).append(entry.volumes[0])
    result = {}
    for chapter, volumes in candidates.items():
        volumes = list(dict.fromkeys(volumes))
        if len(volumes) > 1:
            result[chapter] = Coverage(
                unmapped=bool(set(volumes) & owned_volumes), candidates=tuple(volumes)
            )
            continue
        owned = next((volume for volume in volumes if volume in owned_volumes), None)
        result[chapter] = Coverage(
            volume=owned, exact=owned is not None, candidates=tuple(volumes)
        )
    for entry in entry_list:
        if entry.exact or not (set(entry.volumes) & owned_volumes):
            continue
        for chapter in entry.chapters:
            result.setdefault(
                chapter, Coverage(unmapped=True, candidates=entry.volumes)
            )
    return result


def integer_chapter_total(entries: Iterable[MapEntry]) -> int | None:
    """Highest integer chapter the map knows, when the map ends the work."""

    highest = 0
    for entry in entries:
        for label in entry.chapters:
            try:
                highest = max(highest, int(Decimal(label)))
            except (InvalidOperation, ValueError):
                continue
    return highest or None
