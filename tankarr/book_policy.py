"""The final book edition is the target; exact boundaries are a separate fact."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from tankarr.catalogue_consensus import count_is_reliable, managed_volume_count
from tankarr.series_form import _is_webtoon
from tankarr.series_summary import publication_summary


def _positive(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not number.is_finite() or number <= 0 or number != number.to_integral_value():
        return None
    return int(number)


def expected_volume_count(
    manga: dict[str, Any], metadata: dict[str, Any]
) -> int | None:
    unit = str(manga.get("expected_count_unit_override") or "").casefold()
    if unit in {"volume", "book", "issue"}:
        if count := _positive(manga.get("expected_count_override")):
            return count
    if count := _positive(manga.get("edition_book_count")):
        return count
    if count := managed_volume_count(metadata):
        return count
    if count_is_reliable(metadata, "volume"):
        return _positive(metadata.get("volume_count"))
    return None


def completed_volume_count(
    manga: dict[str, Any], metadata: dict[str, Any] | None
) -> int | None:
    """A finished, non-webtoon edition with a reliable final number of books.

    This says nothing about chapter membership. Only explicit book boundaries
    may authorize acquiring chapters for a book or assembling its archive.
    """
    metadata = metadata or {}
    publication = publication_summary(manga, metadata)
    if (
        publication["status"] != "ended"
        or publication["paused"]
        or _is_webtoon(metadata)
    ):
        return None
    return expected_volume_count(manga, metadata)
