"""What the official edition in the operator's language actually numbers.

``source_ranking.official_frontier`` answers "what may still be acquired",
and is deliberately permissive: for an ended work it falls back to the
catalogue's chapter count so a partially mapped official source cannot
condemn the tail of a finished series.

Removing a file that is already in the library is a different question and
needs different evidence. A catalogue total is a claim about the original
work; it is routinely wrong about a translation, and it must never be the
reason a readable file is deleted. So this module recomputes the frontier
from the official releases themselves and refuses to answer at all unless
the official source has shown enough of the work to be believed:

* at least ``MIN_OFFICIAL_ANCHORS`` numbered official releases - three mapped
  chapters say nothing about where the edition has got to;
* an unbroken tail: every integer in the ``TAIL_RUN`` chapters below the
  frontier is present, so one stray high number cannot become the frontier;
* a run that starts where the work does (chapter 0 or 1) and is near-complete
  over it. A source exposing only a recent window is internally consistent and
  says nothing about where the edition really ends, so it must not be allowed
  to place a head that files are then deleted against.

Even then a file is only surplus while it is *running ahead* of the edition
by a plausible margin. A release numbered 9999 is a mis-parse or a special,
not an episode the publisher has yet to reach, and the frontier has nothing
to say about it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from tankarr.chapter_mapping import canonical_number
from tankarr.source_ranking import is_official_release

MIN_OFFICIAL_ANCHORS = 20
TAIL_RUN = 10
MIN_RANGE_COVERAGE = 0.9
# The highest chapter an official run may start at and still count as the
# whole edition: 1 normally, 0 when the publisher numbers its prologue.
FIRST_CHAPTER = 1
MAX_PLAUSIBLE_LEAD = 200


@dataclass(frozen=True)
class FrontierVerdict:
    """The official head, and whether it is solid enough to remove files by."""

    frontier: Decimal | None = None
    anchors: int = 0
    enforceable: bool = False
    reason: str = "no official release in this language"
    hosts: tuple[str, ...] = field(default_factory=tuple)

    @property
    def label(self) -> str:
        if self.frontier is None:
            return ""
        return format(self.frontier.normalize(), "f")


def _number(value: object) -> Decimal | None:
    normalized = canonical_number(value)
    if normalized is None:
        return None
    try:
        number = Decimal(normalized)
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def official_chapter_frontier(
    releases: list[dict[str, Any]],
    official_hosts: frozenset[str] = frozenset(),
) -> FrontierVerdict:
    """The last chapter the official edition has published, with its warrant."""

    if not official_hosts:
        return FrontierVerdict(
            reason="the work has no official platform in this language"
        )
    numbers: list[Decimal] = []
    for release in releases:
        if not is_official_release(release, official_hosts):
            continue
        number = _number(release.get("chapter"))
        if number is not None:
            numbers.append(number)
    hosts = tuple(sorted(official_hosts))
    if not numbers:
        return FrontierVerdict(
            hosts=hosts, reason="the official source exposes no numbered chapter"
        )
    frontier = max(numbers)
    integers = {int(n) for n in numbers if n == n.to_integral_value()}
    verdict = FrontierVerdict(
        frontier=frontier, anchors=len(numbers), hosts=hosts, reason=""
    )
    if len(numbers) < MIN_OFFICIAL_ANCHORS:
        return FrontierVerdict(
            **{
                **verdict.__dict__,
                "reason": (
                    f"only {len(numbers)} official chapters are mapped, "
                    f"fewer than the {MIN_OFFICIAL_ANCHORS} needed to place the head"
                ),
            }
        )
    head = int(frontier) if frontier == frontier.to_integral_value() else None
    if head is None:
        return FrontierVerdict(
            **{**verdict.__dict__, "reason": "the official head is not a whole chapter"}
        )
    tail = [head - offset for offset in range(TAIL_RUN)]
    missing_tail = [number for number in tail if number > 0 and number not in integers]
    if missing_tail:
        return FrontierVerdict(
            **{
                **verdict.__dict__,
                "reason": (
                    "the official numbering has holes just below its head "
                    f"({', '.join(str(n) for n in sorted(missing_tail))})"
                ),
            }
        )
    low = min(integers)
    if low > FIRST_CHAPTER:
        return FrontierVerdict(
            **{
                **verdict.__dict__,
                "reason": (
                    f"the official source starts at chapter {low}, not at the "
                    "beginning of the work: it shows a window, not the edition"
                ),
            }
        )
    span = head - low + 1
    coverage = len(integers) / span if span > 0 else 0.0
    if coverage < MIN_RANGE_COVERAGE:
        return FrontierVerdict(
            **{
                **verdict.__dict__,
                "reason": (
                    f"the official source covers only {coverage:.0%} of chapters "
                    f"{low}-{head}: too partial to place the head"
                ),
            }
        )
    # A publisher's free sample of the opening chapters looks exactly like a
    # short complete edition: it starts at chapter 1, it has no holes, it
    # covers its own range. What tells them apart is what comes after. When
    # the other sources carry as much again beyond this head, and in an
    # unbroken run, the official listing is a window onto the work, not the
    # end of it, and nothing past it may be called surplus.
    beyond = sorted(
        {
            int(number)
            for release in releases
            if not is_official_release(release, official_hosts)
            for number in (_number(release.get("chapter")),)
            if number is not None
            and number == number.to_integral_value()
            and int(number) > head
        }
    )
    run = 0
    expected = head + 1
    for number in beyond:
        if number != expected:
            break
        run += 1
        expected += 1
    if run >= len(integers):
        return FrontierVerdict(
            **{
                **verdict.__dict__,
                "reason": (
                    f"chapters {head + 1}-{head + run} continue unbroken past the "
                    f"official head with {run} more chapters: the official source "
                    f"shows a window onto the work, not its end"
                ),
            }
        )
    return FrontierVerdict(
        **{
            **verdict.__dict__,
            "enforceable": True,
            "reason": (
                f"{len(numbers)} official chapters covering {low}-{head} "
                f"({coverage:.0%} of the range)"
            ),
        }
    )


def beyond_official_edition(release: dict[str, Any], verdict: FrontierVerdict) -> bool:
    """Whether this release numbers a chapter the official edition has not
    published, by a margin that reads as running ahead rather than as a
    different numbering altogether."""

    if not verdict.enforceable or verdict.frontier is None:
        return False
    number = _number(release.get("chapter"))
    if number is None:
        return False  # a volume, or an unnumbered release: not a claim
    return bool(
        verdict.frontier < number <= verdict.frontier + Decimal(MAX_PLAUSIBLE_LEAD)
    )


def surplus_downloads(
    releases: list[dict[str, Any]], verdict: FrontierVerdict
) -> list[dict[str, Any]]:
    """Library files numbered past the official edition's last chapter.

    One slot can hold several downloaded rows (a re-import, a second source);
    every one of them is surplus, because the slot itself does not exist in
    the edition this library follows.
    """

    if not verdict.enforceable:
        return []
    surplus = [
        release
        for release in releases
        if release.get("downloaded")
        and str(release.get("release_unit") or "chapter") == "chapter"
        and not is_official_release(release, frozenset(verdict.hosts))
        and beyond_official_edition(release, verdict)
    ]
    return sorted(surplus, key=lambda item: _number(item.get("chapter")) or Decimal(0))


__all__ = [
    "FIRST_CHAPTER",
    "MAX_PLAUSIBLE_LEAD",
    "MIN_OFFICIAL_ANCHORS",
    "MIN_RANGE_COVERAGE",
    "TAIL_RUN",
    "FrontierVerdict",
    "beyond_official_edition",
    "official_chapter_frontier",
    "surplus_downloads",
]
