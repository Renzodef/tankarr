from __future__ import annotations

import asyncio
import logging
import re
import threading
import unicodedata
import uuid
from collections import Counter
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import Any

from tankarr.catalogue import catalogue_card
from tankarr.config import Settings
from tankarr.database import Database
from tankarr.metadata.base import (
    canonical_person_name,
    creator_name_similarity,
    normalized_person,
)

logger = logging.getLogger(__name__)

_NON_PERSON_CREDITS = frozenset(
    {
        "anthology",
        "various",
        "various artists",
        "va",
        "aa vv",
        "collective",
        "anonymous",
        "unknown",
        "unknown author",
        "author unknown",
        "n a",
        "staff",
    }
)


@lru_cache(maxsize=4096)
def _person_tokens(value: str) -> tuple[str, ...]:
    return normalized_person(value)


def author_key(value: object) -> str:
    raw = str(value or "")
    key = " ".join(_person_tokens(raw))
    qualifiers = _identity_qualifiers(raw)
    if not qualifiers:
        return key
    # Manga catalogues use parenthetical text to disambiguate distinct pen
    # names which otherwise have the same romanization (for example ``Ayuko``
    # and ``Ayuko (あゆこ)``). ``normalized_person`` intentionally discards
    # non-Latin text, so retain the provider's explicit qualifier in the
    # durable identity key.
    return f"{key} [{' | '.join(qualifiers)}]"


def _identity_qualifiers(value: object) -> tuple[str, ...]:
    raw = unicodedata.normalize("NFKC", str(value or ""))
    return tuple(
        qualifier
        for match in re.finditer(r"\(([^()]*)\)", raw)
        if (qualifier := " ".join(match.group(1).casefold().split()))
    )


def _creator_identity_similarity(left: object, right: object) -> float:
    """Compare names without erasing an explicit identity qualifier."""

    if _identity_qualifiers(left) != _identity_qualifiers(right):
        return 0.0
    return creator_name_similarity(left, right)


def _is_person(value: object) -> bool:
    key = author_key(value)
    return bool(key) and key not in _NON_PERSON_CREDITS


def _credit_names(record: dict[str, Any]) -> list[str]:
    values = [
        *(record.get("authors") or []),
        *(
            creator.get("name")
            for creator in record.get("creators") or []
            if isinstance(creator, dict)
        ),
    ]
    result: list[str] = []
    for value in values:
        name = " ".join(str(value or "").split())
        if name and _is_person(name) and name not in result:
            result.append(name)
    return result


def _romanization_token(value: str) -> str:
    # Manga catalogues use all of Kondo, Kondou and Kondoh for the same long
    # vowel. Keep this deliberately narrower than general fuzzy matching.
    folded = re.sub(r"oh$", "o", value)
    folded = re.sub(r"uu", "u", re.sub(r"(?:ou|oo)", "o", folded))
    return re.sub(r"([aeiou])\1", r"\1", folded)


def _romanization_identity(left: object, right: object) -> str | None:
    left_tokens = _person_tokens(str(left or ""))
    right_tokens = _person_tokens(str(right or ""))
    if len(left_tokens) < 2 or len(left_tokens) != len(right_tokens):
        return None
    if not set(left_tokens) & set(right_tokens):
        return None
    left_folded = tuple(sorted(_romanization_token(token) for token in left_tokens))
    right_folded = tuple(sorted(_romanization_token(token) for token in right_tokens))
    if left_folded != right_folded or left_tokens == right_tokens:
        return None
    return " ".join(left_folded)


def _credits(record: dict[str, Any]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    creators = [
        creator
        for creator in record.get("creators") or []
        if isinstance(creator, dict) and _is_person(creator.get("name"))
    ]
    if not creators:
        creators = [
            {"name": name, "role": "writer"}
            for name in record.get("authors") or []
            if _is_person(name)
        ]
    for creator in creators:
        raw_name = " ".join(str(creator.get("name") or "").split())
        key = author_key(raw_name)
        if not key:
            continue
        entry = grouped.setdefault(
            key,
            {
                "name": canonical_person_name(raw_name),
                "aliases": set(),
                "roles": set(),
            },
        )
        entry["aliases"].add(raw_name)
        role = str(creator.get("role") or "writer").strip().casefold()
        if role:
            entry["roles"].add(role)
    return [
        {
            "name": value["name"],
            "aliases": sorted(value["aliases"]),
            "roles": sorted(value["roles"]),
        }
        for value in grouped.values()
    ]


class AuthorRegistry:
    """Persist MangaBaka work credits and reconcile their string-only identities."""

    def __init__(self, settings: Settings, database: Database, catalogue: Any):
        self.settings = settings
        self.database = database
        self.catalogue = catalogue
        self._database_lock = threading.RLock()
        self._sync_lock = threading.Lock()
        self._refresh_lock = asyncio.Lock()

    def register_catalogue_series(
        self,
        manga_id: str,
        record: dict[str, Any],
        *,
        reconcile: bool = True,
    ) -> list[str]:
        catalogue_id = str(record.get("external_id") or "").strip()
        if not catalogue_id:
            return []
        card = catalogue_card(record)
        links: list[dict[str, Any]] = []
        with self._database_lock:
            for credit in _credits(record):
                name = str(credit["name"])
                key = author_key(name)
                candidates = self.database.find_authors_by_alias(key)
                if candidates:
                    target = min(
                        candidates,
                        key=lambda item: (str(item["created_at"]), str(item["id"])),
                    )
                    for duplicate in candidates:
                        if duplicate["id"] == target["id"]:
                            continue
                        self.database.merge_authors(
                            str(target["id"]),
                            str(duplicate["id"]),
                            reason="same MangaBaka staff query",
                            evidence={"normalized_name": key},
                        )
                else:
                    target = self.database.create_author(
                        "author-" + uuid.uuid4().hex, name
                    )
                author_id = str(target["id"])
                for alias in credit["aliases"]:
                    self.database.add_author_alias(
                        author_id,
                        normalized_name=author_key(alias),
                        name=canonical_person_name(alias),
                        source_url="",
                    )
                self.database.seed_author_work(author_id, card)
                links.append(
                    {
                        "author_id": author_id,
                        "credited_names": credit["aliases"],
                        "roles": credit["roles"],
                    }
                )
            self.database.replace_manga_authors(manga_id, links)
        author_ids = sorted({str(link["author_id"]) for link in links})
        if reconcile:
            author_ids = sorted(
                {self.reconcile_author(author_id) for author_id in author_ids}
            )
        return author_ids

    def sync_library(self) -> dict[str, int]:
        """Backfill author entities only from persisted MangaBaka work records."""

        with self._sync_lock:
            return self._sync_library()

    def _sync_library(self) -> dict[str, int]:

        checked = linked = 0
        for manga in self.database.list_manga():
            manga_id = str(manga["id"])
            records = self.database.list_metadata_source_records(
                manga_id, entity_type="work"
            )
            source = next(
                (
                    item.get("data")
                    for item in records
                    if item.get("source") == "mangabaka"
                    and isinstance(item.get("data"), dict)
                ),
                None,
            )
            if (
                source is None
                and manga.get("provider") == "catalogue"
                and str(manga.get("source_id") or "").strip()
            ):
                # Older catalogue rows may predate metadata snapshots. Their
                # source fields were written from MangaBaka at Add time; use
                # those, never a later manual title/creator override, to seed
                # the persistent author identity before the remote refresh.
                source = {
                    "external_id": str(manga["source_id"]),
                    "title": str(manga.get("source_title") or manga["title"]),
                    "localized_titles": {},
                    "alternate_titles": list(manga.get("alternate_titles") or []),
                    "description": str(manga.get("description") or ""),
                    "cover": (
                        {"url": str(manga["cover_url"])}
                        if manga.get("cover_url")
                        else None
                    ),
                    "authors": list(manga.get("source_authors") or []),
                    "year": manga.get("year"),
                    "status": manga.get("status"),
                    "original_language": manga.get("original_language"),
                }
            if source is None:
                continue
            checked += 1
            credits = _credits(source)
            existing = self.database.list_manga_authors(manga_id)
            expected_names = {
                author_key(alias) for credit in credits for alias in credit["aliases"]
            }
            existing_names = {
                author_key(alias)
                for link in existing
                for alias in link["credited_names"]
            }
            expected_roles = {role for credit in credits for role in credit["roles"]}
            existing_roles = {role for link in existing for role in link["roles"]}
            if expected_names == existing_names and expected_roles == existing_roles:
                continue
            linked += len(
                self.register_catalogue_series(manga_id, source, reconcile=False)
            )
        if linked:
            self.reconcile_all()
        return {"checked": checked, "linked": linked}

    def page(self, author_id: str) -> dict[str, Any] | None:
        author = self.database.get_author(author_id, include_works=True)
        if author is None:
            return None
        owners = self.database.catalogue_identity_owners()
        works = []
        for work in sorted(
            author.get("works") or [],
            key=lambda item: (item.get("year") or 9999, item.get("title") or ""),
        ):
            existing = owners.get(str(work.get("external_id") or ""))
            works.append(
                {
                    **work,
                    "in_library": existing is not None,
                    "library_manga_id": existing,
                }
            )
        aliases = [str(alias["name"]) for alias in author.get("aliases") or []]
        return {
            "id": str(author["id"]),
            "author": str(author["display_name"]),
            "aliases": aliases,
            "source": "mangabaka",
            # MangaBaka currently exposes creator names, not stable person IDs.
            # A staff search URL is a query, not an author identity.
            "source_url": None,
            "source_urls": [],
            "source_pages": [],
            "works": works,
            "work_count": len(works),
            "library_manga_count": int(author.get("library_manga_count") or 0),
            "last_refreshed_at": author.get("last_refreshed_at"),
            "next_refresh_at": author.get("next_refresh_at"),
            "last_refresh_error": author.get("last_refresh_error"),
            "merged_from": author.get("merged_from") or [],
            "redirected_from": author.get("redirected_from"),
        }

    async def refresh(self, author_id: str) -> dict[str, Any]:
        async with self._refresh_lock:
            author = self.database.get_author(author_id, include_works=True)
            if author is None:
                raise KeyError(author_id)
            canonical_id = str(author["id"])
            aliases = [
                str(alias["name"])
                for alias in author.get("aliases") or []
                if str(alias.get("name") or "").strip()
            ] or [str(author["display_name"])]
            try:
                records: dict[str, dict[str, Any]] = {}
                for alias in aliases:
                    for record in await self.catalogue.search_series_by_author(
                        alias, limit=1000
                    ):
                        external_id = str(record.get("external_id") or "").strip()
                        if not external_id:
                            continue
                        if (
                            max(
                                (
                                    _creator_identity_similarity(alias, name)
                                    for name in _credit_names(record)
                                ),
                                default=0.0,
                            )
                            < 0.90
                        ):
                            continue
                        records[external_id] = record
                if not records:
                    raise RuntimeError(
                        "MangaBaka returned no works for a credited library author"
                    )
                cards = [catalogue_card(record) for record in records.values()]
                observed = Counter[str]()
                for record in records.values():
                    for name in _credit_names(record):
                        if (
                            max(
                                (
                                    _creator_identity_similarity(name, alias)
                                    for alias in aliases
                                ),
                                default=0.0,
                            )
                            >= 0.90
                        ):
                            observed[canonical_person_name(name)] += 1
                refreshed_at = datetime.now(UTC)
                next_refresh_at = refreshed_at + timedelta(
                    hours=self.settings.author_refresh_interval_hours
                )
                self.database.replace_author_works(
                    canonical_id,
                    cards,
                    refreshed_at=refreshed_at.isoformat(),
                    next_refresh_at=next_refresh_at.isoformat(),
                )
                # Reconcile before learning spellings from returned co-credits.
                # Otherwise two similarly named people credited on the same work
                # could acquire each other's exact alias and bypass the shared
                # credit conflict check on the next pass.
                canonical_id = self.reconcile_author(canonical_id)
                accepted_observed: Counter[str] = Counter()
                for name in observed:
                    owners = self.database.find_authors_by_alias(author_key(name))
                    if any(str(owner["id"]) != canonical_id for owner in owners):
                        continue
                    self.database.add_author_alias(
                        canonical_id,
                        normalized_name=author_key(name),
                        name=name,
                        source_url="",
                    )
                    accepted_observed[name] = observed[name]
                if accepted_observed:
                    preferred = sorted(
                        accepted_observed,
                        key=lambda name: (
                            -accepted_observed[name],
                            name != author["display_name"],
                            name.casefold(),
                        ),
                    )[0]
                    self.database.update_author_display_name(canonical_id, preferred)
                page = self.page(canonical_id)
                if page is None:
                    raise RuntimeError("Refreshed author disappeared")
                return page
            except Exception as exc:
                current = self.database.get_author(canonical_id, include_works=False)
                failures = int((current or {}).get("refresh_failures") or 0) + 1
                retry_at = datetime.now(UTC) + timedelta(
                    hours=min(2 ** max(0, failures - 1), 24)
                )
                self.database.record_author_refresh_failure(
                    str((current or {}).get("id") or canonical_id),
                    error=f"{type(exc).__name__}: {exc}",
                    next_refresh_at=retry_at.isoformat(),
                )
                raise

    def reconcile_author(self, author_id: str) -> str:
        """Merge aliases only with shared MangaBaka work/credit evidence."""

        with self._database_lock:
            while True:
                current = self.database.get_author(author_id, include_works=True)
                if current is None:
                    raise KeyError(author_id)
                merged = False
                for candidate in self.database.list_author_identities():
                    if candidate["id"] == current["id"]:
                        continue
                    evidence = self._merge_evidence(current, candidate)
                    if evidence is None:
                        continue
                    target, source = sorted(
                        (current, candidate),
                        key=lambda item: (str(item["created_at"]), str(item["id"])),
                    )
                    reason = str(evidence.pop("reason"))
                    self.database.merge_authors(
                        str(target["id"]),
                        str(source["id"]),
                        reason=reason,
                        evidence=evidence,
                    )
                    author_id = str(target["id"])
                    merged = True
                    break
                if not merged:
                    return str(current["id"])

    def reconcile_all(self) -> int:
        """Apply evidence-backed local merges without making remote calls."""

        with self._database_lock:
            initial_count = len(self.database.list_author_identities())
            while True:
                active = self.database.list_author_identities()
                merged = False
                for index, left in enumerate(active):
                    for right in active[index + 1 :]:
                        evidence = self._merge_evidence(left, right)
                        if evidence is None:
                            continue
                        target, source = sorted(
                            (left, right),
                            key=lambda item: (
                                str(item["created_at"]),
                                str(item["id"]),
                            ),
                        )
                        reason = str(evidence.pop("reason"))
                        self.database.merge_authors(
                            str(target["id"]),
                            str(source["id"]),
                            reason=reason,
                            evidence=evidence,
                        )
                        merged = True
                        break
                    if merged:
                        break
                if not merged:
                    return initial_count - len(active)

    def audit_duplicates(self) -> list[dict[str, Any]]:
        authors = self.database.list_author_identities()
        result: list[dict[str, Any]] = []
        for index, left in enumerate(authors):
            for right in authors[index + 1 :]:
                pair = self._closest_aliases(left, right)
                if pair is None or pair[2] < 0.90:
                    continue
                shared = sorted(set(left["work_ids"]) & set(right["work_ids"]))
                result.append(
                    {
                        "left": {"id": left["id"], "name": left["display_name"]},
                        "right": {"id": right["id"], "name": right["display_name"]},
                        "similarity": round(pair[2], 3),
                        "shared_works": shared,
                        "reason": "conflicting shared credit"
                        if shared
                        else "no shared MangaBaka work evidence",
                    }
                )
        return result

    def _merge_evidence(
        self, left: dict[str, Any], right: dict[str, Any]
    ) -> dict[str, Any] | None:
        pair = self._closest_aliases(left, right)
        if pair is None:
            return None
        left_name, right_name, similarity = pair
        left_key = author_key(left_name)
        right_key = author_key(right_name)
        if left_key == right_key:
            return {
                "reason": "same MangaBaka staff query",
                "aliases": [left_name, right_name],
                "normalized_name": left_key,
            }
        shared = sorted(set(left["work_ids"]) & set(right["work_ids"]))
        cards: dict[str, dict[str, Any]] = {}
        if shared:
            full_left = self.database.get_author(str(left["id"]), include_works=True)
            if full_left is None:
                return None
            cards = {
                str(card.get("external_id") or ""): card
                for card in full_left.get("works") or []
            }
            for work_id in shared:
                names = _credit_names(cards.get(work_id) or {})
                exact_left = {name for name in names if author_key(name) == left_key}
                exact_right = {name for name in names if author_key(name) == right_key}
                if exact_left and exact_right and exact_left.isdisjoint(exact_right):
                    return None
        romanization = _romanization_identity(left_name, right_name)
        if romanization is not None:
            return {
                "reason": "deterministic MangaBaka romanization variants",
                "aliases": [left_name, right_name],
                "romanization_identity": romanization,
                "shared_works": shared,
            }
        if similarity < 0.90 or not shared:
            return None
        corroborated = False
        for work_id in shared:
            names = _credit_names(cards.get(work_id) or {})
            if any(
                _creator_identity_similarity(name, left_name) >= 0.90
                and _creator_identity_similarity(name, right_name) >= 0.90
                for name in names
            ):
                corroborated = True
        if not corroborated:
            return None
        return {
            "reason": "matching aliases share MangaBaka works and one credit",
            "aliases": [left_name, right_name],
            "similarity": round(similarity, 3),
            "shared_works": shared,
        }

    @staticmethod
    def _closest_aliases(
        left: dict[str, Any], right: dict[str, Any]
    ) -> tuple[str, str, float] | None:
        best: tuple[str, str, float] | None = None
        left_aliases = [str(alias["name"]) for alias in left.get("aliases") or []] or [
            str(left["display_name"])
        ]
        right_aliases = [
            str(alias["name"]) for alias in right.get("aliases") or []
        ] or [str(right["display_name"])]
        for left_name in left_aliases:
            for right_name in right_aliases:
                if not AuthorRegistry._name_shapes_could_match(left_name, right_name):
                    continue
                score = _creator_identity_similarity(left_name, right_name)
                if best is None or score > best[2]:
                    best = (left_name, right_name, score)
        return best

    @staticmethod
    def _name_shapes_could_match(left: str, right: str) -> bool:
        """Cheap necessary condition for a >= .90 creator-name match."""

        left_tokens = _person_tokens(left)
        right_tokens = _person_tokens(right)
        if not left_tokens or not right_tokens:
            return False
        if left_tokens == right_tokens:
            return True
        shorter, longer = sorted(
            (left_tokens, right_tokens), key=lambda tokens: len(tokens)
        )
        if len(shorter) < 2:
            return False
        remaining = list(longer)
        for token in shorter:
            match = next(
                (
                    index
                    for index, candidate in enumerate(remaining)
                    if token[0] == candidate[0]
                ),
                None,
            )
            if match is None:
                return False
            remaining.pop(match)
        return True


__all__ = ["AuthorRegistry", "author_key"]
