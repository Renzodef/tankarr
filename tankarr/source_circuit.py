"""Stable source identities for temporary, durable failure isolation."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

# A remote overload (429 or a 5xx from the extension's site). The word
# "cloudflare" is not itself an overload: a challenge is reported separately.
_OVERLOAD = re.compile(
    r"\b429\b|\b5\d\d\b|rate limit|too many requests",
    re.IGNORECASE,
)
# First overload waits 15 minutes, then 1 hour, 4 hours, and 12 hours.
_OVERLOAD_DELAYS = (15 * 60, 3600, 4 * 3600, 12 * 3600)


def source_gate_key(provider: object, url: object, name: object = "") -> str:
    try:
        host = urlsplit(str(url or "")).hostname
    except ValueError:
        host = None
    return f"{provider}:{host or str(name or provider).strip().casefold()}"


def http_overload(message: str) -> bool:
    """True when a failure is an HTTP 429 or 5xx, including rate-limit text."""

    return _OVERLOAD.search(message or "") is not None


def circuit_delay_seconds(failures: int, message: str = "") -> int:
    """Seconds to wait before using a source again.

    Overload responses back off for much longer than a timeout. A single
    timeout still retries immediately; the second one uses the short curve
    capped at 10 minutes.
    """

    if failures <= 0:
        return 0
    if http_overload(message):
        index = min(len(_OVERLOAD_DELAYS) - 1, failures - 1)
        return _OVERLOAD_DELAYS[index]
    if failures <= 1:
        return 0
    return min(600, 30 * 2 ** min(5, failures - 2))
