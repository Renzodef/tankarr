"""Conservatively fix reversed book scans when a trusted cover is available."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageChops, ImageStat, UnidentifiedImageError


def _cover_distance(reference: Image.Image, path: Path) -> float:
    with Image.open(path) as opened:
        opened.draft("RGB", (40, 60))
        candidate = opened.convert("RGB").resize((40, 60))
    difference = ImageChops.difference(reference, candidate)
    return sum(ImageStat.Stat(difference).mean) / 3


def restore_cover_first(pages: list[Path], cover: Path) -> bool:
    """Reverse a volume only when its final page clearly matches its cover.

    The filename sequence alone cannot distinguish a reversed scan from a
    deliberate back cover. Without strong visual evidence, leave it untouched.
    """

    if len(pages) < 20 or not cover.is_file() or cover.is_symlink():
        return False
    try:
        with Image.open(cover) as opened:
            opened.draft("RGB", (40, 60))
            reference = opened.convert("RGB").resize((40, 60))
        first = _cover_distance(reference, pages[0])
        last = _cover_distance(reference, pages[-1])
    except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError):
        return False
    if last > 75 or first - last < 25:
        return False

    destination = pages[0].parent / ".cover-first"
    destination.mkdir(exist_ok=False)
    reordered: list[Path] = []
    for number, page in enumerate(reversed(pages), start=1):
        target = destination / f"{number:04d}{page.suffix.lower()}"
        page.rename(target)
        reordered.append(target)
    pages[:] = reordered
    return True
