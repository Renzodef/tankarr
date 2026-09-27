"""Is this release a chapter, a whole book, or a pack of books?

Sources rarely say. MangaFire lists the one-volume graphic novel *Grass*
as "Ch. 1 - volume 1" with 454 pages; Usenet names a Kodansha book
"BECK v03 (2019) (Kodansha Comics USA) (Digital)"; a torrent bundles
"The Legend of Kamui v01-02 (2025) (c2c)". The classifier reads, in this
order of strength: page count, name markers (volume/chapter/range,
cover-to-cover), and size bands. Language tags reject non-English items.
Every verdict carries its evidence so the UI can show why.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from tankarr.torrent_utils import release_number_hints

BOOK_MIN_PAGES = 120
CHAPTER_MAX_PAGES = 90
BOOK_MIN_BYTES = 40 * 1024 * 1024
CHAPTER_MAX_BYTES = 60 * 1024 * 1024
PACK_RANGE = re.compile(
    r"(?i)(?:^|[\s._\-[(])(?:v|vol(?:ume)?|t)\.?\s*0*(\d+)\s*[-–~]\s*(?:v|vol(?:ume)?|t)?\.?\s*0*(\d+)(?![a-z0-9])"
)
COVER_TO_COVER = re.compile(r"(?i)\(c2c\)|\bc2c\b|cover.to.cover")
VOLUME_WORD = re.compile(
    r"(?i)(?:^|[\s._\-[(])(?:v|vol(?:ume)?|tome|t)\.?\s*0*(\d+)(?![a-z0-9])"
)
FRENCH_TOME = re.compile(r"(?i)\bT0*(\d+)\b")
# A different binding of the same work. The shelf is counted in the
# catalogue's tankobon; an omnibus, deluxe or perfect edition numbers its
# books differently ("Deluxe Edition v05" of Yokohama Kaidashi Kikou is
# tankobon 13-14) and grabbing it beside single volumes filed the same
# story twice under two numberings.
OTHER_EDITION = re.compile(
    r"(?i)\b(?:deluxe|omnibus|collector'?s?|perfect|master|big|ultimate|definitive)[\s._-]+edition\b"
    r"|\bomnibus\b|\b\d+\s*-?\s*in\s*-?\s*1\b"
)
NON_ENGLISH = re.compile(
    r"(?i)\b(french|français|german|deutsch|spanish|español|italian|italiano|portugu[eê]s|polish|russian|japanese|korean|chinese|vostfr|multi)\b|\[(fr|de|es|it|pt|ru|ja|jp|ko|zh)\]|\bT0*\d+\b.*\b(FRENCH|TONER)\b"
    # A Japanese volume marker ("第01-14巻", "全14巻") names a raw tankobon
    # pack, whatever English the uploader appended in brackets: Yokohama
    # Kaidashi Kikou's "第01-14巻 [... vol 01-14]" was grabbed twice for an
    # English shelf and refused twice at import.
    r"|第\s*\d+(?:\s*[-~]\s*\d+)?\s*巻|全\s*\d+\s*巻|\[raws?\]"
)


@dataclass(frozen=True)
class ReleaseKind:
    kind: str  # "chapter" | "volume" | "pack" | "unknown" | "rejected"
    volume: str | None = None
    volumes: tuple[int, ...] = ()
    chapter: str | None = None
    evidence: tuple[str, ...] = field(default_factory=tuple)


def classify_release(
    *,
    title: str,
    pages: int | None = None,
    size_bytes: int | None = None,
    language: str = "en",
) -> ReleaseKind:
    name = " ".join(str(title or "").split())
    evidence: list[str] = []
    if language.casefold() == "en" and NON_ENGLISH.search(name):
        return ReleaseKind("rejected", evidence=("non-English tag in the name",))
    if OTHER_EDITION.search(name):
        return ReleaseKind("rejected", evidence=("another edition of the work",))
    span = PACK_RANGE.search(name)
    if span:
        first, last = int(span.group(1)), int(span.group(2))
        if 0 < first <= last and last - first < 60:
            return ReleaseKind(
                "pack",
                volumes=tuple(range(first, last + 1)),
                evidence=(f"volume range {first}-{last}",),
            )
    volume_hint, chapter_hint = release_number_hints(name)
    if pages is not None and pages > 0:
        if pages >= BOOK_MIN_PAGES:
            evidence.append(f"{pages} pages")
            return ReleaseKind(
                "volume",
                volume=volume_hint
                or (chapter_hint if _looks_like_book_number(chapter_hint) else None),
                evidence=tuple(evidence),
            )
        if pages <= CHAPTER_MAX_PAGES and not volume_hint:
            evidence.append(f"{pages} pages")
            return ReleaseKind(
                "chapter", chapter=chapter_hint, evidence=tuple(evidence)
            )
    if volume_hint and not chapter_hint:
        evidence.append("volume marker")
        if COVER_TO_COVER.search(name):
            evidence.append("cover-to-cover scan")
        return ReleaseKind("volume", volume=volume_hint, evidence=tuple(evidence))
    if chapter_hint and not volume_hint:
        if size_bytes and size_bytes >= BOOK_MIN_BYTES * 4:
            return ReleaseKind(
                "unknown",
                chapter=chapter_hint,
                evidence=("chapter marker but book-sized",),
            )
        return ReleaseKind(
            "chapter", chapter=chapter_hint, evidence=("chapter marker",)
        )
    if volume_hint and chapter_hint:
        return ReleaseKind(
            "chapter",
            volume=volume_hint,
            chapter=chapter_hint,
            evidence=("chapter inside a volume label",),
        )
    if size_bytes:
        if size_bytes >= BOOK_MIN_BYTES:
            return ReleaseKind(
                "volume",
                evidence=(f"{size_bytes // (1024 * 1024)} MB without a number",),
            )
        if size_bytes <= CHAPTER_MAX_BYTES:
            return ReleaseKind(
                "chapter",
                evidence=(f"{size_bytes // (1024 * 1024)} MB without a number",),
            )
    return ReleaseKind("unknown", evidence=("no number, no pages, no size",))


ALLOWED_AFTER_TITLE = re.compile(
    r"^(?:(?:v|vol|volume|tome|t|ch|chapter|c)\d*|digital|complete|omnibus|deluxe|edition|master|color|colored|colour|coloured|hybrid|manga|comic|comics|ebook|\d+|20\d\d|19\d\d)$"
)


def title_matches_release(
    title: str, release_name: str, creators: tuple[str, ...] | list[str] = ()
) -> bool:
    """The work's title must appear as a contiguous phrase, followed only by
    a number, a volume/chapter marker, a year, an edition word, a bracket,
    or the work's own creator ("Black Magic by Masamune Shirow").
    "Kingdom Vol.10" and "VIZ.Media-Kingdom.Vol.10" pass; "Momose Akira no
    Hatsukoi" (for *Akira*) and "Monster War v01" (for *Monster*) do not.
    A leading "The" in the catalogue title is optional: releases drop it."""

    def tokens(value: str) -> list[str]:
        return [t for t in re.split(r"[^a-z0-9]+", value.casefold()) if t]

    creator_tokens = {
        token for name in creators for token in tokens(str(name)) if len(token) >= 3
    }
    forms = [tokens(title)]
    if forms[0][:1] == ["the"] and len(forms[0]) > 1:
        forms.append(forms[0][1:])
    found = tokens(release_name)
    for wanted in forms:
        if not wanted or len(found) < len(wanted):
            continue
        for start in range(len(found) - len(wanted) + 1):
            if found[start : start + len(wanted)] != wanted:
                continue
            after = (
                found[start + len(wanted)] if start + len(wanted) < len(found) else None
            )
            if (
                after is None
                or ALLOWED_AFTER_TITLE.match(after)
                or after == "by"
                or after in creator_tokens
            ):
                return True
    return False


def _looks_like_book_number(value: str | None) -> bool:
    try:
        return (
            value is not None
            and float(value) == int(float(value))
            and 0 < int(float(value)) <= 300
        )
    except ValueError:
        return False


__all__ = ["ReleaseKind", "classify_release", "title_matches_release", "BOOK_MIN_PAGES"]
