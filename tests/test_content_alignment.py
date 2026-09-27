from __future__ import annotations

import io
import random
import zipfile
from pathlib import Path

from PIL import Image, ImageDraw

from tankarr.content_alignment import (
    AMBIGUOUS,
    INSIDE,
    OUTSIDE,
    UNREADABLE,
    align,
    decode_signatures,
    encode_signatures,
    page_signatures,
    page_signatures_from_images,
)


def page(seed: int, *, size: tuple[int, int] = (600, 900)) -> Image.Image:
    """A distinct page: random panels, deterministic per seed."""

    rng = random.Random(seed)
    image = Image.new("L", size, 255)
    draw = ImageDraw.Draw(image)
    for _ in range(12):
        x0, y0 = rng.randrange(size[0] - 80), rng.randrange(size[1] - 80)
        x1, y1 = x0 + rng.randrange(40, 200), y0 + rng.randrange(40, 200)
        draw.rectangle(
            (x0, y0, min(x1, size[0]), min(y1, size[1])), fill=rng.randrange(0, 140)
        )
    return image


def rescan(image: Image.Image) -> Image.Image:
    """The same page from another scan: smaller, slightly brighter, jpeg."""

    smaller = image.resize((int(image.width * 0.7), int(image.height * 0.7)))
    brighter = smaller.point(lambda value: min(255, int(value * 1.08) + 4))
    buffer = io.BytesIO()
    brighter.save(buffer, format="JPEG", quality=70)
    buffer.seek(0)
    return Image.open(buffer).copy()


def cbz(path: Path, images: list[Image.Image], *, fmt: str = "PNG") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        for index, image in enumerate(images, start=1):
            buffer = io.BytesIO()
            image.save(buffer, format=fmt)
            archive.writestr(f"{index:03d}.{fmt.lower()}", buffer.getvalue())
    return path


def test_a_rescanned_chapter_is_found_inside_its_book():
    book = [page(seed) for seed in range(1, 41)]
    chapter = [rescan(book[index]) for index in range(10, 18)]
    verdict = align(
        page_signatures_from_images(chapter), page_signatures_from_images(book)
    )
    assert verdict.verdict == INSIDE
    assert verdict.matched >= 6
    assert (verdict.first_index, verdict.last_index) == (10, 17)


def test_pages_of_another_work_are_outside_every_book():
    book = [page(seed) for seed in range(1, 41)]
    foreign = [page(seed) for seed in range(500, 512)]
    verdict = align(
        page_signatures_from_images(foreign), page_signatures_from_images(book)
    )
    assert verdict.verdict == OUTSIDE
    assert verdict.matched == 0


def test_a_half_matching_chapter_is_ambiguous_not_outside():
    book = [page(seed) for seed in range(1, 41)]
    mixed = [rescan(book[3]), rescan(book[4])] + [
        page(seed) for seed in range(600, 610)
    ]
    verdict = align(
        page_signatures_from_images(mixed), page_signatures_from_images(book)
    )
    assert verdict.verdict == AMBIGUOUS


def test_a_book_of_spreads_still_matches_single_pages():
    singles = [page(seed) for seed in range(1, 21)]
    spreads = []
    for right, left in zip(singles[0::2], singles[1::2], strict=True):
        spread = Image.new("L", (right.width * 2, right.height), 255)
        spread.paste(left, (0, 0))
        spread.paste(right, (right.width, 0))
        spreads.append(spread)
    chapter = [rescan(image) for image in singles[4:10]]
    verdict = align(
        page_signatures_from_images(chapter), page_signatures_from_images(spreads)
    )
    assert verdict.verdict == INSIDE


def test_unreadable_archives_yield_no_verdict(tmp_path: Path):
    junk = tmp_path / "junk.cbz"
    junk.write_bytes(b"cbz")
    assert page_signatures(junk) == []
    assert align([], [1, 2, 3, 4]).verdict == UNREADABLE
    assert align([1, 2, 3, 4], []).verdict == UNREADABLE


def test_signatures_are_read_from_archives_and_round_trip(tmp_path: Path):
    images = [page(seed) for seed in range(1, 6)]
    path = cbz(tmp_path / "book.cbz", images, fmt="PNG")
    signatures = page_signatures(path)
    assert len(signatures) == 5
    assert decode_signatures(encode_signatures(signatures)) == signatures
    assert decode_signatures("") == [] and decode_signatures(None) == []
