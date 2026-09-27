from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from tankarr.database import logical_chapter_coverage
from tankarr.monitoring import TERMINAL_PUBLICATION_STATUSES
from tankarr.series_summary import CONTINUING_STATUSES


def _positive_integer(value: object) -> int | None:
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    if number <= 0 or number != number.to_integral_value():
        return None
    return int(number)


def _chapter_number(value: object) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None


def _chapter_ranges(values: set[int]) -> list[str]:
    if not values:
        return []
    ordered = sorted(values)
    ranges: list[str] = []
    start = previous = ordered[0]
    for value in ordered[1:]:
        if value == previous + 1:
            previous = value
            continue
        ranges.append(str(start) if start == previous else f"{start}–{previous}")
        start = previous = value
    ranges.append(str(start) if start == previous else f"{start}–{previous}")
    return ranges


def _provider_label(manga: dict[str, Any]) -> str:
    provider = str(manga.get("provider") or "download source")
    source_name = str(manga.get("source_name") or "").strip()
    if provider == "suwayomi" and source_name:
        return f"Suwayomi → {source_name}"
    return {
        "mangadex": "MangaDex",
        "mangapill": "MangaPill",
        "suwayomi": "Suwayomi",
    }.get(provider, source_name or provider.replace("_", " ").title())


def assess_chapter_completeness(
    manga: dict[str, Any],
    chapters: list[dict[str, Any]],
    *,
    language: str,
    available_chapter_count: int,
    source_checks: list[dict[str, Any]],
) -> dict[str, Any]:
    """Compare one translated feed with independently matched work metadata.

    A provider's final chapter is useful context, but it cannot independently
    prove that its own feed has no holes.  A definitive expected total therefore
    requires at least one accepted external catalogue match for a terminal work.
    """

    public_sources: list[dict[str, Any]] = []
    external_claims: list[tuple[str, str, int]] = []
    terminal_matches: list[tuple[str, str]] = []
    continuing_matches: list[tuple[str, str, str]] = []
    for check in source_checks:
        record = check.get("record") or {}
        publication_status = str(record.get("status") or "").strip().casefold()
        chapter_count = _positive_integer(record.get("chapter_count"))
        matched = check.get("state") == "matched" and bool(record)
        public_sources.append(
            {
                "name": str(check.get("name") or record.get("source") or "metadata"),
                "label": str(check.get("label") or record.get("source") or "Metadata"),
                "state": str(check.get("state") or "error"),
                "matched": matched,
                "chapter_count": chapter_count if matched else None,
                "publication_status": publication_status or None,
                "confidence": check.get("confidence") if matched else None,
                "reason": str(check.get("reason") or ""),
            }
        )
        if matched and publication_status in TERMINAL_PUBLICATION_STATUSES:
            identity = (
                str(check.get("name") or record.get("source") or "metadata"),
                str(check.get("label") or record.get("source") or "Metadata"),
            )
            terminal_matches.append(identity)
            if chapter_count is not None:
                external_claims.append((*identity, chapter_count))
        elif matched and publication_status in CONTINUING_STATUSES:
            continuing_matches.append(
                (
                    str(check.get("name") or record.get("source") or "metadata"),
                    str(check.get("label") or record.get("source") or "Metadata"),
                    publication_status,
                )
            )

    provider_status = str(manga.get("status") or "").strip().casefold()
    provider_claim = (
        _positive_integer(manga.get("last_chapter"))
        if provider_status in TERMINAL_PUBLICATION_STATUSES
        else None
    )
    provider_label = _provider_label(manga)
    language_code = language.strip().upper() or "SELECTED"
    matched_labels = [str(item["label"]) for item in public_sources if item["matched"]]
    verified_labels = [label for _name, label in terminal_matches]

    base = {
        "status": "unknown",
        "expected_chapter_count": None,
        "available_chapter_count": int(max(0, available_chapter_count)),
        "covered_chapter_count": None,
        "missing_chapter_count": None,
        "missing_chapter_ranges": [],
        "provider_claimed_chapter_count": provider_claim,
        "provider_label": provider_label,
        "metadata_sources": public_sources,
        "checked_source_count": len(source_checks),
        "matched_source_count": len(matched_labels),
        "verified_source_count": len(verified_labels),
        "verification_basis": None,
        "numbering_disagreement": False,
        "message": "",
    }

    claimed_totals = {count for _name, _label, count in external_claims}
    claims = " · ".join(f"{label}: {count}" for _name, label, count in external_claims)
    evidence = " and ".join(dict.fromkeys(verified_labels))

    if terminal_matches and continuing_matches:
        continuing = " and ".join(
            dict.fromkeys(label for _name, label, _status in continuing_matches)
        )
        return {
            **base,
            "status": "conflict",
            "message": (
                f"{evidence} marks the work as ended, while {continuing} still marks "
                "it as continuing. Publication status needs review."
            ),
        }

    if provider_claim is not None:
        if continuing_matches:
            continuing = " and ".join(
                dict.fromkeys(label for _name, label, _status in continuing_matches)
            )
            return {
                **base,
                "status": "conflict",
                "message": (
                    f"{provider_label} marks chapter {provider_claim} as final, while "
                    f"{continuing} still marks the work as continuing."
                ),
            }
        if not terminal_matches:
            return {
                **base,
                "message": (
                    f"{provider_label} marks chapter {provider_claim} as final, but no "
                    "matched metadata catalogue confirms that the work has ended. "
                    "Completeness is not independently confirmed."
                ),
            }
        expected = provider_claim
        verification_basis = "provider_final"
        numbering_disagreement = any(
            count != provider_claim for _name, _label, count in external_claims
        )
    else:
        if terminal_matches and provider_status in CONTINUING_STATUSES:
            return {
                **base,
                "status": "conflict",
                "message": (
                    f"{evidence} marks the work as ended, while {provider_label} marks "
                    f"the feed as {provider_status}. Publication status needs review."
                ),
            }
        if len(claimed_totals) > 1:
            return {
                **base,
                "status": "conflict",
                "message": (
                    f"Metadata catalogues disagree on the final total ({claims}). "
                    "Tankarr cannot confirm completeness."
                ),
            }
        if not claimed_totals:
            if terminal_matches:
                message = (
                    f"{evidence} confirms that the work has ended, but no matched "
                    "catalogue or download source publishes a final chapter number."
                )
            elif matched_labels:
                message = (
                    "Matched metadata does not publish a reliable final chapter total. "
                    f"Tankarr cannot confirm whether the {language_code} feed is complete."
                )
            elif source_checks:
                message = (
                    "No metadata catalogue could verify a final chapter total for this "
                    "work. Tankarr will not assume the feed is complete."
                )
            else:
                message = (
                    "No applicable metadata catalogue is enabled, so completeness "
                    "cannot be verified."
                )
            return {**base, "message": message}
        expected = next(iter(claimed_totals))
        verification_basis = "metadata_total"
        numbering_disagreement = False

    distinct_numbers = {
        number
        for chapter in chapters
        if (number := _chapter_number(chapter.get("chapter"))) is not None
    }
    integer_numbers = {
        int(number)
        for number in distinct_numbers
        if number == number.to_integral_value() and number >= 0
    }
    # Some catalogues split one canonical chapter across multiple releases,
    # for example 28.1 and 28.2.  Those parts collectively cover chapter 28;
    # treating only integral release numbers as coverage creates false gaps.
    # Require at least two distinct positive parts so a lone decimal special
    # (such as 107.5) cannot silently stand in for a missing main chapter.
    split_parts: dict[int, set[Decimal]] = {}
    for number in distinct_numbers:
        if number <= 0 or number == number.to_integral_value():
            continue
        chapter_base = int(number)
        split_parts.setdefault(chapter_base, set()).add(number)
    integer_numbers.update(
        chapter_base for chapter_base, parts in split_parts.items() if len(parts) >= 2
    )
    one_based = set(range(1, expected + 1))
    zero_based = set(range(0, expected))
    expected_numbers = (
        zero_based
        if len(integer_numbers & zero_based) > len(integer_numbers & one_based)
        else one_based
    )
    missing = expected_numbers - integer_numbers
    covered = expected - len(missing)
    numbered_ratio = len(distinct_numbers) / max(available_chapter_count, 1)
    logical_coverage = logical_chapter_coverage(
        {"chapter": str(number), "downloaded": True} for number in distinct_numbers
    )
    logical_entry_count = (
        logical_coverage["logical_chapter_count"]
        + logical_coverage["special_chapter_count"]
    )
    unexpected_high = {
        value for value in integer_numbers if value > max(expected_numbers)
    }

    assessed = {
        **base,
        "expected_chapter_count": expected,
        "covered_chapter_count": covered,
        "verification_basis": verification_basis,
        "numbering_disagreement": numbering_disagreement,
    }
    if unexpected_high and verification_basis == "provider_final":
        extras = ", ".join(_chapter_ranges(unexpected_high))
        message = (
            f"{provider_label} marks {expected} as the final translated chapter, "
            f"but the feed also contains higher chapter number(s): {extras}."
        )
        return {
            **assessed,
            "status": "conflict",
            "message": message,
        }

    if available_chapter_count == 0:
        ranges = _chapter_ranges(missing)
        return {
            **assessed,
            "status": "incomplete",
            "covered_chapter_count": 0,
            "missing_chapter_count": expected,
            "missing_chapter_ranges": ranges,
            "message": (
                (
                    f"{provider_label} defines final translated chapter {expected}"
                    if verification_basis == "provider_final"
                    else f"{evidence} expects {expected} chapters"
                )
                + f", but none are available in {language_code}."
            ),
        }

    # A feed made mostly of unnumbered entries cannot prove chapter-by-chapter
    # coverage merely because its raw item count happens to equal a catalogue total.
    if numbered_ratio < 0.8:
        if available_chapter_count < expected:
            missing_count = expected - available_chapter_count
            return {
                **assessed,
                "status": "incomplete",
                "covered_chapter_count": available_chapter_count,
                "missing_chapter_count": missing_count,
                "message": (
                    (
                        f"{provider_label} defines final translated chapter {expected}"
                        if verification_basis == "provider_final"
                        else f"{evidence} expects {expected} chapters"
                    )
                    + f"; this feed exposes only {available_chapter_count}. Its "
                    "numbering is insufficient to list the exact missing chapters."
                ),
            }
        return {
            **assessed,
            "status": "unknown",
            "covered_chapter_count": None,
            "message": (
                (
                    f"{provider_label} defines final translated chapter {expected}"
                    if verification_basis == "provider_final"
                    else f"{evidence} expects {expected} chapters"
                )
                + f" and the feed exposes {available_chapter_count} entries, but too "
                "many are unnumbered to prove complete coverage."
            ),
        }

    if verification_basis == "metadata_total":
        # A catalogue chapter_count is a cardinality, not a promise that the
        # provider must use the integral sequence 1..N. Numbered specials such
        # as 90.5 can be part of that count, while .1/.2 split releases count
        # once through logical_chapter_coverage().
        if logical_entry_count < expected:
            missing_count = expected - logical_entry_count
            return {
                **assessed,
                "status": "incomplete",
                "covered_chapter_count": logical_entry_count,
                "missing_chapter_count": missing_count,
                "missing_chapter_ranges": [],
                "message": (
                    f"{evidence} expects {expected} chapters; the {language_code} "
                    f"feed exposes {logical_entry_count} distinct chapters or "
                    "numbered specials. The catalogue total does not identify the "
                    "missing chapter numbers."
                ),
            }
        return {
            **assessed,
            "status": "complete",
            "covered_chapter_count": expected,
            "missing_chapter_count": 0,
            "missing_chapter_ranges": [],
            "message": (
                f"All {expected} expected chapters are represented in the "
                f"{language_code} feed, including numbered specials where the "
                f"provider segmentation differs. Verified against {evidence}."
            ),
        }

    if missing:
        ranges = _chapter_ranges(missing)
        return {
            **assessed,
            "status": "incomplete",
            "missing_chapter_count": len(missing),
            "missing_chapter_ranges": ranges,
            "message": (
                (
                    f"{provider_label} defines final translated chapter {expected}"
                    if verification_basis == "provider_final"
                    else f"{evidence} expects {expected} chapters"
                )
                + f"; {covered} canonical chapters are available in {language_code}."
            ),
        }

    if verification_basis == "provider_final":
        confirms = "confirms" if len(dict.fromkeys(verified_labels)) == 1 else "confirm"
        message = (
            f"{evidence} {confirms} that the work has ended. All translated chapter "
            f"numbers through {provider_label} final chapter {expected} are available "
            f"in {language_code}."
        )
        if numbering_disagreement:
            message += (
                f" Catalogue segmentation differs ({claims}); Tankarr uses the "
                "download source's numbering for gap detection."
            )
    else:
        message = (
            f"All {expected} expected chapters are available in {language_code}. "
            f"Verified against {evidence}."
        )
    return {
        **assessed,
        "status": "complete",
        "covered_chapter_count": expected,
        "missing_chapter_count": 0,
        "message": message,
    }
