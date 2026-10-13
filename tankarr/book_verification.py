"""Operator proof that an imported book holds exactly the chapters it is mapped to.

A book Tankarr did not assemble itself cannot prove what is inside it, so the size
check and the machine-translation rule keep it suspect. A person can read the book
and say what it holds. The proof records what was checked and is bound to the
book's content digest and to its chapter set, so it stops applying the moment the
content or the chapter map changes.

A verified book may satisfy Wanted for the chapters it is mapped to and is no
longer taken for another edition or a wrong split. It never gains the authority
to retire a chapter file: that stays with books Tankarr assembled or downloaded.
"""

from __future__ import annotations

import json
import re
from typing import Any

from tankarr.chapter_map import canonical_label
from tankarr.series_unit import is_volume_release

MAX_CHAPTERS = 10_000
MAX_SOURCE = 1024
MAX_NOTE = 32_768
DIGEST = re.compile(r"[a-f0-9]{64}")


def book_verification(value: object) -> dict[str, Any] | None:
    """A stored proof, or None when it is not a well-formed version 1 proof."""

    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return None
    if not isinstance(value, dict) or value.get("version") != 1:
        return None
    release_id = value.get("release_id")
    volume = canonical_label(value.get("volume"))
    digest = value.get("content_sha256")
    chapters = value.get("chapters")
    source = value.get("source", "")
    note = value.get("note")
    verified_at = value.get("verified_at", "")
    if (
        not isinstance(release_id, str)
        or not release_id
        or len(release_id) > 512
        or volume is None
        or not isinstance(digest, str)
        or not DIGEST.fullmatch(digest)
        or not isinstance(chapters, list)
        or not 1 <= len(chapters) <= MAX_CHAPTERS
        or any(canonical_label(item) is None for item in chapters)
        or not isinstance(source, str)
        or len(source) > MAX_SOURCE
        or not isinstance(note, str)
        or not note.strip()
        or len(note) > MAX_NOTE
        or not isinstance(verified_at, str)
    ):
        return None
    labels = [canonical_label(item) for item in chapters]
    if len(set(labels)) != len(labels):
        return None
    return {
        "version": 1,
        "release_id": release_id,
        "volume": volume,
        "content_sha256": digest,
        "chapters": labels,
        "source": source,
        "note": note,
        "verified_at": verified_at,
    }


def verification_problem(
    release: dict[str, Any], mapped: dict[str, set[str]] | None = None
) -> str | None:
    """Why a stored proof does not apply to this release now; None when it does.

    It is bound to the release and to the digest of the pages Tankarr imported,
    not to the file, which Tankarr may rewrite without changing a page. A book
    without a content digest cannot be verified. With ``mapped`` (the exact
    chapter sets per volume) the chapter map must still list the proven chapters
    and nothing else for the book's volume.
    """

    proof = book_verification(release.get("verified_book"))
    if proof is None:
        return "no valid proof is stored for this book"
    if not release.get("downloaded"):
        return "the book file is gone"
    if not is_volume_release(release) or release.get("provider") == "assembled":
        return "only an imported whole book can be verified"
    if proof["release_id"] != str(release.get("id")):
        return "the proof belongs to another release"
    volume = canonical_label(release.get("volume"))
    if proof["volume"] != volume:
        return "the book's volume changed since it was verified"
    digest = release.get("local_import_sha256")
    if not isinstance(digest, str) or proof["content_sha256"] != digest:
        return "the book's content changed since it was verified"
    if mapped is not None and mapped.get(volume or "", set()) != set(proof["chapters"]):
        return f"the chapter map no longer lists exactly these chapters for volume {volume}"
    return None


def verified_book(release: dict[str, Any]) -> dict[str, Any] | None:
    """The proof of this release while it still describes this very book.

    The chapter map is checked by the caller, which holds it."""

    if verification_problem(release) is not None:
        return None
    return book_verification(release.get("verified_book"))
