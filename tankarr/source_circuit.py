"""Stable source identities for temporary, durable failure isolation."""

from urllib.parse import urlsplit


def source_gate_key(provider: object, url: object, name: object = "") -> str:
    try:
        host = urlsplit(str(url or "")).hostname
    except ValueError:
        host = None
    return f"{provider}:{host or str(name or provider).strip().casefold()}"
