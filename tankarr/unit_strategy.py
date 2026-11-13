"""Choose chapters or volumes before a series is hunted.

A source that publishes a tankōbon as its only entries (``Volume 3``,
``第3卷``) used to be stored as chapter 3. Strict unit rules then correctly
refused to let that book satisfy a chapter in Wanted, so a series whose only
completable releases were volumes stayed on chapters forever.

Finished non-webtoon works with a reliable final book count follow volumes,
even if only chapters are available. Their chapter jobs remain intact: exact
book boundaries allow missing books to be acquired and assembled from chapters.
An explicit series choice wins. No unit decision moves or removes library files.

Other works retain the availability-based strategy:

* an explicit chapters or volumes choice on the series is kept;
* a webtoon stays on episodes;
* the catalogue must have a reliable volume count (or the operator's edition
  count), and offers must include every book from 1 through that count;
* the preferred language is used first; another language counts only when
  global translation is on, and only when the preferred language cannot
  finish that unit;
* chapters win whenever they already cover the catalogue chapter total,
  including through translation;
* a finished work whose preferred-language books are already on disk follows
  volumes even if chapter releases also exist, so Wanted stops asking for
  chapter files the library does not have.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from tankarr.book_policy import completed_volume_count, expected_volume_count
from tankarr.catalogue_consensus import count_is_reliable
from tankarr.series_form import _is_webtoon
from tankarr.series_summary import publication_summary
from tankarr.series_unit import is_volume_release, normalize_series_unit

_LANGUAGE_TAG = r"(?:vf|vostfr|vo|va|vus|fr|en|eng|es|esp|it|ita|raw|hd)"
_WHOLE_BOOK = re.compile(
    rf"^\s*(?:vol(?:ume)?\.?|tome|book)\s*0*(\d+)"
    rf"(?:\s*[\[(]?\s*{_LANGUAGE_TAG}\s*[\])]?)?"
    rf"\s*(?:[:\-\u2013\u2014|].*)?$",
    re.IGNORECASE,
)
_CJK_VOLUME = re.compile(
    r"^\s*[【\[（(]?\s*第\s*0*(\d{1,4})\s*[卷巻]\s*[】\]）)]?"
    r"(?:\s*[\[(（【][^)\]】]{0,16}[\]）】)])?"
    r"\s*(?:[:：\-—–|].*)?$"
)
_KO_VOLUME = re.compile(
    r"^\s*(?:제\s*)?0*(\d{1,4})\s*권"
    r"(?:\s*[\[(（【][^)\]】]{0,16}[\]）】)])?"
    r"\s*(?:[:：\-—–|].*)?$"
)
_CHAPTER_MARKER = re.compile(
    r"(?i)\b(?:chapter|chap\.?|ch\.?|episode|ep\.?)\s*\d"
    r"|第\s*\d+\s*[话話章]"
    r"|\d+\s*화"
)


def whole_book_number(title: object) -> int | None:
    """The book number when a title is only a volume, not a chapter.

    ``第3卷`` and ``Volume 3 VF`` are books. ``第3话``, ``第3章`` and
    ``Volume 3 Chapter 12`` stay chapters.
    """

    name = " ".join(str(title or "").split())
    if not name or _CHAPTER_MARKER.search(name):
        return None
    for pattern in (_WHOLE_BOOK, _CJK_VOLUME, _KO_VOLUME):
        match = pattern.match(name)
        if match is None:
            continue
        number = int(match.group(1))
        if 1 <= number <= 5000:
            return number
    return None


def whole_book_reclassifications(
    releases: list[dict[str, Any]],
) -> list[tuple[str, str]]:
    """Releases stored as chapters whose titles name a single book."""

    found: list[tuple[str, str]] = []
    for release in releases:
        if str(release.get("release_unit") or "chapter") == "volume":
            continue
        number = whole_book_number(release.get("title"))
        if number is None or not release.get("id"):
            continue
        found.append((str(release["id"]), str(number)))
    return found


def strategy_record(manga: dict[str, Any]) -> dict[str, Any]:
    raw = manga.get("series_unit_strategy")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def operator_chose_unit(manga: dict[str, Any]) -> bool:
    """True when the stored unit is an operator's choice, not this strategy."""

    if normalize_series_unit(manga.get("series_unit_override")) is None:
        return False
    record = strategy_record(manga)
    if record.get("operator") is True:
        return True
    if record.get("operator") is False:
        return False
    return True


def strategy_unit_reason(manga: dict[str, Any], unit: str, fallback: str) -> str:
    record = strategy_record(manga)
    reason = record.get("reason")
    if record.get("operator") is False and record.get("unit") == unit and reason:
        return str(reason)
    return fallback


@dataclass(frozen=True)
class UnitStrategyDecision:
    action: str  # "stay" | "switch" | "maintain" | "reset"
    unit: str
    reason: str
    translation_used: bool = False
    languages: tuple[str, ...] = ()
    volumes: tuple[int, ...] = ()
    volume_expected: int | None = None
    chapter_expected: int | None = None
    chapter_fallback: bool = False

    def record(self) -> dict[str, Any]:
        return {
            "operator": False,
            "unit": "volumes",
            "reason": self.reason,
            "translation_used": self.translation_used,
            "languages": list(self.languages),
            "volumes": list(self.volumes),
            "volume_expected": self.volume_expected,
            "chapter_expected": self.chapter_expected,
            "chapter_fallback": self.chapter_fallback,
        }


def decide_unit_strategy(
    *,
    manga: dict[str, Any],
    metadata: dict[str, Any] | None,
    releases: list[dict[str, Any]],
    translation_enabled: bool,
    auto_unit_strategy: bool,
) -> UnitStrategyDecision:
    metadata = metadata or {}
    if operator_chose_unit(manga):
        unit = normalize_series_unit(manga.get("series_unit_override")) or "chapters"
        return UnitStrategyDecision(
            "stay", unit, "the series unit was chosen for this series"
        )
    record = strategy_record(manga)
    if auto_unit_strategy and (count := completed_volume_count(manga, metadata)):
        # The target edition does not depend on today's source availability.
        # Keep chapter jobs: they are the inputs for books not offered whole.
        return UnitStrategyDecision(
            "maintain"
            if record.get("operator") is False
            and normalize_series_unit(manga.get("series_unit_override")) == "volumes"
            else "switch",
            "volumes",
            "finished edition with a confirmed book total; chapters fill missing books",
            volume_expected=count,
            chapter_expected=_expected_chapters(manga, metadata),
            chapter_fallback=True,
        )
    if record.get("chapter_fallback") and record.get("operator") is False:
        # A metadata correction (ongoing/webtoon/uncertain total), or disabling
        # the automatic policy, must release this policy's own saved override.
        return UnitStrategyDecision(
            "reset", "chapters", "finished-book policy no longer applies"
        )
    if (
        normalize_series_unit(manga.get("series_unit_override")) == "volumes"
        and record.get("operator") is False
    ):
        return UnitStrategyDecision(
            "maintain",
            "volumes",
            str(record.get("reason") or "already following volumes"),
            translation_used=bool(record.get("translation_used")),
            languages=tuple(record.get("languages") or ()),
            volumes=tuple(int(n) for n in record.get("volumes") or ()),
            volume_expected=_positive(record.get("volume_expected")),
            chapter_expected=_positive(record.get("chapter_expected")),
        )
    if not auto_unit_strategy:
        return UnitStrategyDecision(
            "stay", "chapters", "automatic unit strategy is off"
        )
    if _is_webtoon(metadata):
        return UnitStrategyDecision(
            "stay", "chapters", "a webtoon is followed in episodes"
        )

    preferred = str(manga.get("preferred_language") or "").strip().casefold()
    volume_expected = _expected_volumes(manga, metadata)
    chapter_expected = _expected_chapters(manga, metadata)
    preferred_volumes: set[int] = set()
    foreign_volumes: set[int] = set()
    on_disk: set[int] = set()
    preferred_chapters: set[int] = set()
    foreign_chapters: set[int] = set()
    volume_languages: set[str] = set()
    for release in releases:
        if str(release.get("provider") or "") == "expected":
            continue
        language = str(release.get("language") or "").strip().casefold()
        volume = _volume_number(release)
        chapter = None if volume is not None else _chapter_number(release)
        if volume is not None:
            if language == preferred:
                preferred_volumes.add(volume)
                if _downloaded(release):
                    on_disk.add(volume)
            elif language:
                foreign_volumes.add(volume)
                volume_languages.add(language)
        elif chapter is not None:
            if language == preferred:
                preferred_chapters.add(chapter)
            elif language:
                foreign_chapters.add(chapter)

    chapters_complete = _chapters_cover(
        preferred_chapters,
        foreign_chapters,
        chapter_expected,
        translation_enabled=translation_enabled,
    )
    preferred_complete = _covers(preferred_volumes, volume_expected)
    foreign_complete = _covers(preferred_volumes | foreign_volumes, volume_expected)
    translation_used = (
        not preferred_complete and foreign_complete and translation_enabled
    )
    volumes_complete = preferred_complete or translation_used
    languages = (preferred,) if preferred_complete and preferred else ()
    if translation_used:
        languages = tuple(sorted(volume_languages))
    chosen_volumes = (
        preferred_volumes if preferred_complete else preferred_volumes | foreign_volumes
    )

    if volumes_complete and not chapters_complete:
        where = (
            f" in {', '.join(languages)} with translation" if translation_used else ""
        )
        return UnitStrategyDecision(
            "switch",
            "volumes",
            (
                f"volumes 1-{volume_expected} are available{where}"
                " and chapters do not cover the edition"
            ),
            translation_used=translation_used,
            languages=languages,
            volumes=tuple(sorted(chosen_volumes)),
            volume_expected=volume_expected,
            chapter_expected=chapter_expected,
        )

    publication = publication_summary(manga, metadata)
    ended = publication["status"] == "ended" and not publication["paused"]
    if ended and _covers(on_disk, volume_expected):
        return UnitStrategyDecision(
            "switch",
            "volumes",
            f"volumes 1-{volume_expected} are already in the library",
            languages=(preferred,) if preferred else (),
            volumes=tuple(sorted(on_disk)),
            volume_expected=volume_expected,
            chapter_expected=chapter_expected,
        )

    if chapters_complete and chapter_expected is None:
        reason = "chapter releases exist and the chapter total is unknown"
    elif chapters_complete:
        reason = "chapters already cover the edition"
    elif volume_expected is None:
        reason = "no reliable volume count"
    elif not translation_enabled and not preferred_complete and foreign_complete:
        reason = "foreign-language volumes cover the edition, but translation is off"
    else:
        reason = "volumes do not cover the edition"
    return UnitStrategyDecision(
        "stay",
        "chapters",
        reason,
        volume_expected=volume_expected,
        chapter_expected=chapter_expected,
    )


def _expected_volumes(manga: dict[str, Any], metadata: dict[str, Any]) -> int | None:
    return expected_volume_count(manga, metadata)


def _expected_chapters(manga: dict[str, Any], metadata: dict[str, Any]) -> int | None:
    unit = str(manga.get("expected_count_unit_override") or "").casefold()
    if unit == "chapter":
        count = _positive(manga.get("expected_count_override"))
        if count:
            return count
    if count_is_reliable(metadata, "chapter"):
        return _positive(metadata.get("chapter_count"))
    return None


def _chapters_cover(
    preferred: set[int],
    foreign: set[int],
    expected: int | None,
    *,
    translation_enabled: bool,
) -> bool:
    """Whether chapter releases can finish the edition.

    An unknown chapter total plus any real chapter is not evidence that
    chapters fall short, so the series stays on chapters. A known total is
    covered only when every number from 1 through that total is offered.
    """

    if expected is None:
        if preferred:
            return True
        return bool(translation_enabled and foreign)
    if _covers(preferred, expected):
        return True
    return bool(translation_enabled and _covers(preferred | foreign, expected))


def _covers(numbers: set[int], expected: int | None) -> bool:
    if not expected:
        return False
    return all(number in numbers for number in range(1, expected + 1))


def _volume_number(release: dict[str, Any]) -> int | None:
    titled = whole_book_number(release.get("title"))
    if titled is not None:
        return titled
    if is_volume_release(release):
        return _positive(release.get("volume"))
    return None


def _chapter_number(release: dict[str, Any]) -> int | None:
    if is_volume_release(release):
        return None
    return _positive(release.get("chapter"))


def _downloaded(release: dict[str, Any]) -> bool:
    return bool(release.get("downloaded"))


def _positive(value: object) -> int | None:
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, AttributeError):
        return None
    if number <= 0 or number != number.to_integral_value():
        return None
    return int(number)


__all__ = [
    "UnitStrategyDecision",
    "decide_unit_strategy",
    "operator_chose_unit",
    "strategy_record",
    "strategy_unit_reason",
    "whole_book_number",
    "whole_book_reclassifications",
]
