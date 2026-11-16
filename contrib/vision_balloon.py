"""Optional OpenAI-compatible vision guidance for MIT OCR and lettering."""

from __future__ import annotations

import base64
import io
import json
import re
from collections import defaultdict
from difflib import SequenceMatcher

import httpx

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
    page.thumbnail((1100, 1100))
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
            (prompt + " Return compact JSON and at most 40 objects.", False),
            (geometry_prompt, True),
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
                break
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
        raise ValueError("Vision provider returned no usable balloon geometry")
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


def fit(text, width, height):
    from PIL import ImageFont

    for size in range(min(32, height), 10, -1):
        try:
            font = ImageFont.truetype("DejaVuSans.ttf", size)
        except OSError:
            font = ImageFont.load_default(size=size)
        lines = []
        line = ""
        for word in text.split():
            extended = f"{line} {word}".strip()
            if line and font.getlength(extended) > width:
                lines.append(line)
                line = word
            else:
                line = extended
        if line:
            lines.append(line)
        if len(lines) * (size + 2) <= height and all(
            font.getlength(line) <= width for line in lines
        ):
            return font, lines, size + 2
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
            pixels = cv2.inRange(
                source, np.maximum(color - 16, 0), np.minimum(color + 16, 255)
            )
            yield cv2.morphologyEx(
                pixels, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8)
            ).astype(bool)


def merge_balloon_fragments(shapes, boxes):
    """Combine compatible color fragments belonging to one vision balloon."""
    import cv2
    import numpy as np

    groups = defaultdict(list)
    for shape in shapes:
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
    for group in groups.values():
        dominant = max(group, key=lambda shape: int(shape[1].sum()))
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
        score, index = max(intact, default=(0, -1))
        if score >= 0.985:
            merged.append(group[index])
            continue
        x = min(shape[0][0] for shape in group)
        y = min(shape[0][1] for shape in group)
        right = max(shape[0][2] for shape in group)
        bottom = max(shape[0][3] for shape in group)
        mask = np.zeros((bottom - y, right - x), dtype=np.uint8)
        for (sx, sy, sr, sb), fragment, _ in group:
            mask[sy - y : sb - y, sx - x : sr - x] |= fragment
        points = cv2.findNonZero(mask)
        if points is None:
            continue
        # Printed letters can split the flat background into disconnected
        # components. Join only fragments assigned to this same vision box.
        cv2.fillConvexPoly(mask, cv2.convexHull(points), 1)
        merged.append(((x, y, right, bottom), mask, dominant[2]))
    return merged


async def render_guided(fallback, config, ctx):
    import cv2
    import numpy as np
    from PIL import Image, ImageDraw

    source = np.asarray(ctx.img_rgb)
    height, width = source.shape[:2]
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
    masks.extend(dark_palette_masks(source, dark))
    masks.extend(vision_palette_masks(source, ctx.vision_boxes))
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
                or not 0.49 <= area / (w * h) <= 0.95
                or not 0.001 <= area / (width * height) <= 0.12
            ):
                continue
            bounds = (x, y, x + w, y + h)
            if not any(
                intersection(bounds, box) / (w * h) >= 0.28 for box in ctx.vision_boxes
            ):
                continue
            mask = np.zeros((h, w), dtype=np.uint8)
            cv2.drawContours(mask, [contour - [x, y]], -1, 1, cv2.FILLED)
            clip = source[y : y + h, x : x + w]
            samples = clip[(mask != 0) & pixels[y : y + h, x : x + w]]
            if len(samples) < 100:
                continue
            median = np.median(samples, axis=0)
            if np.median(np.max(np.abs(samples.astype(float) - median), axis=1)) > 14:
                continue
            shapes.append((bounds, mask, median.astype(np.uint8)))

    shapes = merge_balloon_fragments(shapes, list(ctx.vision_boxes))
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
        interior = cv2.erode(mask, np.ones((9, 9), np.uint8))
        options = []
        for ratio in (0.55, 0.66, 0.77):
            iw, ih = int(w * ratio), int(h * ratio)
            left, top = (w - iw) // 2, (h - ih) // 2
            if not np.all(interior[top : top + ih, left : left + iw]):
                continue
            if layout := fit(text, iw - 3, ih - 3):
                options.append((layout[2], left, top, iw, ih, layout))
        if not options:
            continue
        _, left, top, iw, ih, layout = max(options, key=lambda option: option[0])
        erase = cv2.erode(mask, np.ones((5, 5), np.uint8))
        placements.append(
            (x, y, right, bottom, erase, color, left, top, iw, ih, layout)
        )
        assigned.update(map(id, group))

    ctx.text_regions = [region for region in regions if id(region) not in assigned]
    try:
        base = (
            await fallback(config, ctx)
            if ctx.text_regions
            else ctx.img_inpainted.copy()
        )
    finally:
        ctx.text_regions = regions
    for x, y, right, bottom, erase, color, *_ in placements:
        base[y:bottom, x:right][erase != 0] = color
    canvas = Image.fromarray(base)
    draw = ImageDraw.Draw(canvas)
    for x, y, _, _, _, color, left, top, iw, ih, (font, lines, step) in placements:
        fill = "#fff8e8" if sum(map(int, color)) < 320 else "#151515"
        top += max(0, (ih - step * len(lines)) // 2)
        for line in lines:
            draw.text(
                (x + left + iw / 2, y + top), line, anchor="mt", font=font, fill=fill
            )
            top += step
    return np.asarray(canvas).copy()
