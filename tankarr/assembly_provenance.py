"""Stable proof of a book assembled by Tankarr from specific chapter files."""

from __future__ import annotations

import json
import re
from typing import Any

from tankarr.chapter_map import canonical_label


def assembly_provenance(value: object) -> dict[str, Any] | None:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return None
    if not isinstance(value, dict) or value.get("version") != 1:
        return None
    identifiers = value.get("chapter_ids")
    chapters = value.get("chapters")
    volume = canonical_label(value.get("volume"))
    if (
        not isinstance(identifiers, list)
        or not 1 <= len(identifiers) <= 10_000
        or any(
            not isinstance(item, str) or not item or len(item) > 512
            for item in identifiers
        )
        or len(set(identifiers)) != len(identifiers)
        or not isinstance(chapters, list)
        or not 1 <= len(chapters) <= 10_000
        or any(canonical_label(item) is None for item in chapters)
        or volume is None
    ):
        return None
    sources = value.get("sources")
    notes = value.get("notes")
    if not isinstance(sources, list) or any(
        not isinstance(item, str) or len(item) > 1024 for item in sources
    ):
        return None
    if not isinstance(notes, str) or not notes or len(notes) > 32_768:
        return None
    return {
        "version": 1,
        "chapter_ids": list(identifiers),
        "chapters": [canonical_label(item) for item in chapters],
        "volume": volume,
        "sources": list(sources),
        "notes": notes,
    }


def proven_assembly(release: dict[str, Any]) -> dict[str, Any] | None:
    if (
        release.get("provider") != "assembled"
        or release.get("release_unit") != "volume"
    ):
        return None
    if not re.fullmatch(r"[a-f0-9]{64}", str(release.get("library_sha256") or "")):
        return None
    proof = assembly_provenance(release.get("assembled_from"))
    if proof is None or proof["volume"] != canonical_label(release.get("volume")):
        return None
    return proof
