"""Automatic volume search through Prowlarr for series that follow volumes.

Whole-volume books rarely come from Suwayomi extensions; they live on
Usenet and torrent indexers as official digital releases ("BECK v03 (2019)
(Kodansha Comics USA) (Digital)"). For every missing volume of a series in
the volumes unit the Wanted cycle asks Prowlarr with a few deterministic
queries and grabs one release only when the match is unambiguous: every
word of the title is in the release name and the release names exactly
that volume number. Usenet official digital releases win; torrents need
seeders.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from tankarr.internet_archive import PROVIDER_NAME as DIRECT_PROVIDER
from tankarr.release_kind import classify_release, title_matches_release
from tankarr.torrent_utils import release_match_score

logger = logging.getLogger(__name__)

MAX_GRABS_PER_CYCLE = 10
DIGITAL_MARKER = re.compile(r"\(digital\)|\bdigital\b", re.I)
IDENTITY_WORD = re.compile(r"[a-z0-9]+", re.I)
GENERIC_PUBLISHER_WORDS = frozenset(
    {"comic", "comics", "edition", "media", "press", "publishing", "usa"}
)
PUBLISHER_TAG_WORDS = frozenset(
    {"comics", "press", "publishing", "publisher", "studios", "books"}
)
RELEASE_TAG = re.compile(r"\[([^\]]+)\]|\(([^)]+)\)")


def series_queries(title: str, publisher: str | list[str] | None = None) -> list[str]:
    """Indexers tokenise poorly ("BECK v03" finds nothing, "BECK Kodansha"
    lists every volume): ask once per series, publishers first (the English
    edition's before the original), then the bare title, and pick the books
    from the pooled results."""

    base = " ".join(str(title or "").split()).strip()
    publishers = [publisher] if isinstance(publisher, str) else list(publisher or [])
    queries: list[str] = []
    for name in publishers:
        clean = " ".join(str(name or "").replace("&", " ").split()).strip()
        if clean and f"{base} {clean}" not in queries:
            queries.append(f"{base} {clean}")
    queries.append(base)
    return queries


def volume_queries(title: str, volume: int, publisher: str | None = None) -> list[str]:
    return series_queries(title, publisher)


def book_queries(title: str, volume: int) -> list[str]:
    """One book by number, when the series queries buried it: "Mars" on a
    usenet indexer is seventy-five rows of Mars Attacks and Warlord of Mars
    before "MARS.Vol.14.2019.digital" ever shows; "Mars v14" is that row."""

    base = " ".join(str(title or "").split()).strip()
    number = int(volume)
    return [f"{base} v{number:02d}", f"{base} Vol {number}", f"{base} volume {number}"]


#: Targeted book queries per pass: an indexer call is not free.
MAX_VOLUME_QUERIES_PER_PASS = 6


def rank_release(
    release: dict[str, Any], publisher: str | None
) -> tuple[int, int, int, int]:
    title = str(release.get("title") or "")
    protocol = str(release.get("protocol") or "torrent")
    usenet = protocol == "usenet"
    direct = protocol == "http"
    digital = bool(DIGITAL_MARKER.search(title))
    from_publisher = bool(publisher and publisher.casefold() in title.casefold())
    seeders = int(release.get("seeders") or 0)
    if not usenet and not direct and seeders <= 0:
        return (-1, 0, 0, 0)  # a dead torrent is never a candidate
    indexer_priority = int(release.get("indexer_priority") or 50)
    return (
        2 if usenet else 1,
        int(digital) + int(from_publisher),
        -indexer_priority,
        seeders,
    )


def _undownloadable(release: dict[str, Any]) -> bool:
    """A torrent nobody seeds cannot be grabbed, so it is not a question."""

    if str(release.get("protocol") or "torrent") in {"usenet", "http"}:
        return False
    return int(release.get("seeders") or 0) <= 0


def _ask_or_decide(release: dict[str, Any], reason: str) -> None:
    """Record a near-miss, and answer it here when it cannot be acted on."""

    release["_review"] = reason
    if _undownloadable(release):
        release["_review"] = f"{reason}; no seeders"
        release["_review_resolution"] = "rejected"


def _publisher_conflict_reason(release_title: str, publishers: list[str]) -> str | None:
    if not publishers:
        return None
    known_words = {
        word
        for publisher in publishers
        for word in IDENTITY_WORD.findall(publisher.casefold())
        if len(word) > 2 and word not in GENERIC_PUBLISHER_WORDS
    }
    for match in RELEASE_TAG.finditer(release_title):
        tag_words = set(IDENTITY_WORD.findall((match[1] or match[2]).casefold()))
        distinctive = tag_words - PUBLISHER_TAG_WORDS - GENERIC_PUBLISHER_WORDS
        if (
            tag_words & PUBLISHER_TAG_WORDS
            and distinctive
            and not tag_words & known_words
        ):
            return "release names an unverified publisher"
    return None


def _identity_review_reason(
    title: str,
    release_title: str,
    publishers: list[str],
    creators: tuple[str, ...] | list[str] = (),
) -> str | None:
    words = IDENTITY_WORD.findall(title.casefold())
    if not words:
        return "missing work title"
    # A book can lead with the correct title and still be a different work.
    # Bracketed publisher names are explicit edition evidence; a conflicting
    # one must not be overridden by a title match or a numbered family.
    conflict = _publisher_conflict_reason(release_title, publishers)
    if conflict:
        return conflict
    # A multiword title inside a different work's name can be a subtitle:
    # "The Witcher v08 - Wild Animals" is not the two-book Wild Animals.
    if len(words) > 1 and _title_leads(title, release_title):
        return None
    original = "".join(character for character in title if character.isalnum())
    if len(words) == 1 and original.isupper() and len(original) <= 5:
        return None
    release_words = set(IDENTITY_WORD.findall(release_title.casefold()))
    # The author's name in the release is as good as the publisher's:
    # "Dominion Tank Police masamune shirow" is Shirow's Dominion.
    for creator in creators:
        evidence = {
            word
            for word in IDENTITY_WORD.findall(str(creator).casefold())
            if len(word) > 3
        }
        if evidence and evidence & release_words:
            return None
    for publisher in publishers:
        evidence = {
            word
            for word in IDENTITY_WORD.findall(publisher.casefold())
            if len(word) > 2 and word not in GENERIC_PUBLISHER_WORDS
        }
        if evidence & release_words:
            return None
    return (
        "single-word title without publisher corroboration"
        if len(words) == 1
        else "title occurs within another work without publisher or creator corroboration"
    )


def _family_corroborates(
    title: str,
    results: list[dict[str, Any]],
    volume_count: int | None,
    publishers: list[str],
) -> bool:
    """Whether the indexers' own run of books vouches for a one-word title.

    "Mars v01 [2019] [Digital]" alone could be any Mars. Fifteen releases
    "Mars v01".."Mars v15" for a fifteen-volume work are the work: no other
    title produces a numbered family that fits the edition. Anything that
    merely contains the word never leads with it and is not counted.
    """

    if not volume_count or volume_count < 3:
        return False
    numbers: set[int] = set()
    for release in results:
        name = str(release.get("title") or "")
        if _publisher_conflict_reason(name, publishers):
            continue
        if not _title_leads(title, name) or not title_matches_release(title, name):
            continue
        kind = classify_release(title=name, size_bytes=release.get("size_bytes"))
        if kind.kind == "pack":
            numbers.update(int(value) for value in kind.volumes)
        elif kind.kind == "volume" and str(kind.volume or "").isdigit():
            numbers.add(int(str(kind.volume)))
    numbers = {number for number in numbers if 1 <= number <= volume_count}
    return len(numbers) >= max(3, -(-volume_count * 6 // 10))


def _title_leads(title: str, release_title: str) -> bool:
    """Whether the release title begins with the work's title as a word."""

    words = IDENTITY_WORD.findall(title.casefold())
    if not words:
        return False
    text = release_title.casefold()
    # Release names often open with a group tag: "[Group] And v01".
    text = re.sub(r"^\s*(\[[^\]]*\]\s*|\([^)]*\)\s*)+", "", text)
    lead = IDENTITY_WORD.findall(text)[: len(words)]
    return lead == words


def pick_release(
    results: list[dict[str, Any]],
    *,
    title: str,
    volume: int,
    publisher: str | list[str] | None,
    single_volume: bool = False,
    volume_count: int | None = None,
    wanted_volumes: set[int] | None = None,
    family_corroborated: bool | None = None,
    creators: tuple[str, ...] | list[str] = (),
) -> dict[str, Any] | None:
    publishers = [publisher] if isinstance(publisher, str) else list(publisher or [])
    # The planner asks about one release at a time: the family it belongs
    # to was judged on the whole pool and is handed down, not recomputed.
    corroborated = (
        _family_corroborates(title, results, volume_count, publishers)
        if family_corroborated is None
        else family_corroborated
    )
    candidates = []
    for release in results:
        if release.get("download"):
            continue  # already grabbed or being grabbed
        release_title = str(release.get("title") or "")
        if _identity_review_reason(
            title, release_title, publishers, creators
        ) and not _title_leads(title, release_title):
            # "And": every release on earth contains the word. Without the
            # publisher, only a release that *starts* with the title can be
            # the work; the rest ("Queen of Mars", "Biker Mice from Mars")
            # are other works and not even a question, family or no family.
            continue
        kind = classify_release(
            title=str(release.get("title") or ""), size_bytes=release.get("size_bytes")
        )
        if kind.kind == "rejected" or kind.kind == "chapter":
            continue
        if kind.kind == "pack":
            if volume not in kind.volumes:
                continue
            # A pack is safe when it stays inside the work and is mostly what
            # the library is missing. One that runs past the work's last
            # volume, or that would mostly re-download owned books, is a
            # human's call.
            covered = set(kind.volumes)
            overruns = volume_count is not None and any(
                number > volume_count for number in covered
            )
            wanted = set(wanted_volumes or ())
            useful = len(covered & wanted) / len(covered) if wanted else 0.0
            if overruns:
                # Books past the work's last volume mean this is another work
                # (or several bundled together), not a doubtful match. Keep it
                # as history and never spend the operator's attention on it.
                release["_review"] = (
                    f"pack of {len(covered)} volumes beyond the work's {volume_count}"
                )
                release["_review_resolution"] = "rejected"
                continue
            if not wanted:
                release["_review"] = (
                    f"pack of {len(covered)} volumes, none of them missing"
                )
                release["_review_resolution"] = "rejected"
                continue
            if useful < 0.5:
                # Inside the work, but mostly books already owned: whether that
                # is worth the download is a judgement, so it is offered.
                _ask_or_decide(
                    release,
                    f"pack of {len(covered)} volumes, "
                    f"{len(covered & wanted)} of them missing",
                )
                continue
        else:
            hinted = str(kind.volume or release.get("volume") or "")
            if hinted != str(volume):
                # A one-book work is released without a volume number
                # ("Grass (2019) (Drawn&Quarterly) (Digital)"): no hint is fine
                # for volume 1, any other number is not.
                if not (
                    single_volume
                    and volume == 1
                    and not hinted
                    and not release.get("chapter")
                ):
                    continue
        if release_match_score(
            title, str(release.get("title") or "")
        ) < 100 or not title_matches_release(
            title, str(release.get("title") or ""), creators
        ):
            continue
        identity_review = _identity_review_reason(
            title, str(release.get("title") or ""), publishers, creators
        )
        if corroborated and identity_review != "release names an unverified publisher":
            identity_review = None
        if identity_review:
            _ask_or_decide(release, identity_review)
            continue
        score = max(
            (rank_release(release, name) for name in publishers),
            default=rank_release(release, None),
        )
        if score[0] < 0:
            continue
        # A single book beats a pack that happens to contain it.
        candidates.append(((0 if kind.kind == "pack" else 1, *score), release))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def plan_releases(
    results: list[dict[str, Any]],
    *,
    title: str,
    missing_volumes: list[int],
    publisher: str | list[str] | None,
    single_volume: bool = False,
    volume_count: int | None = None,
    creators: tuple[str, ...] | list[str] = (),
) -> list[tuple[dict[str, Any], tuple[int, ...]]]:
    """Greedily cover the backlog with the fewest safe unique releases."""

    wanted = set(missing_volumes)
    publishers = [publisher] if isinstance(publisher, str) else list(publisher or [])
    family_corroborated = _family_corroborates(title, results, volume_count, publishers)
    candidates: list[
        tuple[tuple[int, int, int, int, int, int], dict[str, Any], set[int]]
    ] = []
    for release in results:
        covered = {
            volume
            for volume in wanted
            if pick_release(
                [release],
                title=title,
                volume=volume,
                publisher=publisher,
                single_volume=single_volume,
                volume_count=volume_count,
                wanted_volumes=wanted,
                family_corroborated=family_corroborated,
                creators=creators,
            )
            is not None
        }
        if not covered:
            continue
        quality = max(
            (rank_release(release, name) for name in publishers),
            default=rank_release(release, None),
        )
        size_per_volume = int(release.get("size_bytes") or 0) // len(covered)
        score = (
            len(covered),
            quality[0],
            quality[1],
            quality[2],
            -size_per_volume,
            quality[3],
        )
        candidates.append((score, release, covered))

    selected: list[tuple[dict[str, Any], tuple[int, ...]]] = []
    uncovered = set(wanted)
    used: set[str] = set()
    while uncovered and len(selected) < MAX_GRABS_PER_CYCLE:
        available = [
            (score, release, covered & uncovered)
            for score, release, covered in candidates
            if str(release.get("id")) not in used and covered & uncovered
        ]
        if not available:
            break
        score, release, newly_covered = max(
            available,
            key=lambda item: (
                len(item[2]),
                *item[0][1:],
                -min(item[2]),
                str(item[1].get("id") or ""),
            ),
        )
        del score
        coverage = tuple(sorted(newly_covered))
        selected.append((release, coverage))
        used.add(str(release.get("id")))
        uncovered.difference_update(newly_covered)
    return selected


def offered_volumes(
    results: list[dict[str, Any]],
    *,
    title: str,
    publisher: str | list[str] | None = None,
    single_volume: bool = False,
    volume_count: int | None = None,
    creators: tuple[str, ...] | list[str] = (),
) -> list[dict[str, Any]]:
    """Every book the indexers offer for this work: one row per volume."""

    rows: list[dict[str, Any]] = []
    publishers = [publisher] if isinstance(publisher, str) else list(publisher or [])
    corroborated = _family_corroborates(title, results, volume_count, publishers)
    for release in results:
        name = str(release.get("title") or "")
        if release_match_score(title, name) < 100 or not title_matches_release(
            title, name, creators
        ):
            continue
        identity_review = _identity_review_reason(title, name, publishers, creators)
        if identity_review and not (
            corroborated
            and _title_leads(title, name)
            and identity_review != "release names an unverified publisher"
        ):
            continue
        kind = classify_release(title=name, size_bytes=release.get("size_bytes"))
        if kind.kind == "rejected" or kind.kind == "chapter":
            continue
        if not (
            str(release.get("protocol") or "torrent") in {"usenet", "http"}
            or int(release.get("seeders") or 0) > 0
        ):
            continue
        volumes: list[int] = []
        if kind.kind == "pack":
            volumes = list(kind.volumes)
        elif kind.volume:
            try:
                volumes = [int(float(kind.volume))]
            except ValueError:
                volumes = []
        elif single_volume:
            volumes = [1]
        for number in volumes:
            rows.append(
                {
                    "volume": number,
                    "protocol": release.get("protocol") or "torrent",
                    "title": name,
                    "size_bytes": release.get("size_bytes") or 0,
                }
            )
    return rows


def _prune_conflicting_stored_offers(
    torrents: Any,
    manga: dict[str, Any],
    publisher: str | list[str] | None,
) -> None:
    """Discard cached books that explicitly name a different publisher."""

    database = getattr(torrents, "database", None)
    listing = getattr(database, "list_indexer_offers", None)
    deleting = getattr(database, "delete_indexer_offers", None)
    if listing is None or deleting is None:
        return
    manga_id = str(manga["id"])
    stored = listing(manga_id)
    if not stored:
        return
    publishers = [publisher] if isinstance(publisher, str) else list(publisher or [])
    invalid = [
        row
        for row in stored
        if _publisher_conflict_reason(str(row["title"]), publishers)
    ]
    if invalid:
        deleting(manga_id, invalid)


async def probe_offers(
    torrents: Any,
    *,
    manga: dict[str, Any],
    publisher: str | list[str] | None,
    single_volume: bool = False,
    volume_count: int | None = None,
    creators: tuple[str, ...] | list[str] = (),
) -> int:
    """Record what the indexers offer for a work without grabbing anything
    (coverage for the unit choice of ended works)."""

    _prune_conflicting_stored_offers(torrents, manga, publisher)
    pooled: dict[str, dict[str, Any]] = {}
    direct_available = bool(getattr(torrents, "direct_available", False))
    indexer_available = bool(
        getattr(getattr(torrents, "prowlarr", None), "enabled", False)
        and getattr(getattr(torrents, "prowlarr", None), "configured", False)
    )
    if direct_available:
        try:
            result = await torrents.search(
                str(manga["id"]),
                str(manga["title"]),
                limit=75,
                sources=(DIRECT_PROVIDER,),
            )
            for release in result.get("results") or []:
                pooled.setdefault(str(release.get("id")), release)
        except Exception:  # noqa: BLE001 - the indexers may still offer books
            pass
    search_sources = {"sources": ("prowlarr",)} if direct_available else {}
    for query in (
        series_queries(str(manga["title"]), publisher)
        if not direct_available or indexer_available
        else []
    ):
        try:
            result = await torrents.search(
                str(manga["id"]), query, limit=75, **search_sources
            )
        except Exception:  # noqa: BLE001 - next cycle
            continue
        for release in result.get("results") or []:
            pooled.setdefault(str(release.get("id")), release)
    offers = offered_volumes(
        list(pooled.values()),
        title=str(manga["title"]),
        publisher=publisher,
        single_volume=single_volume,
        volume_count=volume_count,
        creators=creators,
    )
    recorder = getattr(
        getattr(torrents, "database", None), "record_indexer_offers", None
    )
    if recorder is not None and offers:
        recorder(str(manga["id"]), offers)
    return len({row["volume"] for row in offers})


async def hunt_volumes(
    torrents: Any,
    *,
    manga: dict[str, Any],
    missing_volumes: list[int],
    publisher: str | list[str] | None,
    single_volume: bool = False,
    volume_count: int | None = None,
    creators: tuple[str, ...] | list[str] = (),
) -> dict[str, Any]:
    """Grab one release per missing volume when an unambiguous one exists."""

    _prune_conflicting_stored_offers(torrents, manga, publisher)
    grabbed: list[dict[str, Any]] = []
    errors: list[str] = []
    wanted = sorted(set(missing_volumes))
    if not wanted:
        return {"grabbed": [], "errors": [], "searched_volumes": []}
    pooled: dict[str, dict[str, Any]] = {}
    direct_available = bool(getattr(torrents, "direct_available", False))
    indexer_available = bool(
        getattr(getattr(torrents, "prowlarr", None), "enabled", False)
        and getattr(getattr(torrents, "prowlarr", None), "configured", False)
    )
    if direct_available:
        try:
            result = await torrents.search(
                str(manga["id"]),
                str(manga["title"]),
                limit=75,
                _include_download_ref=True,
                sources=(DIRECT_PROVIDER,),
            )
            errors.extend(str(error)[:200] for error in result.get("errors") or [])
            for release in result.get("results") or []:
                pooled.setdefault(str(release.get("id")), release)
        except Exception as exc:  # noqa: BLE001 - the indexers may still offer books
            errors.append(f"Internet Archive: {type(exc).__name__}: {exc}"[:200])
    search_sources = {"sources": ("prowlarr",)} if direct_available else {}
    for query in (
        series_queries(str(manga["title"]), publisher)
        if not direct_available or indexer_available
        else []
    ):
        try:
            # A release the rules send to review keeps its download_ref: a
            # human accepting it later must be able to grab the same result,
            # not one whose reference was stripped for public API callers.
            result = await torrents.search(
                str(manga["id"]),
                query,
                limit=75,
                _include_download_ref=True,
                **search_sources,
            )
        except Exception as exc:  # noqa: BLE001 - next query / next cycle
            errors.append(f"{query!r}: {type(exc).__name__}: {exc}"[:200])
            continue
        errors.extend(str(error)[:200] for error in result.get("errors") or [])
        for release in result.get("results") or []:
            pooled.setdefault(str(release.get("id")), release)
        planned = plan_releases(
            list(pooled.values()),
            title=str(manga["title"]),
            missing_volumes=wanted,
            publisher=publisher,
            single_volume=single_volume,
            volume_count=volume_count,
            creators=creators,
        )
        if set().union(*(set(coverage) for _release, coverage in planned)) == set(
            wanted
        ):
            break  # every wanted book already has a candidate
    # Books the series queries did not surface: ask for them by number.
    planned = plan_releases(
        list(pooled.values()),
        title=str(manga["title"]),
        missing_volumes=wanted,
        publisher=publisher,
        single_volume=single_volume,
        volume_count=volume_count,
        creators=creators,
    )
    covered_now = set().union(*(set(coverage) for _release, coverage in planned))
    for number in (
        [v for v in wanted if v not in covered_now][:MAX_VOLUME_QUERIES_PER_PASS]
        if not direct_available or indexer_available
        else []
    ):
        for query in book_queries(str(manga["title"]), number):
            try:
                result = await torrents.search(
                    str(manga["id"]),
                    query,
                    limit=40,
                    _include_download_ref=True,
                    **search_sources,
                )
            except Exception as exc:  # noqa: BLE001 - next query / next cycle
                errors.append(f"{query!r}: {type(exc).__name__}: {exc}"[:200])
                continue
            found = False
            for release in result.get("results") or []:
                pooled.setdefault(str(release.get("id")), release)
                kind = classify_release(
                    title=str(release.get("title") or ""),
                    size_bytes=release.get("size_bytes"),
                )
                if (
                    kind.kind == "volume"
                    and str(kind.volume or "").isdigit()
                    and int(kind.volume) == number
                ):
                    found = True
            if found:
                break
    offers = offered_volumes(
        list(pooled.values()),
        title=str(manga["title"]),
        publisher=publisher,
        single_volume=single_volume,
        volume_count=volume_count,
        creators=creators,
    )
    recorder = getattr(
        getattr(torrents, "database", None), "record_indexer_offers", None
    )
    if recorder is not None and offers:
        recorder(str(manga["id"]), offers)
    reviewer = getattr(getattr(torrents, "database", None), "add_match_review", None)
    if reviewer is not None:
        for release in pooled.values():
            note = release.get("_review")
            if note:
                reviewer(
                    str(manga["id"]),
                    kind="release",
                    provider=str(release.get("provider") or "prowlarr"),
                    candidate_id=str(release.get("id")),
                    title=str(release.get("title") or ""),
                    source_name=release.get("indexer"),
                    source_url=release.get("source_url"),
                    confidence=0.6,
                    reason=note,
                    payload={
                        key: value
                        for key, value in release.items()
                        if key not in {"_review", "_review_resolution"}
                    },
                    resolution=release.get("_review_resolution"),
                )
    planned = plan_releases(
        list(pooled.values()),
        title=str(manga["title"]),
        missing_volumes=wanted,
        publisher=publisher,
        single_volume=single_volume,
        volume_count=volume_count,
        creators=creators,
    )
    for chosen, coverage in planned:
        try:
            job = await torrents.grab(
                str(manga["id"]), str(chosen["provider"]), str(chosen["id"])
            )
            # The hunt knows which book this is even when the release name
            # carries no number (one-book works): tell the importer.
            setter = getattr(
                getattr(torrents, "database", None), "update_torrent_download", None
            )
            if setter is not None and job.get("id") is not None and len(coverage) == 1:
                setter(job["id"], volume_hint=str(coverage[0]))
            grabbed.append(
                {
                    "volume": coverage[0] if len(coverage) == 1 else None,
                    "volumes": list(coverage),
                    "title": chosen.get("title"),
                    "protocol": chosen.get("protocol"),
                    "job_id": job.get("id"),
                }
            )
        except Exception as exc:  # noqa: BLE001
            label = (
                f"v{coverage[0]}"
                if len(coverage) == 1
                else f"v{coverage[0]}-{coverage[-1]}"
            )
            errors.append(f"{label}: {type(exc).__name__}: {exc}"[:200])
    if grabbed:
        logger.info(
            "Volume hunt for %s grabbed %s",
            manga.get("title"),
            [item["volumes"] for item in grabbed],
        )
    return {
        "grabbed": grabbed,
        "errors": errors,
        "searched_volumes": wanted,
        # add_match_review records near-misses as already refused by the
        # deterministic rules. They remain visible in history, but there is
        # no open review for the operator to resolve.
        "needs_review": False,
    }


__all__ = [
    "hunt_volumes",
    "offered_volumes",
    "pick_release",
    "plan_releases",
    "probe_offers",
    "rank_release",
    "series_queries",
    "volume_queries",
]
