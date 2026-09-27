"""Deterministic release selection across providers and Suwayomi sources.

Every release carries a *source key*: the provider name for direct
providers (``mangadex``) or ``suwayomi:<id>`` for a Suwayomi extension
source. Every source belongs to a *class* known from a static table:

* ``official`` — publishers' own platforms (MANGA Plus, Webtoons…): legal,
  day-zero, sometimes reduced resolution, often a rolling window;
* ``origin`` — groups that publish their own series (Asura, Flame…): best
  quality for those series, nothing else;
* ``scan_hq`` — curated scanlation catalogues (Weeb Central, MangaDex);
* ``aggregator`` — fast mirrors of everything, uneven quality;
* ``unknown`` — anything Tankarr has never heard of.

Selection happens in one of two *phases*. A chapter published in the last
``FRESH_WINDOW_DAYS`` is *fresh*: the official source wins because it is
the legitimate day-zero release and its window closes soon. Everything
else is *backfill*: completeness and scan quality win, volumes beat loose
chapters, official platforms (which keep only a few chapters) come last.

The operator can pin an explicit order per phase; any source not listed
falls back to its class. The final key is a total order, so the same
input always yields the same choice.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from functools import lru_cache
from typing import Any
from urllib.parse import urlsplit

from tankarr.catalogue_consensus import count_is_reliable
from tankarr.source_health import NEUTRAL_COMPONENT

FRESH_WINDOW_DAYS = 21
SUSPECT_PAGE_COUNT = 5

SOURCE_CLASSES = ("official", "origin", "scan_hq", "aggregator", "unknown")
SOURCE_ROLES = ("primary_official", "secondary_official", "alternative")
ACQUISITION_POLICIES = (
    "prefer_official",
    "first_available",
)

# Slugs are matched against the normalized source name and package suffix.
KNOWN_SOURCE_CLASSES: dict[str, str] = {
    # official platforms
    "mangaplus": "official",
    "webtoons": "official",
    "webtoon": "official",
    "kmanga": "official",
    "comikey": "official",
    "azuki": "official",
    "inkr": "official",
    "mangamo": "official",
    "shonenjump": "official",
    "viz": "official",
    "tapas": "official",
    "bilibilicomics": "official",
    "lezhin": "official",
    "tappytoon": "official",
    "pocketcomics": "official",
    # origin groups
    "asurascans": "origin",
    "asura": "origin",
    "flamecomics": "origin",
    "flamescans": "origin",
    "reaperscans": "origin",
    "luminousscans": "origin",
    "drakescans": "origin",
    "genzupdates": "origin",
    "hivescans": "origin",
    "rizzfables": "origin",
    "nightscans": "origin",
    # curated scanlation catalogues
    "weebcentral": "scan_hq",
    "mangasee": "scan_hq",
    "mangalife": "scan_hq",
    "mangadex": "scan_hq",
    "dynasty": "scan_hq",
    # aggregators
    "mangafire": "aggregator",
    "mangabuddy": "aggregator",
    "mangak": "aggregator",
    "mangapill": "aggregator",
    "mangakakalot": "aggregator",
    "manganato": "aggregator",
    "manganelo": "aggregator",
    "batoto": "aggregator",
    "bato": "aggregator",
    "comick": "aggregator",
    "mangareader": "aggregator",
    "mangago": "aggregator",
    "mangaowl": "aggregator",
    "toonily": "aggregator",
    "mangapark": "aggregator",
    "mangahub": "aggregator",
    "mangatown": "aggregator",
    "mangafox": "aggregator",
    "fanfox": "aggregator",
}

PHASE_CLASS_ORDER: dict[str, tuple[str, ...]] = {
    "fresh": ("official", "origin", "scan_hq", "aggregator", "unknown"),
    # Without the official preference a fresh chapter is just "whoever
    # published first": classes still order quality, official last because
    # its rolling window makes it the worst backfill source.
    "fresh_first_out": ("origin", "scan_hq", "aggregator", "official", "unknown"),
    "backfill_official": ("official", "origin", "scan_hq", "aggregator", "unknown"),
    "backfill": ("origin", "scan_hq", "aggregator", "official", "unknown"),
}

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


_PARENTHETICAL = re.compile(r"\([^)]*\)")


def official_hosts(
    official_links: Iterable[dict[str, Any]] | None, *, language: str | None = None
) -> frozenset[str]:
    """Hosts of the work's official platforms, optionally for one language."""

    hosts: set[str] = set()
    for link in official_links or []:
        link_language = str(link.get("language") or "unknown").casefold()
        if language and link_language not in {language.casefold(), "unknown"}:
            continue
        host = (urlsplit(str(link.get("url") or "")).hostname or "").casefold()
        if host:
            hosts.add(host.removeprefix("www."))
    return frozenset(hosts)


def official_source_roles(
    official_links: Iterable[dict[str, Any]] | None,
    *,
    preferred_language: str | None,
    original_language: str | None,
) -> dict[str, str]:
    """Classify a work's publisher platforms by the edition they represent.

    A publisher can expose the same work in several languages.  The managed
    edition is the only primary source for display numbering and calendar
    dates.  The original-language edition is retained as secondary evidence:
    it can validate a stable cross-edition mapping but is never a download
    candidate for a differently managed language.
    """

    preferred = str(preferred_language or "").strip().casefold()
    original = str(original_language or "").strip().casefold()
    roles: dict[str, str] = {}
    for link in official_links or []:
        host = (urlsplit(str(link.get("url") or "")).hostname or "").casefold()
        host = host.removeprefix("www.")
        if not host:
            continue
        language = str(link.get("language") or "unknown").strip().casefold()
        if preferred and language == preferred:
            roles[host] = "primary_official"
        elif (
            original and language == original and roles.get(host) != "primary_official"
        ):
            roles[host] = "secondary_official"
        else:
            roles.setdefault(host, "alternative")
    return roles


def source_role_for_release(
    release: dict[str, Any], source_roles: Mapping[str, str] | None
) -> str:
    """Return the declared edition role for a concrete release URL."""

    host = release_host(release)
    for candidate, role in (source_roles or {}).items():
        if host and (host == candidate or host.endswith("." + candidate)):
            return role
    return "alternative"


def release_host(release: dict[str, Any]) -> str:
    return _release_host(str(release.get("source_url") or ""))


@lru_cache(maxsize=16384)
def _release_host(url: str) -> str:
    host = (urlsplit(url).hostname or "").casefold()
    return host.removeprefix("www.")


def slug(value: object) -> str:
    """``"MANGA Plus by SHUEISHA (EN)"`` → ``"mangaplusbyshueisha"``.

    Parenthesised suffixes (the language tag Suwayomi appends) are dropped
    so operators can write ``suwayomi:weebcentral`` for "Weeb Central (EN)".
    """

    return _SLUG_STRIP.sub("", _PARENTHETICAL.sub("", str(value or "")).casefold())


def source_class(
    *, provider: object, source_key: object = None, source_name: object = None
) -> str:
    """Classify a release's source from what Tankarr knows about it."""

    return _source_class(
        str(provider or ""), str(source_key or ""), str(source_name or "")
    )


@lru_cache(maxsize=2048)
def _source_class(provider: str, source_key: str, source_name: str) -> str:
    candidates: list[str] = []
    key = str(source_key or "")
    if ":" in key:
        candidates.append(slug(key.split(":", 1)[1]))
    candidates.append(slug(source_name))
    candidates.append(slug(provider))
    for candidate in candidates:
        if not candidate:
            continue
        if candidate in KNOWN_SOURCE_CLASSES:
            return KNOWN_SOURCE_CLASSES[candidate]
        # "mangaplusbyshueishaen" still starts with "mangaplus".
        for known, klass in KNOWN_SOURCE_CLASSES.items():
            if len(known) >= 5 and candidate.startswith(known):
                return klass
    return "unknown"


def release_source_keys(release: dict[str, Any]) -> tuple[str, ...]:
    """Every key an operator could use to name this release's source."""

    keys: list[str] = []
    provider = str(release.get("provider") or "").casefold()
    source_key = str(release.get("source_key") or "").casefold()
    source_name = slug(release.get("source_name"))
    if source_key:
        keys.append(source_key)
        if ":" in source_key and source_name:
            keys.append(f"{source_key.split(':', 1)[0]}:{source_name}")
    elif provider and source_name:
        keys.append(f"{provider}:{source_name}")
    if provider:
        keys.append(provider)
    return tuple(dict.fromkeys(keys))


def is_official_release(
    release: dict[str, Any], official_hosts: frozenset[str] = frozenset()
) -> bool:
    """Whether this release is from the publisher platform for this work."""

    if not official_hosts:
        return (
            source_class(
                provider=release.get("provider"),
                source_key=release.get("source_key"),
                source_name=release.get("source_name"),
            )
            == "official"
        )
    host = release_host(release)
    return bool(
        host
        and any(
            host == wanted or host.endswith("." + wanted) for wanted in official_hosts
        )
    )


ENDED_STATUSES = frozenset(
    {"ended", "completed", "finished", "complete", "cancelled", "canceled"}
)


def official_frontier(
    releases: Iterable[dict[str, Any]],
    official_hosts: frozenset[str] = frozenset(),
    *,
    metadata: dict[str, Any] | None = None,
    status: object = None,
    chapter_total: object = None,
) -> Decimal | None:
    """Highest chapter that officially exists, in the operator's language.

    Under ``prefer_official`` the official publication defines what exists:
    scanlators routinely run ahead of it, numbering chapters the publisher
    has not released. Following those downloads a chapter the work does not
    have under its official numbering, on a calendar that disagrees with the
    publisher.

    Where the reference comes from depends on the work:

    * **still publishing** (or on hiatus): the last chapter the official
      platform has actually put out *in this language*. The catalogue's own
      ``latest_release_chapter`` cannot be used - it counts the original
      edition, which runs ahead of every translation (Lookism is at 630 on
      NAVER and 611 on WEBTOON English);
    * **ended**: the catalogue knows the final chapter count, and that is
      more reliable than a partially mapped official source - three mapped
      chapters of a finished 55-chapter work would otherwise condemn the
      other 52.

    ``None`` means no frontier applies and every candidate stays eligible.
    """

    if str(status or "").casefold() in ENDED_STATUSES:
        # A manual managed-edition total is an explicit operator decision and
        # therefore wins over the original-work catalogue cardinality.
        total = chapter_total
        if total is None and count_is_reliable(metadata, "chapter"):
            # A count only one catalogue claims is not a frontier: Hansel &
            # Gretel's lone "1 chapter" would otherwise close a 22-chapter
            # series. Corroborated, or from before corroboration existed, it
            # stands; contested or lone, the work has no frontier at all.
            total = (metadata or {}).get("chapter_count")
        try:
            number = Decimal(str(total))
        except (ArithmeticError, ValueError, TypeError):
            number = None
        if number is not None and number.is_finite() and number > 0:
            return number
        # An ended work whose length nobody recorded has no usable frontier:
        # falling back to the mapped official source would cut the tail off
        # exactly the works that need it least.
        return None

    best: Decimal | None = None
    for release in releases:
        if not is_official_release(release, official_hosts):
            continue
        raw = str(release.get("chapter") or "").strip()
        if not raw:
            continue
        try:
            number = Decimal(raw)
        except (ArithmeticError, ValueError):
            continue
        if number.is_finite() and (best is None or number > best):
            best = number
    return best


def beyond_frontier(release: dict[str, Any], frontier: Decimal | None) -> bool:
    """Whether this release claims a chapter the publisher has not reached."""

    if frontier is None:
        return False
    raw = str(release.get("chapter") or "").strip()
    if not raw:
        return False  # a volume, or an unnumbered release: not a claim
    try:
        number = Decimal(raw)
    except (ArithmeticError, ValueError):
        return False
    return bool(number.is_finite() and number > frontier)


def normalize_acquisition_policy(value: object) -> str:
    normalized = str(value or "prefer_official").strip().casefold()
    if normalized not in ACQUISITION_POLICIES:
        raise ValueError(
            "release acquisition policy must be prefer_official or first_available"
        )
    return normalized


def normalize_source_list(value: object) -> str:
    """Canonical comma-separated list of source keys (order kept, dedup)."""

    seen: list[str] = []
    for raw in str(value or "").split(","):
        token = raw.strip().casefold()
        if not token:
            continue
        if not re.fullmatch(r"[a-z0-9_-]+(?::[a-z0-9_-]+)?", token):
            raise ValueError(
                f"Invalid source key {raw.strip()!r}: use provider, "
                "provider:source-id or provider:source-name (letters, digits, '-')"
            )
        if token not in seen:
            seen.append(token)
    return ",".join(seen)


def release_phase(
    publish_at: object,
    *,
    now: datetime | None = None,
    window_days: int = FRESH_WINDOW_DAYS,
) -> str:
    """``fresh`` when the release came out inside the window, else ``backfill``."""

    raw = str(publish_at or "").strip()
    if not raw:
        return "backfill"
    try:
        published = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return "backfill"
    if published.tzinfo is None:
        published = published.replace(tzinfo=UTC)
    current = now or datetime.now(UTC)
    return "fresh" if current - published <= timedelta(days=window_days) else "backfill"


def group_phase(
    releases: Iterable[dict[str, Any]], *, now: datetime | None = None
) -> str:
    """A logical chapter is fresh when any of its releases is."""

    return (
        "fresh"
        if any(release_phase(r.get("publish_at"), now=now) == "fresh" for r in releases)
        else "backfill"
    )


@dataclass(frozen=True)
class SourceRanking:
    """Operator preferences: explicit source order per phase."""

    fresh: tuple[str, ...] = ()
    backfill: tuple[str, ...] = ()
    acquisition_policy: str = "prefer_official"
    preference_profile: str = "balanced"

    @property
    def prefer_official(self) -> bool:
        """Compatibility view for callers that only distinguish two modes."""

        return self.acquisition_policy != "first_available"

    @classmethod
    def from_settings(
        cls,
        *,
        fresh: object = "",
        backfill: object = "",
        provider_priority: Sequence[str] = (),
        acquisition_policy: object | None = None,
        prefer_official: bool | None = None,
        preference_profile: str = "balanced",
    ) -> SourceRanking:
        """Backfill inherits the legacy provider order when it has no list."""

        fresh_keys = tuple(k for k in normalize_source_list(fresh).split(",") if k)
        backfill_keys = tuple(
            k for k in normalize_source_list(backfill).split(",") if k
        )
        if not backfill_keys:
            backfill_keys = tuple(str(name).casefold() for name in provider_priority)
        policy = (
            normalize_acquisition_policy(acquisition_policy)
            if acquisition_policy is not None
            else "prefer_official"
            if prefer_official is not False
            else "first_available"
        )
        return cls(
            fresh=fresh_keys,
            backfill=backfill_keys,
            acquisition_policy=policy,
            preference_profile=preference_profile,
        )

    def class_order(self, default: str) -> tuple[str, ...]:
        if self.preference_profile == "official":
            return PHASE_CLASS_ORDER["fresh"]
        if self.preference_profile == "curated":
            return ("scan_hq", "origin", "official", "aggregator", "unknown")
        return PHASE_CLASS_ORDER[default]

    def upgrade_key(
        self, release: dict[str, Any], hosts: frozenset[str]
    ) -> tuple[int, int]:
        """Stable preferences only: age, size and transient health cannot cause churn."""
        klass = (
            "official"
            if is_official_release(release, hosts)
            else source_class(
                provider=release.get("provider"),
                source_key=release.get("source_key"),
                source_name=release.get("source_name"),
            )
        )
        order = "backfill_official" if self.prefer_official else "backfill"
        return self.explicit_rank(release, "backfill"), self.class_order(order).index(
            klass
        )

    def explicit_rank(self, release: dict[str, Any], phase: str) -> int:
        order = self.fresh if phase == "fresh" else self.backfill
        keys = release_source_keys(release)
        best = len(order)
        for index, wanted in enumerate(order):
            for key in keys:
                # Exact key, or a name prefix ("suwayomi:mangaplus" matches
                # "suwayomi:mangaplusbyshueisha").
                if key == wanted or (
                    ":" in wanted
                    and len(wanted.split(":", 1)[1]) >= 4
                    and key.startswith(wanted)
                ):
                    best = min(best, index)
        return best

    def sort_key(
        self,
        release: dict[str, Any],
        *,
        phase: str,
        official_hosts: frozenset[str] = frozenset(),
        demoted_sources: frozenset[str] = frozenset(),
        health: Mapping[str, int] | None = None,
    ) -> tuple[int | str, ...]:
        """Lower sorts first. Total order: ties end on source key and id.

        ``demoted_sources`` are the sources that keep failing on this work:
        they stay usable, but every healthy source is tried first.
        ``health`` is the perennial standings from measured failures and
        download speed: it orders sources inside their class and follows
        them as their behaviour changes over time.
        """

        klass = source_class(
            provider=release.get("provider"),
            source_key=release.get("source_key"),
            source_name=release.get("source_name"),
        )
        if official_hosts:
            # The catalogue knows which platforms publish *this* work: a
            # release from one of them is official, an "official" platform
            # that does not carry the work is just another mirror.
            host = release_host(release)
            if host and any(
                host == h or host.endswith("." + h) for h in official_hosts
            ):
                klass = "official"
            elif klass == "official":
                klass = "aggregator"
        order_name = phase
        if self.acquisition_policy == "prefer_official" and phase == "backfill":
            order_name = "backfill_official"
        elif phase == "fresh" and self.acquisition_policy == "first_available":
            order_name = "fresh_first_out"
        class_rank = self.class_order(order_name).index(klass)
        is_volume = str(release.get("release_unit") or "chapter") == "volume" or (
            not release.get("chapter") and bool(release.get("volume"))
        )
        # Backfill prefers whole volumes; fresh releases are chapters by nature.
        unit_penalty = (
            (0 if is_volume else 1) if phase == "backfill" else (1 if is_volume else 0)
        )
        pages = int(release.get("pages") or 0)
        suspect = 1 if 0 < pages < SUSPECT_PAGE_COUNT else 0
        keys = release_source_keys(release)
        demoted = (
            1 if demoted_sources and any(key in demoted_sources for key in keys) else 0
        )
        # The perennial standings: a source that keeps failing, or crawls,
        # sorts behind its class peers; a fast reliable one sorts first.
        # The first key with a record wins - keys go most-specific first,
        # and an unmeasured alias must not hide a measured source.
        standings = NEUTRAL_COMPONENT
        if health:
            for key in keys:
                if key in health:
                    standings = health[key]
                    break
        common = (
            self.explicit_rank(release, phase),
            # A source that keeps failing on this work is the last resort.
            demoted,
            # A whole volume file beats loose chapters from a better class:
            # the unit decision was taken when the map was built.
            unit_penalty,
        )
        quality = (
            class_rank,
            standings,
            suspect,
            -int(release.get("version") or 0),
            -pages,
        )
        identity = (
            str(release.get("source_key") or release.get("provider") or ""),
            str(release.get("id") or ""),
        )
        if self.acquisition_policy == "first_available":
            # Failing sources remain a last resort. Otherwise publication time
            # is the policy; explicit source pins and quality only break ties.
            return (
                2
                if release.get("provider") == "translated"
                else int(release.get("provider") == "assembled"),
                demoted,
                str(release.get("publish_at") or "~"),
                self.explicit_rank(release, phase),
                unit_penalty,
                *quality,
                *identity,
            )
        return (
            2
            if release.get("provider") == "translated"
            else int(release.get("provider") == "assembled"),
            *common,
            *quality,
            _descending(str(release.get("publish_at") or "")),
            *identity,
        )

    def best(
        self,
        releases: Sequence[dict[str, Any]],
        *,
        now: datetime | None = None,
        official_hosts: frozenset[str] = frozenset(),
        demoted_sources: frozenset[str] = frozenset(),
        health: Mapping[str, int] | None = None,
    ) -> dict[str, Any] | None:
        phase = group_phase(releases, now=now)
        candidates = list(releases)
        if not candidates:
            return None
        return min(
            candidates,
            key=lambda item: self.sort_key(
                item,
                phase=phase,
                official_hosts=official_hosts,
                demoted_sources=demoted_sources,
                health=health,
            ),
        )


def _descending(text: str) -> str:
    """Map a string so that lexical ascending order equals descending order."""

    return "".join(chr(0x10FFFF - ord(ch)) for ch in text) if text else "￿"


__all__ = [
    "ACQUISITION_POLICIES",
    "beyond_frontier",
    "official_frontier",
    "FRESH_WINDOW_DAYS",
    "KNOWN_SOURCE_CLASSES",
    "PHASE_CLASS_ORDER",
    "SourceRanking",
    "group_phase",
    "is_official_release",
    "official_hosts",
    "official_source_roles",
    "release_host",
    "normalize_source_list",
    "normalize_acquisition_policy",
    "release_phase",
    "release_source_keys",
    "source_class",
    "source_role_for_release",
]
