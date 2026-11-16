from types import SimpleNamespace

import cv2
import numpy as np
import pytest
from PIL import ImageDraw

from contrib import vision_balloon as vision

BLUE = np.array([40, 48, 110], dtype=np.uint8)


def oval(x=20, y=20, width=160, height=280):
    mask = np.zeros((height, width), dtype=np.uint8)
    cv2.ellipse(
        mask,
        (width // 2, height // 2),
        (width // 2 - 2, height // 2 - 2),
        0,
        0,
        360,
        1,
        -1,
    )
    return (x, y, x + width, y + height), mask, BLUE.copy()


def test_fragments_of_one_balloon_become_one_layout_area():
    bounds, whole, color = oval()
    left, right = whole.copy(), whole.copy()
    left[:, 78:] = 0
    right[:, :84] = 0
    merged = vision.merge_balloon_fragments(
        [(bounds, left, color), (bounds, right, color)], [bounds]
    )
    assert len(merged) == 1
    assert merged[0][1][140, 80] == 1
    assert merged[0][1].sum() >= whole.sum() * 0.98


def test_neighbouring_same_color_balloons_stay_separate():
    first, second = oval(), oval(x=190)
    result = vision.merge_balloon_fragments([first, second], [first[0], second[0]])
    assert len(result) == 2
    assert [shape[0] for shape in result] == [first[0], second[0]]


def test_text_color_fragment_does_not_replace_balloon_background():
    shape = oval()
    fragment = (
        (60, 80, 90, 160),
        np.ones((80, 30), np.uint8),
        np.array([250, 230, 30], np.uint8),
    )
    result = vision.merge_balloon_fragments([shape, fragment], [shape[0]])
    assert len(result) == 1
    assert np.array_equal(result[0][2], BLUE)
    assert result[0][0] == shape[0]


def test_intact_outline_wins_over_a_duplicate_with_artwork_extension():
    bounds, mask, color = oval()
    extended = np.zeros((310, 160), dtype=np.uint8)
    extended[30:] = mask
    cv2.fillConvexPoly(extended, np.array([[55, 65], [80, 0], [105, 65]]), 1)
    duplicate = ((20, -10, 180, 300), extended, color)
    result = vision.merge_balloon_fragments(
        [(bounds, mask, color), duplicate], [bounds]
    )
    assert len(result) == 1
    assert result[0][0] == bounds
    assert np.array_equal(result[0][1], mask)


@pytest.mark.asyncio
@pytest.mark.parametrize("background", [[40, 48, 110], [220, 0, 30], [190, 170, 220]])
async def test_render_groups_all_columns_and_keeps_unassigned_regions(
    monkeypatch, background
):
    source = np.full((900, 700, 3), 160, np.uint8)
    bounds, mask, color = oval(x=30, width=220, height=350)
    color = np.array(background, dtype=np.uint8)
    x, y, right, bottom = bounds
    source[y:bottom, x:right][mask != 0] = color
    # The original lettering divides the background into several color regions.
    for tx in (85, 125, 165):
        for ty in range(80, 300, 22):
            source[ty : ty + 10, tx : tx + 10] = [250, 230, 30]
    left = SimpleNamespace(
        xyxy=[80, 75, 140, 310],
        center=[110, 192],
        translation="Second paragraph stays in the same balloon.",
    )
    right_region = SimpleNamespace(
        xyxy=[150, 75, 180, 310],
        center=[165, 192],
        translation="First paragraph must not disappear.",
    )
    outside = SimpleNamespace(
        xyxy=[280, 150, 305, 250], center=[292, 200], translation="Caption"
    )
    regions = [left, right_region, outside]
    ctx = SimpleNamespace(
        img_rgb=source.copy(),
        img_inpainted=source.copy(),
        text_regions=regions,
        vision_boxes={bounds: ""},
    )
    fallback_regions = []

    async def fallback(_config, current):
        fallback_regions.extend(current.text_regions)
        return source.copy()

    printed = []
    draw_text = ImageDraw.ImageDraw.text

    def record(self, xy, text, *args, **kwargs):
        printed.append(text)
        return draw_text(self, xy, text, *args, **kwargs)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", record)
    result = await vision.render_guided(fallback, None, ctx)
    assert fallback_regions == [outside]
    assert ctx.text_regions is regions or ctx.text_regions == regions
    assert "First paragraph must not disappear." in " ".join(printed)
    assert "Second paragraph stays in the same balloon." in " ".join(printed)
    assert not np.any(np.all(result == [250, 230, 30], axis=2))
    allowed = np.zeros(source.shape[:2], dtype=bool)
    allowed[y:bottom, x:right] = mask != 0
    assert np.array_equal(result[~allowed], source[~allowed])
    assert np.any(result[allowed] != source[allowed])


def test_empty_candidates_have_no_guided_layout():
    assert vision.merge_balloon_fragments([], [(0, 0, 10, 10)]) == []
