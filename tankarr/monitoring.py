from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from tankarr.series_summary import TERMINAL_STATUSES

MONITOR_MODES = frozenset({"all", "future", "existing", "none"})
FUTURE_MONITOR_MODES = frozenset({"all", "future"})
BACKLOG_MONITOR_MODES = frozenset({"all", "existing"})
TERMINAL_PUBLICATION_STATUSES = TERMINAL_STATUSES


def normalize_monitor_mode(value: str) -> str:
    mode = value.strip().lower()
    if mode not in MONITOR_MODES:
        raise ValueError(f"Unsupported monitor mode: {value}")
    return mode


def _chapter_number(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None


def evaluate_future_monitoring(
    manga: dict[str, Any], chapters: list[dict[str, Any]]
) -> tuple[bool, str]:
    """Decide whether future translated releases can still reasonably appear.

    A provider's publication status describes the original work, while Tankarr tracks
    a selected translation. A completed original can therefore still need monitoring
    until that translation reaches the provider's declared final chapter.
    """

    status = str(manga.get("status") or "unknown").lower()
    if status not in TERMINAL_PUBLICATION_STATUSES:
        if status == "ongoing":
            return True, "The source marks the series as ongoing."
        return True, "The publication has no confirmed terminal status."

    final_raw = manga.get("last_chapter")
    final_number = _chapter_number(final_raw)
    available_numbers = [
        number
        for chapter in chapters
        if (number := _chapter_number(chapter.get("chapter"))) is not None
    ]
    latest_number = max(available_numbers, default=None)

    if final_number is not None and latest_number is not None:
        if latest_number >= final_number:
            return (
                False,
                f"The {manga.get('preferred_language', 'selected').upper()} "
                f"translation has reached final chapter {final_raw}.",
            )
        return (
            True,
            f"The original is {status}, but this translation is at chapter "
            f"{latest_number} of {final_raw}.",
        )

    if final_raw is not None and any(
        str(chapter.get("chapter") or "").strip() == str(final_raw).strip()
        for chapter in chapters
    ):
        return False, f"The translation has reached final chapter {final_raw}."

    if final_raw is None:
        return (
            False,
            f"The source marks the series as {status}; no final chapter target is available.",
        )

    return (
        True,
        f"The original is {status}, but the selected translation has not reached "
        f"final chapter {final_raw}.",
    )


def terminal_monitor_mode(mode: str) -> str:
    """Drop only the future portion when a title becomes fully translated."""

    normalized = normalize_monitor_mode(mode)
    if normalized == "all":
        return "existing"
    if normalized == "future":
        return "none"
    return normalized
