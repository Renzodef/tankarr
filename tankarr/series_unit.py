"""The primary download unit for a series: chapters or whole-volume books.

Sources offer two shapes of file for the same work: single chapters and
whole tankōbon volumes. The index, Wanted and calendar follow a primary
unit; books may fill gaps in a continuing chapter series. The unit is
either chosen by the user or resolved
deterministically from the catalogue and from what the mapped sources
actually offer:

* only volume books available            → ``volumes``
* only chapters available                → ``chapters``
* both: ended work with a known volume
  count                                  → ``volumes`` (finite, clean books)
* both: running work                     → ``chapters``
* nothing mapped yet: ≤ 3 catalogued
  volumes, or an ended work with volumes
  but no chapter count                   → ``volumes``
* otherwise                              → ``chapters``

Within the ``chapters`` unit, sources disagree on nomenclature in four
well-known ways, each resolved without looking at the other unit:

1. the same integer listed twice by one source (bonus pages reusing a
   number, duplicated listings) — one release per (source, number) survives;
2. ``N.1 … N.k`` parts from a source that has no ``N`` — merged as split
   parts of chapter ``N`` (handled downstream via ``split_parts``);
3. a lone ``N.x`` from a source without ``N`` while another source has
   ``N`` — the same chapter under a different label, renamed to ``N``;
4. every other decimal (side stories, fan art, "1053.1 Road to Laughtale")
   and chapter 0 / negative labels — extras, dropped from the index; they
   stay reachable through the manual release search only.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from typing import Any

from tankarr.series_form import _is_webtoon
from tankarr.series_summary import publication_summary
from tankarr.source_numbering import is_not_yet_released, is_special_title

SERIES_UNITS = ("chapters", "volumes")
AUTOMATIC = "automatic"
SMALL_WORK_VOLUMES = 3


@lru_cache(maxsize=8192)
def _parse_number(raw: str) -> Decimal | None:
    # Labels repeat across every series ("1" to "200"), and a render parses
    # each one a dozen times; Decimal objects are immutable, so sharing them
    # is safe.
    try:
        number = Decimal(raw)
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def _number(value: object) -> Decimal | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    return _parse_number(raw)


def _positive_int(value: object) -> int | None:
    number = _number(value)
    if number is None or number <= 0 or number != number.to_integral_value():
        return None
    return int(number)


def is_volume_release(release: dict[str, Any]) -> bool:
    """A whole-book release: flagged as such, or a volume without a chapter."""

    unit = release.get("release_unit")
    if unit is not None and unit != "chapter" and str(unit) == "volume":
        return True
    chapter = release.get("chapter")
    volume = release.get("volume")
    has_chapter = bool(chapter) and str(chapter).strip() != ""
    has_volume = bool(volume) and str(volume).strip() != ""
    return has_volume and not has_chapter


def is_chapter_release(release: dict[str, Any]) -> bool:
    return (
        not is_volume_release(release) and _number(release.get("chapter")) is not None
    )


def normalize_series_unit(value: object) -> str | None:
    text = str(value or "").strip().casefold()
    if text in SERIES_UNITS:
        return text
    if text in {"chapter", "volume"}:
        return text + "s"
    return None


# Set by the app: manga_id → {"preferred": "chapters"|"volumes", "indexer_volumes": set[int]}.
# Tests leave it None (pure resolution from releases and metadata).
unit_context: Any = None


def _context(manga: dict[str, Any]) -> dict[str, Any]:
    embedded = manga.get("_unit_context")
    if isinstance(embedded, dict):
        return dict(embedded)
    if unit_context is None or not manga.get("id"):
        return {}
    try:
        return dict(unit_context(str(manga["id"])) or {})
    except Exception:  # noqa: BLE001 - resolution must never fail
        return {}


def acquisition_policy(manga: dict[str, Any]) -> str:
    """The release policy in force, as the download gate sees it."""

    return str(_context(manga).get("acquisition_policy") or "").strip().casefold()


def coverage_for_units(
    manga: dict[str, Any],
    metadata: dict[str, Any] | None,
    releases: list[dict[str, Any]],
    indexer_volumes: set[int] | None = None,
    unobtainable_volumes: set[int] | None = None,
    *,
    normalized_chapters: list[dict[str, Any]] | None = None,
    pending_volumes: set[int] | None = None,
) -> dict[str, dict[str, Any]]:
    """How far each unit can go, aggregating every source and the indexers.

    A book already on its way (a grab queued or downloading) counts as
    available whatever the indexer ledger says about it: Yokohama Kaidashi
    Kikou flipped to chapters with ten of its fourteen books in SABnzbd
    because one slot had answered "not offered" and 142/144 chapters beat
    13/14 books by a hair.

    An indexer offer counts as a book only until the hunt has tried it and
    come back empty. Junko Mizuno's Hansel & Gretel sat on the volume unit
    for days because a torrent named "Hansel and Gretel" - another author's
    - was on file as an offer for v1 while the hunt kept refusing it, and
    the 22 chapters that would have completed the work were never asked for.
    """

    metadata = metadata or {}
    usable_offers = set(indexer_volumes or ()) - set(unobtainable_volumes or ())
    chapters = normalized_chapters
    if chapters is None:
        chapters, _dropped = normalize_chapter_releases(releases)

    from tankarr.official_numbering import (
        beyond_official_edition,
        official_chapter_frontier,
    )
    from tankarr.source_ranking import official_hosts as _official_hosts

    hosts = _official_hosts(
        metadata.get("official_links"),
        language=str(manga.get("preferred_language") or ""),
    )
    # The same restoration and trim ``select_releases`` applies, so the
    # count agrees with what the series actually shows: a confirmed prologue
    # (chapter 0 the official edition numbers) counts, and a scanlator
    # chapter beyond that edition's own head does not - it belongs to a
    # different, unreleased-here edition and is not "available".
    prologue = _prologue_releases(releases, chapters, metadata, hosts)
    if prologue:
        chapters = [*prologue, *chapters]
    verdict = official_chapter_frontier(chapters, hosts) if hosts else None
    if verdict is not None and verdict.enforceable:
        chapters = [
            release
            for release in chapters
            if not beyond_official_edition(release, verdict)
        ]
    chapter_numbers = {
        int(_number(r["chapter"]))
        for r in chapters
        if _number(r["chapter"]) is not None
        and _number(r["chapter"]) == _number(r["chapter"]).to_integral_value()
    }
    volume_numbers = (
        {
            number
            for item in releases
            if is_volume_release(item)
            and (number := _positive_int(item.get("volume"))) is not None
        }
        | usable_offers
        | {int(number) for number in (pending_volumes or ()) if int(number) > 0}
    )
    # The official edition in this language is the reference once it has
    # shown enough of itself to be believed. The catalogue's own
    # chapter_count/latest_release_chapter counts the original-language
    # edition, which runs ahead of every translation (Lookism: 630 on
    # NAVER, 611 on WEBTOON English) - using it here made a complete
    # translation read as permanently one or more chapters short, and made
    # scanlator chapters the acquisition gate already refuses to fetch read
    # as the library being "ahead" of the publisher.
    if verdict is not None and verdict.enforceable and verdict.frontier is not None:
        chapter_reference = int(verdict.frontier)
    else:
        from tankarr.catalogue_consensus import count_is_reliable

        chapter_reference = _positive_int(
            metadata.get("chapter_count")
        ) or _positive_int(metadata.get("latest_release_chapter"))
        if not count_is_reliable(metadata, "chapter"):
            chapter_reference = None
    from tankarr.catalogue_consensus import managed_volume_count

    # The English edition's own volume count, when the catalogue states it:
    # Kodansha's two Queen Emeraldas omnibuses are the books the operator can
    # own, not the four the Japanese original was bound into.
    volume_reference = managed_volume_count(metadata) or _positive_int(
        metadata.get("volume_count")
    )
    owned_chapters = sum(
        1 for r in releases if r.get("downloaded") and is_chapter_release(r)
    )
    owned_volumes = sum(
        1 for r in releases if r.get("downloaded") and is_volume_release(r)
    )
    return {
        "chapters": {
            "available": len(chapter_numbers),
            "expected": chapter_reference,
            "complete": bool(chapter_reference)
            and len(chapter_numbers) >= chapter_reference,
            "owned": owned_chapters,
        },
        "volumes": {
            "available": len(volume_numbers),
            "expected": volume_reference,
            "complete": bool(volume_reference)
            and len(volume_numbers) >= volume_reference,
            "owned": owned_volumes,
        },
    }


def resolve_series_unit(
    manga: dict[str, Any],
    metadata: dict[str, Any] | None,
    releases: Iterable[dict[str, Any]],
    *,
    coverage: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return ``{"unit", "reason", "override"}`` for a series.

    Deterministic and driven by what can actually be downloaded:

    * a user override wins;
    * a running work follows chapters unless only books exist;
    * otherwise books already on disk keep the series on books;
    * an ended work takes the unit that can **complete** it; when both can,
      the global preference decides; when neither can, the higher coverage
      wins and the preference breaks ties.
    """

    metadata = metadata or {}
    override = normalize_series_unit(manga.get("series_unit_override"))
    if override:
        return {"unit": override, "reason": "chosen for this series", "override": True}
    # A webtoon is read and published as episodes. Books exist for some of
    # them, late and as a courtesy, and they only say which episodes a
    # publisher chose to bind together - an arbitrary line drawn over a work
    # that has none. Following a webtoon in books means waiting for a division
    # that may never come, and asking sources for volumes they do not carry:
    # Tower of God's "books" were episodes 418-420 misread, and Omniscient
    # Reader's five missing books had no release anywhere, so the series could
    # never have completed. Episodes are the unit; an operator override wins.
    if _is_webtoon(metadata):
        return {
            "unit": "chapters",
            "reason": "a webtoon is followed in episodes; its books only divide them",
            "override": False,
        }
    context = _context(manga)
    preferred = normalize_series_unit(context.get("preferred")) or "volumes"
    items = list(releases)
    cov = coverage
    if cov is None:
        cov = coverage_for_units(
            manga,
            metadata,
            items,
            context.get("indexer_volumes"),
            context.get("unobtainable_volumes"),
            pending_volumes=context.get("pending_volumes"),
        )
    publication = publication_summary(manga, metadata)
    ended = publication["status"] == "ended"
    running = publication["status"] == "continuing" or publication["paused"]
    has_chapters = cov["chapters"]["available"] > 0
    has_volumes = cov["volumes"]["available"] > 0
    if running:
        if has_volumes and not has_chapters:
            return {
                "unit": "volumes",
                "reason": "continuing work: only books are offered",
                "override": False,
            }
        return {
            "unit": "chapters",
            "reason": "continuing work: chapters preferred, books can fill gaps",
            "override": False,
        }
    volume_count = _positive_int(metadata.get("volume_count"))
    from tankarr.catalogue_consensus import count_is_reliable

    if (
        ended
        and preferred == "volumes"
        and volume_count
        and count_is_reliable(metadata, "volume")
        and not count_is_reliable(metadata, "chapter")
        and not cov["chapters"]["complete"]
    ):
        return {
            "unit": "volumes",
            "reason": "confirmed book total; chapter total is uncorroborated",
            "override": False,
        }
    if (
        cov["volumes"]["owned"]
        and not cov["chapters"]["owned"]
        # A finished or unknown-status shelf that books cannot complete may
        # still be completed with chapters. Continuing works already chose
        # chapters above, independently of what is on disk.
        and not (
            cov["chapters"]["complete"]
            and not cov["volumes"]["complete"]
            and (ended or cov["volumes"]["available"] <= cov["volumes"]["owned"])
        )
    ):
        return {
            "unit": "volumes",
            "reason": f"{cov['volumes']['owned']} book{'s' if cov['volumes']['owned'] != 1 else ''} already in the library",
            "override": False,
        }
    if (
        cov["chapters"]["owned"]
        and not cov["volumes"]["owned"]
        and not (ended and cov["volumes"]["complete"] and preferred == "volumes")
    ):
        return {
            "unit": "chapters",
            "reason": f"{cov['chapters']['owned']} chapters already in the library",
            "override": False,
        }
    if (
        volume_count
        and volume_count <= SMALL_WORK_VOLUMES
        and not cov["chapters"]["complete"]
    ):
        # A graphic novel or a 2–3 book work (status known or not): the book
        # is what the indexers carry; episode scans are the fallback.
        return {
            "unit": "volumes",
            "reason": f"short work of {volume_count} volume{'s' if volume_count > 1 else ''}",
            "override": False,
        }
    if not ended and (has_chapters or has_volumes):
        # Running (or status unknown but sources mapped): chapters unless
        # only books exist.
        if has_volumes and not has_chapters:
            return {
                "unit": "volumes",
                "reason": "running work: only books are offered",
                "override": False,
            }
        return {
            "unit": "chapters",
            "reason": "running work: chapters until it ends",
            "override": False,
        }
    c_ok, v_ok = cov["chapters"]["complete"], cov["volumes"]["complete"]
    if c_ok and v_ok:
        return {
            "unit": preferred,
            "reason": f"both units can complete it; preference is {preferred}",
            "override": False,
        }
    if v_ok:
        return {
            "unit": "volumes",
            "reason": f"all {cov['volumes']['expected']} books are offered",
            "override": False,
        }
    if c_ok:
        return {
            "unit": "chapters",
            "reason": f"all {cov['chapters']['expected']} chapters are offered, books are not complete",
            "override": False,
        }
    if not has_chapters and not has_volumes:
        return {
            "unit": preferred,
            "reason": "nothing mapped yet; global preference",
            "override": False,
        }
    c_ratio = (
        cov["chapters"]["available"] / cov["chapters"]["expected"]
        if cov["chapters"]["expected"]
        else (1.0 if has_chapters else 0.0)
    )
    v_ratio = (
        cov["volumes"]["available"] / cov["volumes"]["expected"]
        if cov["volumes"]["expected"]
        else (1.0 if has_volumes else 0.0)
    )
    if abs(c_ratio - v_ratio) < 1e-9:
        return {
            "unit": preferred,
            "reason": "neither unit completes it; preference breaks the tie",
            "override": False,
        }
    unit = "chapters" if c_ratio > v_ratio else "volumes"
    return {
        "unit": unit,
        "reason": f"neither unit completes it; {unit} reach {int(max(c_ratio, v_ratio) * 100)}%",
        "override": False,
    }


def normalize_chapter_releases(
    releases: Iterable[dict[str, Any]],
    canonical_labels: frozenset[str] | set[str] = frozenset(),
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Apply the nomenclature rules; return (kept releases, drop counters).

    ``canonical_labels`` are decimal chapters the work's own map lists as
    chapters of a volume ("5.5" inside volume 1 of And): they are chapters,
    not extras, and are kept one per source like any integer.
    """

    dropped: dict[str, int] = defaultdict(int)
    by_source: dict[str, list[tuple[Decimal, dict[str, Any]]]] = defaultdict(list)
    for release in releases:
        if (
            is_special_title(release.get("title"))
            and str(release.get("chapter") or "") not in canonical_labels
        ):
            dropped["non_chapter_title"] += 1
            continue
        if not is_chapter_release(release):
            dropped["not_a_chapter"] += 1
            continue
        number = _number(release.get("chapter"))
        assert number is not None
        if number <= 0:
            dropped["chapter_zero_or_negative"] += 1
            continue
        if is_not_yet_released(release) and not release.get("downloaded"):
            # A paid or not-yet-published episode cannot be fetched: it is
            # neither a download candidate nor part of what the library can
            # reach (TBATE 252: dated 10 September, listed on 5 September).
            dropped["locked"] += 1
            continue
        by_source[_source_key(release)].append((number, release))

    integers_anywhere: set[int] = set()
    for items in by_source.values():
        integers_anywhere.update(
            int(n) for n, _r in items if n == n.to_integral_value()
        )

    kept: list[dict[str, Any]] = []
    for items in by_source.values():
        integers_here = {int(n) for n, _r in items if n == n.to_integral_value()}
        # rule 1: one release per (source, integer). The volume label a
        # source writes next to a chapter ("Vol.54 … Omake" reusing number 1)
        # never creates a second identity: the chapter number is the key.
        best: dict[int, dict[str, Any]] = {}
        for number, release in items:
            if number != number.to_integral_value():
                continue
            key = int(number)
            current = best.get(key)
            if current is None or _rank(release) > _rank(current):
                if current is not None:
                    dropped["duplicate_number"] += 1
                best[key] = release
            else:
                dropped["duplicate_number"] += 1
        kept.extend(best.values())
        # decimals
        parts: dict[int, list[tuple[Decimal, dict[str, Any]]]] = defaultdict(list)
        for number, release in items:
            if number == number.to_integral_value():
                continue
            parts[int(number)].append((number, release))
        for base, group in parts.items():
            canonical = [
                (n, r)
                for n, r in group
                if str(r.get("chapter") or "") in canonical_labels
            ]
            if canonical:
                best_canonical: dict[str, dict[str, Any]] = {}
                for _n, release in canonical:
                    label = str(release.get("chapter"))
                    current = best_canonical.get(label)
                    if current is None or _rank(release) > _rank(current):
                        if current is not None:
                            dropped["duplicate_number"] += 1
                        best_canonical[label] = release
                    else:
                        dropped["duplicate_number"] += 1
                kept.extend(best_canonical.values())
                group = [(n, r) for n, r in group if (n, r) not in canonical]
                if not group:
                    continue
            labels = sorted({n for n, _r in group})
            if base <= 0:
                dropped["extra"] += len(group)  # 0.1, 0.5…: prologues and extras
                continue
            on_disk_as_parts = (
                len(labels) >= 2
                and _consecutive_parts(base, labels)
                and all(release.get("downloaded") for _n, release in group)
            )
            if base in integers_here and not on_disk_as_parts:
                dropped["extra"] += len(group)  # rule 4: N exists here → side content
                continue
            if len(labels) >= 2 and _consecutive_parts(base, labels):
                # rule 2 - and rule 4's exception: a source may list N and
                # still have delivered it as N.1..N.k (its N unreadable and
                # blocked, the parts imported and passed by the audit). What
                # is on disk is the chapter; measured live on chapter 45 of
                # a webtoon that sat in Wanted with its six parts imported.
                kept.extend(release for _n, release in group)
                continue
            if len(labels) == 1 and base in integers_anywhere:
                for _n, release in group:  # rule 3
                    renamed = dict(release)
                    renamed["chapter"] = str(base)
                    renamed["chapter_label_source"] = str(release.get("chapter"))
                    kept.append(renamed)
                continue
            dropped["extra"] += len(group)
    return kept, dict(dropped)


def _consecutive_parts(base: int, labels: list[Decimal]) -> bool:
    expected = Decimal(base)
    for index, label in enumerate(labels, start=1):
        want = expected + Decimal(index) / Decimal(10)
        if label != want:
            return False
    return True


_EXTRA_WORDS = re.compile(
    r"\b(omake|extra|bonus|special|guidebook|side story|oneshot|one-shot|preview)\b",
    re.I,
)


def _source_key(release: dict[str, Any]) -> str:
    return str(
        release.get("source_key")
        or release.get("source_name")
        or release.get("provider")
        or ""
    )


def _rank(release: dict[str, Any]) -> tuple[int, int, int, str, Decimal]:
    """Which copy of a duplicated number wins inside one source: a plain
    chapter over an extra reusing its number, then the richer/newer file,
    then the lowest volume label."""

    title = str(release.get("title") or "")
    volume = _number(release.get("volume"))
    return (
        0 if _EXTRA_WORDS.search(title) else 1,
        int(release.get("pages") or 0),
        int(release.get("version") or 0),
        str(release.get("publish_at") or ""),
        -(volume if volume is not None else Decimal(10**6)),
    )


def confirm_chapter_numbers(
    releases: list[dict[str, Any]],
    *,
    official_hosts: frozenset[str] | set[str] = frozenset(),
) -> tuple[list[dict[str, Any]], int]:
    """Drop chapter numbers only one source vouches for beyond the consensus.

    Sources sometimes number extras or season splits as real chapters
    (MangaK lists 253–280 for a work whose other sources stop at 250).
    The consensus ceiling is the highest number at least two sources — or
    the official platform — expose. Above it, a number survives only when
    it continues the sequence without a gap from the ceiling (a source that
    is simply ahead: 878 after 877 when everyone else lags); anything past
    the first gap is unconfirmed and stays out until a second source or the
    official platform confirms it.
    """

    from tankarr.source_ranking import release_host

    sources_by_number: dict[int, set[str]] = defaultdict(set)
    official_numbers: set[int] = set()
    for release in releases:
        number = _number(release.get("chapter"))
        if number is None or number != number.to_integral_value():
            continue
        key = _source_key(release)
        sources_by_number[int(number)].add(key)
        if official_hosts and release_host(release) in official_hosts:
            official_numbers.add(int(number))
    consensus = max(
        [n for n, keys in sources_by_number.items() if len(keys) >= 2]
        + list(official_numbers)
        or [0]
    )
    if not consensus:
        return releases, 0  # a single source: nothing to compare against
    confirmed: set[int] = {n for n in sources_by_number if n <= consensus}
    cursor = consensus + 1
    while cursor in sources_by_number:
        confirmed.add(cursor)
        cursor += 1
    kept: list[dict[str, Any]] = []
    dropped = 0
    for release in releases:
        number = _number(release.get("chapter"))
        if (
            number is not None
            and number == number.to_integral_value()
            and int(number) not in confirmed
            # A file on disk is not a rumour: Master Keaton 121-144 came from
            # a source no other source reaches, and dropping them here made
            # the library forget chapters it holds and hunt them again.
            and not release.get("downloaded")
        ):
            dropped += 1
            continue
        kept.append(release)
    return kept, dropped


def _prologue_releases(
    items: list[dict[str, Any]],
    selected: list[dict[str, Any]],
    metadata: dict[str, Any] | None,
    official_hosts: frozenset[str] | set[str] = frozenset(),
) -> list[dict[str, Any]]:
    """Chapter 0 releases, when the prologue is a chapter of this edition.

    Webtoons often open with a chapter 0. It is an extra when the edition
    ignores it, and the first chapter when the edition publishes it, so the
    question is only ever "does the publication this library follows have a
    chapter 0", and there are two ways to answer it.

    The publisher's own platform is the direct answer and outranks everything:
    WEBTOON lists "Ep. 0 (ch. 0)" for Tower of God, "Prologue (ch. 0)" for
    unOrdinary and "Episode 0 - Prologue (ch. 0)" for Omniscient Reader.  When
    the official source in the operator's language numbers an episode 0, the
    official edition has a chapter 0 and dropping it puts the whole library one
    chapter behind the numbering it is supposed to follow.

    Without an official source, the catalogue total decides as before: it can
    only settle the question for a work whose length it already knows, which
    is why it never answers for a running webtoon (MangaBaka counts the
    original Korean edition, which runs ahead of every translation).
    """

    zero = [
        item
        for item in items
        if is_chapter_release(item) and _number(item.get("chapter")) == Decimal(0)
    ]
    if not zero:
        return []
    from tankarr.source_ranking import is_official_release

    if official_hosts and any(
        is_official_release(item, frozenset(official_hosts)) for item in zero
    ):
        return zero
    catalogue_total = _positive_int((metadata or {}).get("chapter_count"))
    if catalogue_total is None:
        return []
    numbered = {
        int(number)
        for item in selected
        if (number := _number(item.get("chapter"))) is not None
        and number == number.to_integral_value()
    }
    if not numbered or catalogue_total != len(numbered) + 1:
        return []
    if 1 not in numbered:
        return []
    return zero


def _drop_beyond_official_edition(
    selected: list[dict[str, Any]],
    official_hosts: frozenset[str] | set[str],
) -> tuple[list[dict[str, Any]], int]:
    """Remove chapters the official edition in this language has not reached.

    Scanlators number from the original edition, which runs ahead of every
    translation: Lookism is at 630 on NAVER and 611 on WEBTOON English, so
    six hundred and twelve is not a chapter of the publication this library
    follows - it is a chapter of a different one. Those rows are releases of
    nothing, and a slot built from them is a hole that can never be filled.

    Only an *enforceable* frontier does this. A source exposing a handful of
    official chapters says nothing about where the edition has got to, and
    silently truncating a series on that would be far worse than showing a
    few chapters too many.
    """

    from tankarr.official_numbering import (
        beyond_official_edition,
        official_chapter_frontier,
    )

    verdict = official_chapter_frontier(selected, frozenset(official_hosts))
    if not verdict.enforceable:
        return selected, 0
    kept = [
        release for release in selected if not beyond_official_edition(release, verdict)
    ]
    return kept, len(selected) - len(kept)


def _drop_beyond_catalogue_total(
    selected: list[dict[str, Any]],
    manga: dict[str, Any],
    metadata: dict[str, Any] | None,
    official_hosts: frozenset[str] | set[str],
) -> tuple[list[dict[str, Any]], int]:
    """For a finished work, numbers past its total are not chapters of it.

    Aggregators number Blade of the Phantom Master to 284 - magazine
    instalments of a work whose books hold 76 chapters. The download gate
    already stops at the catalogue total for an ended work (see
    ``official_frontier``); this makes the index say the same thing, so a
    complete series is not shown with 208 "missing" chapters that nothing
    will ever try to fetch. Files already on disk are kept: a catalogue that
    is off by one must never hide a chapter the operator has.
    """

    from tankarr.source_ranking import ENDED_STATUSES, is_official_release

    status = str((metadata or {}).get("status") or manga.get("status") or "")
    if status.casefold() not in ENDED_STATUSES:
        return selected, 0
    total = None
    if str(manga.get("expected_count_unit_override") or "").casefold() == "chapter":
        total = _positive_int(manga.get("expected_count_override"))
    if total is None:
        from tankarr.catalogue_consensus import count_is_reliable

        if not count_is_reliable(metadata, "chapter"):
            return selected, 0  # a lone or contested count is not a total
        total = _positive_int((metadata or {}).get("chapter_count"))
    if total is None:
        return selected, 0
    kept: list[dict[str, Any]] = []
    dropped = 0
    hosts = frozenset(official_hosts)
    for release in selected:
        number = _number(release.get("chapter"))
        if (
            number is not None
            and number > total
            and not release.get("downloaded")
            and not (hosts and is_official_release(release, hosts))
        ):
            dropped += 1
            continue
        kept.append(release)
    return kept, dropped


def select_releases(
    manga: dict[str, Any],
    metadata: dict[str, Any] | None,
    releases: Iterable[dict[str, Any]],
    canonical_labels: frozenset[str] | set[str] = frozenset(),
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Resolve the unit and return only the releases that belong to it."""

    items = [dict(release) for release in releases]
    context = _context(manga)
    normalized, normalization_dropped = normalize_chapter_releases(items)
    coverage = coverage_for_units(
        manga,
        metadata,
        items,
        context.get("indexer_volumes"),
        context.get("unobtainable_volumes"),
        normalized_chapters=normalized,
        pending_volumes=context.get("pending_volumes"),
    )
    # Resolving the unit and reporting its coverage use exactly the same
    # evidence. Recomputing it here normalized and ranked the whole series
    # three times per Library/Wanted render.
    unit_info = resolve_series_unit(
        {**manga, "_unit_context": context}, metadata, items, coverage=coverage
    )
    if unit_info["unit"] == "volumes":
        selected = [
            item
            for item in items
            if is_volume_release(item) and _positive_int(item.get("volume")) is not None
        ]
        dropped = (
            {"not_a_volume": len(items) - len(selected)}
            if len(items) != len(selected)
            else {}
        )
    else:
        selected, dropped = (
            normalize_chapter_releases(items, canonical_labels)
            if canonical_labels
            else (list(normalized), dict(normalization_dropped))
        )
        from tankarr.source_ranking import official_hosts as _official_hosts

        hosts = _official_hosts(
            (metadata or {}).get("official_links"),
            language=str(manga.get("preferred_language") or ""),
        )
        selected, unconfirmed = confirm_chapter_numbers(selected, official_hosts=hosts)
        if unconfirmed:
            dropped = {**dropped, "unconfirmed": unconfirmed}
        prologue = _prologue_releases(items, selected, metadata, hosts)
        if prologue:
            selected = [*prologue, *selected]
            dropped = {
                **dropped,
                "chapter_zero_or_negative": max(
                    0, int(dropped.get("chapter_zero_or_negative", 0)) - len(prologue)
                ),
            }
        selected, ahead = _drop_beyond_official_edition(selected, hosts)
        if ahead:
            dropped = {**dropped, "beyond_official_edition": ahead}
        selected, past_total = _drop_beyond_catalogue_total(
            selected, manga, metadata, hosts
        )
        if past_total:
            dropped = {**dropped, "beyond_catalogue_total": past_total}
    unit_info = {
        **unit_info,
        "dropped": dropped,
        "coverage": {
            unit: {"available": values["available"], "expected": values["expected"]}
            for unit, values in coverage.items()
        },
    }
    return unit_info, selected


def unit_coverage(
    manga: dict[str, Any],
    releases: Iterable[dict[str, Any]],
    metadata: dict[str, Any] | None,
    indexer_volumes: set[int] | None = None,
    unobtainable_volumes: set[int] | None = None,
) -> dict[str, dict[str, int | None]]:
    """How complete each unit could be from the mapped sources (and indexers)."""

    cov = coverage_for_units(
        manga, metadata, list(releases), indexer_volumes, unobtainable_volumes
    )
    return {
        "chapters": {
            "available": cov["chapters"]["available"],
            "expected": cov["chapters"]["expected"],
        },
        "volumes": {
            "available": cov["volumes"]["available"],
            "expected": cov["volumes"]["expected"],
        },
    }


__all__ = [
    "AUTOMATIC",
    "coverage_for_units",
    "SERIES_UNITS",
    "is_chapter_release",
    "is_volume_release",
    "normalize_chapter_releases",
    "normalize_series_unit",
    "resolve_series_unit",
    "select_releases",
    "unit_coverage",
]
