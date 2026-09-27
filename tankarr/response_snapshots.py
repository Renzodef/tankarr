"""Disposable HTTP snapshots for immediate paint after a process restart.

These bytes are only used by explicit cached reads. Fresh reads and all
acquisition decisions still use the database and the normal reconciliation.
"""

import json
import logging
import os
import tempfile
import time
from contextlib import suppress
from pathlib import Path

logger = logging.getLogger(__name__)
VERSION = 1
MAX_BYTES = 64 * 1024 * 1024
MAX_AGE = 7 * 24 * 3600


class ResponseSnapshots:
    def __init__(self, root: Path, database: Path, *, scope: str):
        self.root = root
        self.database = database
        self.scope = scope

    def identity(self) -> list[int]:
        stat = self.database.stat()
        return [stat.st_dev, stat.st_ino]

    def load(self, kind: str) -> dict[str, bytes]:
        try:
            path = self.root / f"{kind}.json"
            with path.open("rb") as handle:
                raw = handle.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                return {}
            saved = json.loads(raw)
            if (
                saved["version"] != VERSION
                or saved["database"] != self.identity()
                or saved["scope"] != self.scope
                or not 0 <= time.time() - saved["created"] <= MAX_AGE
            ):
                return {}
            payloads = saved["payloads"]
            if not isinstance(payloads, dict) or not payloads:
                return {}
            for value in payloads.values():
                if not isinstance(value, str) or not isinstance(
                    json.loads(value), list
                ):
                    return {}
            return {key: value.encode("utf-8") for key, value in payloads.items()}
        except (OSError, ValueError, TypeError, KeyError):
            return {}

    def save(self, kind: str, payloads: dict[str, bytes]) -> None:
        temporary = None
        try:
            raw = json.dumps(
                {
                    "version": VERSION,
                    "database": self.identity(),
                    "scope": self.scope,
                    "created": time.time(),
                    "payloads": {
                        key: value.decode("utf-8") for key, value in payloads.items()
                    },
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            if len(raw) > MAX_BYTES:
                return
            self.root.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=self.root, delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(raw)
            os.replace(temporary, self.root / f"{kind}.json")
        except (OSError, ValueError):
            # Losing a disposable cache must never break an otherwise valid
            # response (read-only disk, full filesystem, or concurrent restore).
            logger.warning("Unable to persist %s response snapshot", kind)
        finally:
            if temporary is not None:
                with suppress(OSError):
                    temporary.unlink(missing_ok=True)
