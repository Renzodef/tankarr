"""Optional OpenAI-compatible vision guidance for MIT OCR and lettering."""

from __future__ import annotations

import base64
import io
import json
import os
import re
from collections import defaultdict
from difflib import SequenceMatcher
from functools import lru_cache

import httpx

THUMBNAIL = int(os.environ.get("TANKARR_VISION_THUMBNAIL", "1440"))

GEOMETRY_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "balloon_geometry",
        "schema": {
            "type": "object",
            "required": ["boxes"],
            "additionalProperties": False,
            "properties": {
                "boxes": {
                    "type": "array",
                    "maxItems": 28,
                    "items": {
                        "type": "array",
                        "minItems": 4,
                        "maxItems": 4,
                        "items": {"type": "integer", "minimum": 0, "maximum": 1000},
                    },
                }
            },
        },
    },
}


class InvalidBalloonGeometry(ValueError):
    """The provider answered both requests without usable page geometry."""


def intersection(a, b):
    return max(0.0, min(a[2], b[2]) - max(a[0], b[0])) * max(
        0.0, min(a[3], b[3]) - max(a[1], b[1])
    )


def parse_boxes(content, width, height, *, geometry_only=False):
    match = re.search(r"\[[\s\S]*\]", content)
    if match is None:
        raise ValueError("Vision provider did not return a box list")
    entries = json.loads(match.group())
    if geometry_only and isinstance(entries, list):
        entries = [{"bbox_2d": entry} for entry in entries]

    if not isinstance(entries, list) or len(entries) > 72:
        raise ValueError("Vision provider returned an invalid box list")
    boxes = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        coords = entry.get("bbox_2d")
        if not isinstance(coords, list) or len(coords) != 4:
            continue
        if not all(type(value) is int and 0 <= value <= 1000 for value in coords):
            continue
        x1, y1, x2, y2 = coords
        if not (x1 < x2 and y1 < y2 and (x2 - x1) * (y2 - y1) <= 320_000):
            continue
        text = entry.get("text")
        boxes[
            (
                x1 * width / 1000,
                y1 * height / 1000,
                x2 * width / 1000,
                y2 * height / 1000,
            )
        ] = text.strip() if isinstance(text, str) and len(text) <= 1000 else ""
    if entries and not boxes:
        raise ValueError("Vision provider returned no usable geometry")
    return boxes


async def analyze_page(ctx, vision, source_language):
    from PIL import Image

    original = Image.fromarray(ctx.img_rgb)
    page = original.copy()
    # 2026-11-17: MLX Qwen has no --image-min-tokens; 1440 px gives ~1300 image
    # tokens (>= 1024 needed for grounding) on a 980x1426 page.
    page.thumbnail((THUMBNAIL, THUMBNAIL))
    compressed = io.BytesIO()
    page.save(compressed, format="JPEG", quality=90)
    image = "data:image/jpeg;base64," + base64.b64encode(compressed.getvalue()).decode()
    prompt = (
        f"Find the speech balloons and narration captions printed in {source_language}. "
        "Return only a JSON array of objects with bbox_2d (the whole balloon including "
        "margins, [x1,y1,x2,y2] normalized to 0..1000) and text (exact original text, "
        "empty if unreadable). Ignore artwork and page numbers. Do not translate."
    )
    headers = (
        {"Authorization": f"Bearer {vision['api_key']}"}
        if vision.get("api_key")
        else {}
    )
    boxes = None
    # CPU vision workers can spend several minutes encoding a full page before
    # producing tokens. Keep connection failures quick while allowing inference.
    timeout = httpx.Timeout(600, connect=10)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        geometry_prompt = (
            'Return a compact JSON object with key "boxes" and a list of up to 28 '
            "speech balloon or caption boxes, found in reading order. Each box "
            "is [x1,y1,x2,y2] on a 0..1000 scale. Omit all text and prose."
        )
        for instruction, geometry_only in (
            # MIT already provides OCR. Ask for compact geometry first so CPU
            # vision workers do not transcribe the whole page a second time.
            (geometry_prompt, True),
            (prompt + " Return compact JSON and at most 40 objects.", False),
        ):
            options = {"response_format": GEOMETRY_FORMAT} if geometry_only else {}
            response = await client.post(
                vision["base_url"].rstrip("/") + "/chat/completions",
                headers=headers,
                json={
                    "model": vision["model"],
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {"type": "image_url", "image_url": {"url": image}},
                                {"type": "text", "text": instruction},
                            ],
                        }
                    ],
                    "temperature": 0,
                    "max_tokens": 2400 if not geometry_only else 1200,
                    **options,
                },
            )
            if geometry_only and response.status_code in {400, 422}:
                continue
            response.raise_for_status()
            try:
                choice = response.json()["choices"][0]
                boxes = parse_boxes(
                    choice["message"]["content"],
                    *original.size,
                    geometry_only=geometry_only,
                )
            except (IndexError, KeyError, TypeError, ValueError):
                continue
            break
    if boxes is None:
        raise InvalidBalloonGeometry(
            "Vision provider returned no usable balloon geometry"
        )
    ctx.vision_boxes = boxes
    used = set()
    for region in ctx.text_regions:
        rect = tuple(map(float, region.xyxy))
        area = intersection(rect, rect)
        if not area:
            continue
        ranked = [
            (intersection(rect, box) / area, box, text)
            for box, text in ctx.vision_boxes.items()
            if box not in used
        ]
        confidence, box, text = max(ranked, default=(0, None, ""))
        source = re.sub(r"\W+", "", region.text, flags=re.UNICODE)
        candidate = re.sub(r"\W+", "", text, flags=re.UNICODE)
        if (
            confidence >= 0.72
            and min(len(source), len(candidate)) >= 4
            and 0.8 <= len(candidate) / len(source) <= 1.25
            and SequenceMatcher(None, source, candidate, autojunk=False).ratio() >= 0.7
        ):
            region.text = text
            used.add(box)
    if SOUND_EFFECTS and str(source_language or "").split("-")[0].casefold() == "ja":
        try:
            await find_sound_effects(ctx, vision, image, headers, timeout)
        except Exception:  # noqa: BLE001 - sound effects are optional; the page goes on without them
            pass


# 2026-11-17 (Miles, Renzo): sound effects drawn in kana (ドスン, バタタ, ブバーン) are artwork the
# text detector does not see, so they stayed Japanese. In a Japanese book the vision model is
# asked for them once per page; each becomes a region the provider translates with the page
# (as English onomatopoeia, see the Japanese prompt note). Nothing is erased: MIT has no text
# mask there, and the English is lettered over the original effect.
# Off by default until reviewed: on the Miles trial half of the effects came back right
# (RATTATAT, THUD), half split or misread (ド|スン, むく read as お|く). TANKARR_SOUND_EFFECTS=1 enables it.
SOUND_EFFECTS = os.environ.get("TANKARR_SOUND_EFFECTS", "0") == "1"
SFX_PROMPT = (
    "Find the sound effects drawn as part of the artwork (large hand-drawn katakana or hiragana "
    "onomatopoeia such as ドスン, バタタ, ギャ), not the text inside speech balloons or captions. "
    "Return only a JSON array of objects with bbox_2d ([x1,y1,x2,y2] normalized to 0..1000, tight "
    "around the letters) and text (the kana). Return [] if there are none."
)
SFX_TEXT = re.compile(
    r"[\u3040-\u30ff\u4e00-\u9fff\uff66-\uff9fー〜～ッっ!！?？…・゛゜]{1,10}"
)


async def find_sound_effects(ctx, vision, image, headers, timeout):
    import numpy as np
    from manga_translator.utils import TextBlock

    height, width = np.asarray(ctx.img_rgb).shape[:2]
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        response = await client.post(
            vision["base_url"].rstrip("/") + "/chat/completions",
            headers=headers,
            json={
                "model": vision["model"],
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "image_url", "image_url": {"url": image}},
                            {"type": "text", "text": SFX_PROMPT},
                        ],
                    }
                ],
                "temperature": 0,
                "max_tokens": 600,
            },
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
    try:
        found = parse_boxes(content, width, height)
    except ValueError:
        return
    taken = [tuple(map(float, region.xyxy)) for region in ctx.text_regions]
    effects = []
    for (x1, y1, x2, y2), text in found.items():
        text = re.sub(r"\s+", "", text or "")
        box = (x1, y1, x2, y2)
        area = (x2 - x1) * (y2 - y1)
        if (
            not SFX_TEXT.fullmatch(text)
            or not re.search(r"[\u3040-\u30ff\uff66-\uff9f]", text)
            or area <= 0
            or area > 0.12 * width * height
            or min(x2 - x1, y2 - y1) < 12
            or any(intersection(box, other) > 0.2 * area for other in taken + effects)
        ):
            continue
        effects.append(box)
        x1, y1, x2, y2 = map(int, box)
        region = TextBlock(
            [[[x1, y1], [x2, y1], [x2, y2], [x1, y2]]],
            [text],
            language="ja",
            font_size=max(12, min(x2 - x1, y2 - y1)),
            source_lang="ja",
        )
        region.tankarr_sfx = True
        ctx.text_regions.append(region)
        if len(effects) >= 8:
            break


def sfx_font(size):
    from PIL import ImageFont

    for name in (
        "DejaVuSans-Bold.ttf",
        "DejaVuSans.ttf",
        "fonts/Arial-Unicode-Regular.ttf",
    ):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def draw_sound_effects(canvas, effects):
    """Letter each translated effect over its original: bold capitals, white with a dark outline."""
    from PIL import ImageDraw

    draw = ImageDraw.Draw(canvas)
    for region in effects:
        text = str(getattr(region, "translation", "") or "").strip().strip("\"'")
        if (
            not text
            or not re.search(r"[A-Za-z]", text)
            or re.search(r"[\u3040-\u30ff]", text)
        ):
            continue
        text = text.upper()
        x1, y1, x2, y2 = map(int, region.xyxy)
        w, h = max(1, x2 - x1), max(1, y2 - y1)
        size = max(12, min(40, int(min(h, w * 1.6 / max(1, len(text))))))
        font = sfx_font(size)
        while size > 12 and font.getlength(text) > max(w, h) * 1.1:
            size -= 2
            font = sfx_font(size)
        stroke = max(2, size // 8)
        draw.text(
            ((x1 + x2) / 2, (y1 + y2) / 2),
            text,
            anchor="mm",
            font=font,
            fill="#ffffff",
            stroke_width=stroke,
            stroke_fill="#111111",
        )


def fit_strip(text, length, thickness):
    """Lines of text read along a strip `length` px long and `thickness` px wide."""
    from PIL import ImageFont

    words = text.split()
    for size in range(min(26, thickness - 2), 9, -1):
        try:
            font = ImageFont.truetype("DejaVuSans.ttf", size)
        except OSError:
            font = ImageFont.load_default(size=size)
        rows, line = [], ""
        for word in words:
            candidate = (line + " " + word).strip()
            if font.getlength(candidate) <= length:
                line = candidate
                continue
            if not line:
                rows = None
                break
            rows.append(line)
            line = word
        if rows is None:
            continue
        rows.append(line)
        if len(rows) * (size + 3) <= thickness:
            return font, rows, size
    return None


def fit_mask(text, mask):
    """Wrap each line against the safe interior of an irregular balloon."""
    import numpy as np
    from PIL import ImageFont

    height, width = mask.shape
    words = re.sub(r"([—–])(?=\S)", r"\1 ", text).split()
    if not words:
        return None
    # 2026-11-17: short lines (sound effects, interjections) may go down to 8 px
    # rather than leave the balloon to a renderer that splits "Shhk!!" in two.
    smallest = 8 if len(text) <= 16 else 11
    # Exact vertical-window queries: repeated candidate layouts used to scan
    # the same mask rows for every line. Count excluded pixels once, preserving
    # the irregular contour and all safety margins.
    excluded = np.zeros((height + 1, width), dtype=np.uint32)
    np.cumsum(mask == 0, axis=0, dtype=np.uint32, out=excluded[1:])
    for size in range(min(32, height), smallest - 1, -1):
        try:
            font = ImageFont.truetype("DejaVuSans.ttf", size)
        except OSError:
            try:
                # MIT ships this font, including punctuation missing from
                # Pillow's small fallback font on some worker installations.
                font = ImageFont.truetype("fonts/Arial-Unicode-Regular.ttf", size)
            except OSError:
                font = ImageFont.load_default(size=size)
        length = lru_cache(maxsize=1024)(font.getlength)

        @lru_cache(maxsize=256)
        def span(y):
            safe = excluded[min(height, y + size + 2)] == excluded[y]
            edges = np.diff(np.r_[False, safe, False].astype(int))
            starts = np.flatnonzero(edges == 1)
            ends = np.flatnonzero(edges == -1)
            return max(
                zip(starts, ends), key=lambda pair: pair[1] - pair[0], default=None
            )

        step = size + 3
        for row_count in range(1, height // step + 1):
            top = (height - row_count * step) // 2
            rows, pending = [], list(words)
            for row in range(row_count):
                y = top + row * step
                bounds = span(y)
                if bounds is None:
                    break
                left, right = bounds
                available = right - left - 6
                line = ""
                while pending:
                    candidate = (line + " " + pending[0]).strip()
                    if length(candidate) > available:
                        # Only split a long word after all ordinary layouts
                        # have been tried. This keeps narrow balloons inside
                        # their contours without dropping dialogue.
                        punctuated = re.fullmatch(
                            r"([^\w\s]*)([^\W\d_]+)([^\w\s]*)", pending[0]
                        )
                        if not line and size == smallest and punctuated:
                            prefix, word, suffix = punctuated.groups()
                            for cut in range(len(word) - 3, 2, -1):
                                fragment = prefix + word[:cut] + "-"
                                if length(fragment) <= available:
                                    line = fragment
                                    pending[0] = word[cut:] + suffix
                                    break
                        break
                    line = candidate
                    pending.pop(0)
                if not line:
                    break
                rows.append(((left + right) / 2, y, line))
                if not pending:
                    return font, rows
    return None


def dark_palette_masks(source, dark):
    """Separate flat ink colors when their hue band also contains artwork."""
    import cv2
    import numpy as np

    samples = source[dark].astype(np.int32) // 16
    packed = samples @ np.array([256, 16, 1])
    counts = np.bincount(packed, minlength=4096)
    best = np.argsort(counts)[-4:]
    for index in best:
        if counts[index] < source.shape[0] * source.shape[1] * 0.002:
            continue
        red, green, blue = int(index // 256), int(index // 16 % 16), int(index % 16)
        color = np.array([red, green, blue]) * 16 + 8
        close_color = cv2.inRange(
            source, np.maximum(color - 16, 0), np.minimum(color + 16, 255)
        )
        yield cv2.morphologyEx(
            close_color, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8)
        ).astype(bool)


def ellipse_fit_score(mask):
    import cv2
    import numpy as np

    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return 0.0
    contour = max(contours, key=cv2.contourArea)
    if len(contour) < 5:
        return 0.0
    fitted = np.zeros_like(mask)
    cv2.ellipse(fitted, cv2.fitEllipse(contour), 1, -1)
    union = np.count_nonzero((fitted != 0) | (mask != 0))
    return np.count_nonzero((fitted != 0) & (mask != 0)) / union if union else 0.0


def vision_palette_masks(source, boxes):
    """Include vivid and pastel balloon colors that fixed HSV bands miss."""
    import cv2
    import numpy as np

    height, width = source.shape[:2]
    colors = set()
    for box in boxes:
        x1, y1, x2, y2 = map(int, box)
        clip = source[max(0, y1) : min(height, y2), max(0, x1) : min(width, x2)]
        if clip.size == 0:
            continue
        samples = clip.reshape(-1, 3).astype(np.int32) // 16
        counts = np.bincount(samples @ np.array([256, 16, 1]), minlength=4096)
        for index in np.argsort(counts)[-2:]:
            if counts[index] < len(samples) * 0.1 or int(index) in colors:
                continue
            colors.add(int(index))
            color = np.array([index // 256, index // 16 % 16, index % 16]) * 16 + 8
            for tolerance in (16, 32):
                pixels = cv2.inRange(
                    source,
                    np.maximum(color - tolerance, 0),
                    np.minimum(color + tolerance, 255),
                )
                yield cv2.morphologyEx(
                    pixels, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8)
                ).astype(bool)


def supported_balloon_boxes(boxes, regions, width, height):
    """Reject panel-sized guidance and recover boxes around omitted OCR text."""
    rects = [tuple(map(float, region.xyxy)) for region in regions]
    supported = []
    for box in boxes:
        covered = [
            rect
            for rect in rects
            if intersection(rect, rect) > 0
            and intersection(rect, box) / intersection(rect, rect) >= 0.5
        ]
        text_area = sum(intersection(rect, box) for rect in covered)
        if text_area and intersection(box, box) <= 6 * text_area:
            supported.append(box)
    for rect in rects:
        area = intersection(rect, rect)
        if not area or any(intersection(rect, box) / area >= 0.7 for box in supported):
            continue
        x1, y1, x2, y2 = rect
        dx, dy = max(16, (x2 - x1) * 0.2), max(16, (y2 - y1) * 0.12)
        supported.append(
            (
                max(0, x1 - dx),
                max(0, y1 - dy),
                min(width, x2 + dx),
                min(height, y2 + dy),
            )
        )
    return supported


def mask_coverage(bounds, mask, rect):
    left, top, right, bottom = bounds
    x1, y1, x2, y2 = rect
    area = (x2 - x1) * (y2 - y1)
    crop_left, crop_right = max(left, x1), min(right, x2)
    crop_top, crop_bottom = max(top, y1), min(bottom, y2)
    if area <= 0 or crop_left >= crop_right or crop_top >= crop_bottom:
        return 0
    return (
        float(
            mask[
                crop_top - top : crop_bottom - top, crop_left - left : crop_right - left
            ].sum()
        )
        / area
    )


def merge_balloon_fragments(shapes, boxes, regions=()):
    """Combine compatible color fragments belonging to one vision balloon."""
    import cv2
    import numpy as np

    groups = defaultdict(list)
    for bounds, mask, color in shapes:
        # Printed columns can leave a deep opening in an otherwise oval
        # background. Recover only near-elliptical hulls; jagged dialogue
        # balloons must retain their intentional concavities.
        points = cv2.findNonZero(mask)
        if points is not None and ellipse_fit_score(mask) < 0.97:
            hull = np.zeros_like(mask)
            cv2.fillConvexPoly(hull, cv2.convexHull(points), 1)
            if mask.sum() >= 0.8 * hull.sum() and ellipse_fit_score(hull) >= 0.97:
                mask = hull
        shape = (bounds, mask, color)
        bounds = shape[0]
        ranked = []
        for index, box in enumerate(boxes):
            overlap = intersection(bounds, box)
            union = intersection(bounds, bounds) + intersection(box, box) - overlap
            if union > 0:
                ranked.append((overlap / union, index))
        score, index = max(ranked, default=(0, -1))
        if score >= 0.06:
            groups[index].append(shape)

    merged = []
    for box_index, group in groups.items():
        text_rects = [
            tuple(map(int, region.xyxy))
            for region in regions
            if intersection(region.xyxy, boxes[box_index])
            >= 0.7 * intersection(region.xyxy, region.xyxy)
        ]
        anchored = [
            shape
            for shape in group
            if any(
                intersection(shape[0], rect) >= 0.7 * intersection(rect, rect)
                for rect in text_rects
            )
        ]
        # A loose vision box can include a larger patch of adjacent artwork.
        # Choose its ink color from candidates that actually contain the text.
        dominant = max(anchored or group, key=lambda shape: int(shape[1].sum()))
        group = [
            shape
            for shape in group
            if np.max(np.abs(shape[2].astype(float) - dominant[2])) <= 32
        ]
        # Prefer a complete outline when one color mask already recovered it.
        # Otherwise a duplicate with a small extension into the artwork would
        # enlarge an otherwise accurate oval when taking the union.
        intact = [
            (ellipse_fit_score(shape[1]), index)
            for index, shape in enumerate(group)
            if shape[1].sum() >= dominant[1].sum() * 0.7
        ]
        score, index = max(
            (item for item in intact if item[0] >= 0.97),
            key=lambda item: int(group[item[1]][1].sum()),
            default=(0, -1),
        )
        if score >= 0.97:
            bounds, mask, color = group[index]
            # A narrow fragment can itself look more elliptical than the
            # complete balloon. Prefer the complete oval, clipping small
            # protrusions without extending removal into adjacent artwork.
            if score < 0.985:
                contours, _ = cv2.findContours(
                    mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
                )
                fitted = np.zeros_like(mask)
                cv2.ellipse(
                    fitted,
                    cv2.fitEllipse(max(contours, key=cv2.contourArea)),
                    1,
                    -1,
                )
                mask = mask & fitted
            merged.append((bounds, mask, color))
            continue
        # A complete tighter contour avoids color bleed into neighbouring art.
        # Fragmented contours cannot win merely by having a smaller area.
        complete = []
        for shape in group:
            bounds, candidate, _ = shape
            if not text_rects or candidate.sum() < dominant[1].sum() * 0.65:
                continue
            if all(
                mask_coverage(bounds, candidate, rect) >= 0.85 for rect in text_rects
            ):
                complete.append(shape)
        if complete:
            merged.append(min(complete, key=lambda shape: int(shape[1].sum())))
            continue
        x = min(shape[0][0] for shape in group)
        y = min(shape[0][1] for shape in group)
        right = max(shape[0][2] for shape in group)
        bottom = max(shape[0][3] for shape in group)
        mask = np.zeros((bottom - y, right - x), dtype=np.uint8)
        for (sx, sy, sr, sb), fragment, _ in group:
            mask[sy - y : sb - y, sx - x : sr - x] |= fragment
        # Printed letters can split the flat background. Preserve irregular
        # outlines when closing gaps already reconnects the components.
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8))
        outlines, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if len(outlines) == 1:
            cv2.drawContours(mask, outlines, -1, 1, cv2.FILLED)
            merged.append(((x, y, right, bottom), mask, dominant[2]))
            continue
        # One vision box may span multiple captions or unrelated artwork.
        # Use only the detached components that independently cover source text.
        for outline in outlines:
            cx, cy, width, height = cv2.boundingRect(outline)
            component = np.zeros((height, width), dtype=np.uint8)
            cv2.drawContours(component, [outline - [cx, cy]], 0, 1, cv2.FILLED)
            bounds = (x + cx, y + cy, x + cx + width, y + cy + height)
            if any(
                mask_coverage(bounds, component, rect) >= 0.7 for rect in text_rects
            ):
                merged.append((bounds, component, dominant[2]))
    return merged


# Share of a balloon candidate covered by dark pixels that are not its lettering.
# Clean balloons measured <= 1 % (SR 18); panels taken for balloons 3.5-23 % (Miles).
FOREIGN_INK_LIMIT = 0.02


def foreign_ink(source, bounds, mask, group, text_mask=None, color=None):
    import cv2
    import numpy as np

    x, y, right, bottom = bounds
    inside = cv2.erode(mask, np.ones((7, 7), np.uint8)) != 0
    total = int(inside.sum())
    if not total:
        return 1.0
    clip = source[y:bottom, x:right].astype(np.int32)
    if color is None:
        color = np.median(clip[inside], axis=0)
    # Ink is what differs strongly from the balloon colour (dark lines on white,
    # light lines on a dark caption), so coloured balloons are measured alike.
    dark = np.max(np.abs(clip - np.asarray(color, dtype=np.int32)), axis=-1) > 110
    lettering = np.zeros(mask.shape, dtype=bool)
    for region in group:
        rx1, ry1, rx2, ry2 = map(int, region.xyxy)
        lettering[
            max(0, ry1 - y - 8) : max(0, ry2 - y + 8),
            max(0, rx1 - x - 8) : max(0, rx2 - x + 8),
        ] = True
    if text_mask is not None:
        lettering |= (
            cv2.dilate(
                (text_mask[y:bottom, x:right] > 127).astype(np.uint8),
                np.ones((9, 9), np.uint8),
            )
            != 0
        )
    return float((dark & inside & ~lettering).sum()) / total


async def render_guided(fallback, config, ctx):
    import cv2
    import numpy as np
    from PIL import Image, ImageDraw

    effects = [r for r in ctx.text_regions if getattr(r, "tankarr_sfx", False)]
    if effects:
        ctx.text_regions = [
            r for r in ctx.text_regions if not getattr(r, "tankarr_sfx", False)
        ]
        try:
            canvas = Image.fromarray(
                np.ascontiguousarray(await render_guided(fallback, config, ctx))
            )
        finally:
            ctx.text_regions = ctx.text_regions + effects
        draw_sound_effects(canvas, effects)
        return np.asarray(canvas).copy()
    source = np.asarray(ctx.img_rgb)
    height, width = source.shape[:2]
    boxes = supported_balloon_boxes(
        getattr(ctx, "vision_boxes", {}), ctx.text_regions, width, height
    )
    hue, saturation, value = cv2.split(cv2.cvtColor(source, cv2.COLOR_RGB2HSV))
    # Distinct flat color populations keep page margins and illustrations
    # from bleeding into a balloon candidate.
    dark = (saturation > 120) & (value > 28) & (value < 135)
    masks = [
        dark & (hue >= low) & (hue < high)
        for low, high in ((0, 30), (30, 60), (60, 90), (90, 135), (135, 180))
    ] + [
        (hue >= 14)
        & (hue <= 43)
        & (saturation >= 17)
        & (saturation <= 83)
        & (value >= 149),
        (saturation <= 24) & (value >= 180),
    ]
    # Include bright caption backgrounds while keeping their printed outline.
    masks.extend(
        (saturation > 120) & (value >= 135) & (hue >= low) & (hue < low + 30)
        for low in range(0, 180, 30)
    )
    masks.extend(dark_palette_masks(source, dark))
    masks.extend(vision_palette_masks(source, boxes))
    shapes = []
    for pixels in masks:
        opened = cv2.morphologyEx(
            pixels.astype(np.uint8), cv2.MORPH_OPEN, np.ones((3, 3), np.uint8)
        )
        contours, _ = cv2.findContours(opened, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        for contour in contours:
            x, y, w, h = cv2.boundingRect(contour)
            area = cv2.contourArea(contour)
            if (
                min(w, h) < 35
                or not 0.30 <= area / (w * h) <= 1.0
                or not 0.001 <= area / (width * height) <= 0.12
            ):
                continue
            bounds = (x, y, x + w, y + h)
            if not any(intersection(bounds, box) / (w * h) >= 0.28 for box in boxes):
                continue
            mask = np.zeros((h, w), dtype=np.uint8)
            cv2.drawContours(mask, [contour - [x, y]], -1, 1, cv2.FILLED)
            clip = source[y : y + h, x : x + w]
            samples = clip[(mask != 0) & pixels[y : y + h, x : x + w]]
            # RETR_EXTERNAL fills holes in a color contour. A thin ring of
            # sky around a balloon must not become its replacement background.
            if len(samples) < max(100, mask.sum() * 0.35):
                continue
            median = np.median(samples, axis=0)
            # Compressed narrow labels can vary more than flat speech balloons.
            tolerance = (
                24 if area / (w * h) >= 0.9 and min(w, h) <= max(w, h) * 0.5 else 14
            )
            if (
                np.median(np.max(np.abs(samples.astype(float) - median), axis=1))
                > tolerance
            ):
                continue
            shapes.append((bounds, mask, median.astype(np.uint8)))

    shapes = merge_balloon_fragments(shapes, boxes, ctx.text_regions)
    groups = defaultdict(list)
    regions = list(ctx.text_regions)
    for region in regions:
        rect = tuple(map(float, region.xyxy))
        area = intersection(rect, rect)
        scores = [
            (intersection(rect, bounds) / area, intersection(bounds, bounds), index)
            for index, (bounds, _, _) in enumerate(shapes)
            if area > 0
        ]
        score, _, index = max(scores, default=(0, 0, -1))
        if score >= 0.7:
            groups[index].append(region)

    text_mask = getattr(ctx, "mask", None)
    if text_mask is not None:
        text_mask = np.asarray(text_mask)
        if text_mask.ndim == 3:
            text_mask = text_mask[..., 0]
        if text_mask.shape[:2] != (height, width):
            text_mask = None
    assigned = set()
    placements = []
    for index, group in groups.items():
        (x, y, right, bottom), mask, color = shapes[index]
        group.sort(
            key=lambda region: (-float(region.center[0]), float(region.center[1]))
        )
        text = " ".join(str(region.translation).strip() for region in group).strip()
        if not text:
            continue
        w, h = right - x, bottom - y
        # 2026-11-17: a colour shape is a balloon only if it is the background the
        # text sits on. Grouping uses bounding boxes, so an artwork patch whose box
        # surrounds a white balloon was taken for it (p20: dark block). Require the
        # shape mask to cover the text and its colour to be the colour under it.
        covered, total, close = 0, 0, 0
        for region in group:
            rx1, ry1, rx2, ry2 = map(int, region.xyxy)
            total += max(0, rx2 - rx1) * max(0, ry2 - ry1)
            rx1, ry1 = max(rx1, x), max(ry1, y)
            rx2, ry2 = min(rx2, right), min(ry2, bottom)
            if rx2 > rx1 and ry2 > ry1:
                inside = mask[ry1 - y : ry2 - y, rx1 - x : rx2 - x] != 0
                covered += int(inside.sum())
                pixels = source[ry1:ry2, rx1:rx2][inside].astype(float)
                close += int(
                    (np.max(np.abs(pixels - color.astype(float)), axis=1) <= 40).sum()
                )
        if not total or covered < 0.5 * total or close < 0.3 * covered:
            continue
        # 2026-11-17 (Miles p13/p21/p39): on black-and-white pages a balloon whose
        # outline touches the panel's white background becomes one shape with the
        # panel, and filling it erased faces and whole panels. A balloon holds no
        # ink but its own lettering: refuse the shape (MIT inpaints and letters
        # the region instead) when line art other than the text is inside it.
        if (
            foreign_ink(source, (x, y, right, bottom), mask, group, text_mask, color)
            > FOREIGN_INK_LIMIT
        ):
            continue
        interior = cv2.erode(mask, np.ones((9, 9), np.uint8))
        layout = fit_mask(text, interior)
        if layout is None:
            continue
        erase = cv2.erode(mask, np.ones((3, 3), np.uint8))
        placements.append((x, y, right, bottom, erase, color, layout))
        assigned.update(map(id, group))

    ctx.text_regions = [region for region in regions if id(region) not in assigned]
    # 2026-11-17: a narrow vertical caption strip (p10 left edge) cannot hold
    # horizontal English at a readable size; MIT shrank it to a few pixels.
    # Such strips get the English rotated along the strip instead.
    strips = []
    for region in list(ctx.text_regions):
        x1, y1, x2, y2 = map(int, region.xyxy)
        w, h = x2 - x1, y2 - y1
        text = str(region.translation).strip()
        # Short strips (< 200 px) stay with MIT: they still fit a few words.
        if text and 0 < w <= 80 and h >= max(4 * w, 200):
            layout = fit_strip(text, h - 8, w - 2)
            if layout is not None:
                strips.append((x1, y1, x2, y2, layout))
                ctx.text_regions.remove(region)
    inpainted = ctx.img_inpainted
    try:
        if placements:
            # MIT's text-removal mask may extend beyond a balloon into artwork.
            # Guided balloons are erased by their own contour below. Keep the
            # native inpainting only around regions still rendered by MIT.
            clean = source.copy()
            for region in regions:
                if id(region) in assigned:
                    continue
                x1, y1, x2, y2 = map(int, region.xyxy)
                padding = 16
                x1, y1 = max(0, x1 - padding), max(0, y1 - padding)
                x2, y2 = min(width, x2 + padding), min(height, y2 + padding)
                clean[y1:y2, x1:x2] = inpainted[y1:y2, x1:x2]
            ctx.img_inpainted = clean
        base = (
            await fallback(config, ctx)
            if ctx.text_regions
            else ctx.img_inpainted.copy()
        )
    finally:
        ctx.text_regions = regions
        ctx.img_inpainted = inpainted
    for x, y, right, bottom, erase, color, *_ in placements:
        base[y:bottom, x:right][erase != 0] = color
        if text_mask is not None:
            # 2026-11-17: glyphs touching the balloon edge (bold names, "!!") break
            # its colour contour and survived the contour erase; MIT's inpainting
            # left grey there. Paint MIT's text mask with the balloon colour, 2 px
            # inside the balloon's bounds so its outline is never touched.
            # Limited to the convex hull of the balloon (a notch cut by a glyph is
            # filled, artwork beyond the outline is not).
            points = cv2.findNonZero((erase != 0).astype(np.uint8))
            if points is not None:
                hull = np.zeros_like(erase, dtype=np.uint8)
                cv2.fillConvexPoly(hull, cv2.convexHull(points), 1)
                hull = cv2.erode(hull, np.ones((3, 3), np.uint8))
                inner = (text_mask[y:bottom, x:right] > 127) & (hull != 0)
                base[y:bottom, x:right][inner] = color
    for x1, y1, x2, y2, (font, rows, size) in strips:
        box = base[y1:y2, x1:x2]
        if text_mask is not None:
            ink = text_mask[y1:y2, x1:x2] > 127
            paper = box[~ink] if (~ink).any() else box.reshape(-1, 3)
            background = np.median(paper, axis=0).astype(np.uint8)
            box[ink] = background
        else:
            background = np.median(box.reshape(-1, 3), axis=0).astype(np.uint8)
        dark = sum(map(int, background)) < 320
        strip = Image.new("RGBA", (y2 - y1, x2 - x1), (0, 0, 0, 0))
        pen = ImageDraw.Draw(strip)
        top = ((x2 - x1) - len(rows) * (size + 3)) // 2
        for index, line in enumerate(rows):
            pen.text(
                ((y2 - y1) / 2, top + index * (size + 3)),
                line,
                anchor="mt",
                font=font,
                fill="#fff8e8" if dark else "#151515",
            )
        rotated = strip.rotate(-90, expand=True)
        patch = Image.fromarray(np.ascontiguousarray(base[y1:y2, x1:x2]))
        patch.paste(rotated, (0, 0), rotated)
        base[y1:y2, x1:x2] = np.asarray(patch)
    canvas = Image.fromarray(base)
    draw = ImageDraw.Draw(canvas)
    for x, y, _, _, _, color, (font, rows) in placements:
        fill = "#fff8e8" if sum(map(int, color)) < 320 else "#151515"
        for cx, top, line in rows:
            draw.text((x + cx, y + top), line, anchor="mt", font=font, fill=fill)
    return np.asarray(canvas).copy()
