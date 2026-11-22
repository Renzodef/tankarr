"""Find pages that still carry a translation strip below the artwork.

Some scanlation releases append, under each page, a white band separated by a
thin grey rule and filled with a numbered translation of the balloons
("English translation · page 12"). Lettered volumes made from those sources
keep the band unless it is cropped, and a reader sees it on random pages.

The scan reads every page of each library volume once, in the background, and
remembers the result by the file's SHA-256, so a changed file is scanned again.
"""

from __future__ import annotations

import io
import json
import os
import re
import tempfile
import threading
import time
import zipfile
from collections.abc import Iterable
from pathlib import Path, PurePosixPath
from typing import Any

from PIL import Image, UnidentifiedImageError

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".avif"}
SCAN_WIDTH = 600
# Height over width of a typical single manga page, used when a volume has too few plain pages.
DEFAULT_PAGE_ASPECT = 1.42
SHAPE_TOLERANCE = 0.08
MAX_PAGES = 2000
MAX_PAGE_BYTES = 64 * 1024 * 1024
# Version 2: the page-shape test; results of the first detector are discarded.
STORE_VERSION = 2


def _natural(value: str) -> list[Any]:
    return [
        int(part) if part.isdigit() else part.casefold()
        for part in re.split(r"(\d+)", value)
    ]


def candidate_rows(image: Image.Image) -> tuple[int, int, list[int]]:
    """Width, height and rows (at scan width) that look like a strip rule.

    A rule is a full-width light-grey line with white just above and below it,
    followed by a mostly white band that holds some dark text. A panel border
    fails the band test because artwork, not paper, follows it. The shape of
    the page decides later which of these rows really start a strip.
    """

    gray = image.convert("L")
    if gray.width > SCAN_WIDTH:
        gray = gray.resize(
            (SCAN_WIDTH, max(1, round(gray.height * SCAN_WIDTH / gray.width)))
        )
    width, height = gray.size
    if width < 50 or height < 100:
        return width, height, []

    def row_histogram(row: int) -> list[int]:
        return gray.crop((0, row, width, row + 1)).histogram()

    def share(histogram: list[int], low: int, high: int) -> float:
        return sum(histogram[low:high]) / max(1, sum(histogram))

    rows = []
    for row in range(int(height * 0.35), height - max(10, int(height * 0.08))):
        if rows and row - rows[-1] < 4:
            continue
        if share(row_histogram(row), 101, 245) < 0.8:
            continue
        if share(row_histogram(row - 2), 241, 256) < 0.95:
            continue
        if share(row_histogram(min(row + 3, height - 1)), 241, 256) < 0.9:
            continue
        band = gray.crop((0, row + 3, width, height)).histogram()
        if share(band, 236, 256) >= 0.85 and 0.002 <= share(band, 0, 100) <= 0.15:
            rows.append(row)
    return width, height, rows


def _fits(row: int, width: int, height: int, page_aspect: float) -> bool:
    """The band is appended below a whole page or two-page spread.

    So the part above the rule has the shape of the volume's pages, or half of
    it for a spread, within a few percent. A white bottom margin, a page
    number, a text afterword or a mid-page panel border has a different shape.
    """

    above = row / width
    return height / row >= 1.1 and any(
        abs(above / shape - 1) <= SHAPE_TOLERANCE
        for shape in (page_aspect, page_aspect / 2)
    )


def strip_row(
    image: Image.Image, page_aspect: float = DEFAULT_PAGE_ASPECT
) -> int | None:
    """Row where a translation strip starts, or None."""

    width, height, rows = candidate_rows(image)
    for row in rows:
        if _fits(row, width, height, page_aspect):
            return row
    return None


def volume_page_aspect(plain_shapes: list[float], rule_shapes: list[float]) -> float:
    """Shape of the volume's original pages.

    When many pages carry a rule at the same height, that height is the
    strip's signature and gives the original page shape, even if every page
    has a strip. Otherwise the plain pages give it, or a typical manga page.
    """

    if len(rule_shapes) >= 5:
        ordered = sorted(rule_shapes)
        middle = ordered[len(ordered) // 2]
        close = sum(1 for shape in ordered if abs(shape / middle - 1) <= 0.03)
        if close / len(ordered) >= 0.6 and 1.25 <= middle <= 1.6:
            return middle
    return typical_page_aspect(plain_shapes)


def scan_archive(path: Path) -> list[int]:
    """1-based numbers of the pages that carry a translation strip."""

    found: list[int] = []
    with zipfile.ZipFile(path) as archive:
        pages = []
        for item in archive.infolist():
            name = PurePosixPath(item.filename)
            if item.is_dir() or name.suffix.lower() not in IMAGE_SUFFIXES:
                continue
            if name.is_absolute() or ".." in name.parts:
                raise ValueError("Archive contains unsafe paths")
            pages.append(item)
        pages.sort(key=lambda item: _natural(item.filename))
        pages = [
            item
            for item in pages[:MAX_PAGES]
            if item.file_size <= MAX_PAGE_BYTES and not item.flag_bits & 1
        ]
        measured = []
        for number, item in enumerate(pages, 1):
            try:
                with Image.open(io.BytesIO(archive.read(item))) as image:
                    image.draft("L", (SCAN_WIDTH, SCAN_WIDTH * 4))
                    measured.append((number, *candidate_rows(image)))
            except (OSError, UnidentifiedImageError, Image.DecompressionBombError):
                continue
    plain = [height / width for _, width, height, rows in measured if not rows]
    ruled = [rows[0] / width for _, width, _, rows in measured if rows]
    page_aspect = volume_page_aspect(plain, ruled)
    for number, width, height, rows in measured:
        if any(_fits(row, width, height, page_aspect) for row in rows):
            found.append(number)
    return found


def typical_page_aspect(shapes: list[float]) -> float:
    """Median shape of the plain single pages, or a typical manga page.

    Pages taller than 1.7 are left out: those are the ones a strip lengthened.
    """

    plain = sorted(shape for shape in shapes if 1.0 <= shape <= 1.7)
    if len(plain) < 5:
        return DEFAULT_PAGE_ASPECT
    return plain[len(plain) // 2]


class StripScanStore:
    """Scan results by library file SHA-256, written atomically."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._data: dict[str, dict[str, Any]] | None = None

    def _load(self) -> dict[str, dict[str, Any]]:
        if self._data is None:
            try:
                raw = json.loads(self.path.read_text())
                self._data = (
                    raw.get("files", {}) if raw.get("version") == STORE_VERSION else {}
                )
            except (OSError, ValueError):
                self._data = {}
        return self._data

    def get(self, sha256: str) -> dict[str, Any] | None:
        with self._lock:
            return self._load().get(sha256)

    def put(self, sha256: str, pages: list[int]) -> None:
        with self._lock:
            data = self._load()
            data[sha256] = {"pages": pages, "checked_at": time.time()}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle, temporary = tempfile.mkstemp(
                dir=self.path.parent, prefix=".strips-"
            )
            with os.fdopen(handle, "w") as output:
                json.dump({"version": STORE_VERSION, "files": data}, output)
            os.replace(temporary, self.path)

    def pages_by_sha(self) -> dict[str, list[int]]:
        with self._lock:
            return {
                sha: list(entry.get("pages") or [])
                for sha, entry in self._load().items()
            }


def library_files(database: Any) -> list[dict[str, Any]]:
    """Downloaded library files with a known hash."""

    with database.connect() as connection:
        rows = connection.execute(
            """
            SELECT id, manga_id, volume, chapter, release_unit, library_path, library_sha256
            FROM chapter_release
            WHERE downloaded=1 AND library_path IS NOT NULL
              AND library_sha256 IS NOT NULL AND library_sha256 <> ''
            ORDER BY manga_id, id
            """
        ).fetchall()
    return [dict(row) for row in rows]


def next_unscanned(
    files: Iterable[dict[str, Any]], store: StripScanStore
) -> dict[str, Any] | None:
    for release in files:
        if store.get(release["library_sha256"]) is None:
            return release
    return None


def strip_findings_by_series(
    files: Iterable[dict[str, Any]], store: StripScanStore
) -> dict[str, list[dict[str, Any]]]:
    """Files with strip pages, grouped by series."""

    pages = store.pages_by_sha()
    result: dict[str, list[dict[str, Any]]] = {}
    for release in files:
        found = pages.get(release["library_sha256"])
        if found:
            result.setdefault(str(release["manga_id"]), []).append(
                {
                    "chapter_id": str(release["id"]),
                    "unit": "volume"
                    if release.get("release_unit") == "volume"
                    else "chapter",
                    "number": release.get("volume")
                    if release.get("release_unit") == "volume"
                    else release.get("chapter"),
                    "pages": found,
                }
            )
    return result


def store_path(data_dir: Path) -> Path:
    return Path(data_dir).resolve() / "cache" / "translation-strips.json"


def strip_finding(volumes: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "code": "translation_strip",
        "label": "Some pages still carry a translation strip at the bottom",
        "volumes": volumes,
    }
