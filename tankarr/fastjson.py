"""Compact JSON bytes for the large payloads (library, series, Wanted, Calendar).

``orjson`` encodes them several times faster than the standard library and
is a normal dependency; a source installation without its wheel still works,
on the standard library, with byte-identical output for everything Tankarr
serialises (strings are never ASCII-escaped, separators are compact).
"""

from __future__ import annotations

import json
from typing import Any

try:  # pragma: no cover - exercised by whichever encoder the host has
    import orjson
except ImportError:  # pragma: no cover
    orjson = None  # type: ignore[assignment]


def dumps(value: Any) -> bytes:
    """Serialise ``value`` to compact UTF-8 JSON bytes."""

    if orjson is not None:
        try:
            return orjson.dumps(value)
        except TypeError:
            # Non-string keys and a few exotic values are accepted by the
            # standard encoder (keys are coerced); keep that behaviour.
            pass
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
