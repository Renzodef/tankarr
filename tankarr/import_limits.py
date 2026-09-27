"""Budgets for untrusted archive decoding, independent of language/matching policy."""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import warnings
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, UnidentifiedImageError

MAX_PAGE_PIXELS = 40_000_000


def check_page_geometry(path: Path) -> None:
    """Inspect dimensions before downstream OCR/webtoon decoding allocates pixels."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path) as image:
                if image.width * image.height > MAX_PAGE_PIXELS:
                    raise ImportLimitError(
                        "Image exceeds the 40-megapixel import safety limit"
                    )
    except (Image.DecompressionBombWarning, Image.DecompressionBombError) as exc:
        raise ImportLimitError("Image exceeds the pixel safety limit") from exc
    except UnidentifiedImageError:
        # An unavailable image codec stays opaque; never force a full decode
        # here. Existing importer policy decides whether such pages are usable.
        return


class ImportLimitError(ValueError):
    pass


@dataclass(frozen=True)
class ImportLimits:
    expanded_bytes: int = 16 * 1024**3
    pages: int = 20000
    memory_mb: int = 1024
    reserve_bytes: int = 512 * 1024**2

    @classmethod
    def from_settings(cls, settings):
        return cls(
            settings.import_max_expanded_bytes,
            settings.import_max_pages,
            settings.import_subprocess_memory_mb,
            settings.import_disk_reserve_bytes,
        )

    def check(
        self, size: int, pages: int, directory: Path, *, reserve_output: bool = True
    ) -> None:
        if pages > self.pages:
            raise ImportLimitError(f"Import exceeds the {self.pages}-page safety limit")
        if size > self.expanded_bytes:
            raise ImportLimitError(
                f"Import exceeds the {self.expanded_bytes}-byte expanded-size limit"
            )
        needed = self.reserve_bytes + (size if reserve_output else 0)
        if shutil.disk_usage(directory).free < needed:
            raise ImportLimitError("Insufficient free space for safe import staging")


def run_decoder(
    command: list[str],
    target: Path,
    limits: ImportLimits,
    *,
    timeout: float,
    capture_limit: int = 4096,
) -> subprocess.CompletedProcess:
    """Bound the child process, and monitor aggregate output every 100 ms.

    POSIX RLIMIT_AS/FSIZE/CPU enforce hard per-process/per-file ceilings. The
    aggregate directory budget is polled (not a filesystem quota), so multi-file
    extraction can overshoot by one polling interval before the group is killed.
    """
    target.mkdir(parents=True, exist_ok=True)
    limits.check(0, 0, target)
    wrapped = [
        sys.executable,
        "-m",
        "tankarr.import_limits",
        str(limits.memory_mb),
        # Permit one sentinel byte above the aggregate budget: a decoder that
        # swallows EFBIG/short writes must not make a truncated file look valid.
        str(limits.expanded_bytes + 1),
        str(max(1, int(timeout))),
        *command,
    ]
    with tempfile.TemporaryFile(dir=target.parent) as output:
        process = subprocess.Popen(
            wrapped,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=(os.name == "posix"),
        )
        started = time.monotonic()
        try:
            while True:
                if os.fstat(output.fileno()).st_size > 1024 * 1024:
                    raise ImportLimitError(
                        "Archive decoder exceeded its diagnostic-output budget"
                    )
                files = [
                    entry
                    for entry in target.rglob("*")
                    if entry.is_file() and not entry.is_symlink()
                ]
                limits.check(
                    sum(entry.stat().st_size for entry in files),
                    len(files),
                    target,
                    reserve_output=False,
                )
                if process.poll() is not None:
                    break
                if time.monotonic() - started > timeout:
                    raise ImportLimitError("Archive decoder exceeded its time budget")
                time.sleep(0.1)
            output.seek(0)
            detail = output.read(min(capture_limit, 1024 * 1024)).decode(
                "utf-8", "replace"
            )
            if process.returncode:
                raise ImportLimitError(
                    f"Archive decoder failed or exceeded resource limits: {detail[:300]}"
                )
            return subprocess.CompletedProcess(
                command, process.returncode, stdout=detail, stderr=""
            )
        finally:
            if process.poll() is None:
                if os.name == "posix":
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
                process.wait()


def main() -> None:
    memory, size, cpu = (int(value) for value in sys.argv[1:4])
    if os.name == "posix":
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (memory * 1024**2, memory * 1024**2))
        resource.setrlimit(resource.RLIMIT_FSIZE, (size, size))
        resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    os.execvp(sys.argv[4], sys.argv[4:])


if __name__ == "__main__":
    main()
