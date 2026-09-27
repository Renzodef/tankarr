"""Map provider-local episode indexes onto canonical chapter slots.

Raw provider numbers are identities, not cross-provider chapter numbers.  This
module only promotes them when there is explicit title evidence, an official
anchor, or a coherent content-aligned segment.  Ambiguous rows stay visible but
cannot extend the canonical sequence.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
from typing import Any

from tankarr.chapter_mapping import canonical_number
from tankarr.source_numbering import is_side_story, is_special_title, title_number
from tankarr.source_ranking import source_role_for_release

NUMBERING_VERSION = 6
MIN_CONTENT_ANCHORS = 5
MIN_IDENTITY_ANCHORS = 3


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


def reconcile_numbering(
    releases: list[dict[str, Any]],
    *,
    source_roles: dict[str, str] | None = None,
    official_hosts: frozenset[str] = frozenset(),
    secondary_evidence: list[dict[str, Any]] | None = None,
    canonical_labels: frozenset[str] | set[str] = frozenset(),
    canonical_end: int | None = None,
) -> dict[str, dict[str, Any]]:
    """Return deterministic numbering decisions keyed by release id.

    ``canonical_labels`` are decimal chapters the work's map places inside a
    volume, or that independent sources agree on: they are chapters and map
    to themselves, whatever their titles say.
    """

    # Compatibility for callers predating edition roles.  Production always
    # passes roles derived from MangaBaka's official links.
    if source_roles is None:
        source_roles = {str(host): "primary_official" for host in official_hosts}

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
        if stored_version < NUMBERING_VERSION:
            if status in {"pending_evidence", "unmapped", "ambiguous"}:
                canonical = None
                status = "pending_evidence"
                method = "awaiting_official_evidence"
                confidence = 0.0
            else:
                canonical = source
                status = (
                    "mapped"
                    if canonical_number(source) is not None
                    else "pending_evidence"
                )
                method = "source_identity"
                confidence = 0.6 if status == "mapped" else 0.0
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

    releases_by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for release in chapter_releases:
        releases_by_source[_source_identity(release)].append(release)
    for source, source_releases in releases_by_source.items():
        explicit: list[tuple[int, int, dict[str, Any]]] = []
        for release in source_releases:
            current = decisions[str(release["id"])]
            if current["numbering_method"] == "special_title":
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
            if len(run) < MIN_CONTENT_ANCHORS:
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

    primary_official = [
        release
        for release in chapter_releases
        if source_role_for_release(release, source_roles) == "primary_official"
    ]
    secondary_official = [
        release
        for release in chapter_releases
        if source_role_for_release(release, source_roles) == "secondary_official"
    ]
    for release in primary_official:
        release_id = str(release["id"])
        current = decisions[release_id]
        if current["numbering_method"] == "special_title":
            continue
        written_number = title_number(release.get("title"))
        canonical = (
            written_number or current["canonical_chapter"] or current["source_chapter"]
        )
        if canonical is None:
            continue
        explicit = written_number is not None or str(
            current["numbering_method"]
        ).startswith("title_")
        decisions[release_id] = _decision(
            release,
            canonical,
            status="mapped",
            method="official_title" if explicit else "official_source",
            confidence=1.0 if explicit else 0.9,
            evidence={
                "source_chapter": current["source_chapter"],
                "title_chapter": canonical_number(written_number),
                "official": True,
                "title": str(release.get("title") or ""),
            },
        )

    official_by_number: dict[int, dict[str, Any]] = {}
    for release in primary_official:
        decision = decisions[str(release["id"])]
        number = _integer(decision["canonical_chapter"])
        if number is None:
            continue
        incumbent = official_by_number.get(number)
        if incumbent is None or int(release.get("pages") or 0) > int(
            incumbent.get("pages") or 0
        ):
            official_by_number[number] = release
    if not official_by_number:
        return decisions

    official_head = max(official_by_number)
    by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for release in chapter_releases:
        if release not in primary_official:
            by_source[_source_identity(release)].append(release)

    # The original-language publisher edition is metadata evidence only.  It
    # establishes a bridge only after it agrees with the managed edition on a
    # coherent prefix.  A mirror can never manufacture this evidence.
    secondary_numbers: set[int] = set()
    for release in secondary_official:
        decision = decisions[str(release["id"])]
        if decision["numbering_method"] == "special_title":
            continue
        source_number = _integer(decision["source_chapter"])
        if (
            source_number is not None
            and source_number in official_by_number
            and decision["canonical_chapter"] == decision["source_chapter"]
        ):
            secondary_numbers.add(source_number)
    secondary_evidence_numbers: set[int] = set()
    for item in secondary_evidence or []:
        host = str(item.get("host") or "").casefold().removeprefix("www.")
        if source_roles.get(host) != "secondary_official" or is_special_title(
            item.get("title")
        ):
            continue
        number = _integer(item.get("edition_chapter", item.get("source_chapter")))
        if number is not None:
            secondary_evidence_numbers.add(number)
            if number in official_by_number:
                secondary_numbers.add(number)
    secondary_bridge_stable = (
        len(_longest_consecutive(list(secondary_numbers))) >= MIN_IDENTITY_ANCHORS
    )
    if secondary_bridge_stable:
        secondary_source_numbers = {
            number
            for release in secondary_official
            if (decision := decisions[str(release["id"])])["numbering_method"]
            != "special_title"
            and (number := _integer(decision["source_chapter"])) is not None
        }
        secondary_continuation_numbers = (
            secondary_source_numbers | secondary_evidence_numbers
        )
        secondary_head = official_head
        while secondary_head + 1 in secondary_continuation_numbers:
            secondary_head += 1
        secondary_numbers.update(
            number
            for number in secondary_continuation_numbers
            if number <= secondary_head
        )
        for release in secondary_official:
            release_id = str(release["id"])
            source_number = _integer(decisions[release_id]["source_chapter"])
            if source_number is None or source_number > secondary_head:
                continue
            if source_number > official_head:
                decisions[release_id] = _decision(
                    release,
                    source_number,
                    status="mapped",
                    method="secondary_official_continuation",
                    confidence=0.95,
                    evidence={
                        "primary_official_head": official_head,
                        "secondary_bridge_anchors": len(secondary_numbers),
                        "secondary_official_head": secondary_head,
                    },
                )
                secondary_numbers.add(source_number)

    identity_anchors_by_source: dict[str, set[int]] = {
        source: {
            number
            for release in source_releases
            if (decision := decisions[str(release["id"])])["numbering_method"]
            != "special_title"
            and (number := _integer(decision["source_chapter"])) in official_by_number
            and decision["canonical_chapter"] == decision["source_chapter"]
        }
        for source, source_releases in by_source.items()
    }

    # First align coherent page-count sequences.  Matching one page count is
    # weak; five consecutive chapters with one offset is strong and also
    # bounds the segment, so a later numbering reset cannot inherit it.
    canonical_by_pages: dict[int, list[tuple[int, str]]] = defaultdict(list)
    for number, release in official_by_number.items():
        pages = int(release.get("pages") or 0)
        if pages >= 5:
            canonical_by_pages[pages].append((number, "official"))
    # A source with three equal-number official anchors can lend its downloaded
    # page counts to the resolver when the official API does not expose them.
    # The source being aligned is never allowed to anchor itself.
    for source, source_releases in by_source.items():
        if len(identity_anchors_by_source[source]) < MIN_IDENTITY_ANCHORS:
            continue
        for release in source_releases:
            source_number = _integer(decisions[str(release["id"])]["source_chapter"])
            pages = int(release.get("pages") or 0)
            if source_number in official_by_number and pages >= 5:
                canonical_by_pages[pages].append((source_number, source))
    for source, source_releases in by_source.items():
        offsets: Counter[int] = Counter()
        rows_by_source_number: dict[int, dict[str, Any]] = {}
        for release in source_releases:
            release_id = str(release["id"])
            decision = decisions[release_id]
            if str(decision["numbering_method"]) == "special_title" or str(
                decision["numbering_method"]
            ).startswith(("official_title", "title_")):
                continue
            source_number = _integer(decision["source_chapter"])
            pages = int(release.get("pages") or 0)
            if source_number is None or pages < 5:
                continue
            rows_by_source_number.setdefault(source_number, release)
            for canonical, reference_source in canonical_by_pages.get(pages, []):
                if reference_source != source:
                    offsets[source_number - canonical] += 1
        for offset, support in offsets.most_common():
            if support < MIN_CONTENT_ANCHORS:
                break
            matched = [
                source_number
                for source_number, release in rows_by_source_number.items()
                if (canonical := source_number - offset) in official_by_number
                and any(
                    anchor == canonical and reference_source != source
                    for anchor, reference_source in canonical_by_pages.get(
                        int(release.get("pages") or 0), []
                    )
                )
            ]
            run = _longest_consecutive(matched)
            if len(run) < MIN_CONTENT_ANCHORS:
                continue
            start, end = run[0], run[-1]
            for release in source_releases:
                release_id = str(release["id"])
                current = decisions[release_id]
                if str(current["numbering_method"]) == "special_title" or str(
                    current["numbering_method"]
                ).startswith(("official_title", "title_")):
                    continue
                source_number = _integer(current["source_chapter"])
                if source_number is None or not start <= source_number <= end:
                    continue
                canonical = source_number - offset
                if canonical not in official_by_number:
                    continue
                decisions[release_id] = _decision(
                    release,
                    canonical,
                    status="mapped",
                    method="content_sequence_alignment",
                    confidence=min(0.99, 0.8 + len(run) * 0.01),
                    evidence={
                        "source": source,
                        "offset": offset,
                        "anchors": len(run),
                        "source_range": [start, end],
                        "official_range": [start - offset, end - offset],
                    },
                )
            break

    # Three equal-numbered anchors establish a source identity.  A continuation
    # is stronger: the anchors must reach the official head, the source must
    # proceed without holes, and no title/page evidence may show a numbering
    # offset.  Mapping that identity is separate from deciding whether policy
    # allows the chapter to be acquired; ``official_frontier`` owns that gate.
    for source, source_releases in by_source.items():
        identity_anchors = identity_anchors_by_source[source]
        identity_verified = len(identity_anchors) >= MIN_IDENTITY_ANCHORS
        identity_run = _longest_consecutive(list(identity_anchors))
        continuation_verified = (
            len(identity_run) >= MIN_IDENTITY_ANCHORS
            and identity_run[-1] == official_head
        )
        source_numbers = {
            number
            for release in source_releases
            if (decision := decisions[str(release["id"])])["numbering_method"]
            != "special_title"
            and (number := _integer(decision["source_chapter"])) is not None
        }
        continuation_head = official_head
        while continuation_head + 1 in source_numbers:
            continuation_head += 1
        drift_detected = any(
            (
                str(decision["numbering_method"]) == "special_title"
                and _integer(decision.get("source_chapter")) is not None
            )
            or (
                str(decision["numbering_method"]) == "content_sequence_alignment"
                and int((decision.get("numbering_evidence") or {}).get("offset") or 0)
                != 0
            )
            or (
                str(decision["numbering_method"]) == "title_sequence"
                and decision.get("canonical_chapter") != decision.get("source_chapter")
            )
            for release in source_releases
            if (decision := decisions[str(release["id"])])
        )
        for release in source_releases:
            release_id = str(release["id"])
            current = decisions[release_id]
            method = str(current["numbering_method"])
            if method in {
                "special_title",
                "official_title",
                "title_sequence",
                "catalogue_bounded_title",
                "content_sequence_alignment",
                "secondary_official_continuation",
            }:
                continue
            source_number = _integer(current["source_chapter"])
            if source_number is None:
                continue
            if source_number > official_head:
                lead = source_number - official_head
                if secondary_bridge_stable and source_number in secondary_numbers:
                    decisions[release_id] = _decision(
                        release,
                        source_number,
                        status="mapped",
                        method="secondary_official_bridge",
                        confidence=0.9,
                        evidence={
                            "source": source,
                            "primary_official_head": official_head,
                            "secondary_bridge_anchors": len(secondary_numbers),
                            "early_lead": lead,
                        },
                    )
                elif (
                    continuation_verified
                    and not drift_detected
                    and source_number <= continuation_head
                ):
                    decisions[release_id] = _decision(
                        release,
                        source_number,
                        status="mapped",
                        method="verified_identity_continuation",
                        confidence=0.72,
                        evidence={
                            "source": source,
                            "identity_anchors": len(identity_anchors),
                            "primary_official_head": official_head,
                            "verified_continuation_head": continuation_head,
                            "early_lead": lead,
                        },
                    )
                else:
                    decisions[release_id] = _decision(
                        release,
                        None,
                        status="pending_evidence",
                        method="awaiting_secondary_official_evidence",
                        confidence=0.0,
                        evidence={
                            "source": source,
                            "source_chapter": source_number,
                            "primary_official_head": official_head,
                            "secondary_official_bridge": secondary_bridge_stable,
                            "identity_anchors": len(identity_anchors),
                            "verified_continuation": continuation_verified,
                            "verified_continuation_head": continuation_head,
                            "drift_detected": drift_detected,
                        },
                    )
            elif source_number in official_by_number or identity_verified:
                decisions[release_id] = _decision(
                    release,
                    source_number,
                    status="mapped",
                    method="official_number_match",
                    confidence=0.85 if source_number in official_by_number else 0.75,
                    evidence={
                        "source": source,
                        "identity_anchors": len(identity_anchors),
                        "official_head": official_head,
                    },
                )

    return decisions


__all__ = [
    "MIN_CONTENT_ANCHORS",
    "NUMBERING_VERSION",
    "reconcile_numbering",
]
