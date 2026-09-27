from __future__ import annotations

import re
from urllib.parse import quote

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def normalized_artwork_sha256(value: object) -> str | None:
    digest = str(value or "").strip().casefold()
    return digest if _SHA256_RE.fullmatch(digest) else None


def versioned_artwork_url(path: str, artwork_sha256: object) -> str:
    """Return a content-addressed URL when a canonical artwork hash exists."""

    digest = normalized_artwork_sha256(artwork_sha256)
    return f"{path}?v={digest}" if digest else path


def series_artwork_url(manga_id: str, artwork_sha256: object) -> str:
    path = f"/api/metadata/artwork/{quote(str(manga_id), safe='')}/series"
    return versioned_artwork_url(path, artwork_sha256)


def volume_artwork_url(manga_id: str, volume: str, artwork_sha256: object) -> str:
    path = (
        f"/api/metadata/artwork/{quote(str(manga_id), safe='')}/volumes/"
        f"{quote(str(volume), safe='')}"
    )
    return versioned_artwork_url(path, artwork_sha256)


def artwork_cache_headers(
    requested_version: object, artwork_sha256: object
) -> dict[str, str]:
    """Cache forever only when the requested URL names the current content."""

    digest = normalized_artwork_sha256(artwork_sha256)
    requested = normalized_artwork_sha256(requested_version)
    headers = {"Cache-Control": "no-cache, max-age=0"}
    if digest:
        headers["ETag"] = f'"sha256-{digest}"'
    if digest and requested == digest:
        headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return headers
