"""Resume an interrupted acquisition only from the same ordered page manifest."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Sequence
from pathlib import Path


class DownloadPageCache:
    def __init__(self, directory: Path, chapter_id: str, urls: Sequence[str]):
        self.directory = directory
        self.path = directory / ".tankarr-page-cache.json"
        # Signed URLs and credentials must never be saved in a staging ledger.
        manifest = json.dumps([chapter_id, *urls], separators=(",", ":"))
        self.fingerprint = hashlib.sha256(manifest.encode()).hexdigest()
        self.files: dict[str, dict[str, str | int]] = {}
        try:
            if self.path.is_symlink():
                return
            data = json.loads(self.path.read_text())
            if (
                isinstance(data, dict)
                and data.get("manifest") == self.fingerprint
                and isinstance(data.get("files"), dict)
            ):
                self.files = data["files"]
        except (OSError, ValueError, UnicodeError):
            pass

    def get(self, index: int, verify: Callable[[bytes], None]) -> Path | None:
        item = self.files.get(str(index))
        if not isinstance(item, dict):
            return None
        name = item.get("name")
        if not isinstance(name, str) or not re.fullmatch(
            rf"{index + 1:04d}\.[a-z0-9]{{1,5}}", name
        ):
            return None
        path = self.directory / name
        try:
            if path.is_symlink() or path.stat().st_size != item.get("size"):
                return None
            content = path.read_bytes()
            if hashlib.sha256(content).hexdigest() != item.get("sha256"):
                return None
            verify(content)
        except Exception:  # noqa: BLE001 - an invalid cache entry is a cache miss
            return None
        return path

    def record(self, index: int, path: Path, content: bytes) -> None:
        self.files[str(index)] = {
            "name": path.name,
            "size": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }
        temporary = self.path.with_suffix(".tmp")
        try:
            temporary.write_text(
                json.dumps({"manifest": self.fingerprint, "files": self.files}),
                encoding="utf-8",
            )
            temporary.replace(self.path)
        except OSError:
            # The downloaded image remains usable even if cache bookkeeping
            # failed. A retry will fetch it again rather than trust its bytes.
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
