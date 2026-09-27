"""Official platforms of a work, resolved from MangaBaka and served by Suwayomi.

MangaBaka's ``links_v2`` says where a work is legally published (type
``webplatform``, per language). Keiyoushi ships an extension for many of
those platforms; installed sources expose their ``homeUrl``, so the host
of an official link maps to a source without any hand-kept table. A small
seed table covers platforms whose extension is not installed yet (host →
package) and records which platforms are free to read, so Tankarr can
install the right extension on its own and only list free platforms on
the series page.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from tankarr.source_ranking import official_hosts


@dataclass(frozen=True)
class KnownPlatform:
    host: str
    pkg_name: str
    label: str
    free: bool


# Hosts as MangaBaka writes them → Keiyoushi package. ``free`` = chapters
# readable without an account (the extension exposes them).
KNOWN_PLATFORMS: tuple[KnownPlatform, ...] = (
    KnownPlatform(
        "mangaplus.shueisha.co.jp",
        "eu.kanade.tachiyomi.extension.all.mangaplus",
        "MANGA Plus",
        True,
    ),
    KnownPlatform(
        "webtoons.com", "eu.kanade.tachiyomi.extension.all.webtoons", "WEBTOON", True
    ),
    KnownPlatform(
        "tapas.io", "eu.kanade.tachiyomi.extension.en.tapastic", "Tapas", True
    ),
    KnownPlatform(
        "comikey.com", "eu.kanade.tachiyomi.extension.all.comikey", "Comikey", True
    ),
    KnownPlatform(
        "global.manga-up.com",
        "eu.kanade.tachiyomi.extension.all.mangaup",
        "Manga UP!",
        True,
    ),
    KnownPlatform("viz.com", "eu.kanade.tachiyomi.extension.en.viz", "VIZ", False),
    KnownPlatform(
        "kmanga.kodansha.com",
        "eu.kanade.tachiyomi.extension.en.kmanga",
        "K MANGA",
        False,
    ),
    KnownPlatform(
        "kodansha.us", "eu.kanade.tachiyomi.extension.en.kodansha", "Kodansha", False
    ),
    KnownPlatform(
        "manta.net", "eu.kanade.tachiyomi.extension.en.manta", "Manta", False
    ),
    KnownPlatform(
        "tappytoon.com",
        "eu.kanade.tachiyomi.extension.all.tappytoon",
        "Tappytoon",
        False,
    ),
    KnownPlatform(
        "toomics.com", "eu.kanade.tachiyomi.extension.all.toomics", "Toomics", False
    ),
    KnownPlatform(
        "mangamo.com", "eu.kanade.tachiyomi.extension.en.mangamo", "Mangamo", False
    ),
    KnownPlatform(
        "global.bookwalker.jp",
        "eu.kanade.tachiyomi.extension.all.bookwalker",
        "BOOK☆WALKER",
        False,
    ),
    KnownPlatform(
        "coolmic.me", "eu.kanade.tachiyomi.extension.en.coolmic", "Coolmic", False
    ),
    KnownPlatform(
        "globalcomix.com",
        "eu.kanade.tachiyomi.extension.en.globalcomix",
        "GlobalComix",
        False,
    ),
    KnownPlatform(
        "izneo.com", "eu.kanade.tachiyomi.extension.all.izneo", "izneo", False
    ),
)


def host_of(url: object) -> str:
    return (urlsplit(str(url or "")).hostname or "").casefold().removeprefix("www.")


def _hosts_match(link_host: str, source_host: str) -> bool:
    if not link_host or not source_host:
        return False
    return (
        link_host == source_host
        or link_host.endswith("." + source_host)
        or source_host.endswith("." + link_host)
    )


def official_platforms_for(
    manga: dict[str, Any],
    metadata: dict[str, Any] | None,
    *,
    installed_sources: list[dict[str, Any]] | None = None,
    release_sources: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """The work's official platforms with the extension that serves each.

    ``installed_sources`` are Suwayomi sources (``name``, ``pkg_name``,
    ``home_url``); ``release_sources`` the series' mapped download sources.
    Returned in catalogue order, one entry per platform.
    """

    language = str(manga.get("preferred_language") or "")
    links = list((metadata or {}).get("official_links") or [])
    wanted_hosts = official_hosts(links, language=language)
    mapped_names = {
        str(item.get("source_name") or "") for item in release_sources or []
    }
    mapped_hosts = {
        host_of(item.get("source_url"))
        for item in release_sources or []
        if item.get("source_url")
    }
    installed_by_host: dict[str, dict[str, Any]] = {}
    for source in installed_sources or []:
        home = host_of(source.get("home_url"))
        if home:
            installed_by_host.setdefault(home, source)
    output: list[dict[str, Any]] = []
    seen: set[str] = set()

    # One platform serves many languages (WEBTOON es/fr/th/en): the entry
    # shown - and mapped - must be the one in the series' own language, so
    # those links come first and win the one-per-host pick below.
    def language_rank(link: dict[str, Any]) -> int:
        link_language = str(link.get("language") or "").casefold()
        if language and link_language == language.casefold():
            return 0
        return 1 if link_language in {"", "unknown"} else 2

    for link in sorted(links, key=language_rank):
        host = host_of(link.get("url"))
        if not host or host not in wanted_hosts or host in seen:
            continue
        known = next(
            (item for item in KNOWN_PLATFORMS if _hosts_match(host, item.host)), None
        )
        installed = next(
            (src for h, src in installed_by_host.items() if _hosts_match(host, h)), None
        )
        if known is None and installed is None:
            continue  # an official site nobody can read from automatically
        seen.add(host)
        pkg_name = (installed or {}).get("pkg_name") or (
            known.pkg_name if known else ""
        )
        label = str(link.get("name") or (known.label if known else host))
        source_name = str((installed or {}).get("name") or "")
        output.append(
            {
                "name": label,
                "url": str(link.get("url") or ""),
                "host": host,
                "pkg_name": pkg_name,
                "free": bool(known.free) if known else True,
                "installed": installed is not None,
                "mapped": any(_hosts_match(host, mapped) for mapped in mapped_hosts)
                or (
                    any(
                        name and name.startswith(source_name.split(" (")[0])
                        for name in mapped_names
                    )
                    if source_name
                    else False
                ),
            }
        )
    return output


def extensions_to_install(platforms: list[dict[str, Any]]) -> list[str]:
    """Packages worth installing automatically: free platforms not installed."""

    return sorted(
        {
            item["pkg_name"]
            for item in platforms
            if item.get("free") and not item.get("installed") and item.get("pkg_name")
        }
    )


__all__ = [
    "KNOWN_PLATFORMS",
    "extensions_to_install",
    "host_of",
    "official_platforms_for",
]
