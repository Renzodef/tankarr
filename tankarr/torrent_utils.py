"""Torrent helpers shared by Prowlarr, qBittorrent and the importer:
release numbering hints, title scoring, magnet/torrent info hashes.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
import xml.etree.ElementTree as ET
from urllib.parse import parse_qsl, urlsplit

MAX_TORRENT_BYTES = 5 * 1024 * 1024
INFO_HASH_PATTERN = re.compile(r"^[0-9a-f]{40}$")
SIZE_PATTERN = re.compile(r"^([0-9]+(?:\.[0-9]+)?)\s*([KMGT]?i?B)$", re.I)
VOLUME_PATTERN = re.compile(
    r"(?i)(?:^|[\s._\-[(])(?:v|vol(?:ume)?)\.?\s*0*(\d+(?:\.\d+)?)"
    r"(?:\s*[-–]\s*(?:v|vol(?:ume)?)?\.?\s*0*(\d+(?:\.\d+)?))?"
    r"(?![a-z0-9])"
)
CHAPTER_PATTERN = re.compile(
    r"(?i)(?:^|[\s._\-[(])(?:ch(?:apter)?|c)\.?\s*0*(\d+(?:\.\d+)?)"
    r"(?:\s*[-–]\s*(?:ch(?:apter)?|c)?\.?\s*0*(\d+(?:\.\d+)?))?"
    r"(?![a-z])"
)


def _text(element: ET.Element | None) -> str:
    return str(element.text or "").strip() if element is not None else ""


def _integer(value: object) -> int:
    try:
        return max(0, int(str(value or "0")))
    except ValueError:
        return 0


def parse_size_bytes(value: str) -> int:
    match = SIZE_PATTERN.fullmatch(value.strip())
    if match is None:
        return 0
    amount = float(match.group(1))
    unit = match.group(2).casefold()
    powers = {
        "b": 0,
        "kb": 1,
        "kib": 1,
        "mb": 2,
        "mib": 2,
        "gb": 3,
        "gib": 3,
        "tb": 4,
        "tib": 4,
    }
    base = 1024 if "i" in unit else 1000
    return int(amount * (base ** powers[unit]))


def release_number_hints(title: str) -> tuple[str | None, str | None]:
    """Return conservative volume/chapter hints from a release title."""

    volume_match = VOLUME_PATTERN.search(title)
    chapter_match = CHAPTER_PATTERN.search(title)

    def value(match: re.Match[str] | None) -> str | None:
        # "Kingdom.Vol.10.2026" is volume 10 followed by a year, not 10.2026:
        # a fractional part of three or more digits is never a decimal number.
        if match is not None:
            raw = match.group(1)
            if "." in raw and len(raw.split(".", 1)[1]) >= 3:
                return raw.split(".", 1)[0].lstrip("0") or "0"
        if match is None:
            return None
        start = match.group(1).lstrip("0") or "0"
        end = match.group(2)
        if end is None:
            return start
        normalized_end = end.lstrip("0") or "0"
        return f"{start}-{normalized_end}"

    return value(volume_match), value(chapter_match)


def _normalized_words(value: str) -> set[str]:
    normalized = unicodedata.normalize("NFKD", value).casefold()
    return {
        token
        for token in re.findall(r"[a-z0-9]+", normalized)
        if len(token) > 1 and token not in {"the", "and", "vol", "volume", "chapter"}
    }


def release_match_score(query: str, title: str) -> int:
    wanted = _normalized_words(query)
    if not wanted:
        return 0
    found = _normalized_words(title)
    return round(100 * len(wanted & found) / len(wanted))


def magnet_info_hash(value: str) -> str:
    """Return the canonical v1 hash from a bounded magnet URI."""

    raw = str(value or "").strip()
    if not raw or len(raw) > 65_535:
        raise ValueError("Invalid magnet URI")
    parsed = urlsplit(raw)
    if parsed.scheme.casefold() != "magnet" or parsed.netloc or parsed.fragment:
        raise ValueError("Invalid magnet URI")
    hashes = {
        item.removeprefix("urn:btih:").casefold()
        for key, item in parse_qsl(parsed.query, keep_blank_values=True)
        if key.casefold() == "xt" and item.casefold().startswith("urn:btih:")
    }
    if len(hashes) != 1:
        raise ValueError("Magnet URI must contain one BitTorrent v1 hash")
    info_hash = hashes.pop()
    if INFO_HASH_PATTERN.fullmatch(info_hash) is None:
        raise ValueError("Magnet URI has an unsupported info hash")
    return info_hash


class _BencodeScanner:
    def __init__(self, payload: bytes):
        self.payload = payload
        self.items = 0

    def skip(self, index: int, depth: int = 0) -> int:
        if depth > 64 or index >= len(self.payload):
            raise ValueError("Invalid bencoded torrent")
        self.items += 1
        if self.items > 1_000_000:
            raise ValueError("Torrent metadata is too complex")
        marker = self.payload[index : index + 1]
        if marker == b"i":
            end = self.payload.find(b"e", index + 1)
            if end < 0 or not re.fullmatch(
                b"-?(?:0|[1-9][0-9]*)", self.payload[index + 1 : end]
            ):
                raise ValueError("Invalid bencoded integer")
            return end + 1
        if marker in {b"l", b"d"}:
            cursor = index + 1
            while (
                cursor < len(self.payload) and self.payload[cursor : cursor + 1] != b"e"
            ):
                cursor = self.skip(cursor, depth + 1)
            if cursor >= len(self.payload):
                raise ValueError("Unterminated bencoded collection")
            return cursor + 1
        if marker.isdigit():
            colon = self.payload.find(b":", index)
            if colon < 0 or not self.payload[index:colon].isdigit():
                raise ValueError("Invalid bencoded byte string")
            size = int(self.payload[index:colon])
            end = colon + 1 + size
            if end > len(self.payload):
                raise ValueError("Truncated bencoded byte string")
            return end
        raise ValueError("Invalid bencoded marker")

    def bytes_value(self, index: int) -> tuple[bytes, int]:
        colon = self.payload.find(b":", index)
        if colon < 0 or not self.payload[index:colon].isdigit():
            raise ValueError("Invalid bencoded dictionary key")
        size = int(self.payload[index:colon])
        start = colon + 1
        end = start + size
        if end > len(self.payload):
            raise ValueError("Truncated bencoded dictionary key")
        return self.payload[start:end], end


def torrent_info_hash(payload: bytes) -> str:
    """Compute the BitTorrent v1 info hash from the exact encoded info value."""

    if not payload or len(payload) > MAX_TORRENT_BYTES or payload[:1] != b"d":
        raise ValueError("Invalid torrent payload")
    scanner = _BencodeScanner(payload)
    cursor = 1
    info_slice: tuple[int, int] | None = None
    while cursor < len(payload) and payload[cursor : cursor + 1] != b"e":
        key, cursor = scanner.bytes_value(cursor)
        value_start = cursor
        cursor = scanner.skip(cursor, 1)
        if key == b"info":
            if info_slice is not None:
                raise ValueError("Torrent contains more than one info dictionary")
            info_slice = (value_start, cursor)
    if cursor != len(payload) - 1 or info_slice is None:
        raise ValueError("Torrent has no canonical top-level info dictionary")
    start, end = info_slice
    return hashlib.sha1(payload[start:end], usedforsecurity=False).hexdigest()
