from __future__ import annotations

import zipfile
from io import BytesIO
from pathlib import Path

from PIL import Image

from tankarr.page_quality import (
    DEGRADED,
    MIN_BASELINE_SAMPLES,
    OK,
    UNKNOWN,
    assess,
    baseline_from,
    combine,
    measure_archive,
    measure_pages,
    split_part_groups,
)


def page(width: int, height: int) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (width, height), "white").save(buffer, format="PNG")
    return buffer.getvalue()


def write_pages(directory: Path, sizes: list[tuple[int, int]]) -> list[Path]:
    directory.mkdir(parents=True, exist_ok=True)
    paths = []
    for index, (width, height) in enumerate(sizes, start=1):
        path = directory / f"{index:04d}.png"
        path.write_bytes(page(width, height))
        paths.append(path)
    return paths


def write_cbz(path: Path, sizes: list[tuple[int, int]]) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("ComicInfo.xml", b"<ComicInfo />")
        for index, (width, height) in enumerate(sizes, start=1):
            archive.writestr(f"{index:04d}.png", page(width, height))
    return path


def measurement(height: int) -> dict[str, int]:
    return {
        "normalized_height": height,
        "pages": 10,
        "measured_pages": 10,
        "max_width": 800,
        "median_width": 800,
    }


def test_normalized_height_is_independent_of_the_width_a_source_serves(tmp_path: Path):
    # The same strip at half the width is the same amount of comic.
    wide = measure_pages(write_pages(tmp_path / "wide", [(1600, 4000)]))
    narrow = measure_pages(write_pages(tmp_path / "narrow", [(800, 2000)]))

    assert wide["normalized_height"] == narrow["normalized_height"] == 2000
    assert wide["max_width"] == 1600


def test_a_measurement_reports_pages_it_could_not_read(tmp_path: Path):
    archive_path = tmp_path / "partly-unreadable.cbz"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("0001.png", page(800, 1200))
        archive.writestr("0002.png", b"not an image at all")

    result = measure_archive(archive_path)

    assert result["pages"] == 2
    assert result["measured_pages"] == 1
    assert result["normalized_height"] == 1200


def test_no_baseline_below_the_minimum_sample():
    assert baseline_from(
        measurement(1000) for _ in range(MIN_BASELINE_SAMPLES - 1)
    ) is (None)
    baseline = baseline_from([measurement(1000) for _ in range(MIN_BASELINE_SAMPLES)])
    assert baseline is not None
    assert baseline["samples"] == MIN_BASELINE_SAMPLES


def test_a_series_without_a_baseline_is_never_condemned():
    verdict = assess(measurement(1), None)

    assert verdict["verdict"] == UNKNOWN
    assert "fewer than" in verdict["reason"]


def test_the_omniscient_reader_split_is_the_case_this_separates():
    # The measured library: 311 good chapters between 83k and 171k, and the
    # seventeen comickfan fragments between 1.1k and 6k.
    good = [measurement(height) for height in (83_374, 132_042, 149_914, 171_081) * 20]
    baseline = baseline_from(good)
    assert baseline is not None

    assert assess(measurement(83_374), baseline)["verdict"] == OK
    fragment = assess(measurement(5_971), baseline)
    assert fragment["verdict"] == DEGRADED
    assert "shorter chapters" in fragment["reason"]


def test_a_quarter_of_degraded_samples_does_not_move_the_floor():
    # The state the library is in before the first audit: the bad files are
    # already inside the baseline. p25 has to survive them.
    contaminated = [measurement(1_500) for _ in range(25)] + [
        measurement(120_000) for _ in range(75)
    ]
    baseline = baseline_from(contaminated)
    assert baseline is not None

    assert assess(measurement(1_500), baseline)["verdict"] == DEGRADED
    assert assess(measurement(120_000), baseline)["verdict"] == OK


def test_a_short_print_chapter_is_not_degraded():
    # HUNTER×HUNTER as measured: chapters from 9_599 to 39_600 normalized.
    baseline = baseline_from(
        [measurement(height) for height in (17_994, 19_194, 22_184, 39_600) * 20]
    )
    assert baseline is not None

    assert assess(measurement(9_599), baseline)["verdict"] == OK


def test_an_archive_with_nothing_measurable_yields_no_verdict(tmp_path: Path):
    archive_path = tmp_path / "unreadable.cbz"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("0001.png", b"junk")
    baseline = baseline_from([measurement(100_000) for _ in range(20)])

    verdict = assess(measure_archive(archive_path), baseline)

    assert verdict["verdict"] == UNKNOWN
    assert verdict["reason"] == "no page in the archive could be measured"


def test_measuring_an_archive_matches_measuring_its_pages(tmp_path: Path):
    sizes = [(800, 1280), (720, 720), (1424, 720)]
    pages = write_pages(tmp_path / "pages", sizes)
    archive_path = write_cbz(tmp_path / "same.cbz", sizes)

    assert (
        measure_archive(archive_path)["normalized_height"]
        == measure_pages(pages)["normalized_height"]
    )


def strip_measurement(aspect: float, pages: int = 10) -> dict[str, float]:
    return {
        "normalized_height": round(800 * aspect * pages),
        "pages": pages,
        "measured_pages": pages,
        "max_width": 800,
        "median_width": 800,
        "median_aspect": aspect,
    }


def test_un_sliced_strips_are_unreadable_whatever_the_chapter_carries():
    # Omniscient Reader c308 as measured: ten pages of 800x15711 from
    # flamecomics, the same episode WEBTOON serves as 121 tiles of 800x1280.
    # The comic is all there; it cannot be shown on a screen.
    baseline = baseline_from([measurement(150_000) for _ in range(20)])

    verdict = assess(strip_measurement(18.2), baseline)

    assert verdict["verdict"] == DEGRADED
    assert "taller than they are wide" in verdict["reason"]


def test_a_normal_webtoon_tile_is_not_a_strip():
    baseline = baseline_from([measurement(150_000) for _ in range(20)])

    # 800x1280 tiles, and the tallest good page measured in the library.
    assert assess(strip_measurement(1.6, pages=121), baseline)["verdict"] == OK
    assert assess(strip_measurement(2.32, pages=41), baseline)["verdict"] == OK


def test_page_shape_is_judged_without_any_baseline():
    # Unreadable is a property of screens, so it needs no series to compare to.
    verdict = assess(strip_measurement(18.2), None)

    assert verdict["verdict"] == DEGRADED


def test_an_extra_is_still_judged_on_shape_but_not_on_length():
    baseline = baseline_from([measurement(150_000) for _ in range(20)])

    short = assess(strip_measurement(1.6, pages=2), baseline, compare_length=False)
    assert short["verdict"] == OK

    strip = assess(strip_measurement(18.2, pages=2), baseline, compare_length=False)
    assert strip["verdict"] == DEGRADED


def test_one_tall_banner_does_not_condemn_a_sliced_chapter():
    from tankarr.page_quality import _summarize

    # c308 opens with a 1778x1000 banner; the median is what the chapter reads
    # like, so a single outlier page cannot decide either way.
    sliced = _summarize([(1778, 1000)] + [(800, 1280)] * 120)
    assert sliced["median_aspect"] == 1.6

    strips = _summarize([(1778, 1000)] + [(800, 15711)] * 9)
    assert strips["median_aspect"] > 5


def release(chapter: str, *, source: str = "flame", downloaded: bool = True):
    return {
        "id": f"{source}-{chapter}",
        "chapter": chapter,
        "provider": "suwayomi",
        "source_name": source,
        "downloaded": downloaded,
    }


def test_split_parts_are_consecutive_tenths_from_one_source():
    groups = split_part_groups(
        [
            release("12.1"),
            release("12.2"),
            release("12.3"),
            release("13"),
            release("14.5"),  # an extra, not a part
            release("20.1"),  # alone: nothing to add up
            release("21.2"),  # .2 without .1: not a split
            release("30.1", source="asura"),
            release("30.2", source="mangapill"),  # two sources are not one split
            release("40.1", downloaded=False),
            release("40.2"),
        ]
    )

    assert set(groups) == {"12"}
    assert [part["id"] for part in groups["12"]] == [
        "flame-12.1",
        "flame-12.2",
        "flame-12.3",
    ]


def test_parts_are_judged_as_one_archive():
    good = [measurement(80_000) for _ in range(MIN_BASELINE_SAMPLES)]
    baseline = baseline_from(good)
    assert baseline is not None

    whole = combine([measurement(30_000), measurement(30_000), measurement(25_000)])
    assert whole["normalized_height"] == 85_000
    assert assess(whole, baseline)["verdict"] == OK

    fragments = combine([measurement(5_000), measurement(6_000)])
    assert assess(fragments, baseline)["verdict"] == DEGRADED


def test_a_prologue_has_no_expected_length():
    from tankarr.page_quality import is_measurable

    assert is_measurable("12")
    assert not is_measurable("12.5")
    assert not is_measurable("0")  # the WEBTOON prologue is short by nature
    assert not is_measurable("")


def test_a_double_page_spread_counts_as_two_pages(tmp_path: Path):
    # ONE PIECE 554, live: 7 spreads (2133x1595) and 2 pages (1066x1600) read
    # as 31% of a chapter and were refused. They are sixteen pages.
    singles = write_pages(tmp_path / "singles", [(1066, 1600)] * 16)
    spreads = write_pages(tmp_path / "spreads", [(2133, 1595)] * 7 + [(1066, 1600)] * 2)

    as_singles = measure_pages(singles)
    as_spreads = measure_pages(spreads)

    assert as_spreads["pages"] == 9  # nine files
    assert as_spreads["measured_pages"] == 9
    assert abs(as_spreads["normalized_height"] - as_singles["normalized_height"]) < (
        as_singles["normalized_height"] * 0.02
    )


def strip(path: Path, width: int, panels: int, panel_height: int, gutter: int) -> Path:
    """A webtoon strip: black panels separated by white gutters."""
    from PIL import ImageDraw

    height = panels * panel_height + (panels + 1) * gutter
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    top = gutter
    for _ in range(panels):
        draw.rectangle((0, top, width - 1, top + panel_height - 1), fill="black")
        top += panel_height + gutter
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, format="PNG")
    return path


def test_an_un_sliced_strip_is_cut_into_pages_on_its_gutters(tmp_path: Path):
    from tankarr.page_quality import MAX_READABLE_ASPECT, slice_strips

    original = strip(
        tmp_path / "0001.png", width=800, panels=12, panel_height=900, gutter=60
    )
    normal = write_pages(tmp_path / "rest", [(800, 1200)])[0]
    before = measure_pages([original, normal])
    assert before["median_aspect"] > MAX_READABLE_ASPECT

    pages, sliced = slice_strips([original, normal])

    assert sliced == 1
    assert not original.exists()
    assert pages[-1] == normal
    pieces = pages[:-1]
    assert len(pieces) >= 6
    for piece in pieces:
        with Image.open(piece) as image:
            width, height = image.size
            assert height <= width * MAX_READABLE_ASPECT
            # Every cut landed on a gutter: the first and last rows are white.
            assert image.getpixel((width // 2, 0)) == (255, 255, 255)
            assert image.getpixel((width // 2, height - 1)) == (255, 255, 255)
    after = measure_pages(pages)
    assert after["median_aspect"] <= MAX_READABLE_ASPECT
    # Nothing was lost: the strip's content is spread over its pieces.
    assert abs(after["normalized_height"] - before["normalized_height"]) < 5


def test_thumbnail_tiles_are_never_a_chapter(tmp_path: Path):
    # And chapter 17, live: 42 identical 200x200 tiles, 832 bytes each,
    # imported because the series had no baseline yet. Width is absolute.
    from tankarr.page_quality import MIN_PAGE_WIDTH

    tiles = write_pages(tmp_path / "tiles", [(200, 200)] * 42)
    verdict = assess(measure_pages(tiles), None)
    assert verdict["verdict"] == DEGRADED
    assert "px wide" in verdict["reason"]
    assert MIN_PAGE_WIDTH > 200
    real = write_pages(tmp_path / "real", [(1066, 1600)] * 10)
    assert assess(measure_pages(real), None)["verdict"] != DEGRADED


def black_card(width: int, height: int) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (width, height), "black").save(buffer, format="PNG")
    return buffer.getvalue()


def test_black_cards_mark_a_recut_edition(tmp_path: Path):
    """1Manga served Blade of the Phantom Master as webtoon episodes: a
    "pirating content is illegal" card, panels, a studio splash. Pages by
    shape, another work by content."""

    from tankarr.page_quality import DEGRADED, OK, assess, measure_pages

    directory = tmp_path / "episode"
    directory.mkdir()
    sizes = [(800, 1150)] * 10
    paths = []
    for index, (width, height) in enumerate(sizes, start=1):
        path = directory / f"{index:04d}.png"
        path.write_bytes(
            black_card(width, height) if index in (1, 10) else page(width, height)
        )
        paths.append(path)
    measured = measure_pages(paths)
    assert measured["dark_pages"] == 2
    verdict = assess(measured, None, compare_length=False)
    assert verdict["verdict"] == DEGRADED and "black cards" in verdict["reason"]
    # One dark page in ten (a real night scene, an end slate) is fine.
    paths[9].write_bytes(page(800, 1150))
    assert assess(measure_pages(paths), None, compare_length=False)["verdict"] == OK


def test_strip_slices_in_a_paged_series_are_another_edition():
    from tankarr.page_quality import DEGRADED, OK, assess, baseline_from

    paged = baseline_from(
        [{**measurement(11000), "median_aspect": 1.45} for _ in range(10)]
    )
    assert paged is not None and paged["median_aspect"] == 1.45
    slices = {**measurement(11000), "median_aspect": 2.6}
    verdict = assess(slices, paged)
    assert verdict["verdict"] == DEGRADED and "strip slices" in verdict["reason"]
    # The same slices in a series that itself reads in strips are normal.
    strips = baseline_from(
        [{**measurement(11000), "median_aspect": 2.5} for _ in range(10)]
    )
    assert assess(slices, strips)["verdict"] == OK
