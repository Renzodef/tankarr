from __future__ import annotations

import asyncio
import hashlib
import os
import re
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageOps

ARTWORK_THUMBNAIL_VERSION = 1
SUPPORTED_ARTWORK_THUMBNAIL_WIDTHS = frozenset({64, 192, 320, 640})
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ArtworkThumbnailCache:
    """Bounded, content-addressed WebP cache for canonical artwork."""

    def __init__(self, root: Path, *, render_concurrency: int = 1):
        self.root = root
        self._render_slots = asyncio.Semaphore(max(1, render_concurrency))
        self._locks_guard = asyncio.Lock()
        self._locks: dict[Path, asyncio.Lock] = {}
        self._users: dict[Path, int] = {}

    async def get(
        self, source: Path, artwork_sha256: str | None, width: int
    ) -> tuple[Path, str]:
        if width not in SUPPORTED_ARTWORK_THUMBNAIL_WIDTHS:
            raise ValueError(f"Unsupported artwork thumbnail width: {width}")
        digest = str(artwork_sha256 or "").strip().casefold()
        if not _SHA256_RE.fullmatch(digest):
            digest = await asyncio.to_thread(self._file_sha256, source)
        destination = self.root / (
            f"{digest}-w{width}-v{ARTWORK_THUMBNAIL_VERSION}.webp"
        )
        if self._usable(destination):
            return destination, digest

        async with self._locks_guard:
            lock = self._locks.setdefault(destination, asyncio.Lock())
            self._users[destination] = self._users.get(destination, 0) + 1
        try:
            async with lock:
                if not self._usable(destination):
                    async with self._render_slots:
                        render_source = source
                        for larger_width in sorted(
                            candidate
                            for candidate in SUPPORTED_ARTWORK_THUMBNAIL_WIDTHS
                            if candidate > width
                        ):
                            larger = self.root / (
                                f"{digest}-w{larger_width}-v{ARTWORK_THUMBNAIL_VERSION}.webp"
                            )
                            if self._usable(larger):
                                render_source = larger
                                break
                        await self._render_in_child(render_source, destination, width)
        finally:
            async with self._locks_guard:
                self._users[destination] -= 1
                if not self._users[destination]:
                    del self._users[destination]
                    del self._locks[destination]
        return destination, digest

    @staticmethod
    def _usable(path: Path) -> bool:
        return path.is_file() and not path.is_symlink() and path.stat().st_size > 0

    @staticmethod
    def _file_sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    async def _render_in_child(source: Path, destination: Path, width: int) -> None:
        # Pillow and image codecs can retain their largest allocation in the
        # long-lived API process. A short child returns that memory to Linux
        # after each cache miss while the semaphore bounds peak memory.
        destination.parent.mkdir(parents=True, exist_ok=True)
        # The parent owns temporary files so cancelling/killing the child cannot
        # leave partial images behind or remove another worker's temporary file.
        with tempfile.TemporaryDirectory(
            prefix=".render-", dir=destination.parent
        ) as directory:
            output = Path(directory) / destination.name
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "tankarr.artwork_thumbnails",
                str(source),
                str(output),
                str(width),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                _stdout, stderr = await process.communicate()
            except asyncio.CancelledError:
                # Retain the render slot until the child is reaped. Otherwise a
                # cancelled request leaves CPU work running outside the limit.
                if process.returncode is None:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                await process.communicate()
                raise
            if process.returncode != 0:
                detail = stderr.decode("utf-8", errors="replace").strip().splitlines()
                raise ValueError(
                    detail[-1] if detail else "Artwork thumbnail renderer failed"
                )
            output.replace(destination)


def _render_thumbnail(source: Path, destination: Path, width: int) -> None:
    if width not in SUPPORTED_ARTWORK_THUMBNAIL_WIDTHS:
        raise ValueError(f"Unsupported artwork thumbnail width: {width}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        file_descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.stem}.", suffix=".tmp", dir=destination.parent
        )
        os.close(file_descriptor)
        with Image.open(source) as raw:
            raw.draft("RGB", (width, width * 2))
            image = ImageOps.exif_transpose(raw)
            image.thumbnail(
                (width, width * 2),
                Image.Resampling.LANCZOS,
                reducing_gap=3.0,
            )
            image.convert("RGB").save(
                temporary_name,
                format="WEBP",
                quality=78,
                method=3,
            )
        Path(temporary_name).replace(destination)
    finally:
        if temporary_name:
            Path(temporary_name).unlink(missing_ok=True)


if __name__ == "__main__":
    if len(sys.argv) != 4:
        raise SystemExit("usage: artwork_thumbnails SOURCE DESTINATION WIDTH")
    _render_thumbnail(Path(sys.argv[1]), Path(sys.argv[2]), int(sys.argv[3]))
