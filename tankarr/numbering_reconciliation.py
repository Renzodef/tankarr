"""Map provider-local episode indexes onto canonical chapter slots.

Raw provider numbers are identities, not cross-provider chapter numbers.  This
module uses source identities, explicit title evidence and coherent content
sequences. Ambiguous rows stay visible but cannot extend the canonical sequence.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
from typing import Any

from tankarr.chapter_mapping import canonical_number
from tankarr.source_numbering import (
    is_season_zero_episode,
    is_side_story,
    is_special_title,
    title_number,
)

NUMBERING_VERSION = 7
MIN_CONTENT_ANCHORS = 10
MIN_TITLE_ANCHORS = 5


def _integer(value: object) -> int | None:
    normalized = canonical_number(value)
    if normalized is None:
        return None
    try:
        number = Decimal(normalized)
    except (InvalidOperation, ValueError):
        return None
    if number <= 0 or number != number.to_integral_value():
        return None
    return int(number)


def _source_identity(release: dict[str, Any]) -> str:
    return str(
        release.get("source_key")
        or release.get("source_name")
        or release.get("provider")
        or "unknown"
    )


def _decision(
    release: dict[str, Any],
    canonical: object,
    *,
    status: str,
    method: str,
    confidence: float,
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    source_chapter = canonical_number(
        release.get("source_chapter", release.get("chapter"))
    )
    edition_chapter = canonical_number(release.get("edition_chapter"))
    if edition_chapter is None:
        edition_chapter = canonical_number(title_number(release.get("title")))
    if edition_chapter is None:
        edition_chapter = source_chapter
    mapped = status == "mapped"
    return {
        "id": str(release["id"]),
        "source_chapter": source_chapter,
        "edition_chapter": edition_chapter,
        "canonical_chapter": canonical_number(canonical),
        "primary_chapter": canonical_number(canonical) if mapped else None,
        "numbering_status": status,
        "numbering_method": method,
        "numbering_confidence": max(0.0, min(1.0, float(confidence))),
        "numbering_evidence": evidence or {},
        "numbering_version": NUMBERING_VERSION,
    }


def _longest_consecutive(values: list[int]) -> list[int]:
    best: list[int] = []
    current: list[int] = []
    for value in sorted(set(values)):
        if current and value != current[-1] + 1:
            if len(current) > len(best):
                best = current
            current = []
        current.append(value)
    return current if len(current) > len(best) else best


def _page_sequences(pages: dict[int, int]) -> dict[tuple[int, ...], int | None]:
    """Index distinctive consecutive page-count patterns in linear space.

    Uniform or repeated patterns cannot prove chapter identity. Fixed-size
    lookups avoid a quadratic cross-product on long or uniform source feeds.
    """
    patterns: dict[tuple[int, ...], int | None] = {}
    for start in sorted(pages):
        pattern = tuple(
            pages.get(start + step, 0) for step in range(MIN_CONTENT_ANCHORS)
        )
        if 0 in pattern or len(set(pattern)) < 4:
            continue
        patterns[pattern] = start if pattern not in patterns else None
    return patterns


def reconcile_numbering(
    releases: list[dict[str, Any]],
    *,
    canonical_labels: frozenset[str] | set[str] = frozenset(),
    canonical_end: int | None = None,
) -> dict[str, dict[str, Any]]:
    """Return deterministic numbering decisions keyed by release id.

    ``canonical_labels`` are decimal chapters the work's map places inside a
    volume, or that independent sources agree on: they are chapters and map
    to themselves, whatever their titles say.
    """

    decisions: dict[str, dict[str, Any]] = {}
    chapter_releases = [
        release
        for release in releases
        if str(release.get("release_unit") or "chapter") == "chapter"
    ]
    for release in chapter_releases:
        source = release.get("source_chapter", release.get("chapter"))
        canonical = release.get("canonical_chapter", release.get("chapter"))
        method = str(release.get("numbering_method") or "legacy")
        status = str(release.get("numbering_status") or "mapped")
        confidence = float(release.get("numbering_confidence") or 0.5)
        stored_version = int(release.get("numbering_version") or 0)
        evidence = dict(release.get("numbering_evidence") or {})
        preserve_content = (
            status == "mapped"
            and canonical is not None
            and (
                bool(release.get("downloaded"))
                or method
                in {
                    "content_sequence_alignment",
                    "operator_correspondence",
                    "canonical_decimal",
                }
            )
        )
        if stored_version < NUMBERING_VERSION and not preserve_content:
            # Retired publisher gates must not leave stored releases permanently
            # pending. Rebuild from source identity; title/content evidence and
            # explicit operator correspondences are applied below.
            canonical = source
            status = (
                "mapped" if canonical_number(source) is not None else "pending_evidence"
            )
            method = "source_identity"
            confidence = 0.6 if status == "mapped" else 0.0
            evidence = {
                "source_chapter": canonical_number(source),
                "reconciled_from_version": stored_version,
            }
        decisions[str(release["id"])] = _decision(
            release,
            canonical,
            status=status,
            method=method,
            confidence=confidence,
            evidence=evidence,
        )
        decisions[str(release["id"])]["source_chapter"] = canonical_number(source)
        if is_special_title(release.get("title")) or is_side_story(
            release.get("title"), source
        ):
            decisions[str(release["id"])] = _decision(
                release,
                None,
                status="unmapped",
                method="special_title",
                confidence=1.0,
                evidence={"title": str(release.get("title") or "")},
            )
        label = canonical_number(source)
        if label is not None and label in canonical_labels:
            decisions[str(release["id"])] = _decision(
                release,
                label,
                status="mapped",
                method="canonical_decimal",
                confidence=1.0,
                evidence={"source_chapter": label, "canonical": "map_or_sources"},
            )
            decisions[str(release["id"])]["source_chapter"] = label
            continue
        # A finished catalogue's corroborated size proves that an explicitly
        # named, same-number release is inside the work.  This repairs old
        # pending rows without letting a mirror extend the work past that
        # boundary (or turn numbered prologues/extras into ordinary chapters).
        current = decisions[str(release["id"])]
        source_integer = _integer(label)
        title_integer = _integer(title_number(release.get("title")))
        if (
            current["numbering_method"] != "special_title"
            and current["numbering_status"]
            in {"pending_evidence", "unmapped", "ambiguous"}
            and canonical_end is not None
            and source_integer is not None
            and source_integer == title_integer
            and source_integer <= canonical_end
        ):
            decisions[str(release["id"])] = _decision(
                release,
                source_integer,
                status="mapped",
                method="catalogue_bounded_title",
                confidence=0.95,
                evidence={
                    "source_chapter": source_integer,
                    "title_chapter": title_integer,
                    "catalogue_end": canonical_end,
                },
            )

    stable_owned = {
        str(release["id"])
        for release in chapter_releases
        if release.get("downloaded")
        and str(release.get("numbering_status") or "mapped") == "mapped"
        and release.get("canonical_chapter", release.get("chapter")) is not None
        and decisions[str(release["id"])]["numbering_status"] == "mapped"
    }

    releases_by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for release in chapter_releases:
        releases_by_source[_source_identity(release)].append(release)
    for source, source_releases in releases_by_source.items():
        explicit: list[tuple[int, int, dict[str, Any]]] = []
        for release in source_releases:
            current = decisions[str(release["id"])]
            if (
                str(release["id"]) in stable_owned
                or current["numbering_method"] == "special_title"
            ):
                continue
            source_number = _integer(decisions[str(release["id"])]["source_chapter"])
            written_number = title_number(release.get("title"))
            title_integer = _integer(written_number)
            if (
                source_number is not None
                and title_integer is not None
                and (
                    source_number != title_integer
                    or current["numbering_status"] == "provisional"
                    or current["numbering_method"] == "rank_after_specials"
                    or release.get("numbering_status") == "provisional"
                    or release.get("numbering_method") == "rank_after_specials"
                )
            ):
                explicit.append((source_number, title_integer, release))
        ordered = sorted(explicit, key=lambda item: item[0])
        runs: list[list[tuple[int, int, dict[str, Any]]]] = []
        for item in ordered:
            if (
                runs
                and item[0] == runs[-1][-1][0] + 1
                and item[1] == runs[-1][-1][1] + 1
            ):
                runs[-1].append(item)
            else:
                runs.append([item])
        for run in runs:
            if len(run) < MIN_TITLE_ANCHORS:
                continue
            for source_number, canonical, release in run:
                decisions[str(release["id"])] = _decision(
                    release,
                    canonical,
                    status="mapped",
                    method="title_sequence",
                    confidence=0.95,
                    evidence={
                        "source": source,
                        "source_chapter": source_number,
                        "canonical_chapter": canonical,
                        "title": str(release.get("title") or ""),
                        "anchors": len(run),
                        "source_range": [run[0][0], run[-1][0]],
                        "canonical_range": [run[0][1], run[-1][1]],
                    },
                )

    # Every configured source gets the same title interpretation. Provider
    # episode indexes are not chapter numbers when the title explicitly says
    # otherwise. Repeated season openers keep their own fractional slot.
    for source, source_releases in releases_by_source.items():
        title_counts = Counter(
            title_number(release.get("title")) for release in source_releases
        )
        for release in source_releases:
            current = decisions[str(release["id"])]
            if str(release["id"]) in stable_owned or current["numbering_method"] in {
                "special_title",
                "content_sequence_alignment",
                "operator_correspondence",
                "canonical_decimal",
            }:
                continue
            written = title_number(release.get("title"))
            if written is None:
                continue
            opening = (
                title_counts[written] > 1
                and written + Decimal("0.5") not in title_counts
                and is_season_zero_episode(release.get("title"))
            )
            # A coherent title run was handled above. A single conflicting
            # title only overrides the provider index when it contains a
            # deliberate chapter annotation, such as "(ch. 393)".
            annotated = bool(
                re.search(
                    r"\(ch(?:apter)?\.?\s*[-+]?\d",
                    str(release.get("title") or ""),
                    re.I,
                )
            )
            source_label = canonical_number(current["source_chapter"])
            reset_index = (
                source_label is None or Decimal(source_label) <= 0
            ) and written > 0
            previous_manual = release.get(
                "numbering_method"
            ) == "manual" and canonical_number(
                release.get("canonical_chapter")
            ) == canonical_number(written)
            if (
                not opening
                and not annotated
                and not reset_index
                and not previous_manual
            ):
                continue
            canonical = written + Decimal("0.5") if opening else written
            if (
                not opening
                and canonical_number(canonical) == current["canonical_chapter"]
                and current["numbering_status"] == "mapped"
            ):
                continue
            decisions[str(release["id"])] = _decision(
                release,
                canonical,
                status="mapped",
                method="title_season_opening" if opening else "explicit_title",
                confidence=0.95,
                evidence={
                    "source": source,
                    "title": str(release.get("title") or ""),
                    "title_chapter": canonical_number(written),
                },
            )

    # Preserve content-based duplicate detection without choosing an official
    # edition. Prefer a source already represented on disk, then the most
    # extensively numbered source. A short reference never caps another feed.
    if not releases_by_source:
        return decisions
    reference = max(
        releases_by_source,
        key=lambda source: (
            sum(bool(r.get("downloaded")) for r in releases_by_source[source]),
            sum(int(r.get("pages") or 0) >= 5 for r in releases_by_source[source]),
            sum(
                decisions[str(r["id"])]["numbering_status"] == "mapped"
                for r in releases_by_source[source]
            ),
            source,
        ),
    )
    reference_pages: dict[int, int] = {}
    for release in releases_by_source[reference]:
        decision = decisions[str(release["id"])]
        number = _integer(decision["canonical_chapter"])
        pages = int(release.get("pages") or 0)
        if (
            decision["numbering_status"] == "mapped"
            and number is not None
            and pages >= 5
        ):
            reference_pages.setdefault(number, pages)
    reference_patterns = _page_sequences(reference_pages)
    if not reference_patterns:
        return decisions
    for source, source_releases in releases_by_source.items():
        if source == reference:
            continue
        rows: dict[int, dict[str, Any]] = {}
        for release in source_releases:
            current = decisions[str(release["id"])]
            if str(release["id"]) in stable_owned or current["numbering_method"] in {
                "special_title",
                "explicit_title",
                "title_season_opening",
                "canonical_decimal",
                "content_sequence_alignment",
                "operator_correspondence",
            }:
                continue
            number = _integer(current["source_chapter"])
            if number is not None and int(release.get("pages") or 0) >= 5:
                rows.setdefault(number, release)
        patterns = _page_sequences({n: int(r["pages"]) for n, r in rows.items()})
        matches: dict[int, set[int]] = defaultdict(set)
        for pattern, start in patterns.items():
            canonical_start = reference_patterns.get(pattern)
            if start is None or canonical_start is None:
                continue
            for step in range(MIN_CONTENT_ANCHORS):
                matches[start + step].add(canonical_start + step)
        offsets: dict[int, list[int]] = defaultdict(list)
        for number, candidates in matches.items():
            if len(candidates) == 1:
                offsets[number - next(iter(candidates))].append(number)
        drift = False
        aligned: set[int] = set()
        for offset, numbers in sorted(offsets.items()):
            run = _longest_consecutive(numbers)
            if len(run) < MIN_CONTENT_ANCHORS:
                continue
            drift = drift or offset != 0
            aligned.update(run)
            for number in run:
                release = rows[number]
                decisions[str(release["id"])] = _decision(
                    release,
                    number - offset,
                    status="mapped",
                    method="content_sequence_alignment",
                    confidence=min(0.99, 0.8 + len(run) * 0.01),
                    evidence={
                        "source": source,
                        "reference_source": reference,
                        "offset": offset,
                        "anchors": len(run),
                        "source_range": [run[0], run[-1]],
                        "canonical_range": [run[0] - offset, run[-1] - offset],
                    },
                )
        if drift:
            # A proven numbering mismatch requires content evidence outside
            # its verified segment too. Never invent an offset for a new tail
            # or let it masquerade as new chapters. Owned files stay intact.
            for number, release in rows.items():
                if number in aligned or release.get("downloaded"):
                    continue
                decisions[str(release["id"])] = _decision(
                    release,
                    None,
                    status="pending_evidence",
                    method="awaiting_content_alignment",
                    confidence=0.0,
                    evidence={"source": source, "reference_source": reference},
                )
    return decisions


__all__ = ["MIN_CONTENT_ANCHORS", "NUMBERING_VERSION", "reconcile_numbering"]
