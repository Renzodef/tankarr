"""Whether a chapter file's pages are inside a book file: proof by content.

A volume↔chapter map is a claim about numbers; the sources' volume tags
are claims about numbers; both are routinely wrong about a translation
(Nana: the map said vol 21 = 78–80, the source said "Vol.21 Chapter 81",
the book holds none of 81–84). The pages themselves are not a claim. A
chapter whose pages are found, in order, inside a book on the shelf is in
that book; a chapter none of whose pages resemble any page of the book is
not, whatever the numbers say.

Pages are compared by a small average hash of the grey page: 12×12 cells,
one bit each, the border cropped so scan margins do not count. Two scans
of the same page - a scanlation's webp and a publisher's png - differ by a
handful of bits; unrelated pages differ by a third of them. A landscape
page is a spread and is hashed as its two halves, so a book of spreads
still matches chapters of single pages. Nothing here writes to the
library: the verdicts are recorded and read by the retirement rules.
"""

from __future__ import annotations

import zipfile
from collections.abc import Iterable
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

HASH_SIDE = 12
HASH_BITS = HASH_SIDE * HASH_SIDE
BORDER = 0.05
# Bits two scans of the same page may disagree on (Bride of the Water God
# v22 against its own chapters: medians 0–12) versus what unrelated pages
# score (Nana v21 against the uncollected 81–84: never below 19).
MATCH_BITS = 11
FOREIGN_BITS = 16
MIN_PAGES = 3
INSIDE_SHARE = 0.4
OUTSIDE_SHARE = 0.1
IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp")

INSIDE = "inside"
OUTSIDE = "outside"
AMBIGUOUS = "ambiguous"
UNREADABLE = "unreadable"


@dataclass(frozen=True)
class Alignment:
    verdict: str
    matched: int
    pages: int
    min_distance: int | None
    first_index: int | None = None
    last_index: int | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "verdict": self.verdict,
            "matched_pages": self.matched,
            "pages": self.pages,
            "min_distance": self.min_distance,
            "first_index": self.first_index,
            "last_index": self.last_index,
        }


def _hash_image(image) -> int:
    grey = image.convert("L")
    width, height = grey.size
    if width and height:
        grey = grey.crop(
            (
                int(width * BORDER),
                int(height * BORDER),
                int(width * (1 - BORDER)),
                int(height * (1 - BORDER)),
            )
        )
    from PIL import Image

    small = grey.resize((HASH_SIDE, HASH_SIDE), Image.Resampling.BILINEAR)
    pixels = small.tobytes()
    mean = sum(pixels) / len(pixels)
    return sum(1 << index for index, value in enumerate(pixels) if value > mean)


def page_signatures_from_images(images: Iterable[object]) -> list[int]:
    """One signature per page; a spread contributes its right and left halves."""

    signatures: list[int] = []
    for image in images:
        width, height = image.size
        if width > height * 1.15:
            half = width // 2
            # Right-to-left: the right half is read first.
            signatures.append(_hash_image(image.crop((half, 0, width, height))))
            signatures.append(_hash_image(image.crop((0, 0, half, height))))
        else:
            signatures.append(_hash_image(image))
    return signatures


def page_signatures(path: Path) -> list[int]:
    """The signatures of every readable page of one CBZ, in reading order."""

    from PIL import Image, UnidentifiedImageError

    def images():
        try:
            archive = zipfile.ZipFile(path)
        except (OSError, zipfile.BadZipFile):
            return
        with archive:
            names = sorted(
                name
                for name in archive.namelist()
                if not name.endswith("/") and name.lower().endswith(IMAGE_SUFFIXES)
            )
            for name in names:
                try:
                    with archive.open(name) as member:
                        payload = member.read()
                    with Image.open(BytesIO(payload)) as image:
                        image.load()
                        yield image
                except (
                    OSError,
                    ValueError,
                    UnidentifiedImageError,
                    zipfile.BadZipFile,
                ):
                    continue

    return page_signatures_from_images(images())


def _distance(left: int, right: int) -> int:
    return bin(left ^ right).count("1")


def align(chapter: list[int], book: list[int]) -> Alignment:
    """Where the chapter's pages sit in the book, if they do."""

    if len(chapter) < MIN_PAGES or len(book) < MIN_PAGES:
        return Alignment(UNREADABLE, 0, len(chapter), None)
    matches: list[int] = []
    minimum: int | None = None
    for signature in chapter:
        best_index = min(range(len(book)), key=lambda i: _distance(book[i], signature))
        distance = _distance(book[best_index], signature)
        minimum = distance if minimum is None else min(minimum, distance)
        if distance <= MATCH_BITS:
            matches.append(best_index)
    matched = len(matches)
    share = matched / len(chapter)
    if matched >= MIN_PAGES and share >= INSIDE_SHARE:
        ordered = sorted(matches)
        return Alignment(
            INSIDE, matched, len(chapter), minimum, ordered[0], ordered[-1]
        )
    if share <= OUTSIDE_SHARE and (minimum is None or minimum >= FOREIGN_BITS):
        return Alignment(OUTSIDE, matched, len(chapter), minimum)
    return Alignment(AMBIGUOUS, matched, len(chapter), minimum)


def encode_signatures(signatures: Iterable[int]) -> str:
    return ",".join(format(value, "x") for value in signatures)


def decode_signatures(text: str | None) -> list[int]:
    if not text:
        return []
    return [int(item, 16) for item in str(text).split(",") if item]


__all__ = [
    "AMBIGUOUS",
    "Alignment",
    "INSIDE",
    "OUTSIDE",
    "UNREADABLE",
    "align",
    "decode_signatures",
    "encode_signatures",
    "page_signatures",
    "page_signatures_from_images",
]
