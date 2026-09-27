"""Judge whether a chapter carries as much page as the work's chapters carry.

A source can serve a file that unzips, validates and imports while being
unreadable: thumbnail-sized tiles, or one episode chopped into fragments of a
few images each. Nothing in the archive says so - the CBZ is well-formed, the
page count is plausible, ComicInfo is correct - so the only evidence available
is the *geometry of the pages themselves*, compared against what this same
work's other chapters look like.

The measure is the **normalized height**: every page rescaled to a common
reference width, then summed. It answers "how much of the work is in this
file" independently of the resolution a source happens to serve at, which is
what makes it comparable across the mirrors of one series. In the long webtoon
the rule was calibrated on, three sources served good copies with medians
between about 132000 and 171000, while a fourth served unreadable ones with a
median of about 1500.

The lowest good file of that series measures 83374 and the highest bad one
5971: a factor of fourteen, so the threshold has room on both sides. It is
taken from the baseline's first quartile rather than its median so that a
series whose chapters vary in length is judged by its short chapters, and
p25 keeps its meaning while up to a quarter of the samples are themselves
degraded - which is the state a library is in before its first audit.

The same threshold leaves print manga alone, where a chapter is a handful of
book pages rather than a strip: measured over several print series it flags
none, because a short chapter is still a chapter of full-sized pages.

There is a second, independent way for a chapter to be unreadable, and total
content cannot see it: *how the pages are cut*. One source slices an episode
into 121 tiles of 800x1280; another serves the same episode as 10 strips of
800x15711, a page twenty times taller than it is wide. Both carry the same
comic, so the length test passes either way - but a reader that fits a page to
the screen renders the second one as a ribbon a few pixels across.

That failure is a property of screens, not of the work, so unlike length it has
an absolute answer: past ``MAX_READABLE_ASPECT`` no page can be read without
panning, whatever the series. In the calibration sample the two populations do
not come close to touching - every readable chapter sits at or below 2.32, and
the mega-strips start at 11.7 - so the threshold has more than a factor of two
of headroom on each side.

Only *whole* chapters are measured or judged for length. A decimal is an extra - a two
page omake, a bonus illustration, a side story - and has no expected length
at all, so the comparison is meaningless for it. Measuring them anyway
produced exactly the false positives you would predict: a two-page extra at
1688px and a three-page one at 850px, both complete, full-resolution extras
that this would otherwise have called unreadable.

That leaves one gap worth naming: a source that chops an episode into ``310.1,
310.2, 310.3`` produces decimals, and those escape this rule. They are caught
instead by ``official_numbering``, which is the better answer for them - they
are not low-quality chapter 310.1, they are chapters the official edition does
not have.

A verdict is never a reason to delete anything. It says the file came from a
source that served less than the work has; replacing it is the caller's
decision, and only ever worth taking when another source can be asked.
"""

from __future__ import annotations

import re
import zipfile
from collections.abc import Iterable, Sequence
from decimal import Decimal, InvalidOperation
from io import BytesIO
from pathlib import Path
from statistics import median
from typing import Any

from PIL import Image, UnidentifiedImageError

from tankarr.archive import IMAGE_SUFFIXES

# Every page is rescaled to this width before its height is counted, so a
# source serving 720px-wide tiles and one serving 1778px scans are measured
# on the same axis.
REFERENCE_WIDTH = 800

# Below this many measured chapters the series has no shape yet and no file
# can be called an outlier of it.
MIN_BASELINE_SAMPLES = 8

# A file carrying less than this share of the baseline's first quartile is
# not a short chapter, it is a fragment or a thumbnail set.
SHORTFALL_RATIO = 0.35

# A page taller than this many times its width cannot be shown on a screen
# without panning, however long the chapter is. Measured over this library:
# every readable chapter is at or below 2.32, every mega-strip at or above
# 11.7, so the line sits in empty space between the two populations.
MAX_READABLE_ASPECT = 5.0
# A page narrower than this is a thumbnail or a placeholder, never a page:
# measured live, a source served 42 identical 200x200 tiles as chapter 17
# of a work whose pages are 1066 wide, and the length rule - which scales
# by width - could not tell.
MIN_PAGE_WIDTH = 400
# Two sources whose copies of a chapter are within this of each other agree
# on its length: the chapter is short, the files are not fragments.
CORROBORATION_TOLERANCE = 0.20

OK = "ok"
# A download the gate refused: measured, never in the library. Kept so a
# sibling from another source can prove the chapter is simply short.
REFUSED = "refused"
DEGRADED = "degraded"
UNKNOWN = "unknown"


def is_measurable(chapter: object) -> bool:
    """Whether this chapter has an expected length to be compared against.

    A whole chapter does; ``c503.5`` is an extra and does not, and neither
    does chapter 0: a prologue is a short chapter by nature (measured live,
    the official WEBTOON prologue of Girls of the Wild's carries a quarter of
    a regular episode and every source of it was refused as a fragment).
    """

    text = str(chapter if chapter is not None else "").strip()
    if not text:
        return False
    try:
        number = Decimal(text)
    except (InvalidOperation, ValueError):
        return False
    return bool(
        number.is_finite() and number > 0 and number == number.to_integral_value()
    )


def _page_geometry(source: Any) -> tuple[int, int] | None:
    """Width and height of one page, from its header alone.

    ``Image.open`` parses the header and stops, so a page is measured without
    decoding it. Auditing the library depends on that: the sweep walks every
    CBZ in the library storage, often over the network, and reading whole
    55 MB strips to learn two integers would make it unaffordable on a small
    host.
    """

    try:
        with Image.open(source) as image:
            width, height = image.size
    except (UnidentifiedImageError, OSError, ValueError):
        return None
    if width <= 0 or height <= 0:
        return None
    return width, height


DARK_PAGE_LUMINANCE = 40  # a black card: piracy notice, studio splash, "end" slate
DARK_PAGE_SPREAD = 30  # ...and nearly uniform: a dark drawing has contrast
DARK_PAGE_RATIO = 0.15  # more than this share of black cards is not a chapter


def _page_darkness(payload: bytes) -> tuple[float, float] | None:
    """Mean and spread of luminance on a tiny thumbnail: is this a black card?

    Decoding is confined to a 32 px draft, so it costs about as much as the
    header read; it is done only on freshly downloaded pages, never in the
    library sweep.
    """

    try:
        with Image.open(BytesIO(payload)) as image:
            image.draft("L", (32, 32))
            small = image.convert("L")
            small.thumbnail((32, 32))
            pixels = list(small.getdata())
    except (UnidentifiedImageError, OSError, ValueError):
        return None
    if not pixels:
        return None
    mean = sum(pixels) / len(pixels)
    spread = (sum((value - mean) ** 2 for value in pixels) / len(pixels)) ** 0.5
    return mean, spread


def _summarize(geometries: Sequence[tuple[int, int]]) -> dict[str, Any]:
    if not geometries:
        return {
            "pages": 0,
            "measured_pages": 0,
            "normalized_height": 0,
            "max_width": 0,
            "median_width": 0,
            "median_aspect": 0.0,
        }
    widths = [width for width, _height in geometries]
    # A landscape image is a double-page spread: two pages side by side.
    # It carries twice a page and is only half as tall per unit of width,
    # so it must be measured as two pages of half its width. ONE PIECE 554,
    # live: 7 spreads and 2 pages read as "31% of a chapter" and were
    # refused; they are 16 pages, a whole chapter.
    normalized = 0.0
    for width, height in geometries:
        if width > height:
            normalized += 2 * height * (REFERENCE_WIDTH / (width / 2))
        else:
            normalized += height * (REFERENCE_WIDTH / width)
    # The median, not the maximum: a chapter may legitimately open with one
    # tall stitched banner, and that is not how the chapter reads.
    aspects = [height / width for width, height in geometries]
    return {
        "pages": len(geometries),
        "measured_pages": len(geometries),
        "normalized_height": round(normalized),
        "max_width": max(widths),
        "median_width": int(median(widths)),
        "median_aspect": round(float(median(aspects)), 2),
    }


# How the measurement is taken. Bumped when the numbers change meaning, so
# every stored row is re-measured instead of being compared across versions.
MEASURE_VERSION = "2"

# Slicing: a page of this height-to-width ratio reads on any screen.
SLICE_TARGET_ASPECT = 1.45
# The cut may move this far (as a share of the target height) to land on a
# gutter instead of a panel.
SLICE_SEARCH_WINDOW = 0.25
# A remainder shorter than this joins the previous piece.
SLICE_MIN_PIECE = 0.5


def _row_activity(image: Image.Image) -> list[float]:
    """How much each row of pixels changes across its width.

    A gutter (white or black between panels) is uniform across the width:
    its horizontal gradient is close to zero. A row through a panel is not.
    Computed on the image shifted by one pixel against itself and reduced
    to one column, so the strip is scanned once without decoding it twice.
    """

    from PIL import ImageChops

    gray = image.convert("L")
    shifted = ImageChops.offset(gray, 1, 0)
    gradient = ImageChops.difference(gray, shifted)
    gradient_column = gradient.resize((1, gray.height), Image.Resampling.BOX)
    gray_column = gray.resize((1, gray.height), Image.Resampling.BOX)
    # Pillow 12 renamed the iterator ahead of removing ``getdata`` in 14;
    # keep the declared Pillow 11 compatibility while staying warning-free.
    gradient_data = getattr(
        gradient_column, "get_flattened_data", gradient_column.getdata
    )()
    gray_data = getattr(gray_column, "get_flattened_data", gray_column.getdata)()
    edges = [float(value) for value in gradient_data]
    means = [float(value) for value in gray_data]
    # The strip's background is what its outermost rows look like; a solid
    # panel interior is quiet too, but it is not background.
    margin = max(2, len(means) // 100)
    background = median([*means[:margin], *means[-margin:]])
    return [
        edge + abs(mean - background) for edge, mean in zip(edges, means, strict=True)
    ]


def slice_cut_points(activity: list[float], width: int) -> list[int]:
    """Row indexes at which a strip of this width is cut into pages."""

    height = len(activity)
    target = max(1, int(width * SLICE_TARGET_ASPECT))
    if height <= target * (1 + SLICE_MIN_PIECE):
        return []
    window = max(1, int(target * SLICE_SEARCH_WINDOW))
    cuts: list[int] = []
    position = 0
    while height - position > target * (1 + SLICE_MIN_PIECE):
        ideal = position + target
        low = max(position + int(target * SLICE_MIN_PIECE), ideal - window)
        high = min(height - int(target * SLICE_MIN_PIECE), ideal + window)
        if high <= low:
            cut = ideal
        else:
            best = min(
                range(low, high), key=lambda row: (activity[row], abs(row - ideal))
            )
            cut = best
        cuts.append(cut)
        position = cut
    return cuts


def slice_strips(paths: Iterable[Path]) -> tuple[list[Path], int]:
    """Cut un-sliced webtoon strips into readable pages, in place.

    Every page taller than MAX_READABLE_ASPECT times its width is replaced by
    consecutive pieces of about SLICE_TARGET_ASPECT, each cut placed on the
    quietest row near the ideal point (a gutter, not a speech bubble). The
    ordered list of page files is returned with how many strips were cut.
    Pages that already read normally are untouched.
    """

    result: list[Path] = []
    sliced = 0
    for path in paths:
        path = Path(path)
        geometry = _page_geometry(path)
        if geometry is None:
            result.append(path)
            continue
        width, height = geometry
        if height <= width * MAX_READABLE_ASPECT:
            result.append(path)
            continue
        with Image.open(path) as image:
            image.load()
            fmt = (image.format or "PNG").upper()
            cuts = slice_cut_points(_row_activity(image), width)
            if not cuts:
                result.append(path)
                continue
            bounds = [0, *cuts, height]
            suffix = path.suffix.lower() or ".png"
            save_kwargs: dict[str, Any] = {}
            if fmt in {"JPEG", "WEBP"}:
                save_kwargs["quality"] = 92
            pieces: list[Path] = []
            for index in range(len(bounds) - 1):
                top, bottom = bounds[index], bounds[index + 1]
                piece_path = path.with_name(f"{path.stem}-{index + 1:03d}{suffix}")
                piece = image.crop((0, top, width, bottom))
                if fmt == "JPEG" and piece.mode not in {"RGB", "L"}:
                    piece = piece.convert("RGB")
                piece.save(piece_path, format=fmt, **save_kwargs)
                pieces.append(piece_path)
        path.unlink()
        result.extend(pieces)
        sliced += 1
    return result, sliced


def measure_pages(paths: Iterable[Path]) -> dict[str, Any]:
    """Geometry of the pages staged for one chapter, before they are packaged."""

    geometries: list[tuple[int, int]] = []
    total = 0
    dark_pages = 0
    for path in paths:
        total += 1
        try:
            payload = Path(path).read_bytes()
        except OSError:
            continue
        geometry = _page_geometry(BytesIO(payload))
        if geometry is not None:
            geometries.append(geometry)
            darkness = _page_darkness(payload)
            if (
                darkness is not None
                and darkness[0] < DARK_PAGE_LUMINANCE
                and darkness[1] < DARK_PAGE_SPREAD
            ):
                dark_pages += 1
    return {**_summarize(geometries), "pages": total, "dark_pages": dark_pages}


def measure_archive(path: Path) -> dict[str, Any]:
    """Geometry of the pages of one CBZ already in the library."""

    geometries: list[tuple[int, int]] = []
    total = 0
    with zipfile.ZipFile(path) as archive:
        for name in archive.namelist():
            if name.endswith("/") or not name.lower().endswith(IMAGE_SUFFIXES):
                continue
            total += 1
            try:
                with archive.open(name) as member:
                    geometry = _page_geometry(member)
            except (OSError, zipfile.BadZipFile):
                continue
            if geometry is not None:
                geometries.append(geometry)
    return {**_summarize(geometries), "pages": total}


def _quartile(values: Sequence[float]) -> float:
    """First quartile, by nearest rank - no interpolation to argue about."""

    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(len(ordered) * 0.25)))
    return float(ordered[index])


def baseline_from(measurements: Iterable[dict[str, Any]]) -> dict[str, Any] | None:
    """The shape of this series' chapters, or ``None`` when too few are known."""

    items = list(measurements)
    heights = [
        float(item["normalized_height"])
        for item in items
        if float(item.get("normalized_height") or 0) > 0
    ]
    if len(heights) < MIN_BASELINE_SAMPLES:
        return None
    quartile = _quartile(heights)
    # How the series reads: pages (about 1.4-1.5 tall for their width) or
    # strips. A source that serves the other shape is another edition.
    aspects = [
        float(item.get("median_aspect") or 0)
        for item in items
        if float(item.get("median_aspect") or 0) > 0
    ]
    return {
        "samples": len(heights),
        "quartile": round(quartile),
        "median": round(float(median(heights))),
        "median_aspect": round(float(median(aspects)), 2) if aspects else 0.0,
        "floor": round(quartile * SHORTFALL_RATIO),
    }


def assess(
    measurement: dict[str, Any],
    baseline: dict[str, Any] | None,
    *,
    compare_length: bool = True,
) -> dict[str, Any]:
    """Whether this file can be read, and whether it carries a whole chapter.

    Two independent axes. Page shape is absolute and always applies, because a
    page too tall to fit a screen is unreadable in any series. Length is
    relative to the work and needs a baseline; ``compare_length=False`` turns
    it off for an extra, which has no expected length to be short of.

    ``unknown`` is not a soft ``degraded``: with no baseline, or with nothing
    measurable in the archive, there is no claim to make and the caller must
    keep the file.
    """

    height = float(measurement.get("normalized_height") or 0)
    aspect = float(measurement.get("median_aspect") or 0)
    evidence = {
        "normalized_height": round(height),
        "pages": int(measurement.get("pages") or 0),
        "measured_pages": int(measurement.get("measured_pages") or 0),
        "max_width": int(measurement.get("max_width") or 0),
        "median_width": int(measurement.get("median_width") or 0),
        "median_aspect": aspect,
        "reference_width": REFERENCE_WIDTH,
    }
    if measurement.get("measured_pages") and aspect > MAX_READABLE_ASPECT:
        return {
            "verdict": DEGRADED,
            "reason": (
                f"pages are {aspect:.0f} times taller than they are wide: the "
                "source serves un-sliced strips that no reader can fit on a screen"
            ),
            **evidence,
        }
    measured = int(measurement.get("measured_pages") or 0)
    dark_pages = int(measurement.get("dark_pages") or 0)
    if measured >= 4 and dark_pages / measured >= DARK_PAGE_RATIO:
        return {
            "verdict": DEGRADED,
            "reason": (
                f"{dark_pages} of {measured} pages are black cards (piracy "
                "notices, studio splashes): a re-cut edition of the work, not "
                "its pages"
            ),
            **evidence,
        }
    baseline_aspect = float((baseline or {}).get("median_aspect") or 0)
    # A manga page is about 1.4-1.5 tall for its width; a webtoon slice is
    # well past 2. Both bounds, so a tall-page series is not condemned.
    if (
        measured
        and 0 < baseline_aspect < 1.8
        and aspect >= max(2.2, baseline_aspect * 1.4)
    ):
        return {
            "verdict": DEGRADED,
            "reason": (
                f"pages are {aspect:.1f} times taller than wide while this "
                f"series reads in pages ({baseline_aspect:.1f}): strip slices "
                "of a webtoon edition"
            ),
            **evidence,
        }
    median_width = int(measurement.get("median_width") or 0)
    if measurement.get("measured_pages") and 0 < median_width < MIN_PAGE_WIDTH:
        return {
            "verdict": DEGRADED,
            "reason": (
                f"pages are {median_width} px wide: thumbnails or placeholders, "
                "not pages"
            ),
            **evidence,
        }
    if not compare_length:
        return {
            "verdict": OK,
            "reason": "",
            **evidence,
        }
    if baseline is None:
        return {
            "verdict": UNKNOWN,
            "reason": (
                "the series has fewer than "
                f"{MIN_BASELINE_SAMPLES} measured chapters to compare against"
            ),
            **evidence,
        }
    evidence["baseline"] = dict(baseline)
    if not measurement.get("measured_pages"):
        return {
            "verdict": UNKNOWN,
            "reason": "no page in the archive could be measured",
            **evidence,
        }
    floor = float(baseline["floor"])
    if height >= floor:
        return {"verdict": OK, "reason": "", **evidence}
    share = height / float(baseline["quartile"]) if baseline["quartile"] else 0.0
    return {
        "verdict": DEGRADED,
        "reason": (
            f"carries {share:.0%} of what this series' shorter chapters carry "
            f"({round(height)} against a floor of {round(floor)})"
        ),
        **evidence,
    }


__all__ = [
    "DEGRADED",
    "MAX_READABLE_ASPECT",
    "MIN_BASELINE_SAMPLES",
    "OK",
    "REFERENCE_WIDTH",
    "SHORTFALL_RATIO",
    "UNKNOWN",
    "assess",
    "baseline_from",
    "is_measurable",
    "measure_archive",
    "measure_pages",
]


# A source that serves chapter 12 as "12.1", "12.2", "12.3": consecutive
# tenths from one, all from the same source. Anything else that carries a
# decimal - a 12.5 side story, a 4.9 omake - is an extra and is left alone.
SPLIT_PART_PATTERN = re.compile(r"^(\d+)\.([1-9])$")


def _source_key(release: dict[str, Any]) -> str:
    return f"{release.get('provider') or ''}:{release.get('source_name') or ''}"


def split_part_groups(
    releases: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Downloaded parts N.1..N.k of one chapter, by the chapter they split.

    Two or more consecutive parts, starting at .1, from a single source: the
    shape a scanlation site gives a chapter it cut to fit its page limit.
    Judged one at a time each part is "short" by construction; together they
    either carry the chapter or they do not, and only that is worth saying.
    """

    by_key: dict[tuple[str, str], list[tuple[int, dict[str, Any]]]] = {}
    for release in releases:
        if not release.get("downloaded"):
            continue
        match = SPLIT_PART_PATTERN.match(str(release.get("chapter") or "").strip())
        if match is None:
            continue
        whole = str(int(match.group(1)))
        by_key.setdefault((whole, _source_key(release)), []).append(
            (int(match.group(2)), release)
        )
    groups: dict[str, list[dict[str, Any]]] = {}
    for (whole, _source), parts in sorted(by_key.items()):
        if whole in groups:
            continue
        fractions = sorted(fraction for fraction, _release in parts)
        if len(parts) < 2 or fractions != list(range(1, len(parts) + 1)):
            continue
        groups[whole] = [
            release for _fraction, release in sorted(parts, key=lambda p: p[0])
        ]
    return groups


def combine(measurements: list[dict[str, Any]]) -> dict[str, Any]:
    """The parts of one chapter read as a single archive."""

    return {
        "normalized_height": sum(
            float(item.get("normalized_height") or 0) for item in measurements
        ),
        "pages": sum(int(item.get("pages") or 0) for item in measurements),
        "measured_pages": sum(
            int(item.get("measured_pages") or 0) for item in measurements
        ),
        "median_aspect": max(
            (float(item.get("median_aspect") or 0) for item in measurements),
            default=0.0,
        ),
        "max_width": max(
            (int(item.get("max_width") or 0) for item in measurements), default=0
        ),
        "median_width": max(
            (int(item.get("median_width") or 0) for item in measurements), default=0
        ),
    }
