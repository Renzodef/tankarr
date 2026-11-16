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


def test_printed_column_opening_in_oval_does_not_split_english_layout():
    bounds, whole, color = oval()
    broken = whole.copy()
    broken[:150, 95:120] = 0
    result = vision.merge_balloon_fragments([(bounds, broken, color)], [bounds])
    assert result[0][1][80, 110] == 1
    assert result[0][1].sum() >= whole.sum() * 0.98


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


def test_narrow_oval_fragment_does_not_leave_the_last_text_column():
    bounds, complete, color = oval(width=180)
    complete[125:145, 172:] = 0
    narrow = oval(width=150)
    result = vision.merge_balloon_fragments(
        [(bounds, complete, color), narrow], [bounds]
    )
    assert len(result) == 1
    assert result[0][0] == bounds
    # The rightmost text column lies inside the full balloon, outside the
    # deceptively regular narrower fragment.
    assert result[0][1][160, 165] == 1
    assert result[0][1][0, 0] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("background", [[40, 48, 110], [220, 0, 30], [190, 170, 220]])
@pytest.mark.parametrize("empty_guidance", [False, True])
async def test_render_groups_all_columns_and_keeps_unassigned_regions(
    monkeypatch, background, empty_guidance
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
        vision_boxes={} if empty_guidance else {bounds: ""},
    )
    # Simulate native removal spilling from the balloon into adjacent artwork.
    ctx.img_inpainted[20:40, 260:275] = 255
    # Native caption removal must still reach the fallback renderer.
    ctx.img_inpainted[160:170, 285:295] = 255
    original_inpainted = ctx.img_inpainted
    fallback_regions = []

    async def fallback(_config, current):
        fallback_regions.extend(current.text_regions)
        assert np.array_equal(
            current.img_inpainted[20:40, 260:275], source[20:40, 260:275]
        )
        assert np.all(current.img_inpainted[160:170, 285:295] == 255)
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
    assert ctx.img_inpainted is original_inpainted
    assert "First paragraph must not disappear." in " ".join(printed)
    assert "Second paragraph stays in the same balloon." in " ".join(printed)
    assert not np.any(np.all(result == [250, 230, 30], axis=2))
    allowed = np.zeros(source.shape[:2], dtype=bool)
    allowed[y:bottom, x:right] = mask != 0
    assert np.array_equal(result[~allowed], source[~allowed])
    assert np.any(result[allowed] != source[allowed])


def test_empty_candidates_have_no_guided_layout():
    assert vision.merge_balloon_fragments([], [(0, 0, 10, 10)]) == []


def test_panel_geometry_is_replaced_by_separate_ocr_guidance():
    regions = [
        SimpleNamespace(xyxy=[30, 30, 60, 160]),
        SimpleNamespace(xyxy=[200, 50, 240, 180]),
    ]
    boxes = vision.supported_balloon_boxes([(0, 0, 700, 800)], regions, 700, 800)
    assert len(boxes) == 2
    assert vision.intersection(boxes[0], boxes[1]) == 0
    for region, box in zip(regions, boxes):
        assert vision.intersection(region.xyxy, box) == vision.intersection(
            region.xyxy, region.xyxy
        )


def test_irregular_balloon_letters_stay_inside_mask():
    from PIL import Image

    mask = np.zeros((260, 180), dtype=np.uint8)
    cv2.fillPoly(
        mask,
        [np.array([[5, 5], [160, 25], [140, 100], [175, 230], [10, 250], [40, 150]])],
        1,
    )
    text = "A longer dialogue must remain entirely inside this irregular speech balloon without losing words."
    fitted = vision.fit_mask(text, mask)
    assert fitted is not None
    font, rows = fitted
    assert " ".join(row[2] for row in rows) == text
    canvas = Image.new("L", (180, 260))
    draw = ImageDraw.Draw(canvas)
    for cx, top, line in rows:
        draw.text((cx, top), line, anchor="mt", font=font, fill=255)
    pixels = np.asarray(canvas)
    assert not np.any(pixels[mask == 0])


def test_irregular_outline_does_not_fill_its_artwork_notch():
    mask = np.ones((200, 150), dtype=np.uint8)
    mask[60:140, :45] = 0
    bounds = (10, 20, 160, 220)
    result = vision.merge_balloon_fragments([(bounds, mask, BLUE)], [bounds])
    assert result[0][1][100, 20] == 0


def test_unfittable_word_keeps_native_rendering_available():
    assert (
        vision.fit_mask("Unbreakablewordthatcannotfit", np.ones((30, 30), np.uint8))
        is None
    )


def test_tight_complete_contour_wins_over_colour_bleed():
    bounds = (10, 10, 160, 210)
    mask = np.ones((200, 150), dtype=np.uint8)
    mask[60:140, :30] = 0
    wider = np.ones((220, 180), dtype=np.uint8)
    regions = [SimpleNamespace(xyxy=[60, 40, 120, 170])]
    result = vision.merge_balloon_fragments(
        [(bounds, mask, BLUE), ((0, 0, 180, 220), wider, BLUE)],
        [(20, 20, 150, 200)],
        regions,
    )
    assert result[0][0] == bounds
    assert result[0][1][100, 10] == 0


def test_long_word_in_narrow_balloon_can_wrap_without_losing_letters():
    mask = np.ones((240, 75), np.uint8)
    text = "You fundamentally cannot overcome that evil power within you!"
    fitted = vision.fit_mask(text, mask)
    assert fitted is not None
    _font, rows = fitted
    assert "".join(row[2] for row in rows).replace("-", "").replace(
        " ", ""
    ) == text.replace(" ", "")


def test_em_dash_is_a_safe_line_break_in_narrow_balloon():
    fitted = vision.fit_mask(
        "Do not deceive yourself—he really is already dead.",
        np.ones((160, 85), np.uint8),
    )
    assert fitted is not None
    assert (
        "".join(row[2] for row in fitted[1]).replace(" ", "")
        == "Donotdeceiveyourself—hereallyisalreadydead."
    )
