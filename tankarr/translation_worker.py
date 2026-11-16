"""Run installation-owned translation hooks with per-attempt inputs."""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
from contextlib import suppress
from pathlib import Path

HOOK_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}")


class WorkerUnavailable(ValueError):
    """A worker can retry the same attempt."""


def validate_hook_name(value: str) -> str:
    if not HOOK_NAME.fullmatch(value):
        raise ValueError(
            "Translation hooks must be filenames inside the hook directory"
        )
    return value


def worker_executable(directory: Path | None, name: str) -> Path:
    if directory is None:
        raise ValueError(
            "An administrator must configure the translation hook directory"
        )
    root = directory.resolve()
    executable = (root / validate_hook_name(name)).resolve()
    if not executable.is_relative_to(root) or not executable.is_file():
        raise ValueError("Translation hook is missing from the configured directory")
    if not os.access(executable, os.X_OK):
        raise ValueError("Translation hook is not executable")
    return executable


async def run_worker(
    executable: Path,
    request: dict,
    *,
    environment: dict[str, str] | None = None,
    timeout: float = 6 * 3600,
):
    worker = await asyncio.create_subprocess_exec(
        str(executable),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        env={**os.environ, **environment} if environment else None,
        start_new_session=os.name == "posix",
    )
    try:
        async with asyncio.timeout(timeout):
            await worker.communicate(json.dumps(request).encode())
        if worker.returncode == 75:
            raise WorkerUnavailable("Translation worker is temporarily unavailable")
        if worker.returncode:
            raise ValueError("Translation worker failed")
    finally:
        if worker.returncode is None:
            with suppress(ProcessLookupError):
                if os.name == "posix":
                    os.killpg(worker.pid, signal.SIGTERM)
                else:
                    worker.terminate()
            try:
                await asyncio.wait_for(worker.wait(), 5)
            except TimeoutError:
                with suppress(ProcessLookupError):
                    if os.name == "posix":
                        os.killpg(worker.pid, signal.SIGKILL)
                    else:
                        worker.kill()
                await worker.wait()
