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
MAX_PAGES = 2000
MAX_PAGE_BYTES = 64 * 1024 * 1024
STORE_VERSION = 1


def _natural(value: str) -> list[Any]:
    return [
        int(part) if part.isdigit() else part.casefold()
        for part in re.split(r"(\d+)", value)
    ]


def strip_row(image: Image.Image) -> int | None:
    """Row where a translation strip starts, or None.

    The strip is a full-width light-grey rule with white just above and below
    it, followed by a mostly white band that holds some dark text. A panel
    border fails the band test because artwork, not paper, follows it.
    """

    gray = image.convert("L")
    if gray.width > SCAN_WIDTH:
        gray = gray.resize(
            (SCAN_WIDTH, max(1, round(gray.height * SCAN_WIDTH / gray.width)))
        )
    width, height = gray.size
    if width < 50 or height < 100:
        return None

    def row_histogram(row: int) -> list[int]:
        return gray.crop((0, row, width, row + 1)).histogram()

    def share(histogram: list[int], low: int, high: int) -> float:
        return sum(histogram[low:high]) / max(1, sum(histogram))

    for row in range(int(height * 0.35), height - max(10, int(height * 0.08))):
        if share(row_histogram(row), 101, 245) < 0.8:
            continue
        if share(row_histogram(row - 2), 241, 256) < 0.95:
            continue
        if share(row_histogram(min(row + 3, height - 1)), 241, 256) < 0.9:
            continue
        band = gray.crop((0, row + 3, width, height)).histogram()
        if share(band, 236, 256) >= 0.85 and 0.002 <= share(band, 0, 100) <= 0.15:
            return row
    return None


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
        for number, item in enumerate(pages[:MAX_PAGES], 1):
            if item.file_size > MAX_PAGE_BYTES or item.flag_bits & 1:
                continue
            try:
                with Image.open(io.BytesIO(archive.read(item))) as image:
                    image.draft("L", (SCAN_WIDTH, SCAN_WIDTH * 4))
                    if strip_row(image) is not None:
                        found.append(number)
            except (OSError, UnidentifiedImageError, Image.DecompressionBombError):
                continue
    return found


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
