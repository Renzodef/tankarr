from __future__ import annotations

import mimetypes
import re
from pathlib import Path

from tankarr.providers.base import ProgressCallback, Provider

MANGA_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*$")
FILENAME_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")


class LocalProvider(Provider):
    """Series imported from disk: no remote source, covers served from data."""

    name = "local"
    label = "Local Files"
    languages = frozenset()  # Never participates in provider searches.

    def __init__(self, covers_dir: Path):
        self.covers_dir = covers_dir

    async def search(self, query: str, language: str, limit: int = 20) -> list[dict]:
        return []

    async def get_manga(self, manga_id: str) -> dict:
        raise RuntimeError("Local series have no remote source")

    async def list_chapters(self, manga_id: str, language: str) -> list[dict]:
        raise RuntimeError("Local series have no remote source")

    async def download_pages(
        self,
        chapter_id: str,
        target_dir: Path,
        progress: ProgressCallback,
        concurrency: int = 4,
    ) -> list[Path]:
        raise RuntimeError("Local series have no remote source")

    async def get_cover(self, manga_id: str, filename: str) -> tuple[bytes, str]:
        if not MANGA_ID_PATTERN.match(manga_id) or not FILENAME_PATTERN.match(filename):
            raise ValueError("Invalid local cover identifier")
        path = (self.covers_dir / manga_id / filename).resolve()
        if self.covers_dir.resolve() not in path.parents or not path.is_file():
            raise ValueError("Unknown local cover")
        media_type = mimetypes.guess_type(filename)[0] or "image/jpeg"
        if not media_type.startswith("image/"):
            raise ValueError("Local cover is not an image")
        return path.read_bytes(), media_type
