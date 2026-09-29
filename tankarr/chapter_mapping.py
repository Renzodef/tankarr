from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable
from decimal import Decimal
from functools import lru_cache
from typing import Any

from tankarr.catalogue_consensus import managed_volume_count
from tankarr.chapter_map import (
    Coverage,
    InvalidOperation,
    MapEntry,
    chapters_by_volume,
    coverage_by_chapter,
    effective_entries,
)
from tankarr.series_summary import publication_summary
from tankarr.series_unit import (
    acquisition_policy,
    is_volume_release,
    select_releases,
)
from tankarr.source_numbering import is_special_title, is_unpublished, numbered_prologue
from tankarr.source_ranking import (
    beyond_frontier,
    official_frontier,
    official_hosts,
    release_host,
)


def owned_additional_content(releases, slots) -> dict[str, int]:
    """Describe owned side content without changing the reference numbering.

    Catalogue-counted prologues/decimals already in expected slots must not
    be counted twice. Deduplicate alternate releases of the same prologue.
    """
    counted = {
        str(release.get("id"))
        for slot in slots
        if slot.get("expected")
        for release in slot.get("releases", [])
    }
    specials = {
        str(release.get("id")): slot["key"]
        for slot in slots
        if slot.get("special") and not slot.get("expected")
        for release in slot.get("releases", [])
    }
    prologues, extras = set(), set()
    for release in releases:
        identifier = str(release.get("id"))
        if (
            not release.get("downloaded")
            or is_volume_release(release)
            or identifier in counted
        ):
            continue
        title = " ".join(str(release.get("title") or "").casefold().split())
        prologue = numbered_prologue(title)
        label = canonical_number(release.get("chapter"))
        if prologue is not None:
            prologues.add(prologue)
        elif identifier in specials:
            extras.add(("slot", specials[identifier]))
        elif is_special_title(title) or (label is not None and "." in label):
            extras.add((str(release.get("volume") or ""), label or title))
    return {"prologues": len(prologues), "extras": len(extras)}


_DOCUMENTED_EXTRAS = re.compile(
    r"\b(?:includes?|contains?)\s+"
    r"(\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s+"
    r"(?:extra|bonus)\s+chapters?\b",
    re.IGNORECASE,
)
_EXTRA_NUMBER_WORDS = {
    word: number
    for number, word in enumerate(
        (
            "zero",
            "one",
            "two",
            "three",
            "four",
            "five",
            "six",
            "seven",
            "eight",
            "nine",
            "ten",
        )
    )
}
_DOCUMENTED_NUMBERED_EXTRAS = re.compile(
    r"\b(?:includes?|contains?)\s+(?:(?:extra|bonus|special)\s+)?"
    r"chapters?\s+(\d+\.\d+(?:\s*(?:,|&|and)\s*\d+\.\d+)*)\b",
    re.IGNORECASE,
)
_VOLUME_TITLE = re.compile(r"\bvol(?:ume)?\.?\s*\d+\b", re.IGNORECASE)
_NON_CHAPTER_TITLE = re.compile(
    r"\b(?:hiatus|notice|biography|preview|announcement)\b", re.IGNORECASE
)
_NAMED_EXTRA = re.compile(r"(?:extra|special)\s*(?:no\.?\s*)?\d+\Z", re.IGNORECASE)
_NAMED_EXTRA_TITLE = re.compile(r"\b(?:extra|special|bonus)\b", re.IGNORECASE)


def documented_extra_count(
    manga: dict[str, Any], metadata: dict[str, Any]
) -> int | None:
    """Use an explicit bonus count or a documented list of decimal chapters."""

    labels = documented_extra_labels(manga, metadata)
    if labels is None and any(
        _DOCUMENTED_NUMBERED_EXTRAS.search(str(description or ""))
        for description in (manga.get("description"), metadata.get("description"))
    ):
        return None
    counts = {
        int(match.group(1))
        if match.group(1).isdigit()
        else _EXTRA_NUMBER_WORDS[match.group(1).casefold()]
        for description in (manga.get("description"), metadata.get("description"))
        for match in _DOCUMENTED_EXTRAS.finditer(str(description or ""))
    }
    if labels is not None:
        counts.add(len(labels))
    return next(iter(counts)) if len(counts) == 1 else None


def documented_extra_labels(
    manga: dict[str, Any], metadata: dict[str, Any]
) -> frozenset[str] | None:
    """Read explicitly listed decimal extras, rejecting conflicting lists."""

    lists = {
        frozenset(
            canonical_number(label) for label in re.findall(r"\d+\.\d+", match.group(1))
        )
        for description in (manga.get("description"), metadata.get("description"))
        for match in _DOCUMENTED_NUMBERED_EXTRAS.finditer(str(description or ""))
    }
    return next(iter(lists)) if len(lists) == 1 else None


def owned_documented_extras(
    releases: list[dict[str, Any]],
    counted_ids: set[str],
    documented_labels: frozenset[str] | None = None,
) -> int:
    """Count distinct imported book extras with explicit volume identity.

    A decimal alone may be a split main chapter, a notice, or a source's
    numbering artefact. Only a separately imported volume-labelled chapter
    that the canonical slots did not consume can satisfy an extra count.
    """

    owned: set[tuple[str, str]] = set()
    for release in releases:
        if not release.get("downloaded") or is_volume_release(release):
            continue
        chapter = canonical_number(release.get("chapter"))
        named_extra = bool(chapter and _NAMED_EXTRA.fullmatch(chapter))
        if (
            str(release.get("id")) in counted_ids
            and chapter not in (documented_labels or ())
            and not named_extra
        ):
            continue
        volume = canonical_number(release.get("volume"))
        title = str(release.get("title") or "")
        if not chapter or not volume or ("." not in chapter and not named_extra):
            continue
        if (
            not _VOLUME_TITLE.search(title)
            or _NON_CHAPTER_TITLE.search(title)
            or (named_extra and not _NAMED_EXTRA_TITLE.search(title))
        ):
            continue
        owned.add((volume, chapter))
    return len(owned)


MAX_CANONICAL_SEQUENCE = 10_000
SUSPECT_BOOK_PAGES = 600


def canonical_number(value: object) -> str | None:
    """Return one stable numeric label without changing non-numeric labels."""

    raw = (
        value.strip()
        if isinstance(value, str)
        else str(value if value is not None else "").strip()
    )
    if not raw:
        return None
    return _canonical_label(raw)


@lru_cache(maxsize=8192)
def _canonical_label(raw: str) -> str:
    # The same few hundred labels come back for every series and every
    # render; parsing a Decimal each time was a measurable share of a
    # Library rebuild.
    try:
        number = Decimal(raw)
    except (InvalidOperation, ValueError):
        return raw.casefold()
    if not number.is_finite() or number < 0:
        return raw.casefold()
    normalized = format(number, "f")
    if "." in normalized:
        normalized = normalized.rstrip("0").rstrip(".")
    return normalized or "0"


def _decimal(value: object) -> Decimal | None:
    normalized = canonical_number(value)
    if normalized is None:
        return None
    try:
        number = Decimal(normalized)
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() and number >= 0 else None


def _number_sort_key(value: object) -> tuple[int, Decimal | str]:
    try:
        return (0, Decimal(str(value)))
    except (InvalidOperation, ValueError):
        return (1, str(value))


def _positive_integer(value: object) -> int | None:
    number = _decimal(value)
    if number is None or number <= 0 or number != number.to_integral_value():
        return None
    return int(number)


def suspect_volume_reasons(
    releases: Iterable[dict[str, Any]],
    *,
    page_limit: int = SUSPECT_BOOK_PAGES,
    chapter_map: Iterable[MapEntry] | None = None,
) -> dict[str, str]:
    """Owned books that cannot prove coverage of separately acquired chapters.

    Read only release metadata, so every index consumer has the same protection
    without a service call or filesystem scan. Assembly provenance will need
    its own verified exception when assembled releases are supported.
    """
    from tankarr.assembly_provenance import proven_assembly

    current_map = chapters_by_volume(chapter_map) if chapter_map is not None else None
    suspects = {}
    for release in releases:
        if not release.get("downloaded") or not is_volume_release(release):
            continue
        volume = canonical_number(release.get("volume"))
        if volume is None:
            continue
        if release.get("provider") == "translated":
            suspects[volume] = "Machine translation does not retire human chapters"
            continue
        proof = proven_assembly(release)
        if proof is not None:
            if current_map is not None and set(current_map.get(volume, ())) == set(
                proof["chapters"]
            ):
                continue
            suspects[volume] = (
                "Saved book boundaries differ from the assembled chapters"
            )
            continue
        try:
            pages = int(release.get("pages") or 0)
        except (TypeError, ValueError, OverflowError):
            pages = 0
        if pages > page_limit:
            suspects[volume] = f"{pages} pages: not verified as one book"
        elif str(release.get("provider") or "").casefold() == "manual":
            suspects.setdefault(volume, "Imported by hand")
    # Books of one edition are cut to a size: VIZ's Master Keaton runs 300-360
    # pages a book. A book far off that size (434 and 218 pages built by hand
    # from raws) is another edition or a wrong split, and says so, whatever
    # its name claims.
    sizes = {}
    providers: dict[str, str] = {}
    for release in releases:
        if not release.get("downloaded") or not is_volume_release(release):
            continue
        volume = canonical_number(release.get("volume"))
        try:
            pages = int(release.get("pages") or 0)
        except (TypeError, ValueError, OverflowError):
            pages = 0
        if volume is not None and pages > 0:
            sizes[volume] = pages
            providers[volume] = str(release.get("provider") or "")
    reference = sorted(
        pages for volume, pages in sizes.items() if volume not in suspects
    )
    if len(reference) >= 3:
        median = reference[len(reference) // 2]
        for volume, pages in sizes.items():
            # A book built by hand is judged strictly: off the edition's size
            # it is another cut. A book a source delivered as the edition's
            # own is judged by what threatens coverage: a half (a wrong split)
            # or a double (an omnibus). VIZ's Nana runs 175-278 pages a book,
            # bonus manga included, and every one of them is the edition.
            if median < 60:
                continue
            by_hand = str(providers.get(volume) or "").casefold() == "manual"
            off = abs(pages - median) > 0.3 * median
            if (
                by_hand
                and off
                or (not by_hand and (pages < 0.7 * median or pages > 1.6 * median))
            ):
                suspects[volume] = (
                    f"{pages} pages against an edition of about {median}: "
                    "another edition or a wrong split"
                )
    return suspects


def _release_sort_key(release: dict[str, Any]) -> tuple[int, int, str, str]:
    return (
        int(release.get("version") or 0),
        int(release.get("pages") or 0),
        str(release.get("publish_at") or ""),
        str(release.get("id") or ""),
    )


def _volume_for_releases(releases: Iterable[dict[str, Any]]) -> str | None:
    volumes = {
        normalized
        for release in releases
        if (normalized := canonical_number(release.get("volume"))) is not None
    }
    return next(iter(volumes)) if len(volumes) == 1 else None


def volume_scoped_numbering(releases: list[dict[str, Any]]) -> bool:
    """Chapter identity is the chapter number alone.

    Sources write different volume labels next to the same chapter ("Vol.10
    Ch.100" on one site, "Vol.11 Ch.100" on another, no volume on a third);
    scoping identity by that label split one chapter into several Wanted
    rows. Whole-volume books are a separate unit and never mix with
    chapters, so there is nothing left for a volume scope to disambiguate.
    """

    del releases  # kept for call-site compatibility
    return False


def logical_release_key(
    release: dict[str, Any], *, volume_scoped: bool
) -> tuple[str, str]:
    """Return the canonical identity shared by alternative provider releases."""

    chapter = canonical_number(release.get("chapter"))
    if chapter is None:
        return ("id", str(release.get("id") or ""))
    if volume_scoped:
        return (canonical_number(release.get("volume")) or "", chapter)
    return ("chapter", chapter)


def _release_source(release: dict[str, Any]) -> str:
    return str(release.get("source_name") or release.get("provider") or "")


def corroborated_decimal_chapters(
    releases: Iterable[dict[str, Any]], minimum_sources: int = 2
) -> set[str]:
    """Decimal labels at least two sources list under the same number.

    Split parts (N.1..N.k) are not chapters and are left to the split
    rules; a lone omake is an extra. A "5.5" that seven sources number the
    same way is the work's own chapter, whatever the catalogues call it.
    """

    per_source: dict[str, set[str]] = {}
    for release in releases:
        label = canonical_number(release.get("chapter"))
        if label is None:
            continue
        number = _decimal(label)
        if number is None or number <= 0 or number == number.to_integral_value():
            continue
        per_source.setdefault(_release_source(release), set()).add(label)
    by_label: dict[str, set[str]] = {}
    for source, labels in per_source.items():
        for label in labels:
            number = _decimal(label)
            if number is None:
                continue
            fraction = number - number.to_integral_value(rounding="ROUND_FLOOR")
            tenth = int(fraction * 10) if fraction * 10 == int(fraction * 10) else None
            # Only ".5" reads as an omake number, and not when the same
            # source also lists ".4" or ".6": that is a chapter cut in parts.
            if tenth != 5:
                continue
            base = number.to_integral_value(rounding="ROUND_FLOOR")
            neighbours = {
                format(base + Decimal("0.4"), "f"),
                format(base + Decimal("0.6"), "f"),
            }
            if neighbours & labels:
                continue
            by_label.setdefault(label, set()).add(source)
    return {
        label for label, sources in by_label.items() if len(sources) >= minimum_sources
    }


def counted_chapter_total(
    manga: dict[str, Any], metadata: dict[str, Any] | None
) -> int | None:
    """The catalogue's chapter count, when counting means something.

    A running work's total is a moving figure and no source's list can be
    measured against it; a finished work's is the size of the work.
    """

    manual_unit = str(manga.get("expected_count_unit_override") or "").casefold()
    manual_total = _positive_integer(manga.get("expected_count_override"))
    if manual_unit == "chapter" and manual_total is not None:
        return manual_total
    metadata = metadata or {}
    if publication_summary(manga, metadata)["status"] != "ended":
        return None
    return _positive_integer(metadata.get("chapter_count"))


def count_aligned_tail_labels(
    releases: Iterable[dict[str, Any]],
    chapter_total: int | None,
    minimum_sources: int = 2,
) -> frozenset[str]:
    """The decimals a whole list ends with when the list *is* the work.

    Sources that renumber the last chapters still carry them all: SPRIGGAN
    has 62 chapters and the sources that hold it whole end "…59, 60, 60.5,
    60.6" - 62 entries, the catalogue's count exactly. The count is the
    evidence: those trailing decimals are the work's last chapters, not
    extras, and the numbering ends at 60 instead of leaving 61 and 62
    missing forever. The list must be a whole work (every number up to its
    last, decimals only past it), lists that disagree settle nothing, and
    the chapters it ends with must be offered by ``minimum_sources``.
    """

    rows = list(releases)
    if not chapter_total:
        return frozenset()
    per_source: dict[str, set[str]] = defaultdict(set)
    for release in rows:
        if is_volume_release(release):
            continue
        label = canonical_number(release.get("chapter"))
        number = _decimal(label) if label is not None else None
        if label is None or number is None or number <= 0:
            continue
        per_source[_release_source(release)].add(label)
    tails: set[tuple[str, ...]] = set()
    for labels in per_source.values():
        if len(labels) != chapter_total:
            continue
        numbers = {label: _decimal(label) or Decimal(0) for label in labels}
        wholes = {
            int(number)
            for number in numbers.values()
            if number == number.to_integral_value()
        }
        decimals = sorted(
            (
                label
                for label, number in numbers.items()
                if number != number.to_integral_value()
            ),
            key=Decimal,
        )
        if not wholes or not decimals:
            continue
        last = max(wholes)
        if wholes != set(range(1, last + 1)):
            continue
        if any(Decimal(label) < last for label in decimals):
            continue
        tails.add(tuple(decimals))
    if len(tails) != 1:
        return frozenset()
    tail = next(iter(tails))
    carried = {
        label: {
            _release_source(release)
            for release in rows
            if canonical_number(release.get("chapter")) == label
        }
        for label in tail
    }
    if any(len(sources) < minimum_sources for sources in carried.values()):
        return frozenset()
    return frozenset(tail)


def canonical_decimal_labels(
    chapter_map: Iterable[Any] | None,
    releases: Iterable[dict[str, Any]] = (),
    *,
    chapter_total: int | None = None,
    strict_cardinality: bool = False,
) -> frozenset[str]:
    """Decimal chapters that are chapters of the work: the ones the map
    places inside a volume, and the ones a source's complete list ends with
    (see count_aligned_tail_labels). Nothing else - aggregators mirror one
    another, so several of them listing "167.5" is one upload seen many
    times, not agreement, and specials stay opt-in (measured live: twelve
    series of ".5" side episodes flooded Wanted). One rule, used by the
    index, the download candidate selection and the numbering
    reconciliation alike."""

    releases = list(releases)
    entries = effective_entries(chapter_map or [])
    authoritative = frozenset(
        label
        for entry in entries
        if getattr(entry, "exact", False)
        and getattr(entry, "source", "") in {"ocr", "operator"}
        for label in getattr(entry, "chapters", ())
        if (number := _decimal(label)) is not None
        and number != number.to_integral_value()
        and number > 0
    )
    inferred = (
        frozenset(
            label
            for entry in entries
            if getattr(entry, "exact", False)
            and getattr(entry, "source", "") not in {"ocr", "operator"}
            for label in getattr(entry, "chapters", ())
            if (number := _decimal(label)) is not None
            and number != number.to_integral_value()
            and number > 0
        )
        | count_aligned_tail_labels(releases, chapter_total)
        | count_aligned_extra_labels(releases, chapter_total)
    )
    observed_wholes = {
        int(number)
        for release in releases
        if not is_volume_release(release)
        and (label := canonical_number(release.get("chapter"))) is not None
        and (number := _decimal(label)) is not None
        and number > 0
        and number == number.to_integral_value()
    }
    # A mapped file can be one part of an ordinary chapter. If sources also
    # establish a complete sequence, and collapsing .1/.2 part groups
    # explains the independent count, do not promote the parts to chapters.
    part_groups: dict[int, set[Decimal]] = {}
    for label in authoritative:
        number = Decimal(label)
        base = int(number)
        part_groups.setdefault(base, set()).add(number - base)
    split_labels = {
        label
        for label in authoritative
        if (fractions := part_groups.get(int(Decimal(label)), set()))
        and len(fractions) >= 2
        and fractions == {Decimal(n) / 10 for n in range(1, len(fractions) + 1)}
    }
    whole_with_parts = observed_wholes | {int(Decimal(label)) for label in split_labels}
    if (
        chapter_total
        and whole_with_parts == set(range(1, max(whole_with_parts, default=0) + 1))
        and len(whole_with_parts) + len(authoritative - split_labels)
        == int(chapter_total)
    ):
        authoritative -= split_labels
        observed_wholes = whole_with_parts
    all_read_labels = {
        label
        for entry in entries
        if getattr(entry, "exact", False)
        and getattr(entry, "source", "") in {"ocr", "operator"}
        for label in getattr(entry, "chapters", ())
        if (number := _decimal(label)) is not None and number >= 0
    }
    if (
        chapter_total
        and authoritative
        and (
            len(observed_wholes) >= int(chapter_total)
            or (
                strict_cardinality
                and len(observed_wholes) + len(authoritative) != int(chapter_total)
            )
        )
        and len(all_read_labels) != int(chapter_total)
    ):
        authoritative = frozenset()
    # A decimal from a catalogue log is an optional extra when the ordinary
    # contiguous numbering already accounts for the whole work.  This keeps
    # 16.5 out of a 16-chapter total while retaining 60.5 and 60.6 when the
    # work has 62 chapters but its integer numbering ends at 60.  A local
    # reading or operator map remains authoritative.
    if chapter_total and (inferred or authoritative):
        whole = observed_wholes
        if whole and whole == set(range(1, max(whole) + 1)):
            room = max(0, int(chapter_total) - len(whole) - len(authoritative))
            # Cardinality can promote catalogue decimals only when they fill
            # the count exactly.  A partial source list (1..7 plus 7.5 for a
            # 16-chapter work) says nothing about whether 7.5 is one of the
            # sixteen; slicing the candidates to the available room made
            # every such omake canonical.  Complete lists such as AND
            # (1..44 + 5.5 = 45) and SPRIGGAN (1..60 + two tails = 62) still
            # prove their decimal chapters.
            if len(inferred) != room:
                inferred = frozenset()
            if len(whole) + len(authoritative) > int(chapter_total) and len(
                all_read_labels
            ) != int(chapter_total):
                # A partial operator map states where a chapter is printed,
                # not whether an omake contributes to the work's declared
                # cardinality.  Pastel's mapped 184.5 sits beside the complete
                # 1..213 manual sequence and remains an extra.  A complete
                # read map whose own cardinality matches the total still wins.
                authoritative = frozenset()
    return authoritative | inferred


def count_aligned_extra_labels(
    releases: Iterable[dict[str, Any]], chapter_total: int | None
) -> frozenset[str]:
    """Include numbered extras when complete source lists explain the total.

    Repeated extras alone are insufficient. Two sources must agree on the
    complete contiguous run and its half-chapters, and their cardinality must
    equal the finished work's catalogue count. This does not invent chapters
    after the last integer or treat split parts as separate chapters.
    """
    if not chapter_total:
        return frozenset()
    sources: dict[str, set[str]] = defaultdict(set)
    for release in releases:
        if is_volume_release(release):
            continue
        label = canonical_number(release.get("chapter"))
        value = _decimal(label)
        if label is not None and value is not None and value > 0:
            sources[_release_source(release)].add(label)
    candidates: dict[frozenset[str], int] = defaultdict(int)
    for labels in sources.values():
        if len(labels) != chapter_total:
            continue
        integers = {int(Decimal(n)) for n in labels if Decimal(n) % 1 == 0}
        extras = frozenset(n for n in labels if Decimal(n) % 1 != 0)
        if not integers or not extras or integers != set(range(1, max(integers) + 1)):
            continue
        if any(
            Decimal(n) % 1 != Decimal("0.5") or Decimal(n) > max(integers)
            for n in extras
        ):
            continue
        candidates[extras] += 1
    if len(candidates) != 1:
        return frozenset()
    extras, agreeing = next(iter(candidates.items()))
    return extras if agreeing >= 2 else frozenset()


def _complete_parts_on_disk(
    labels: list[str],
    numeric_groups: dict[tuple[str, str], list[dict[str, Any]]],
    scope: str,
) -> list[str] | None:
    """The labels N.1..N.k one source delivered whole, or ``None``.

    Sources split a chapter differently (six parts here, seven there): only a
    source's *own* set counts, and only when every part of it is on disk.
    """

    by_source: dict[tuple[str, str], dict[Decimal, bool]] = {}
    for label in labels:
        for release in numeric_groups[(scope, label)]:
            source = (
                str(release.get("provider") or ""),
                str(release.get("source_name") or ""),
            )
            parts = by_source.setdefault(source, {})
            number = Decimal(label)
            parts[number] = parts.get(number, False) or bool(release.get("downloaded"))
    best: list[str] | None = None
    for parts in by_source.values():
        numbers = sorted(parts)
        if len(numbers) < 2 or not all(parts.values()):
            continue
        base = numbers[0].to_integral_value(rounding="ROUND_FLOOR")
        if any(
            number != base + Decimal(index) / Decimal(10)
            for index, number in enumerate(numbers, start=1)
        ):
            continue
        candidate = [format(number, "f") for number in numbers]
        if best is None or len(candidate) > len(best):
            best = candidate
    return best


def _slot(
    *,
    key: str,
    chapter: str | None,
    volume: str | None,
    releases: list[dict[str, Any]],
    expected: bool,
    special: bool,
    evidence: str,
    monitor_mode: str,
    volume_inferred: bool = False,
    split_parts: list[str] | None = None,
) -> dict[str, Any]:
    ordered = sorted(releases, key=_release_sort_key, reverse=True)
    part_numbers = split_parts or []
    if part_numbers:
        downloaded = all(
            any(
                canonical_number(release.get("chapter")) == part
                and bool(release.get("downloaded"))
                for release in ordered
            )
            for part in part_numbers
        )
    else:
        downloaded = any(bool(release.get("downloaded")) for release in ordered)
    monitored = (
        any(bool(release.get("monitored")) for release in ordered)
        if ordered
        else monitor_mode in {"all", "existing"}
    )
    if special:
        # Specials (extras, omakes, side stories, "chapter 0") are opt-in:
        # they are monitored only when the work's map makes them canonical
        # or the series monitors specials explicitly. Never by default.
        monitored = False
    queue_status = next(
        (
            str(release["queue_status"])
            for release in ordered
            if release.get("queue_status")
        ),
        None,
    )
    return {
        "key": key,
        "volume": volume,
        "chapter": chapter,
        "expected": expected,
        "special": special,
        "evidence": evidence,
        "volume_inferred": volume_inferred,
        "split_parts": part_numbers,
        "releases": ordered,
        "available": bool(ordered),
        "downloaded": downloaded,
        "monitored": monitored,
        "queue_status": queue_status,
        "searchable": chapter is not None and not special,
    }


def effective_edition_book_count(manga: dict[str, Any]) -> int | None:
    """How many books the edition on disk has, when the operator said so.

    One control answers it: a manual edition total expressed in books (the
    published edition picked in the series settings) also tells Tankarr how
    the owned books split the chapters. The older explicit field still wins
    when set, so existing series keep their split.
    """

    explicit = _positive_integer(manga.get("edition_book_count"))
    if explicit is not None:
        return explicit
    unit = str(manga.get("expected_count_unit_override") or "").strip().casefold()
    if unit in {"volume", "issue"}:
        return _positive_integer(manga.get("expected_count_override"))
    return None


def _mapped_whole_span(labels: set[str]) -> tuple[int, int] | None:
    numbers = []
    for label in labels:
        value = _decimal(label)
        if value is not None and value == value.to_integral_value():
            numbers.append(int(value))
    return (min(numbers), max(numbers)) if numbers else None


def _outside_mapped_span(label: str, span: tuple[int, int] | None) -> bool:
    """A decimal, or a whole number before the first or after the last
    number any book claims: never a gap between mapped books."""

    value = _decimal(label)
    if value is None:
        return False
    if value != value.to_integral_value():
        return True
    if span is None:
        return False
    # Before the first mapped book is a gap (a partial map), except a "0"
    # nobody's book starts with; after the last mapped book is an extra.
    return int(value) > span[1] or (int(value) == 0 and span[0] >= 1)


def build_chapter_index(
    manga: dict[str, Any],
    metadata: dict[str, Any] | None,
    raw_releases: Iterable[dict[str, Any]],
    volume_monitor_overrides: dict[str, str] | None = None,
    chapter_map: Iterable[Any] | None = None,
    suspect_covering_volumes: set[str] | None = None,
) -> dict[str, Any]:
    """Build canonical chapter slots independently from provider releases.

    Provider rows answer "what can be downloaded". Catalogue totals answer
    "how large is the work". A canonical slot joins those facts without
    treating a decimal special as a missing integer chapter or fabricating
    numbered chapters from a cardinality-only metadata total.
    """

    metadata = metadata or {}
    # One unit per series: whole-volume books or chapters, never both. The
    # unit also settles nomenclature conflicts between sources (duplicates,
    # split parts, extras) before any slot exists.
    all_releases = [dict(release) for release in raw_releases]
    raw_release_list = all_releases
    for release in all_releases:
        if release.get("chapter") == 0 and not isinstance(release.get("chapter"), bool):
            # Numeric zero is a chapter label, not an absent value. Unit
            # selection's legacy truthiness checks expect string labels.
            release["chapter"] = "0"
    # Decimal chapters the map places inside a volume are chapters of the
    # work (And: "5.5" in volume 1, the 45th chapter the catalogues count).
    # So are decimals two independent sources both list: one source's
    # omake is an extra, a chapter every source numbers is a chapter.
    from tankarr.chapter_map import edition_entries

    map_entries = effective_entries(edition_entries(chapter_map or [], metadata))
    exact_map = chapters_by_volume(map_entries)
    operator_map = chapters_by_volume(
        entry for entry in map_entries if entry.source == "operator"
    )
    operator_labels = {label for labels in operator_map.values() for label in labels}
    # A source whose list holds exactly as many chapters as the work has
    # names the last ones itself, decimals included (SPRIGGAN: 62 chapters,
    # "…60, 60.5, 60.6"). Those are chapters, and the numbering ends there.
    counted_total = counted_chapter_total(manga, metadata)
    count_aligned_tail = count_aligned_tail_labels(all_releases, counted_total)
    canonical_decimals = canonical_decimal_labels(
        map_entries,
        all_releases,
        chapter_total=counted_total,
        strict_cardinality=(
            str(manga.get("expected_count_unit_override") or "").casefold() == "chapter"
            and _positive_integer(manga.get("expected_count_override")) is not None
        ),
    )
    unit_info, releases = select_releases(
        manga, metadata, all_releases, canonical_labels=canonical_decimals, copy=False
    )
    if unit_info["unit"] == "chapters" and "0" in operator_labels:
        selected_ids = {release.get("id") for release in releases}
        releases.extend(
            release
            for release in all_releases
            if not is_volume_release(release)
            and canonical_number(release.get("chapter")) == "0"
            and release.get("id") not in selected_ids
            and (release.get("downloaded") or not is_unpublished(release))
        )
    # Books already on disk keep covering their chapters (through the map)
    # even when the series follows chapters, so switching unit never makes
    # Tankarr re-download content it owns or hide a duplicate.
    owned_books = [
        release
        for release in all_releases
        if unit_info["unit"] == "chapters"
        and is_volume_release(release)
        and release.get("downloaded")
        and canonical_number(release.get("volume")) is not None
    ]
    volume_scoped = volume_scoped_numbering(releases)
    chapters_unit = unit_info["unit"] == "chapters"
    monitor_mode = str(manga.get("monitor_mode") or "none")
    # Volumes are deterministic: a chapter belongs to a volume only when the
    # catalogue's chapter↔volume map says so. Volume numbers sources write in
    # chapter names ("Vol.112 Ch.0") are hints at best and are ignored here.
    mapped_volume_for_chapter = {
        chapter: volume
        for volume, chapters in exact_map.items()
        for chapter in chapters
    }
    monitor_specials = False
    # A running work is tracked by chapter only: its volume split is not
    # final until the work ends, so chapters are never grouped into volumes
    # before then. Whole-volume files that are owned still cover chapters.
    publication = publication_summary(manga, metadata)
    work_ended = publication["status"] == "ended"
    continuing = publication["status"] == "continuing" or publication["paused"]

    def volume_from_map(label: str | None) -> str | None:
        if label is None or (not work_ended and label not in operator_labels):
            return None
        return mapped_volume_for_chapter.get(canonical_number(label) or label)

    numeric_groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    unnumbered: list[dict[str, Any]] = []
    for release in releases:
        chapter = canonical_number(release.get("chapter"))
        if chapter is None or is_volume_release(release):
            unnumbered.append(release)
            continue
        scope = canonical_number(release.get("volume")) if volume_scoped else ""
        numeric_groups[(scope or "", chapter)].append(release)

    main_slots: dict[tuple[str, int], dict[str, Any]] = {}
    fractional_by_scope: dict[tuple[str, int], list[tuple[Decimal, str]]] = defaultdict(
        list
    )
    for (scope, label), grouped in numeric_groups.items():
        number = _decimal(label)
        if number is None:
            continue
        base = int(number)
        # Chapter 0 only reaches this point when the catalogue counts the
        # prologue as a chapter (see series_unit._prologue_releases).
        if number == number.to_integral_value() and base >= 0:
            volume = scope or volume_from_map(label)
            key = (
                f"volume:{volume}:chapter:{base}"
                if volume_scoped
                else f"chapter:{base}"
            )
            main_slots[(scope, base)] = _slot(
                key=key,
                chapter=str(base),
                volume=volume,
                releases=grouped,
                # A number every source lists only as a placeholder (a
                # one-page "coming soon", an undated locked episode) is
                # observed, not expected: Guyver 201/204/205 were three
                # such stubs counted as three missing chapters.
                expected=any(
                    item.get("downloaded") or not is_unpublished(item)
                    for item in grouped
                ),
                special=False,
                evidence="observed",
                monitor_mode=monitor_mode,
            )
        elif number != number.to_integral_value():
            fractional_by_scope[(scope, base)].append((number, label))

    special_slots: list[dict[str, Any]] = []
    canonical_slots: list[dict[str, Any]] = []
    for (scope, base), parts in fractional_by_scope.items():
        labels = sorted({label for _number, label in parts}, key=Decimal)
        for label in [item for item in labels if item in canonical_decimals]:
            counted_tail = label in count_aligned_tail
            # A chapter the sources number "60.5" comes after chapter 60 and
            # is bound with it: the map has no row of its own for it, so it
            # takes the volume of the chapter it follows - the last one.
            volume = scope or volume_from_map(label)
            if volume is None and counted_tail:
                volume = volume_from_map(str(base))
            canonical_slots.append(
                _slot(
                    key=(
                        f"volume:{volume}:chapter:{label}"
                        if volume_scoped
                        else f"chapter:{label}"
                    ),
                    chapter=label,
                    volume=volume,
                    releases=numeric_groups[(scope, label)],
                    expected=True,
                    special=False,
                    evidence="counted_tail" if counted_tail else "map_canonical",
                    monitor_mode=monitor_mode,
                )
            )
        labels = [item for item in labels if item not in canonical_decimals]
        if not labels:
            continue
        whole = main_slots.get((scope, base))
        own_parts = (
            _complete_parts_on_disk(labels, numeric_groups, scope)
            if whole is not None and not whole["downloaded"]
            else None
        )
        if whole is not None and own_parts:
            # The whole chapter is offered but not on disk (blocked, or
            # unreadable everywhere), and one source delivered it as N.1..N.k
            # which the audit judges together: those parts ARE the chapter.
            # Measured live: chapter 45 of a webtoon sat in Wanted with its
            # six parts imported and passed.
            grouped = [
                *whole["releases"],
                *(
                    release
                    for label in own_parts
                    for release in numeric_groups[(scope, label)]
                ),
            ]
            main_slots[(scope, base)] = _slot(
                key=whole["key"],
                chapter=whole["chapter"],
                volume=whole["volume"],
                releases=grouped,
                expected=True,
                special=False,
                evidence="observed_split_parts",
                monitor_mode=monitor_mode,
                volume_inferred=bool(whole.get("volume_inferred")),
                split_parts=own_parts,
            )
            continue
        if base > 0 and (scope, base) not in main_slots and len(labels) >= 2:
            grouped = [
                release
                for label in labels
                for release in numeric_groups[(scope, label)]
            ]
            volume = scope or volume_from_map(labels[0])
            key = (
                f"volume:{volume}:chapter:{base}"
                if volume_scoped
                else f"chapter:{base}"
            )
            main_slots[(scope, base)] = _slot(
                key=key,
                chapter=str(base),
                volume=volume,
                releases=grouped,
                expected=True,
                special=False,
                evidence="observed_split_parts",
                monitor_mode=monitor_mode,
                split_parts=labels,
            )
            continue
        for label in labels:
            grouped = numeric_groups[(scope, label)]
            volume = scope or volume_from_map(label)
            continue  # extras never reach the index (see series_unit)
            special_slots.append(
                _slot(
                    key=(
                        f"volume:{volume}:special:{label}"
                        if volume_scoped
                        else f"special:{label}"
                    ),
                    chapter=label,
                    volume=volume,
                    releases=grouped,
                    expected=False,
                    special=True,
                    evidence="observed_special",
                    monitor_mode=monitor_mode,
                )
            )

    # A volumes series never materialises chapter slots: no chapter total,
    # no final sequence, no gaps. Only books count.
    expected_chapter_total = (
        _positive_integer(metadata.get("chapter_count")) if chapters_unit else None
    )
    # Kept apart: a running work drops the catalogue total below, but the
    # "numbering plus prologue equals the count" check still needs it.
    catalogue_chapter_total = expected_chapter_total
    expected_chapter_source = (
        str((metadata.get("provenance") or {}).get("chapter_count") or "").strip()
        or None
    )
    manual_expected_count = _positive_integer(manga.get("expected_count_override"))
    manual_expected_unit = (
        str(manga.get("expected_count_unit_override") or "").strip().casefold()
    )
    if not chapters_unit:
        manual_expected_count = (
            None if manual_expected_unit == "chapter" else manual_expected_count
        )
    if manual_expected_count is not None and manual_expected_unit == "chapter":
        expected_chapter_total = manual_expected_count
        expected_chapter_source = "manual edition override"
    publication = publication_summary(manga, metadata)
    reference_kind = "catalogue" if expected_chapter_total is not None else "none"
    if chapters_unit and (
        publication["status"] != "ended" or expected_chapter_total is None
    ):
        # A running work has no trustworthy total. The official platform, when
        # one is mapped, is the reference: its chapter count is what exists.
        # Without it a continuing work uses the distinct available chapters,
        # not the catalogue's cardinality or the largest chapter number. A finished
        # work whose catalogue never recorded a total is in the same
        # position: what the official platform published is the total.
        hosts = official_hosts(
            metadata.get("official_links"),
            language=str(manga.get("preferred_language") or ""),
        )
        official_numbers = {
            int(number)
            # The platform's publication calendar is the reference: an episode
            # dated in the future is announced, not published, so it is neither
            # expected nor missing yet. A paid episode whose date has passed
            # is published: Tapas sells every episode after the free ones.
            for release in all_releases
            if hosts
            and release_host(release) in hosts
            and not is_unpublished(release)
            and (number := _decimal(release.get("chapter"))) is not None
            and number == number.to_integral_value()
            and number > 0
        }
        if official_numbers:
            official_name = next(
                (
                    str(release.get("source_name") or release_host(release))
                    for release in releases
                    if release_host(release) in hosts
                ),
                "official source",
            )
            expected_chapter_total = max(official_numbers)
            expected_chapter_source = f"official source ({official_name})"
            reference_kind = "official"
        else:
            expected_chapter_total = None
            expected_chapter_source = None
            reference_kind = "available" if continuing else "none"
        if manual_expected_count is not None and manual_expected_unit == "chapter":
            expected_chapter_total = manual_expected_count
            expected_chapter_source = "manual edition override"
            reference_kind = "manual"
    provider_final = (
        _positive_integer(manga.get("last_chapter"))
        if chapters_unit and publication["status"] == "ended"
        else None
    )
    # Only numbers somebody could read bound the sequence: a stub past the
    # last real chapter must not open a gap of "missing" chapters up to it.
    observed_global = {
        base
        for (scope, base), slot in main_slots.items()
        if not volume_scoped and not scope and slot.get("expected")
    }
    observed_max = max(observed_global, default=0)
    sequence_end: int | None = None
    sequence_basis: str | None = None
    if (
        not volume_scoped
        and manual_expected_count is not None
        and manual_expected_unit == "chapter"
    ):
        sequence_end = manual_expected_count
        sequence_basis = "manual_edition_total"
    elif (
        not volume_scoped
        and publication["status"] == "ended"
        and expected_chapter_total is not None
        and (
            provider_final == expected_chapter_total
            or observed_max == expected_chapter_total
            or (
                provider_final is not None
                and provider_final != expected_chapter_total
                and expected_chapter_total in observed_global
            )
        )
    ):
        sequence_end = expected_chapter_total
        sequence_basis = (
            "provider_final"
            if provider_final == expected_chapter_total
            else "catalogue_total"
        )
    elif (
        not volume_scoped
        and provider_final is not None
        and expected_chapter_total is None
    ):
        sequence_end = provider_final
        sequence_basis = "provider_final"

    # A catalogue's "last chapter" is often its chapter *count*, and the
    # count includes the map's canonical decimals: 44 numbered chapters plus
    # "5.5" are 45 chapters, not a chapter 45. When the arithmetic says so
    # exactly, the sequence ends where the numbering does.
    # What explains a count beyond the numbering: the map's canonical
    # decimals, a canonical prologue, and the ".5" side episodes the sources
    # list below the numbering - those explain the count without becoming
    # wanted (specials stay opt-in).
    corroborated = {
        label
        for label in corroborated_decimal_chapters(all_releases)
        if label not in canonical_decimals
        and (value := _decimal(label)) is not None
        and observed_max
        and value < observed_max
    }
    counted_extras = (
        len(canonical_decimals)
        + len(corroborated)
        + (1 if ("", 0) in main_slots else 0)
    )
    if count_aligned_tail and observed_max and not volume_scoped:
        # The count is already accounted for, chapter by chapter, by the list
        # that matched it: the numbering ends at its last whole number and
        # nothing past it was ever written.
        sequence_end = observed_max
        sequence_basis = "source_list_matches_catalogue_count"
    elif (
        sequence_end is not None
        and counted_extras
        and observed_max
        and sequence_end == observed_max + counted_extras
    ):
        sequence_end = observed_max
        sequence_basis = f"{sequence_basis}_minus_canonical_extras"
    if (
        sequence_end is None
        and not continuing
        and not volume_scoped
        and catalogue_chapter_total is not None
        and manual_expected_count is None
        and observed_max
        and ("", 0) not in main_slots
    ):
        # The catalogue counts a prologue the sources number as chapter 0
        # (AniList/Kitsu say Guyver has 201 chapters; every source ends at
        # 200 and ComicK lists a "Chapter 0"). Numbering plus the prologue is
        # the count exactly: the sequence ends where the numbering does,
        # paused or not, and nothing past it is missing.
        lists_prologue = any(
            not is_volume_release(release)
            and str(canonical_number(release.get("chapter"))) == "0"
            for release in raw_release_list
        )
        candidate = catalogue_chapter_total - counted_extras - 1
        nothing_owned_past = not any(
            not scope and base > candidate and slot.get("downloaded")
            for (scope, base), slot in main_slots.items()
        )
        if lists_prologue and candidate in observed_global and nothing_owned_past:
            sequence_end = candidate
            sequence_basis = "catalogue_total_counts_prologue"
            expected_chapter_total = candidate
            expected_chapter_source = "catalogue total counts the prologue"
            reference_kind = "catalogue"
    verified = _positive_integer(manga.get("verified_chapter_count"))
    if (
        chapters_unit
        and not volume_scoped
        and publication["status"] != "ended"
        and manual_expected_count is None
        and verified is not None
        and manga.get("verified_chapter_source")
    ):
        # A checked publication frontier is a floor, never an edition's end.
        # New source chapters remain expected and can become Wanted, while
        # holes below the verified number are materialised even if unindexed.
        sequence_end = max(verified, observed_max, expected_chapter_total or 0)
        sequence_basis = "verified_publication_frontier"
        expected_chapter_total = sequence_end
        expected_chapter_source = str(manga["verified_chapter_source"])
        reference_kind = "verified"
    if sequence_end is not None and sequence_end > MAX_CANONICAL_SEQUENCE:
        sequence_end = None
        sequence_basis = None
    if sequence_end is not None:
        # The edition ends here. Numbers past it are another edition, an
        # aggregator's mislabelled chapters (Manga Ball's Guyver "202" and
        # "203" were a Chinese manhua) or stubs: observed, never expected or
        # missing, unless a file is on disk.
        for (scope, base), slot in main_slots.items():
            if not scope and base > sequence_end:
                slot["expected"] = False
                slot["special"] = True
        observed_global = {
            base
            for (scope, base), slot in main_slots.items()
            if not volume_scoped and not scope and slot.get("expected")
        }
        observed_max = max(observed_global, default=0)

    missing_numbers: set[int] = set()
    if (
        chapters_unit
        and not volume_scoped
        and observed_global
        and reference_kind != "available"
    ):
        missing_numbers.update(
            set(range(min(observed_global), observed_max + 1)) - observed_global
        )
    if sequence_end is not None:
        missing_numbers.update(set(range(1, sequence_end + 1)) - observed_global)

    if chapters_unit and not volume_scoped:
        owned_volume_numbers = {
            canonical_number(release.get("volume"))
            for release in [*unnumbered, *owned_books]
            if release.get("downloaded") and canonical_number(release.get("volume"))
        }
        for volume, mapped_chapters in exact_map.items():
            if volume not in owned_volume_numbers and volume not in operator_map:
                continue
            for label in mapped_chapters:
                number = _decimal(label)
                if number is None:
                    continue
                if number != number.to_integral_value():
                    if not any(slot["chapter"] == label for slot in canonical_slots):
                        canonical_slots.append(
                            _slot(
                                key=f"chapter:{label}",
                                chapter=label,
                                volume=volume,
                                # A mapped omake may be excluded from the
                                # catalogue's chapter count. Keep an owned
                                # copy visible without inventing a missing
                                # canonical chapter after normalization.
                                releases=[
                                    release
                                    for release in all_releases
                                    if release.get("downloaded")
                                    and not is_volume_release(release)
                                    and canonical_number(release.get("chapter"))
                                    == label
                                ],
                                expected=label in canonical_decimals,
                                special=label not in canonical_decimals,
                                evidence="operator_map"
                                if volume in operator_map
                                else "map_canonical",
                                monitor_mode=monitor_mode,
                            )
                        )
                elif ("", int(number)) not in main_slots:
                    if sequence_end is not None and int(number) > sequence_end:
                        continue
                    missing_numbers.add(int(number))
    known_volumes = {
        base: slot.get("volume")
        for (_scope, base), slot in main_slots.items()
        if not volume_scoped and slot.get("volume")
    }
    ordered_known = sorted(known_volumes)
    for number in sorted(missing_numbers):
        lower = max((item for item in ordered_known if item < number), default=None)
        upper = min((item for item in ordered_known if item > number), default=None)
        inferred_volume = (
            known_volumes[lower]
            if lower is not None
            and upper is not None
            and known_volumes[lower] == known_volumes[upper]
            else None
        )
        mapped_volume = volume_from_map(str(number))
        main_slots[("", number)] = _slot(
            key=f"chapter:{number}",
            chapter=str(number),
            volume=mapped_volume or inferred_volume,
            releases=[],
            expected=True,
            special=False,
            evidence=(
                "mapped_by_catalogue"
                if mapped_volume
                and number not in observed_global
                and sequence_end is None
                else (sequence_basis or "gap_between_observed_chapters")
            ),
            monitor_mode=monitor_mode,
            volume_inferred=mapped_volume is None and inferred_volume is not None,
        )

    volume_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    loose_unnumbered: list[dict[str, Any]] = []
    for release in unnumbered:
        volume = canonical_number(release.get("volume"))
        if volume is None:
            loose_unnumbered.append(release)
        else:
            volume_groups[volume].append(release)
    volume_slots = [
        _slot(
            key=f"volume:{volume}",
            chapter=None,
            volume=volume,
            releases=grouped,
            expected=True,
            special=False,
            evidence="observed_volume",
            monitor_mode=monitor_mode,
        )
        for volume, grouped in volume_groups.items()
    ]
    expected_volume_total = _positive_integer(metadata.get("volume_count"))
    expected_volume_source = (
        str((metadata.get("provenance") or {}).get("volume_count") or "").strip()
        or None
    )
    managed_volumes = managed_volume_count(metadata)
    if managed_volumes:
        # The managed (English) edition is the one the library can complete.
        expected_volume_total = managed_volumes
        expected_volume_source = f"{(metadata.get('managed_edition') or {}).get('publisher') or 'English'} edition"
    if manual_expected_count is not None and manual_expected_unit in {
        "volume",
        "issue",
    }:
        expected_volume_total = manual_expected_count
        expected_volume_source = "manual edition override"
    # The edition on disk is the extent the shelf and the units already
    # count in; the card counted the catalogue's 18 original volumes while
    # the shelf held VIZ's complete 12 and hunted books 13-18 of another
    # edition. One number everywhere.
    edition_books = _positive_integer(manga.get("edition_book_count"))
    if edition_books:
        expected_volume_total = edition_books
        expected_volume_source = "edition on disk"
    # A volumes series counts books even before any source offers one: the
    # catalogue's volume total materialises the expected books so Wanted and
    # the indexer search know what to look for.
    volume_sequence_mode = not chapters_unit or (bool(volume_slots) and not main_slots)
    # A book numbered past the edition's last (volume 15 of a work VIZ bound
    # in 12) belongs to another edition: observed, never expected or missing.
    # Only a file on disk keeps such a row.
    if expected_volume_total:
        for slot in volume_slots:
            number = _positive_integer(slot.get("volume"))
            if (
                number is not None
                and number > expected_volume_total
                and not slot.get("downloaded")
            ):
                slot["expected"] = False
    if (
        volume_sequence_mode
        and expected_volume_total is not None
        and expected_volume_total <= MAX_CANONICAL_SEQUENCE
    ):
        observed_volume_numbers = {
            number
            for slot in volume_slots
            if slot["expected"]
            if (number := _positive_integer(slot.get("volume"))) is not None
        }
        pinned_edition = bool(edition_books) or (
            manual_expected_count is not None
            and manual_expected_unit in {"volume", "issue"}
        )
        if pinned_edition or (
            len(observed_volume_numbers)
            == sum(bool(slot["expected"]) for slot in volume_slots)
            and all(
                number <= expected_volume_total for number in observed_volume_numbers
            )
        ):
            for number in sorted(
                set(range(1, expected_volume_total + 1)) - observed_volume_numbers
            ):
                volume_slots.append(
                    _slot(
                        key=f"volume:{number}",
                        chapter=None,
                        volume=str(number),
                        releases=[],
                        expected=True,
                        special=False,
                        evidence="metadata_volume_total",
                        monitor_mode=monitor_mode,
                    )
                )
    loose_slots: list[dict[str, Any]] = []
    _unused_loose = [
        _slot(
            key=f"release:{release.get('id')}",
            chapter=None,
            volume=None,
            releases=[release],
            expected=False,
            special=True,
            evidence="observed_unnumbered",
            monitor_mode=monitor_mode,
        )
        for release in loose_unnumbered
    ]

    for slot in special_slots:
        slot["canonical"] = slot.get("volume") is not None
        slot["monitored"] = bool(slot["canonical"] or monitor_specials)
    slots = [
        *main_slots.values(),
        *canonical_slots,
        *special_slots,
        *volume_slots,
        *loose_slots,
    ]
    mapped_chapter_slots: list[dict[str, Any]] = []
    if not chapters_unit:
        # Supplemental chapter rows serve the mixed-series view and targeted
        # book fallback. They do not change a volume series' slots or counts.
        all_numeric: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        part_labels_by_base: dict[str, set[str]] = defaultdict(set)
        for release in all_releases:
            if (
                not is_volume_release(release)
                and (number := _decimal(release.get("chapter"))) is not None
            ):
                all_numeric[("", canonical_number(release["chapter"]))].append(release)
                part = canonical_number(release["chapter"])
                if (
                    part not in canonical_decimals
                    and number != number.to_integral_value()
                ):
                    part_labels_by_base[str(int(number))].add(part)
        for volume, labels in exact_map.items():
            for label in sorted(labels, key=Decimal):
                grouped = [
                    release
                    for release in all_numeric[("", label)]
                    if release.get("downloaded") or not is_unpublished(release)
                ]
                split_parts = None
                if not any(release.get("downloaded") for release in grouped):
                    part_labels = list(part_labels_by_base.get(label, ()))
                    split_parts = _complete_parts_on_disk(part_labels, all_numeric, "")
                    if split_parts:
                        grouped.extend(
                            release
                            for part in split_parts
                            for release in all_numeric[("", part)]
                        )
                mapped_chapter_slots.append(
                    _slot(
                        key=f"chapter:{label}",
                        chapter=label,
                        volume=volume,
                        releases=grouped,
                        expected=True,
                        special=False,
                        evidence="operator_map"
                        if label in operator_labels
                        else "exact_map",
                        monitor_mode=monitor_mode,
                        split_parts=split_parts,
                    )
                )
    # A chapter inside an owned volume is not missing. The map decides which
    # chapters that is; without a map entry the chapter is "covered but
    # unmapped": neither wanted nor downloaded twice.
    owned_volumes = {
        str(slot["volume"])
        for slot in volume_slots
        if slot["downloaded"] and slot.get("volume")
    } | {str(canonical_number(release.get("volume"))) for release in owned_books}
    inferred_suspects = suspect_volume_reasons(all_releases, chapter_map=map_entries)
    # A hand-built book whose size is off the edition is a placeholder for
    # the real one: it stays readable, but its slot keeps asking for the
    # edition's own copy, and the import replaces it when that copy lands.
    for slot in volume_slots:
        reason = inferred_suspects.get(str(slot.get("volume") or ""))
        if (
            slot["downloaded"]
            and reason
            and "another edition or a wrong split" in reason
            and all(
                str(release.get("provider") or "") == "manual"
                for release in slot.get("releases") or []
                if release.get("downloaded")
            )
        ):
            slot["replaceable"] = reason
    suspect_volumes = (
        set(inferred_suspects)
        if suspect_covering_volumes is None
        else {canonical_number(volume) for volume in suspect_covering_volumes}
    )
    covering_volumes = owned_volumes - suspect_volumes
    chapter_coverage = coverage_by_chapter(map_entries, covering_volumes)
    # Explicit operator boundaries can satisfy missing chapters in a manually
    # imported book. This does not prove that an existing human chapter file is
    # disposable: duplicate detection still excludes every suspect book.
    operator_coverage = coverage_by_chapter(
        [
            entry
            for entry in map_entries
            if entry.source in ("operator", "ocr") and entry.exact
        ],
        {
            volume
            for volume, reason in inferred_suspects.items()
            if reason == "Imported by hand"
        },
    )
    mapped_labels = {label for labels in exact_map.values() for label in labels}
    mapped_span = _mapped_whole_span(mapped_labels)
    for slot in [*slots, *mapped_chapter_slots]:
        slot["covered_by_volume"] = None
        slot["covered_by_chapters"] = False
        slot["coverage_exact"] = False
        slot["covered_unmapped"] = False
        slot["duplicate_of_volume"] = None
        if slot["chapter"] is None or not (covering_volumes or operator_coverage):
            continue
        coverage = chapter_coverage.get(slot["chapter"], Coverage())
        if slot["downloaded"]:
            # A chapter file whose content is also inside an owned volume is
            # a duplicate: reported, never deleted automatically.
            if coverage.covered:
                slot["duplicate_of_volume"] = coverage.volume
            continue
        explicit = operator_coverage.get(slot["chapter"], Coverage())
        if explicit.covered:
            coverage = explicit
        if coverage.covered:
            slot["covered_by_volume"] = coverage.volume
            slot["coverage_exact"] = coverage.exact
        elif coverage.unmapped and not continuing:
            slot["covered_unmapped"] = True
        elif not continuing and not exact_map and slot["expected"]:
            # Volumes are owned but nothing maps them: stay conservative.
            slot["covered_unmapped"] = True
        elif (
            not continuing
            and slot["expected"]
            and (covering_volumes or operator_coverage)
            and slot["chapter"] not in mapped_labels
            and _outside_mapped_span(slot["chapter"], mapped_span)
            and not (
                sequence_end is not None
                and slot["chapter"].isdigit()
                and 1 <= int(slot["chapter"]) <= sequence_end
            )
        ):
            # Books are owned and no book claims this number, and it lies
            # outside the numbers the map spans: an extra (a decimal omake, a
            # prologue "0" from one source, a number beyond the last book).
            # Opt-in, not wanted. A canonical whole number within the known
            # edition remains missing even beyond a partial map's boundaries.
            slot["covered_unmapped"] = True
    # The reverse holds too: a book whose chapters are all on disk is not a
    # missing book. Every exact mapped number is required, including canonical
    # zero/fractional chapters; neither neighbours nor hints prove its contents.
    downloaded_numbers = {
        number
        for release in all_releases
        if release.get("downloaded")
        and not is_volume_release(release)
        and (number := _decimal(release.get("chapter"))) is not None
    }
    if downloaded_numbers:
        for slot in slots:
            if (
                slot["chapter"] is not None
                or slot["downloaded"]
                or not slot["expected"]
            ):
                continue
            labels = exact_map.get(str(slot.get("volume")), set())
            required = {_decimal(label) for label in labels}
            if required and None not in required and required <= downloaded_numbers:
                slot["covered_by_chapters"] = True
                slot["coverage_exact"] = True
    # Edition totals establish how many books exist, never their contents.
    # Without a map, owned books cannot cover an evenly divided chapter range.
    normalized_overrides = {
        normalized: state
        for raw_volume, raw_state in (volume_monitor_overrides or {}).items()
        if (normalized := canonical_number(raw_volume)) is not None
        and (state := str(raw_state or "").strip().casefold())
        in {"monitored", "ignored"}
    }
    series_marked_up_to_date = (
        str(manga.get("library_status_override") or "").strip().casefold()
        == "up_to_date"
    )
    for slot in [*slots, *mapped_chapter_slots]:
        volume_key = canonical_number(slot.get("volume"))
        monitor_state = normalized_overrides.get(volume_key or "", "automatic")
        slot["volume_monitor_state"] = monitor_state
        slot["ignored"] = monitor_state == "ignored"
        if monitor_state == "ignored":
            slot["monitored"] = False
        elif monitor_state == "monitored":
            slot["monitored"] = True
        if series_marked_up_to_date and slot["expected"] and not slot["downloaded"]:
            slot["ignored"] = True
            slot["series_status_ignored"] = True
            slot["monitored"] = False

    # An aggregator can post the next chapter days before the official
    # platform publishes it. MangaK and Weeb Central offered One Piece 1193
    # on the morning of 2026-09-11 while MANGA Plus, the reference for this
    # work, still ended at 1192 with the next issue a week away. The download
    # gate already refuses that chapter under "prefer official", so counting
    # it as a hole made one card say "1192 / 1192" and "1 missing" at once
    # and promise a chapter nothing would ever fetch. The same frontier that
    # governs acquisition governs the count, and only under the same policy:
    # an operator who takes the first release available still wants it, and
    # still sees it missing. What is already on disk is never reconsidered.
    applied_frontier: Decimal | None = None
    if (
        chapters_unit
        and not volume_sequence_mode
        and acquisition_policy(manga) == "prefer_official"
    ):
        frontier = official_frontier(
            releases,
            official_hosts(
                metadata.get("official_links"),
                language=str(manga.get("preferred_language") or ""),
            ),
            metadata=metadata,
            status=publication["status"],
            chapter_total=(
                manga.get("expected_count_override")
                if str(manga.get("expected_count_unit_override") or "").casefold()
                == "chapter"
                else None
            ),
        )
        applied_frontier = frontier
        if frontier is not None:
            for slot in slots:
                if (
                    not slot["expected"]
                    or slot["downloaded"]
                    or slot.get("chapter") is None
                    or not beyond_frontier({"chapter": slot["chapter"]}, frontier)
                ):
                    continue
                slot["expected"] = False
                slot["beyond_official_frontier"] = True

    # A complete, read chapter map names the edition's actual units. Its
    # cardinality is not a last chapter number: split releases may contain
    # 1.1 and 1.2 without a separate chapter 1, and a prologue may be 0.
    # Partial/estimated maps and ongoing works cannot close a sequence.
    read_books = chapters_by_volume(
        entry
        for entry in map_entries
        if entry.exact and entry.source in {"ocr", "operator"}
    )
    read_labels = [label for labels in read_books.values() for label in labels]
    closed_labels = set(read_labels)
    if (
        chapters_unit
        and not volume_scoped
        and publication["status"] == "ended"
        and expected_volume_total
        and set(read_books) == {str(n) for n in range(1, expected_volume_total + 1)}
        and len(read_labels) == len(closed_labels)
        and closed_labels
        and (
            expected_chapter_total is None or expected_chapter_total == len(read_labels)
        )
    ):
        slots = [
            slot
            for slot in slots
            if slot.get("chapter") in closed_labels
            or slot.get("releases")
            or slot.get("downloaded")
            or slot.get("chapter") is None
        ]
        for slot in slots:
            if slot.get("chapter") is not None:
                slot["expected"] = slot["chapter"] in closed_labels
        sequence_end = max(int(Decimal(label)) for label in closed_labels)
        sequence_basis = "complete_read_map"
        expected_chapter_total = len(read_labels)
        expected_chapter_source = "complete read chapter map"

    if reference_kind == "available" and not volume_sequence_mode:
        for slot in slots:
            if slot.get("expected") and slot.get("chapter") is not None:
                slot["expected"] = bool(
                    slot.get("available")
                    or slot.get("downloaded")
                    or slot.get("covered_by_volume")
                )
        expected_chapter_total = sum(
            1
            for slot in slots
            if slot.get("expected") and slot.get("chapter") is not None
        )
        expected_chapter_source = "available sources"

    expected_total = (
        expected_volume_total if volume_sequence_mode else expected_chapter_total
    )
    expected_source = (
        expected_volume_source if volume_sequence_mode else expected_chapter_source
    )
    catalogue_extra_coverage: dict[str, int] | None = None
    if (
        chapters_unit
        and not volume_scoped
        and publication["status"] == "ended"
        and reference_kind == "catalogue"
        and not series_marked_up_to_date
        and manual_expected_count is None
        and expected_total is not None
        and (extra_count := documented_extra_count(manga, metadata)) is not None
        and 0 < extra_count < expected_total
        # An exact chapter map may already account for the documented extras
        # as canonical decimal slots. In that case adding their cardinality
        # again would double-count owned chapters and invent missing extras.
        and sum(
            1
            for slot in slots
            if slot["expected"]
            and slot.get("chapter") is not None
            and (slot.get("releases") or slot.get("covered_by_volume"))
        )
        < expected_total
    ):
        core_end = expected_total - extra_count
        raw_whole = {
            int(number)
            for release in raw_release_list
            if not is_volume_release(release)
            and (number := _decimal(release.get("chapter"))) is not None
            and number == number.to_integral_value()
            and number > 0
        }
        core_slots = {
            int(number)
            for slot in slots
            if slot["expected"]
            and (number := _decimal(slot.get("chapter"))) is not None
            and number == number.to_integral_value()
            and 1 <= number <= core_end
        }
        beyond = [
            slot
            for slot in slots
            if slot["expected"]
            and (number := _decimal(slot.get("chapter"))) is not None
            and number > core_end
        ]
        # A source's actual integer sequence corroborates the catalogue's
        # main/extra split. Beyond it, only inferred split parts or empty
        # placeholders generated from the unsplit catalogue total may remain;
        # an independently numbered chapter is contrary evidence.
        if (
            raw_whole == set(range(1, core_end + 1))
            and core_slots == raw_whole
            and all(
                slot.get("split_parts")
                or (
                    not slot.get("releases")
                    and str(slot.get("evidence") or "").startswith(
                        ("provider_final", "metadata_total")
                    )
                )
                for slot in beyond
            )
        ):
            counted_ids = {
                str(release.get("id"))
                for slot in slots
                if slot["expected"]
                for release in slot.get("releases") or []
            }
            owned_extras = min(
                extra_count,
                owned_documented_extras(
                    raw_release_list,
                    counted_ids,
                    documented_extra_labels(manga, metadata),
                ),
            )
            for slot in beyond:
                if slot.get("releases"):
                    slot["expected"] = False
                    slot["special"] = True
                else:
                    slots.remove(slot)
            sequence_end = core_end
            sequence_basis = "catalogue_count_minus_documented_extras"
            catalogue_extra_coverage = {"expected": extra_count, "owned": owned_extras}
    mapped_cardinality = sum(1 for slot in slots if slot["expected"] or slot["special"])
    unresolved_expected_count = (
        0
        if volume_sequence_mode or sequence_end is not None or expected_total is None
        else max(0, expected_total - mapped_cardinality)
    )
    if series_marked_up_to_date:
        unresolved_expected_count = 0
    elif catalogue_extra_coverage is not None:
        unresolved_expected_count = (
            catalogue_extra_coverage["expected"] - catalogue_extra_coverage["owned"]
        )

    def _satisfied(slot: dict[str, Any]) -> bool:
        return bool(
            slot["downloaded"]
            or slot["covered_by_volume"]
            or slot.get("covered_by_chapters")
        )

    raw_missing_count = sum(
        1
        for slot in slots
        if slot["expected"] and not _satisfied(slot) and not slot["covered_unmapped"]
    )
    if catalogue_extra_coverage is not None:
        raw_missing_count += (
            catalogue_extra_coverage["expected"] - catalogue_extra_coverage["owned"]
        )
    ignored_missing_count = sum(
        1
        for slot in slots
        if slot["expected"]
        and not _satisfied(slot)
        and not slot["covered_unmapped"]
        and slot["ignored"]
    )
    covered_count = sum(1 for slot in slots if slot["covered_by_volume"])
    duplicate_count = sum(1 for slot in slots if slot["duplicate_of_volume"])
    covered_unmapped_count = sum(1 for slot in slots if slot["covered_unmapped"])
    expected_slots = [
        slot for slot in slots if slot["expected"] and slot["chapter"] is not None
    ]
    read_books = chapters_by_volume(
        entry
        for entry in map_entries
        if entry.exact and entry.source in {"ocr", "operator"}
    )
    read_chapter_count = None
    if expected_volume_total and set(read_books) == {
        str(n) for n in range(1, expected_volume_total + 1)
    }:
        read_labels = [label for labels in read_books.values() for label in labels]
        if len(read_labels) == len(set(read_labels)):
            # Membership also includes optional extras; a book assignment
            # does not make those extras numbered chapters.
            optional = {
                slot["chapter"]
                for slot in slots
                if slot.get("special") and not slot.get("expected")
            }
            counted_labels = set(read_labels) - optional
            counted_labels.update(
                slot["chapter"]
                for slot in slots
                if slot.get("expected")
                and slot.get("split_parts")
                and set(slot["split_parts"]).issubset(read_labels)
            )
            read_chapter_count = len(counted_labels)
    additional_content = owned_additional_content(raw_release_list, slots)
    owned_specials = sum(additional_content.values())
    return {
        "additional_content": additional_content,
        "read_chapter_count": read_chapter_count,
        "slots": slots,
        "mapped_chapter_slots": mapped_chapter_slots,
        # The official head this index counted against, so a later builder
        # working from raw releases cannot reopen what it closed here. A
        # label, not a Decimal: this index is serialised into the payload.
        "official_frontier": canonical_number(applied_frontier),
        "expected_available_count": sum(1 for s in expected_slots if s["available"])
        + (catalogue_extra_coverage or {}).get("owned", 0),
        "expected_satisfied_count": sum(
            1 for s in expected_slots if s["downloaded"] or s["covered_by_volume"]
        )
        + (catalogue_extra_coverage or {}).get("owned", 0),
        "latest_chapter": canonical_number(max(official_numbers))
        if reference_kind == "official"
        else max(
            (
                s["chapter"]
                for s in expected_slots
                if s["available"] or s["downloaded"] or s["covered_by_volume"]
            ),
            key=_number_sort_key,
            default=None,
        ),
        # Optional content enters the public count only after its pages are
        # owned. An offered .5 or omake is never a missing chapter.
        "special_count": owned_specials,
        "special_downloaded_count": owned_specials,
        "catalogue_extra_coverage": catalogue_extra_coverage,
        "special_monitored": monitor_specials,
        "series_unit": unit_info["unit"],
        "series_unit_reason": unit_info["reason"],
        "series_unit_override": unit_info["override"],
        "unit_coverage": unit_info["coverage"],
        "dropped_releases": unit_info["dropped"],
        "expected_volume_count": expected_volume_total,
        "owned_volume_count": len(owned_volumes),
        "edition_split": None,
        "unit": "volume" if volume_sequence_mode else "chapter",
        "numbering_mode": "volume_scoped" if volume_scoped else "global",
        "sequence_end": sequence_end,
        "sequence_basis": sequence_basis,
        "expected_count": expected_total,
        "expected_source": expected_source,
        "reference_kind": reference_kind
        if not volume_sequence_mode
        else ("catalogue" if expected_volume_total else "none"),
        "unresolved_expected_count": unresolved_expected_count,
        "mapped_missing_count": raw_missing_count - ignored_missing_count,
        "raw_missing_count": raw_missing_count,
        "ignored_missing_count": ignored_missing_count,
        "covered_count": covered_count,
        "covered_unmapped_count": covered_unmapped_count,
        "duplicate_count": duplicate_count,
        "owned_volumes": sorted(owned_volumes, key=_number_sort_key),
    }
