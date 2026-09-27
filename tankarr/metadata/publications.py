from __future__ import annotations

from typing import Any


def publication_number(release: dict[str, Any], canonical: dict[str, Any]) -> str:
    """Return the reader-visible number represented by one library book."""

    if str(canonical.get("content_kind") or "").casefold() == "comic" and release.get(
        "chapter"
    ) not in (None, ""):
        return str(release["chapter"])
    return str(release.get("volume") or "")


def publication_kind(release: dict[str, Any], canonical: dict[str, Any]) -> str:
    if str(canonical.get("content_kind") or "").casefold() == "comic" and release.get(
        "chapter"
    ) not in (None, ""):
        return "issue"
    return "volume"


def publication_metadata_key(release: dict[str, Any], canonical: dict[str, Any]) -> str:
    """Map a release to a stable metadata key without changing manga keys."""

    number = publication_number(release, canonical)
    if not number:
        return ""
    if publication_kind(release, canonical) == "issue":
        return f"issue:{number}"
    return number
