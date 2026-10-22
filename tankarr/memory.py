"""Return freed native allocations to the operating system.

A library rebuild or a monitor pass allocates a large temporary working set
and then frees it. glibc keeps that memory in per-thread arenas, so the
process stays large on a small host after the Python objects are gone.
``malloc_trim`` is glibc-only; other libcs do not offer it and this is a
no-op there.
"""

from __future__ import annotations

import ctypes
import gc

try:
    _malloc_trim = ctypes.CDLL(None).malloc_trim
    _malloc_trim.argtypes = [ctypes.c_size_t]
    _malloc_trim.restype = ctypes.c_int
except AttributeError:  # pragma: no cover - Windows and macOS
    _malloc_trim = None


def release_native_heap() -> bool:
    """Collect Python garbage, then ask glibc to return free arenas."""

    gc.collect()
    if _malloc_trim is None:
        return False
    return bool(_malloc_trim(0))
