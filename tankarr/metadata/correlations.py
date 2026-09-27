from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse


@dataclass(frozen=True)
class CorrelationSource:
    name: str
    label: str


# Followers are never searched by title: their ids arrive through MangaBaka.
# MangaUpdates is the exception the operator may pin by hand, because it is
# the one catalogue that lists a work's published editions (publisher, book
# count, completeness) and MangaBaka's record sometimes lacks its id
# (Galaxy Express 999 had AniList and Kitsu, no MangaUpdates, no editions).
EDITABLE_CORRELATION_SOURCES: tuple[CorrelationSource, ...] = (
    CorrelationSource("mangabaka", "MangaBaka"),
    CorrelationSource("mangaupdates", "MangaUpdates"),
)

CORRELATION_LABELS: dict[str, str] = {
    item.name: item.label for item in EDITABLE_CORRELATION_SOURCES
} | {
    "mangaupdates": "MangaUpdates",
    "anilist": "AniList",
    "myanimelist": "MyAnimeList",
    "animeplanet": "Anime-Planet",
    "kitsu": "Kitsu",
}


def correlation_url(source: str, external_id: str) -> str | None:
    """Canonical public page for one catalogue identity, when the form is known."""

    source = source.strip().casefold()
    token = external_id.strip()
    if not token:
        return None
    if source == "mangabaka":
        return f"https://mangabaka.org/{token}"
    if source == "mangaupdates":
        if not token.isdecimal():
            return None
        return f"https://www.mangaupdates.com/series/{_base36(int(token))}"
    if source == "anilist":
        return f"https://anilist.co/manga/{token}"
    if source == "myanimelist":
        return f"https://myanimelist.net/manga/{token}"
    if source == "kitsu":
        return f"https://kitsu.app/manga/{token}"
    if source == "animeplanet":
        return f"https://www.anime-planet.com/manga/{token}"
    return None


def _base36(value: int) -> str:
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyz"
    output = ""
    while value:
        value, remainder = divmod(value, 36)
        output = alphabet[remainder] + output
    return output or "0"


def _url_token(value: str, *, hosts: set[str], prefix: str) -> str:
    parsed = urlparse(value)
    hostname = (parsed.hostname or "").casefold().removeprefix("www.")
    if (
        parsed.scheme.casefold() != "https"
        or hostname not in hosts
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443)
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Use the provider's canonical HTTPS series URL")
    parts = [part for part in parsed.path.split("/") if part]
    prefix_parts = [part for part in prefix.split("/") if part]
    if parts[: len(prefix_parts)] != prefix_parts or len(parts) <= len(prefix_parts):
        raise ValueError("The URL does not identify a provider series")
    return parts[len(prefix_parts)]


def normalize_correlation(source: str, value: str) -> dict[str, str]:
    """Validate a user-entered provider URL/ID without performing arbitrary I/O."""

    source = source.strip().casefold()
    raw = value.strip()
    if source not in CORRELATION_LABELS or source not in {
        item.name for item in EDITABLE_CORRELATION_SOURCES
    }:
        raise ValueError(f"Unsupported metadata source: {source}")
    if not raw or len(raw) > 500:
        raise ValueError("Enter a provider URL or identifier")
    is_url = "://" in raw

    if source == "myanimelist":
        token = (
            _url_token(
                raw,
                hosts={"myanimelist.net"},
                prefix="manga",
            )
            if is_url
            else raw
        )
        if not token.isdecimal() or int(token) <= 0:
            raise ValueError("MyAnimeList requires a positive manga ID")
        external_id = str(int(token))
        return {
            "source": source,
            "label": CORRELATION_LABELS[source],
            "external_id": external_id,
            "url": f"https://myanimelist.net/manga/{external_id}",
        }

    if source in {"mangabaka", "anilist"}:
        hosts = {"mangabaka.org"} if source == "mangabaka" else {"anilist.co"}
        prefix = "" if source == "mangabaka" else "manga"
        token = _url_token(raw, hosts=hosts, prefix=prefix) if is_url else raw
        if not token.isdecimal() or int(token) <= 0:
            raise ValueError(
                f"{CORRELATION_LABELS[source]} requires a positive series ID"
            )
        external_id = str(int(token))
        return {
            "source": source,
            "label": CORRELATION_LABELS[source],
            "external_id": external_id,
            "url": correlation_url(source, external_id) or "",
        }

    if source == "mangaupdates":
        token = (
            _url_token(
                raw,
                hosts={"mangaupdates.com"},
                prefix="series",
            )
            if is_url
            else raw
        ).casefold()
        if not re.fullmatch(r"[0-9a-z]+", token):
            raise ValueError("MangaUpdates requires a series ID or canonical URL")
        external_number = int(token, 10) if token.isdecimal() else int(token, 36)
        if external_number <= 0:
            raise ValueError("MangaUpdates requires a positive series ID")
        slug = _base36(external_number)
        return {
            "source": source,
            "label": CORRELATION_LABELS[source],
            "external_id": str(external_number),
            "url": f"https://www.mangaupdates.com/series/{slug}",
        }

    if source == "google_books":
        token = raw
        if is_url:
            parsed = urlparse(raw)
            hostname = (parsed.hostname or "").casefold().removeprefix("www.")
            try:
                port = parsed.port
            except ValueError as exc:
                raise ValueError("Use a canonical Google Books volume URL") from exc
            safe_host = bool(
                hostname == "googleapis.com"
                or re.fullmatch(
                    r"(?:books\.)?google\.(?:[a-z]{2,3}|[a-z]{2}\.[a-z]{2})",
                    hostname,
                )
            )
            if (
                parsed.scheme.casefold() != "https"
                or not safe_host
                or parsed.username is not None
                or parsed.password is not None
                or port not in (None, 443)
                or parsed.fragment
            ):
                raise ValueError("Use a canonical Google Books volume URL")
            if hostname == "googleapis.com":
                parts = [part for part in parsed.path.split("/") if part]
                token = (
                    parts[3]
                    if len(parts) == 4 and parts[:3] == ["books", "v1", "volumes"]
                    else ""
                )
            else:
                parts = [part for part in parsed.path.split("/") if part]
                if parsed.path.rstrip("/") == "/books":
                    token = (
                        parse_qs(parsed.query, keep_blank_values=False).get("id")
                        or [""]
                    )[0]
                elif len(parts) == 4 and parts[:2] == ["books", "edition"]:
                    token = parts[3]
                else:
                    token = ""
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", token):
            raise ValueError("Google Books requires a volume ID or exact volume URL")
        return {
            "source": source,
            "label": CORRELATION_LABELS[source],
            "external_id": token,
            "url": f"https://books.google.com/books?id={token}",
        }

    if is_url:
        parsed = urlparse(raw)
        hostname = (parsed.hostname or "").casefold().removeprefix("www.")
        try:
            port = parsed.port
        except ValueError as exc:
            raise ValueError("Use Comic Vine's canonical HTTPS volume URL") from exc
        if (
            parsed.scheme.casefold() != "https"
            or hostname != "comicvine.gamespot.com"
            or parsed.username is not None
            or parsed.password is not None
            or port not in (None, 443)
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Use Comic Vine's canonical HTTPS volume URL")
        token = next(
            (
                part
                for part in parsed.path.split("/")
                if re.fullmatch(r"4050-[1-9][0-9]*", part)
            ),
            "",
        )
    else:
        token = raw
    match = re.fullmatch(r"(?:4050-)?([1-9][0-9]*)", token)
    if not match:
        raise ValueError("Comic Vine requires a volume ID such as 4050-12345")
    external_id = match.group(1)
    return {
        "source": source,
        "label": CORRELATION_LABELS[source],
        "external_id": external_id,
        "url": f"https://comicvine.gamespot.com/volume/4050-{external_id}/",
    }
