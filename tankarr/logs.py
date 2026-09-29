"""The application log: its level, a rotating file, and the tail the System page shows.

``docker logs`` keeps working (the console handler stays); the file adds
timestamps and survives a container restart, and the System page tails and
downloads it, like the Logs page of the other *arr applications. The level
follows ``TANKARR_LOG_LEVEL`` and Settings → General, applied at once.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tankarr.config import Settings

LEVELS = ("debug", "info", "warning", "error")
_ALIASES = {"warn": "warning", "critical": "error", "fatal": "error", "trace": "debug"}
LOG_DIRECTORY = "logs"
LOG_FILE = "tankarr.log"
MAX_BYTES = 5 * 1024 * 1024
BACKUP_COUNT = 5
MAX_TAIL_LINES = 2000
CONSOLE_FORMAT = "%(levelname)s:%(name)s:%(message)s"
FILE_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

logger = logging.getLogger(__name__)


def normalize_log_level(value: object) -> str:
    text = str(value or "").strip().casefold()
    text = _ALIASES.get(text, text)
    if text not in LEVELS:
        raise ValueError("log_level must be debug, info, warning or error")
    return text


def log_directory(settings: Settings) -> Path:
    return settings.data_dir / LOG_DIRECTORY


def log_path(settings: Settings) -> Path:
    return log_directory(settings) / LOG_FILE


# Libraries that log every request with its full URL. SABnzbd takes its API key
# in the query string, so at INFO httpx would print it on every poll; they stay
# at WARNING whatever the application level.
QUIET_LOGGERS = ("httpx", "httpcore", "httpx2", "hpack")


def apply_log_level(settings: Settings) -> int:
    """Move the root logger to the configured level; returns the numeric level."""

    level = getattr(logging, normalize_log_level(settings.log_level).upper())
    logging.getLogger().setLevel(level)
    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    return level


def configure_logging(settings: Settings) -> Path | None:
    """Install the console and file handlers once, then keep the level in step.

    Returns the log file path, or None when the data directory refused it (the
    console still gets every line).
    """

    root = logging.getLogger()
    apply_log_level(settings)
    if not any(getattr(handler, "tankarr_console", False) for handler in root.handlers):
        console = logging.StreamHandler()
        console.setFormatter(logging.Formatter(CONSOLE_FORMAT))
        console.tankarr_console = True  # type: ignore[attr-defined]
        root.addHandler(console)
    path = log_path(settings)
    for handler in list(root.handlers):
        if getattr(handler, "tankarr_file", False):
            if Path(getattr(handler, "baseFilename", "")) == path:
                return path
            root.removeHandler(handler)
            handler.close()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8"
        )
    except OSError as exc:
        logger.warning("The log file is not available: %s", exc)
        return None
    file_handler.setFormatter(logging.Formatter(FILE_FORMAT))
    file_handler.tankarr_file = True  # type: ignore[attr-defined]
    root.addHandler(file_handler)
    return path


def log_files(settings: Settings) -> list[dict[str, Any]]:
    """The current log file and its rotated predecessors, newest first."""

    directory = log_directory(settings)
    if not directory.is_dir():
        return []
    files: list[dict[str, Any]] = []
    for entry in directory.iterdir():
        if not entry.is_file() or entry.is_symlink():
            continue
        if entry.name != LOG_FILE and not entry.name.startswith(f"{LOG_FILE}."):
            continue
        stat = entry.stat()
        files.append(
            {
                "name": entry.name,
                "size": stat.st_size,
                "modified": datetime.fromtimestamp(stat.st_mtime, UTC).isoformat(
                    timespec="seconds"
                ),
            }
        )
    files.sort(key=lambda item: str(item["modified"]), reverse=True)
    return files


def _read_last_lines(path: Path, count: int) -> list[str]:
    """The last ``count`` lines of a file, read from its end in blocks."""

    block = 64 * 1024
    chunks: list[bytes] = []
    newlines = 0
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        position = handle.tell()
        while position > 0 and newlines <= count:
            step = min(block, position)
            position -= step
            handle.seek(position)
            data = handle.read(step)
            chunks.append(data)
            newlines += data.count(b"\n")
    text = b"".join(reversed(chunks)).decode("utf-8", "replace")
    lines = text.splitlines()
    return lines[-count:]


def tail_log(
    settings: Settings, lines: int = 200, minimum_level: str | None = None
) -> list[str]:
    """The last lines of the current log file, optionally from one level up.

    With a minimum level, only lines carrying that level or a higher one are
    kept; the continuation lines of a traceback follow the line that started
    it.
    """

    count = max(1, min(int(lines), MAX_TAIL_LINES))
    path = log_path(settings)
    if not path.is_file():
        return []
    # Filtering discards most lines, so read more than asked and trim after.
    raw = _read_last_lines(path, count if minimum_level is None else count * 20)
    if minimum_level is None:
        return raw
    allowed = {
        level.upper()
        for level in LEVELS[LEVELS.index(normalize_log_level(minimum_level)) :]
    }
    all_levels = {level.upper() for level in LEVELS} | {"CRITICAL"}
    kept: list[str] = []
    keeping = False
    for line in raw:
        parts = line.split(" ", 3)
        level_token = parts[2] if len(parts) > 2 else ""
        if level_token in all_levels or level_token == "CRITICAL":
            keeping = level_token in allowed or level_token == "CRITICAL"
        if keeping:
            kept.append(line)
    return kept[-count:]
