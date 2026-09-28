"""Outbound HTTP clients that share one TLS context.

Constructing an ``httpx.AsyncClient`` builds an SSL context and loads the CA
bundle: about 50 ms of blocking CPU, measured here, against 0.7 ms with a
context built once. Several integrations open a client per call (the
qBittorrent and SABnzbd polls every few seconds, every Komga, Prowlarr and
Stump request), so that cost landed on the event loop hundreds of times an
hour. Every client goes through :func:`async_client` and reuses the context;
a caller that needs its own verification settings passes ``verify`` itself.
"""

from __future__ import annotations

import ssl
import threading
from typing import Any

import httpx

_context: ssl.SSLContext | None = None
_context_lock = threading.Lock()


def ssl_context() -> ssl.SSLContext:
    """The process-wide TLS context, honouring httpx's environment handling."""

    global _context
    if _context is None:
        with _context_lock:
            if _context is None:
                _context = httpx.create_ssl_context()
    return _context


def async_client(**kwargs: Any) -> httpx.AsyncClient:
    """An ``httpx.AsyncClient`` with the shared TLS context unless told otherwise."""

    kwargs.setdefault("verify", ssl_context())
    return httpx.AsyncClient(**kwargs)
