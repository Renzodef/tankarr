from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import shutil
import uuid
import zipfile
from collections import defaultdict
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from threading import Lock
from time import monotonic
from typing import Any

import httpx

from tankarr import page_quality
from tankarr.archive import (
    IMAGE_SUFFIXES,
    ImportDurabilityError,
    count_archive_pages,
    fsync_directory,
    install_atomically,
    package_cbz_validated,
    publish_without_overwrite,
    read_comic_info,
    replace_comic_info,
    sha256,
    validate_cbz,
)
from tankarr.artwork_urls import series_artwork_url
from tankarr.catalogue import (
    CATALOGUE_PROVIDER,
    NO_REMOTE_PROVIDERS,
    catalogue_card,
    manga_from_record,
)
from tankarr.chapter_map import integer_chapter_total
from tankarr.chapter_mapping import (
    build_chapter_index,
    canonical_number,
    effective_edition_book_count,
    logical_release_key,
    volume_scoped_numbering,
)
from tankarr.comicinfo import build_comic_info
from tankarr.config import Settings
from tankarr.database import (
    Database,
    ReleaseBlocked,
    local_release_identity,
    local_series_identity,
)
from tankarr.http import async_client
from tankarr.komga import KomgaClient
from tankarr.metadata.correlations import CORRELATION_LABELS, correlation_url
from tankarr.metadata.publications import publication_metadata_key
from tankarr.monitoring import (
    BACKLOG_MONITOR_MODES,
    FUTURE_MONITOR_MODES,
    TERMINAL_PUBLICATION_STATUSES,
    evaluate_future_monitoring,
    normalize_monitor_mode,
    terminal_monitor_mode,
)
from tankarr.naming import (
    LIBRARY_NAMING_FORMAT,
    LIBRARY_NAMING_VERSION,
    chapter_filename,
    final_library_path,
)
from tankarr.notify import Notifier
from tankarr.official_evidence import (
    NAVER_WEBTOON_HOST,
    fetch_naver_webtoon_items,
    naver_webtoon_id,
)
from tankarr.official_numbering import (
    official_chapter_frontier,
    surplus_downloads,
)
from tankarr.providers.base import (
    Provider,
    ProviderRequestError,
    ProviderUnavailableError,
)
from tankarr.read_model_cache import cache_now, next_publication
from tankarr.series_summary import publication_summary
from tankarr.series_unit import normalize_series_unit, resolve_series_unit
from tankarr.source_circuit import source_gate_key
from tankarr.source_numbering import is_special_title, is_unpublished
from tankarr.source_ranking import (
    beyond_frontier,
    official_frontier,
    official_hosts,
    official_source_roles,
    release_source_keys,
)
from tankarr.torrent_sources import EXTERNAL_IMPORT_PROVIDERS, TORRENT_IMPORT_PROVIDERS
from tankarr.unit_reconciliation import (
    misclassified_explicit_chapters,
    release_source,
    volume_numbered_providers,
)
from tankarr.wanted_recovery import SERIES_SLOT

logger = logging.getLogger(__name__)
PRIMARY_SOURCE_FAILURE_NOTE = "tankarr:primary-source-request-failed"


class FutureMonitoringUnavailable(ValueError):
    pass


class UnsafeLibraryPath(ValueError):
    pass


class _FileReferenceSnapshot:
    """Validate a single guard's file claims with one listing per directory.

    Never retained across mutations: directory and symlink evidence is fresh
    for every publication/deletion, without re-statting every ancestor of
    every unrelated book for each incoming chapter.
    """

    def __init__(self, root: Path, legacy_roots: Iterable[Path]):
        self.root = root
        self.root_prefix = str(root).rstrip(os.sep) + os.sep
        self.legacy_prefixes = tuple(
            str(p).rstrip(os.sep) + os.sep for p in legacy_roots
        )
        self.directories: dict[Path, dict[str, os.DirEntry]] = {}
        self.normalized: dict[str, Path] = {}

    def entries(self, directory: Path) -> dict[str, os.DirEntry]:
        if directory in self.directories:
            return self.directories[directory]
        if directory != self.root:
            parent = self.entries(directory.parent)
            entry = parent.get(directory.name)
            if entry is not None:
                if entry.is_symlink():
                    raise UnsafeLibraryPath(
                        f"Refusing symlinked library path component: {directory}"
                    )
                if not entry.is_dir(follow_symlinks=False):
                    raise UnsafeLibraryPath(
                        f"Refusing non-directory library path: {directory}"
                    )
        try:
            with os.scandir(directory) as entries:
                result = {entry.name: entry for entry in entries}
        except FileNotFoundError:
            result = {}
        self.directories[directory] = result
        return result

    def normalize(self, raw: str | Path) -> Path:
        key = os.fspath(raw)
        if key in self.normalized:
            return self.normalized[key]
        path = os.path.abspath(raw)
        if not path.startswith(self.root_prefix):
            for legacy in self.legacy_prefixes:
                if path.startswith(legacy):
                    path = self.root_prefix + path[len(legacy) :]
                    break
        if not path.startswith(self.root_prefix):
            raise UnsafeLibraryPath(
                f"Refusing path outside configured library root: {raw}"
            )
        result = Path(path)
        self.normalized[key] = result
        return result

    def file(self, raw: str | Path, suffixes: Iterable[str] = (".cbz",)) -> Path:
        path = self.normalize(raw)
        if path.suffix.casefold() not in suffixes:
            raise UnsafeLibraryPath(f"Refusing non-library file path: {raw}")
        entry = self.entries(path.parent).get(path.name)
        if entry is not None:
            if entry.is_symlink():
                raise UnsafeLibraryPath(
                    f"Refusing symlinked library path component: {path}"
                )
            if not entry.is_file(follow_symlinks=False):
                raise UnsafeLibraryPath(f"Refusing non-file library path: {path}")
        return path

    def exists(self, path: Path) -> bool:
        return path.name in self.entries(path.parent)


class LibraryUnavailable(RuntimeError):
    pass


class StaleVolumeDeletionError(RuntimeError):
    pass


class StaleMangaDeletionError(RuntimeError):
    pass


OFFICIAL_EVIDENCE_REFRESH_SECONDS = 6 * 60 * 60


class RecoveryBlocked(RuntimeError):
    pass


class LocalImportConflict(RuntimeError):
    pass


def _chapter_int(release: dict[str, Any]) -> int | None:
    """The integer chapter a release or slot names, or None for specials."""

    raw = str(release.get("canonical_chapter") or release.get("chapter") or "").strip()
    if not raw:
        return None
    try:
        number = Decimal(raw)
    except (InvalidOperation, ValueError):
        return None
    if number != number.to_integral_value():
        return None
    return int(number)


class ExternalImportConflict(RuntimeError):
    pass


class SeriesRenameError(RuntimeError):
    pass


class DegradedPagesError(RuntimeError):
    """The source served far less page than this work's chapters carry.

    Raised before packaging, so nothing unreadable ever reaches the library.
    It is a failure of the *release*, not of the infrastructure, which is what
    puts the slot through the normal failover: the release is blocked and the
    next preferred source is queued for the same chapter.
    """


# The container mount is the one root every installation has recorded at some
# point; roots from an earlier layout of a specific installation come from
# TANKARR_LEGACY_LIBRARY_ROOTS, never from this file.
LEGACY_LIBRARY_ROOTS = (Path("/library"),)
LOCAL_CONTENT_FINGERPRINT_VERSION = b"tankarr-local-pages-v1\0"
READER_ARTWORK_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
MANAGED_ARTWORK_SUFFIXES = {*READER_ARTWORK_SUFFIXES, ".tbn"}


def _update_local_content_fingerprint(
    digest: Any, index: int, size: int, chunks: Any
) -> None:
    digest.update(f"{index}:{size}\0".encode())
    for chunk in chunks:
        digest.update(chunk)


def local_page_content_sha256(pages: list[Path]) -> str:
    """Hash ordered page payloads without archive timestamps or metadata."""

    digest = hashlib.sha256(LOCAL_CONTENT_FINGERPRINT_VERSION)
    for index, page in enumerate(pages, start=1):
        size = page.stat().st_size
        with page.open("rb") as handle:
            _update_local_content_fingerprint(
                digest,
                index,
                size,
                iter(lambda: handle.read(1024 * 1024), b""),
            )
    return digest.hexdigest()


@dataclass
class FileQuarantine:
    operation_id: str | None
    directory: Path | None
    manifest_path: Path | None
    moved: list[tuple[Path, Path]]
    missing: int
    source_parents: set[Path]
    disposition: str = "delete"
    retired_at: str | None = None


@dataclass
class LibraryRelocation:
    manga_id: str
    chapter_id: str
    recorded_library_path: str
    source: Path
    destination: Path
    digest: str
    job_path_updates: list[tuple[int, str, str]]
    adopt_existing: bool = False
    records_new_hash: bool = False


def catalogue_future_monitoring(manga: dict[str, Any]) -> tuple[bool, str]:
    """Catalogue works are always monitorable, like Sonarr series.

    An ended work is not frozen: a late special, an extra volume or a new
    edition still appears on the sources, and monitoring is what catches
    it. The status only lowers how often the work is refreshed.
    """

    status = str(manga.get("status") or "unknown").casefold()
    if status in TERMINAL_PUBLICATION_STATUSES:
        return (
            True,
            f"The catalogue marks this work as {status}; it is refreshed less often, and late releases are still picked up.",
        )
    return evaluate_future_monitoring(manga, [])


def _wanted_sort_key(item: dict[str, Any]) -> tuple[int, Decimal, Decimal]:
    """Numeric order for Wanted rows: volume then chapter, unparsable last."""

    def number(value: object) -> Decimal | None:
        try:
            return Decimal(str(value))
        except (InvalidOperation, ValueError):
            return None

    chapter = number(item.get("chapter"))
    volume = number(item.get("volume"))
    if chapter is None and volume is None:
        return (1, Decimal(0), Decimal(0))
    return (
        0,
        volume if volume is not None else Decimal(0),
        chapter if chapter is not None else Decimal(0),
    )


# MangaBaka is the identity of record for every work Tankarr manages.
IDENTITY_SOURCE_PRIORITY = ("mangabaka",)


def _unobtainable_volumes(
    attempts_by_slot: dict[str, list[dict[str, Any]]],
) -> set[int]:
    """Book slots the indexer hunt has asked for and could not take."""

    volumes: set[int] = set()
    for slot_key, attempts in attempts_by_slot.items():
        if not slot_key.startswith("volume:"):
            continue
        if any(
            str(item.get("channel")) == "indexer_book"
            and str(item.get("outcome")) in {"not_offered", "ambiguous", "error"}
            for item in attempts
        ):
            try:
                volumes.add(int(float(slot_key.split(":", 1)[1])))
            except ValueError:
                continue
    return volumes


def _release_source_key(release: dict[str, Any]) -> str:
    """The key that names one release's source for failure accounting."""

    keys = release_source_keys(release)
    return keys[0] if keys else ""


def _wanted_ranking_revision(
    source_keys: frozenset[str], demoted: frozenset[str], health: dict[str, int]
) -> tuple[Any, ...]:
    return (
        tuple(sorted(demoted & source_keys)),
        tuple(sorted((key, health[key]) for key in source_keys if key in health)),
    )


class TankarrService:
    def __init__(
        self,
        settings: Settings,
        database: Database,
        provider: Provider | dict[str, Provider],
        komga: KomgaClient,
        notifier: Notifier | None = None,
    ):
        self.settings = settings
        self.database = database
        self._strict_provider_registry = isinstance(provider, dict)
        if isinstance(provider, dict):
            self.providers = provider
        else:
            self.providers = {getattr(provider, "name", "suwayomi"): provider}
        self.komga = komga
        self.notifier = notifier or Notifier(settings)
        self._mutation_lock = asyncio.Lock()
        self._komga_reconciliation_lock = asyncio.Lock()
        self._komga_refresh_lock = asyncio.Lock()
        self._komga_refresh_post_scan: (
            Callable[[], Awaitable[dict[str, Any]]] | None
        ) = None
        self._komga_refresh_completed_monotonic = monotonic()
        self.last_deletion_recovery: dict[str, Any] | None = None
        self.last_library_organization: dict[str, Any] | None = None
        self.last_komga_reconciliation: dict[str, Any] | None = None
        self.last_komga_library_alignment: dict[str, Any] | None = None
        self.last_komga_refresh: dict[str, Any] | None = None
        self._wanted_series_cache: dict[
            str, tuple[tuple[Any, ...], dict[str, Any] | None, float, frozenset[str]]
        ] = {}
        self._wanted_series_cache_lock = Lock()
        self._orphan_summary: tuple[float, dict[str, Any]] | None = None

    @property
    def provider(self) -> Provider:
        """Default provider, kept for callers that predate multi-provider."""

        return self.providers.get("suwayomi") or next(iter(self.providers.values()))

    def provider_for(self, name: str | None) -> Provider:
        if name and name in self.providers:
            return self.providers[name]
        if name and self._strict_provider_registry:
            available = ", ".join(sorted(self.providers)) or "none"
            raise ProviderUnavailableError(
                f"Provider {name!r} is not configured (available: {available})"
            )
        return self.provider

    def assert_mutations_allowed(self, *, db_only: bool = False) -> None:
        if getattr(self.settings, "restored_safe_mode", False):
            raise RecoveryBlocked(
                "Tankarr mutations are disabled after restore until recovery preflight is confirmed"
            )
        recovery = self.last_deletion_recovery
        if recovery is not None and recovery.get("recovery_blocked"):
            if db_only and not recovery.get("library_available", False):
                # Explicitly keeping files is a DB-only operation and remains useful
                # while a known library mount is offline.
                return
            raise RecoveryBlocked(
                "Tankarr mutations are disabled until deletion recovery is resolved"
            )
        organization = self.last_library_organization
        if (
            organization is not None
            and organization.get("organization_blocked")
            and not db_only
        ):
            raise RecoveryBlocked(
                "Tankarr mutations are disabled until library organization is resolved"
            )

    def _mark_recovery_blocked(self, warning: str) -> None:
        current = dict(self.last_deletion_recovery or {})
        warnings = list(current.get("warnings") or [])
        if warning not in warnings:
            warnings.append(warning)
        current.update(
            {
                "library_available": current.get("library_available", True),
                "rolled_back": int(current.get("rolled_back", 0)),
                "purged": int(current.get("purged", 0)),
                "files_deleted": int(current.get("files_deleted", 0)),
                "warnings": warnings,
                "scan_required": bool(current.get("scan_required", False)),
                "recovery_blocked": True,
            }
        )
        self.last_deletion_recovery = current

    async def preview_manga(
        self,
        manga_id: str,
        language: str,
        provider_name: str | None = None,
        completeness_assessor: Callable[
            [dict[str, Any], list[dict[str, Any]], str, int],
            Awaitable[dict[str, Any]],
        ]
        | None = None,
    ) -> dict[str, Any]:
        provider = self.provider_for(provider_name)
        manga = await provider.get_manga(manga_id)
        manga["preferred_language"] = language
        chapters = await provider.list_chapters(manga_id, language)
        allowed, reason = evaluate_future_monitoring(manga, chapters)
        chapter_count = self._logical_chapter_count(chapters)
        result = {
            **manga,
            "preferred_language": language,
            "chapter_count": chapter_count,
            "latest_available_chapter": self._latest_chapter(chapters),
            "future_monitoring_allowed": allowed,
            "future_monitoring_reason": reason,
        }
        if completeness_assessor is not None:
            result["chapter_completeness"] = await completeness_assessor(
                manga, chapters, language, chapter_count
            )
        return result

    async def add_manga(
        self,
        manga_id: str,
        language: str,
        monitor_mode: str,
        provider_name: str | None = None,
    ) -> dict[str, Any]:
        async with self._mutation_lock:
            self.assert_mutations_allowed()
            mode = normalize_monitor_mode(monitor_mode)
            provider = self.provider_for(provider_name)
            manga = await provider.get_manga(manga_id)
            manga["preferred_language"] = language
            chapters = await provider.list_chapters(manga_id, language)
            allowed, reason = evaluate_future_monitoring(manga, chapters)
            if mode in FUTURE_MONITOR_MODES and not allowed:
                raise FutureMonitoringUnavailable(reason)
            saved = self.database.upsert_manga(manga, language, mode)
            self.database.upsert_chapters(
                manga_id,
                chapters,
                monitor_new=mode in BACKLOG_MONITOR_MODES,
            )
            self.database.set_future_monitoring_capability(
                manga_id, allowed=allowed, reason=reason
            )
            self.database.record_monitor_result(manga_id)
            return self.database.get_manga(saved["id"])

    async def add_catalogue_series(
        self,
        record: dict[str, Any],
        language: str,
        monitor_mode: str,
        *,
        series_unit: str = "automatic",
    ) -> dict[str, Any]:
        """Add a work by catalogue identity; release sources are mapped later."""

        async with self._mutation_lock:
            self.assert_mutations_allowed()
            mode = normalize_monitor_mode(monitor_mode)
            manga = manga_from_record(record, language=language)
            allowed, reason = catalogue_future_monitoring(manga)
            for existing in self.database.list_manga():
                if (
                    existing.get("provider") == CATALOGUE_PROVIDER
                    and str(existing.get("source_id") or "") == manga["source_id"]
                ):
                    raise ValueError(f"{existing['title']} is already in the library")
            if mode in FUTURE_MONITOR_MODES and not allowed:
                raise FutureMonitoringUnavailable(reason)
            saved = self.database.upsert_manga(manga, language, mode)
            chosen_unit = normalize_series_unit(series_unit)
            if chosen_unit:
                self.database.update_manga(
                    saved["id"], {"series_unit_override": chosen_unit}
                )
            self.database.set_future_monitoring_capability(
                saved["id"], allowed=allowed, reason=reason
            )
            self.database.record_monitor_result(saved["id"])
            return self.database.get_manga(saved["id"])

    ADOPTION_MIN_CONFIDENCE = 0.85

    def adopt_catalogue_identities(self) -> dict[str, Any]:
        """Local-import series that a metadata source identified (manual pin or
        a confident match) become catalogue works. Idempotent.

        MangaBaka is the identity of record; works it does not carry - English
        anthologies and one-off art books, typically - fall back to Comic Vine
        and then Google Books, so the series still gains a stable identity."""

        adopted: list[str] = []
        skipped: list[str] = []
        for manga in self.database.list_manga():
            if manga.get("provider") != "local":
                continue
            manga_id = str(manga["id"])
            metadata_row = self.database.get_series_metadata(manga_id)
            external = ((metadata_row or {}).get("data") or {}).get(
                "external_ids"
            ) or {}
            confidence_by_source = {
                str(record.get("source")): float(record.get("match_confidence") or 0)
                for record in self.database.list_metadata_source_records(manga_id)
            }
            weak = False
            for source_name in IDENTITY_SOURCE_PRIORITY:
                external_id = str(external.get(source_name) or "").strip()
                if not external_id:
                    continue
                confidence = confidence_by_source.get(source_name, 1.0)
                if confidence < self.ADOPTION_MIN_CONFIDENCE:
                    weak = True
                    continue
                if self.database.adopt_catalogue_identity(
                    manga_id,
                    external_id,
                    source_label=CORRELATION_LABELS.get(source_name, source_name),
                    source_url=correlation_url(source_name, external_id),
                ):
                    adopted.append(manga_id)
                break
            else:
                if weak:
                    skipped.append(str(manga["title"]))
        return {"adopted": adopted, "skipped": skipped}

    async def backfill_page_counts(self, manga_id: str, limit: int = 60) -> int:
        """Count the pages of library files that never recorded them.

        Files imported before Tankarr stored page counts, and books imported
        from disk, arrive without one; the series page shows the real count
        once it is known."""

        root = self._library_root()
        updated = 0
        for release in self.database.list_releases_without_pages(manga_id, limit):
            try:
                archive = self._recorded_library_path(
                    str(release["library_path"]), root
                )
                pages = await asyncio.to_thread(count_archive_pages, archive)
            except Exception:  # noqa: BLE001 - a missing file is not an error here
                continue
            if self.database.set_release_pages(str(release["id"]), pages):
                updated += 1
        return updated

    def preview_catalogue(
        self, record: dict[str, Any], language: str
    ) -> dict[str, Any]:
        manga = manga_from_record(record, language=language)
        allowed, reason = catalogue_future_monitoring(manga)
        card = catalogue_card(record)
        return {
            **card,
            "preferred_language": language,
            "chapter_count": card.get("chapter_count")
            or card.get("latest_release_chapter"),
            "latest_available_chapter": None,
            "future_monitoring_allowed": allowed,
            "future_monitoring_reason": reason,
            "chapter_completeness": None,
            "suggested_unit": resolve_series_unit(manga, card, [])["unit"],
        }

    async def register_local_import_series(
        self, manga: dict[str, Any], language: str
    ) -> dict[str, Any]:
        """Resolve and register one canonical local series under the mutation lock."""

        async with self._mutation_lock:
            self.assert_mutations_allowed()
            if manga.get("provider") != "local":
                raise ValueError("Local imports must use the local provider")
            identity = local_series_identity(
                manga.get("title"), manga.get("authors", [])
            )
            matches = [
                candidate
                for candidate in self.database.list_manga()
                if candidate.get("provider") in {"local", "catalogue"}
                and local_series_identity(
                    candidate.get("source_title") or candidate.get("title"),
                    candidate.get("authors", []),
                )
                == identity
            ]
            if len(matches) > 1:
                identifiers = ", ".join(sorted(str(item["id"]) for item in matches))
                raise LocalImportConflict(
                    "Multiple series have the same local-import title/author identity: "
                    f"{identifiers}. Merge or delete the duplicate series first."
                )

            resolved = dict(manga)
            if matches:
                current = matches[0]
                if current.get("provider") == "catalogue":
                    # This exact work began as, or is now the unique canonical
                    # target for, the local identity. Do not reset its catalogue
                    # monitor settings while preparing another local book.
                    current["_local_import_target"] = True
                    return current
                resolved["id"] = current["id"]
                # A display-title override is intentionally separate from local
                # import identity. Preserve the canonical import title here;
                # Database.upsert_manga keeps the existing override untouched.
                resolved["title"] = current.get("source_title") or current["title"]
                resolved["authors"] = current.get("authors", [])
                resolved["description"] = current.get("description", "")
                resolved["cover_url"] = current.get("cover_url")
                resolved["available_languages"] = list(
                    dict.fromkeys(
                        [
                            *(current.get("available_languages") or []),
                            *(resolved.get("available_languages") or []),
                            language,
                        ]
                    )
                )
            else:
                try:
                    collision = self.database.get_manga(str(resolved["id"]))
                except KeyError:
                    collision = None
                if collision is not None:
                    raise LocalImportConflict(
                        "Deterministic local series ID collides with a different "
                        f"series: {collision['id']}"
                    )

            saved = self.database.upsert_manga(resolved, language, "none")
            self.database.set_future_monitoring_capability(
                saved["id"],
                allowed=False,
                reason="No download source linked yet",
            )
            return self.database.get_manga(saved["id"])

    async def publish_local_import(
        self,
        manga: dict[str, Any],
        chapter: dict[str, Any],
        staged_cbz: Path,
        archive_sha256: str,
        content_sha256: str,
        cover_source: Path | None = None,
    ) -> dict[str, Any]:
        """Publish a prepared local CBZ and its database ledger as one mutation."""

        async with self._mutation_lock:
            result = await asyncio.to_thread(
                self._publish_local_import_locked,
                manga,
                chapter,
                staged_cbz,
                archive_sha256,
                content_sha256,
                cover_source,
            )
        already_imported = bool(result.pop("_already_imported", False))
        if not already_imported:
            await self.notifier.chapter_imported(manga, result["chapter"])
        return result

    def _managed_local_import_candidate(
        self, manga_id: str, chapter: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any], Path, str | None] | None:
        manga = self.database.get_manga(manga_id)
        if manga.get("provider") != "local":
            return None
        release_identity = local_release_identity(
            chapter.get("language"), chapter.get("volume"), chapter.get("chapter")
        )
        matches: list[dict[str, Any]] = []
        for candidate in self.database.list_all_chapters(manga_id):
            if candidate.get("provider") != "local":
                continue
            try:
                candidate_identity = local_release_identity(
                    candidate.get("language"),
                    candidate.get("volume"),
                    candidate.get("chapter"),
                )
            except ValueError:
                continue
            if candidate_identity == release_identity:
                matches.append(candidate)
        if len(matches) > 1:
            identifiers = ", ".join(sorted(str(item["id"]) for item in matches))
            raise LocalImportConflict(
                "Multiple local chapters have the same language/volume/chapter "
                f"identity: {identifiers}. Resolve the duplicate records first."
            )
        if not matches or not matches[0].get("downloaded"):
            return None
        existing = matches[0]
        recorded_path = existing.get("library_path")
        recorded_sha256 = str(existing.get("library_sha256") or "")
        raw_content_sha256 = str(existing.get("local_import_sha256") or "")
        content_sha256 = (
            raw_content_sha256 if self._valid_sha256(raw_content_sha256) else None
        )
        if not recorded_path or not self._valid_sha256(recorded_sha256):
            return None
        root = self._library_root()
        recorded = self._recorded_library_path(str(recorded_path), root)
        if recorded.is_symlink() or not recorded.is_file():
            return None
        return existing, manga, recorded, content_sha256

    async def managed_local_import_probe(
        self, manga_id: str, chapter: dict[str, Any]
    ) -> dict[str, str | None] | None:
        """Return trusted ledger details for a potentially reusable slot."""

        async with self._mutation_lock:
            self.assert_mutations_allowed()
            candidate = self._managed_local_import_candidate(manga_id, chapter)
            if candidate is None:
                return None
            _existing, _manga, recorded, content_sha256 = candidate
            return {"path": str(recorded), "content_sha256": content_sha256}

    async def reuse_managed_local_import(
        self,
        manga_id: str,
        chapter: dict[str, Any],
        source_content_sha256: str | None = None,
        *,
        archive_pages_match: bool = False,
    ) -> dict[str, Any] | None:
        """Return an occupied managed slot without reopening the import source.

        A library import is never an implicit replacement operation. Once the
        exact local language/volume/chapter slot has a tracked, hashed CBZ, the
        incoming item can be skipped safely: no source identity or filename is
        trusted to authorize overwriting that book. Missing, ambiguous, or
        incompletely-ledgered records deliberately fall through to the full
        content validation path.
        """

        async with self._mutation_lock:
            self.assert_mutations_allowed()
            candidate = self._managed_local_import_candidate(manga_id, chapter)
            if candidate is None:
                return None
            existing, manga, recorded, expected_content_sha256 = candidate
            if archive_pages_match and expected_content_sha256 is None:
                expected_content_sha256 = await asyncio.to_thread(
                    self._local_cbz_content_sha256, recorded
                )
                existing = self.database.publish_local_chapter(
                    manga_id,
                    existing,
                    recorded,
                    str(existing["library_sha256"]),
                    expected_content_sha256,
                )
            if not archive_pages_match and (
                expected_content_sha256 is None
                or source_content_sha256 is None
                or not hmac.compare_digest(
                    expected_content_sha256, source_content_sha256
                )
            ):
                return None
            return {
                "chapter": existing,
                "path": str(recorded),
                "reused": True,
                "cover_url": manga.get("cover_url"),
            }

    @staticmethod
    async def _finish_import_thread(function, *args):
        task = asyncio.create_task(asyncio.to_thread(function, *args))
        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
        result = task.result()
        if cancelled:
            raise asyncio.CancelledError
        return result

    async def publish_external_import(
        self,
        manga_id: str,
        chapter: dict[str, Any],
        staged_cbz: Path,
        archive_sha256: str,
        content_sha256: str,
    ) -> dict[str, Any]:
        """Publish one verified external book into an existing Tankarr series."""

        async with self._mutation_lock:
            # Keep staging and the mutation lock alive if the request closes
            # while its publication thread is still changing files.
            result = await self._finish_import_thread(
                self._publish_external_import_locked,
                manga_id,
                chapter,
                staged_cbz,
                archive_sha256,
                content_sha256,
            )
            manga = self.database.get_manga(manga_id)
        already_imported = bool(result.pop("_already_imported", False))
        if result.get("assembly_retirement"):
            result["assembly_retirement"][
                "komga_scan"
            ] = await self._request_komga_reconciliation(True)
        if not already_imported:
            await self.notifier.chapter_imported(manga, result["chapter"])
        return result

    def _publish_external_import_locked(
        self,
        manga_id: str,
        chapter: dict[str, Any],
        staged_cbz: Path,
        archive_sha256: str,
        content_sha256: str,
    ) -> dict[str, Any]:
        self.assert_mutations_allowed()
        if chapter.get("provider") not in EXTERNAL_IMPORT_PROVIDERS:
            raise ValueError("Unsupported external import provider")
        if not self._valid_sha256(archive_sha256) or not self._valid_sha256(
            content_sha256
        ):
            raise ValueError("External import has an invalid SHA-256 ledger")
        manga = self.database.get_manga(manga_id)
        if chapter.get("language") != manga.get("preferred_language"):
            raise ExternalImportConflict(
                "Torrent release language differs from the series language profile"
            )
        if chapter.get("provider") == "translated":
            from tankarr.translation_policy import matches_slot, slot_key

            if not self.settings.translation_enabled or not manga.get(
                "translation_enabled"
            ):
                raise ExternalImportConflict("Translation fallback is disabled")
            for owned in self.database.list_chapters(manga_id, chapter["language"]):
                if (
                    owned.get("downloaded")
                    and owned["id"] != chapter["id"]
                    and matches_slot(owned, slot_key(chapter))
                ):
                    raise ExternalImportConflict(
                        "This slot already has an imported release"
                    )
        release_identity = local_release_identity(
            chapter.get("language"), chapter.get("volume"), chapter.get("chapter")
        )
        matches: list[dict[str, Any]] = []
        for candidate in self.database.list_all_chapters(manga_id):
            if candidate.get("provider") not in EXTERNAL_IMPORT_PROVIDERS:
                continue
            try:
                candidate_identity = local_release_identity(
                    candidate.get("language"),
                    candidate.get("volume"),
                    candidate.get("chapter"),
                )
            except ValueError:
                continue
            if candidate_identity == release_identity:
                matches.append(candidate)
        if len(matches) > 1:
            raise ExternalImportConflict(
                "Multiple external books already have the same language/volume/chapter"
            )
        imported_chapter = dict(chapter)
        existing = matches[0] if matches else None
        if existing is not None:
            imported_chapter["id"] = existing["id"]
        else:
            try:
                collision = self.database.get_chapter(str(imported_chapter["id"]))
            except KeyError:
                collision = None
            if collision is not None:
                raise ExternalImportConflict(
                    "Torrent chapter identity collides with another release"
                )

        from tankarr.assembly_provenance import assembly_provenance, proven_assembly

        if (
            existing is not None
            and existing.get("provider") == "translated"
            and imported_chapter.get("provider") != "translated"
        ):
            return self._replace_assembled_import_locked(
                manga,
                existing,
                imported_chapter,
                staged_cbz,
                archive_sha256,
                content_sha256,
            )

        replacing_assembly = bool(
            existing is not None
            and imported_chapter.get("provider") not in {"assembled", "translated"}
            and str(imported_chapter.get("release_unit") or "volume") == "volume"
            and imported_chapter.get("chapter") in (None, "")
            and (
                proven_assembly(existing)
                or (
                    existing.get("provider") == "assembled"
                    and not existing.get("downloaded")
                    and assembly_provenance(existing.get("assembled_from"))
                )
            )
        )
        # A book built by hand (the lab's split from raws) whose size is off
        # the edition gives way to the edition's own copy of the same number:
        # the old file goes to the recycle bin with a receipt, as an assembly
        # does, never deleted outright.
        replacing_hand_built = bool(
            existing is not None
            and not replacing_assembly
            and existing.get("provider") == "manual"
            and existing.get("downloaded")
            and imported_chapter.get("provider") in TORRENT_IMPORT_PROVIDERS
            and str(imported_chapter.get("release_unit") or "volume") == "volume"
            and imported_chapter.get("chapter") in (None, "")
            and self._hand_built_book_off_edition(manga_id, existing)
        )
        if replacing_assembly or replacing_hand_built:
            return self._replace_assembled_import_locked(
                manga,
                existing,
                imported_chapter,
                staged_cbz,
                archive_sha256,
                content_sha256,
            )

        rebuilding_missing_assembly = bool(
            existing is not None
            and existing.get("provider")
            == imported_chapter.get("provider")
            == "assembled"
            and not existing.get("downloaded")
            and (
                not existing.get("library_path")
                or not self._recorded_library_path(
                    str(existing["library_path"]), self._library_root()
                ).exists()
            )
            and not self._confined_library_path(
                final_library_path(self._library_root(), manga, imported_chapter),
                self._library_root(),
            ).exists()
        )

        stored_content_sha256 = (
            str(existing.get("local_import_sha256") or "")
            if existing and not rebuilding_missing_assembly
            else ""
        )
        if stored_content_sha256 and not self._valid_sha256(stored_content_sha256):
            raise ExternalImportConflict("Existing external content ledger is invalid")
        if stored_content_sha256 and stored_content_sha256 != content_sha256:
            raise ExternalImportConflict(
                "A different external book already occupies this language/volume/chapter"
            )
        # This private staging artifact was fully validated while packaging.
        # Atomic installation verifies its supplied hash during the copy and
        # reads the durable destination back once, avoiding several extra full
        # passes over large omnibus archives on low-power hosts.
        staged_info = {
            "page_count": int(imported_chapter.get("pages") or 0),
            "size": staged_cbz.stat().st_size,
            "sha256": archive_sha256,
        }

        root = self._library_root()
        destination = self._confined_library_path(
            final_library_path(root, manga, imported_chapter), root
        )
        self._assert_library_paths_not_shared(
            manga_id, {str(imported_chapter["id"])}, [destination]
        )
        if existing is not None and existing.get("downloaded"):
            recorded_path = existing.get("library_path")
            if not recorded_path:
                raise ExternalImportConflict(
                    "Downloaded external book has no library path"
                )
            recorded = self._recorded_library_path(str(recorded_path), root)
            if recorded.exists():
                archive_info = self._validate_matching_local_import(
                    recorded,
                    content_sha256,
                    stored_archive_sha256=existing.get("library_sha256"),
                    stored_content_sha256=stored_content_sha256 or None,
                )
                self.database.publish_external_chapter(
                    manga_id,
                    imported_chapter,
                    recorded,
                    archive_info["sha256"],
                    content_sha256,
                )
                destination = self._organize_imported_chapter_locked(
                    str(imported_chapter["id"]), archive_info["sha256"]
                )
                return {
                    "chapter": self.database.get_chapter(str(imported_chapter["id"])),
                    "path": str(destination),
                    "reused": True,
                    "_already_imported": True,
                }
            if not stored_content_sha256:
                raise ExternalImportConflict(
                    "Downloaded external book is missing and has no content ledger"
                )

        destination_preexisted = destination.exists()
        if destination_preexisted:
            if existing is None:
                self._adopt_matching_metadata_less_external_import(
                    destination,
                    manga,
                    imported_chapter,
                    content_sha256,
                )
            published_info = self._validate_matching_local_import(
                destination,
                content_sha256,
                stored_content_sha256=stored_content_sha256 or None,
            )
        else:
            install_atomically(staged_cbz, destination, expected_sha256=archive_sha256)
            published_info = staged_info
        try:
            self.database.publish_external_chapter(
                manga_id,
                imported_chapter,
                destination,
                published_info["sha256"],
                content_sha256,
            )
        except Exception as exc:
            if not destination_preexisted:
                try:
                    destination.unlink(missing_ok=True)
                    fsync_directory(destination.parent)
                except OSError as cleanup_error:
                    raise ImportDurabilityError(
                        "Torrent import database publication failed and rollback failed: "
                        f"{cleanup_error}"
                    ) from exc
            raise
        destination = self._organize_imported_chapter_locked(
            str(imported_chapter["id"]), published_info["sha256"]
        )
        return {
            "chapter": self.database.get_chapter(str(imported_chapter["id"])),
            "path": str(destination),
            "reused": destination_preexisted,
            "_already_imported": False,
        }

    def _hand_built_book_off_edition(
        self, manga_id: str, existing: dict[str, Any]
    ) -> bool:
        from tankarr.chapter_mapping import canonical_number, suspect_volume_reasons

        volume = canonical_number(existing.get("volume"))
        if volume is None:
            return False
        reasons = suspect_volume_reasons(self.database.list_all_chapters(manga_id))
        return "another edition or a wrong split" in str(reasons.get(str(volume)) or "")

    def _replace_assembled_import_locked(
        self,
        manga: dict[str, Any],
        existing: dict[str, Any],
        chapter: dict[str, Any],
        staged_cbz: Path,
        archive_sha256: str,
        content_sha256: str,
    ) -> dict[str, Any]:
        """Publish a real book before committing retirement of its assembly.

        Keep the replacement at a separate durable library path until the
        chapter ledger and deletion receipt commit together. Prepared-receipt
        recovery can then restore the old canonical path without colliding
        with a replacement published just before a crash.
        """

        if getattr(self.database._read_snapshot, "connection", None) is not None:
            raise ExternalImportConflict(
                "Assembly upgrades require their own transaction"
            )
        manga_id = str(manga["id"])
        chapter_id = str(existing["id"])
        root = self._library_root()
        destination = self._confined_library_path(
            final_library_path(root, manga, chapter), root
        )
        recorded = (
            self._recorded_library_path(str(existing["library_path"]), root)
            if existing.get("library_path")
            else None
        )
        if existing.get("downloaded") and recorded is None:
            raise ExternalImportConflict(
                "Downloaded assembly has no recorded library path"
            )
        if destination.exists() and destination != recorded:
            raise ExternalImportConflict(
                "Another file already occupies the book destination"
            )
        paths = [recorded] if recorded is not None and recorded.exists() else []
        if paths and sha256(paths[0]) != existing.get("library_sha256"):
            raise ExternalImportConflict(
                "The assembled book differs from its hash ledger"
            )
        self._assert_library_paths_not_shared(
            manga_id, {chapter_id}, [destination, *paths]
        )
        self.database.assert_chapter_file_deletion_ready(manga_id, chapter_id)
        replacement = self._confined_library_path(
            destination.with_name(f"tankarr-replacement-{uuid.uuid4().hex}.cbz"), root
        )
        install_atomically(staged_cbz, replacement, expected_sha256=archive_sha256)
        # Even a missing old file gets a prepared receipt: an ambiguous SQLite
        # commit must never lead us to discard a replacement that did publish.
        quarantine = self._stage_library_files(
            paths or [destination], disposition="retain"
        )
        try:
            if any(
                sha256(staged) != existing.get("library_sha256")
                for _original, staged in quarantine.moved
            ):
                raise ExternalImportConflict(
                    "The assembled book changed before retirement"
                )
            with self.database.write_snapshot():
                current = self.database.get_chapter(chapter_id)
                if any(
                    current.get(key) != existing.get(key)
                    for key in (
                        "manga_id",
                        "volume",
                        "chapter",
                        "language",
                        "release_unit",
                        "provider",
                        "downloaded",
                        "library_path",
                        "library_sha256",
                        "local_import_sha256",
                        "assembled_from",
                    )
                ):
                    raise ExternalImportConflict(
                        "The assembled book changed before publication"
                    )
                self.database.publish_external_chapter(
                    manga_id, chapter, replacement, archive_sha256, content_sha256
                )
                self.database.mark_deletion_replacement(
                    quarantine.operation_id, chapter_id, archive_sha256
                )
                with self.database.connect() as connection:
                    self.database._commit_deletion_operation(
                        connection, quarantine.operation_id
                    )
        except Exception as exc:
            self._handle_deletion_database_exception(quarantine, exc)
            # The prepared receipt was rolled back: the original book is back
            # and the replacement was never published in the database.
            if replacement.exists() and sha256(replacement) == archive_sha256:
                replacement.unlink()
                fsync_directory(replacement.parent)
            raise
        retirement = self._commit_file_quarantine(quarantine)
        self.assert_mutations_allowed()
        destination = self._organize_imported_chapter_locked(chapter_id, archive_sha256)
        return {
            "chapter": self.database.get_chapter(chapter_id),
            "path": str(destination),
            "reused": False,
            "_already_imported": False,
            "assembly_retirement": retirement,
        }

    def _adopt_matching_metadata_less_external_import(
        self,
        path: Path,
        manga: dict[str, Any],
        chapter: dict[str, Any],
        content_sha256: str,
    ) -> None:
        """Add ComicInfo to an exact hand-imported ZIP, never to other content.

        A user may copy a completed .cbr/.zip into the canonical library path
        before Tankarr publishes its normalized book.  The portable file then
        exists but has no ComicInfo and is intentionally absent from the DB.
        Compare the ordered page payload ledger first; only an exact match may
        be rewritten and adopted.  This preserves the no-overwrite boundary.
        """

        try:
            read_comic_info(path)
            return
        except ValueError as exc:
            if str(exc) != "CBZ has no ComicInfo.xml":
                raise
        try:
            actual_content_sha256 = self._local_cbz_content_sha256(path)
        except LocalImportConflict as exc:
            raise ExternalImportConflict(str(exc)) from exc
        if not hmac.compare_digest(actual_content_sha256, content_sha256):
            raise ExternalImportConflict(
                "Different content already exists at the canonical external import path"
            )
        pages = count_archive_pages(path)
        replace_comic_info(
            path,
            build_comic_info(manga, chapter, pages),
            allow_missing=True,
        )

    def _publish_local_import_locked(
        self,
        manga: dict[str, Any],
        chapter: dict[str, Any],
        staged_cbz: Path,
        archive_sha256: str,
        content_sha256: str,
        cover_source: Path | None,
    ) -> dict[str, Any]:
        self.assert_mutations_allowed()
        if not self._valid_sha256(archive_sha256) or not self._valid_sha256(
            content_sha256
        ):
            raise ValueError("Local import has an invalid SHA-256 ledger")
        current_manga = self.database.get_manga(str(manga["id"]))
        catalogue_local_target = current_manga.get("provider") == "catalogue" and (
            manga.get("provider") == "local"
            or manga.get("_local_import_target") is True
        )
        if current_manga.get("provider") not in {
            "local",
            "catalogue",
        } or local_series_identity(
            current_manga.get("source_title") or current_manga.get("title"),
            current_manga.get("authors", []),
        ) != local_series_identity(
            manga.get("source_title") or manga.get("title"),
            manga.get("authors", []),
        ):
            raise LocalImportConflict(
                "Local series changed or was deleted while the import was prepared"
            )

        release_identity = local_release_identity(
            chapter.get("language"), chapter.get("volume"), chapter.get("chapter")
        )
        matches: list[dict[str, Any]] = []
        for candidate in self.database.list_all_chapters(current_manga["id"]):
            if candidate.get("provider") != "local":
                continue
            try:
                candidate_identity = local_release_identity(
                    candidate.get("language"),
                    candidate.get("volume"),
                    candidate.get("chapter"),
                )
            except ValueError:
                continue
            if candidate_identity == release_identity:
                matches.append(candidate)
        if len(matches) > 1:
            identifiers = ", ".join(sorted(str(item["id"]) for item in matches))
            raise LocalImportConflict(
                "Multiple local chapters have the same language/volume/chapter "
                f"identity: {identifiers}. Resolve the duplicate records first."
            )

        imported_chapter = dict(chapter)
        existing = matches[0] if matches else None
        if existing is not None:
            imported_chapter["id"] = existing["id"]
        else:
            try:
                collision = self.database.get_chapter(str(imported_chapter["id"]))
            except KeyError:
                collision = None
            if collision is not None:
                raise LocalImportConflict(
                    "Deterministic local chapter ID collides with a different "
                    f"release: {collision['id']}"
                )

        stored_content_sha256 = (
            str(existing.get("local_import_sha256") or "") if existing else ""
        )
        if stored_content_sha256 and not self._valid_sha256(stored_content_sha256):
            raise LocalImportConflict(
                f"Local import ledger is invalid for chapter {imported_chapter['id']}"
            )
        if stored_content_sha256 and stored_content_sha256 != content_sha256:
            raise LocalImportConflict(
                "Different content already exists for local release "
                f"{self._local_release_label(imported_chapter)}"
            )

        # The importer owns this private staging path and validated it while
        # packaging. Atomic installation binds it to the immutable archive
        # ledger and verifies the durable destination after fsync.
        staged_info = {
            "page_count": int(imported_chapter.get("pages") or 0),
            "size": staged_cbz.stat().st_size,
            "sha256": archive_sha256,
        }

        root = self._library_root()
        destination = self._confined_library_path(
            final_library_path(root, current_manga, imported_chapter), root
        )
        self._assert_library_paths_not_shared(
            current_manga["id"], {str(imported_chapter["id"])}, [destination]
        )

        if existing is not None and existing.get("downloaded"):
            recorded_path = existing.get("library_path")
            if not recorded_path:
                raise LocalImportConflict(
                    f"Downloaded local chapter {existing['id']} has no library path"
                )
            recorded = self._recorded_library_path(str(recorded_path), root)
            if recorded.exists():
                archive_info = self._validate_matching_local_import(
                    recorded,
                    content_sha256,
                    stored_archive_sha256=existing.get("library_sha256"),
                    stored_content_sha256=stored_content_sha256 or None,
                )
                self.database.publish_local_chapter(
                    current_manga["id"],
                    imported_chapter,
                    recorded,
                    archive_info["sha256"],
                    content_sha256,
                    allow_catalogue_series=catalogue_local_target,
                )
                destination = self._organize_imported_chapter_locked(
                    str(imported_chapter["id"]), archive_info["sha256"]
                )
                cover_result = self._publish_local_cover_locked(
                    current_manga, imported_chapter["language"], cover_source
                )
                return {
                    "chapter": self.database.get_chapter(str(imported_chapter["id"])),
                    "path": str(destination),
                    "reused": True,
                    "_already_imported": True,
                    **cover_result,
                }
            if not stored_content_sha256:
                raise LocalImportConflict(
                    "Downloaded local chapter is missing and has no content ledger; "
                    "refusing an unverifiable replacement"
                )

        destination_preexisted = destination.exists()
        if destination_preexisted:
            published_info = self._validate_matching_local_import(
                destination,
                content_sha256,
                stored_content_sha256=stored_content_sha256 or None,
            )
        else:
            install_atomically(staged_cbz, destination, expected_sha256=archive_sha256)
            published_info = staged_info

        try:
            self.database.publish_local_chapter(
                current_manga["id"],
                imported_chapter,
                destination,
                published_info["sha256"],
                content_sha256,
                allow_catalogue_series=catalogue_local_target,
            )
        except Exception as exc:
            if not destination_preexisted:
                try:
                    destination.unlink(missing_ok=True)
                    fsync_directory(destination.parent)
                except OSError as cleanup_error:
                    raise ImportDurabilityError(
                        "Local import database publication failed and the new CBZ "
                        f"could not be rolled back: {destination}: {cleanup_error}"
                    ) from exc
            raise

        cover_result = self._publish_local_cover_locked(
            current_manga, imported_chapter["language"], cover_source
        )
        return {
            "chapter": self.database.get_chapter(str(imported_chapter["id"])),
            "path": str(destination),
            "reused": bool(existing is not None or destination_preexisted),
            "_already_imported": False,
            **cover_result,
        }

    def _validate_matching_local_import(
        self,
        path: Path,
        content_sha256: str,
        *,
        stored_archive_sha256: object = None,
        stored_content_sha256: str | None = None,
    ) -> dict[str, Any]:
        if path.is_symlink() or not path.is_file():
            raise LocalImportConflict(f"Unsafe existing local import path: {path}")
        archive_info = validate_cbz(path)
        if stored_archive_sha256:
            expected_archive_sha256 = str(stored_archive_sha256)
            if not self._valid_sha256(expected_archive_sha256):
                raise LocalImportConflict(f"Invalid stored library hash for {path}")
            if archive_info["sha256"] != expected_archive_sha256:
                raise LocalImportConflict(
                    f"Existing local CBZ differs from its stored hash: {path}"
                )
        actual_content_sha256 = self._local_cbz_content_sha256(path)
        if stored_content_sha256 and actual_content_sha256 != stored_content_sha256:
            raise LocalImportConflict(
                f"Existing local CBZ differs from its content ledger: {path}"
            )
        if actual_content_sha256 != content_sha256:
            raise LocalImportConflict(
                "Different content already exists for the same local "
                f"language/volume/chapter: {path}"
            )
        return archive_info

    @staticmethod
    def _local_cbz_content_sha256(path: Path) -> str:
        digest = hashlib.sha256(LOCAL_CONTENT_FINGERPRINT_VERSION)
        with zipfile.ZipFile(path) as archive:
            entries = sorted(
                (
                    entry
                    for entry in archive.infolist()
                    if not entry.is_dir()
                    and Path(entry.filename).suffix.lower() in IMAGE_SUFFIXES
                ),
                key=lambda entry: entry.filename.casefold(),
            )
            if not entries:
                raise LocalImportConflict(f"Local CBZ has no image pages: {path}")
            for index, entry in enumerate(entries, start=1):
                with archive.open(entry) as handle:
                    _update_local_content_fingerprint(
                        digest,
                        index,
                        entry.file_size,
                        iter(lambda: handle.read(1024 * 1024), b""),
                    )
        return digest.hexdigest()

    def _publish_local_cover_locked(
        self,
        manga: dict[str, Any],
        language: str,
        cover_source: Path | None,
    ) -> dict[str, Any]:
        current = self.database.get_manga(str(manga["id"]))
        if current.get("cover_url") or cover_source is None:
            return {"cover_url": current.get("cover_url")}
        suffix = cover_source.suffix.lower()
        if suffix not in IMAGE_SUFFIXES or cover_source.is_symlink():
            return {
                "cover_url": None,
                "cover_error": "Prepared cover is not a supported regular image",
            }
        covers_dir = self.settings.data_dir / "covers" / str(manga["id"])
        destination = covers_dir / f"cover{suffix}"
        preexisted = destination.exists()
        try:
            install_atomically(cover_source, destination)
            updated = dict(current)
            updated["cover_url"] = f"/api/covers/local/{manga['id']}/{destination.name}"
            saved = self.database.upsert_manga(updated, language)
            return {"cover_url": saved.get("cover_url")}
        except Exception as exc:  # noqa: BLE001 - the chapter import remains valid
            if not preexisted:
                try:
                    destination.unlink(missing_ok=True)
                    if destination.parent.exists():
                        fsync_directory(destination.parent)
                except OSError:
                    pass
            return {
                "cover_url": current.get("cover_url"),
                "cover_error": f"{type(exc).__name__}: {exc}",
            }

    @staticmethod
    def _local_release_label(chapter: dict[str, Any]) -> str:
        volume = str(chapter.get("volume") or "-")
        number = str(chapter.get("chapter") or "-")
        language = str(chapter.get("language") or "-")
        return f"volume {volume}, chapter {number}, language {language}"

    async def refresh_metadata(self, manga_id: str) -> dict[str, Any]:
        async with self._mutation_lock:
            self.assert_mutations_allowed()
            current = self.database.get_manga(manga_id)
            if current.get("provider") in NO_REMOTE_PROVIDERS:
                return current
            provider = self.provider_for(current.get("provider"))
            manga = await provider.get_manga(manga_id)
            manga["preferred_language"] = current["preferred_language"]
            self.database.upsert_manga(manga, current["preferred_language"])
            chapters = self.database.list_chapters(
                manga_id, current["preferred_language"]
            )
            allowed, reason = evaluate_future_monitoring(manga, chapters)
            return self.database.set_future_monitoring_capability(
                manga_id, allowed=allowed, reason=reason
            )

    async def refresh_official_edition_evidence(self, manga_id: str) -> int:
        """Refresh publisher indexes used only to validate edition bridges."""

        manga = self.database.get_manga(manga_id)
        metadata_row = self.database.get_series_metadata(manga_id)
        metadata = (metadata_row or {}).get("data") or {}
        roles = official_source_roles(
            metadata.get("official_links"),
            preferred_language=str(manga.get("preferred_language") or ""),
            original_language=str(manga.get("original_language") or ""),
        )
        naver_links = [
            str(link.get("url") or "")
            for link in metadata.get("official_links") or []
            if naver_webtoon_id(link.get("url"))
            and roles.get(NAVER_WEBTOON_HOST) == "secondary_official"
        ]
        if not naver_links or not self.database.official_edition_evidence_due(
            manga_id,
            NAVER_WEBTOON_HOST,
            max_age_seconds=OFFICIAL_EVIDENCE_REFRESH_SECONDS,
        ):
            return 0
        try:
            async with async_client(
                timeout=self.settings.request_timeout_seconds
            ) as client:
                host, items = await fetch_naver_webtoon_items(client, naver_links[0])
        except Exception as exc:  # noqa: BLE001 - evidence is never a hard failure
            logger.info(
                "Official Naver evidence refresh skipped for %s: %s", manga_id, exc
            )
            return 0
        if not host:
            return 0
        return self.database.replace_official_edition_evidence(
            manga_id,
            host=host,
            language=str(manga.get("original_language") or "ko"),
            items=items,
        )

    async def rename_manga(self, manga_id: str, title: str) -> dict[str, Any]:
        """Persist a display-title override and reorganize owned library files."""

        normalized = " ".join(str(title or "").split())
        if not normalized:
            raise ValueError("Series title cannot be empty")
        if len(normalized) > 200:
            raise ValueError("Series title cannot exceed 200 characters")

        async with self._mutation_lock:
            self.assert_mutations_allowed()
            current = self.database.get_manga(manga_id)
            automatic_title = str(
                current.get("metadata_title") or current.get("source_title") or ""
            )
            next_override = None if normalized == automatic_title else normalized
            return self._apply_series_title_state_locked(
                current,
                metadata_title=current.get("metadata_title"),
                title_override=next_override,
                publish_metadata=True,
            )

    async def apply_metadata_title(
        self, manga_id: str, title: str | None
    ) -> dict[str, Any]:
        """Adopt one verified catalogue title without overriding user choice."""

        normalized = " ".join(str(title or "").split()) or None
        if normalized is not None and len(normalized) > 200:
            raise ValueError("Series title cannot exceed 200 characters")
        async with self._mutation_lock:
            self.assert_mutations_allowed()
            current = self.database.get_manga(manga_id)
            return self._apply_series_title_state_locked(
                current,
                metadata_title=normalized,
                title_override=current.get("title_override"),
                publish_metadata=False,
            )

    def _apply_series_title_state_locked(
        self,
        current: dict[str, Any],
        *,
        metadata_title: object,
        title_override: object,
        publish_metadata: bool,
    ) -> dict[str, Any]:
        manga_id = str(current["id"])
        previous_title = str(current["title"])
        previous_metadata_title = current.get("metadata_title")
        previous_override = current.get("title_override")
        next_metadata_title = str(metadata_title or "").strip() or None
        next_override = str(title_override or "").strip() or None
        next_title = str(
            next_override or next_metadata_title or current.get("source_title") or ""
        )
        state_changed = (
            next_metadata_title != previous_metadata_title
            or next_override != previous_override
        )
        if not state_changed:
            return self._series_title_result(current, previous_title, updated=False)

        if next_title == previous_title:
            updated = self.database.set_manga_title_state(
                manga_id,
                metadata_title=next_metadata_title,
                title_override=next_override,
            )
            return self._series_title_result(updated, previous_title, updated=True)

        root = self._library_root()
        renamed = dict(current)
        renamed["title"] = next_title
        file_pairs = self._series_rename_file_pairs(manga_id, renamed, root)
        source_parents = {source.parent for source, _ in file_pairs}
        for _, destination in file_pairs:
            if destination.parent not in source_parents and destination.parent.exists():
                raise FileExistsError(
                    "Refusing to merge a renamed series into an existing "
                    f"directory: {destination.parent}"
                )
        sidecar_pairs = self._series_rename_sidecar_pairs(file_pairs, root)
        previous_organization = self.last_library_organization

        self.database.set_manga_title_state(
            manga_id,
            metadata_title=next_metadata_title,
            title_override=next_override,
        )
        preview = self._organize_library_files(root, True, {manga_id})
        if preview.get("organization_blocked") or preview.get("deferred"):
            self.database.set_manga_title_state(
                manga_id,
                metadata_title=previous_metadata_title,
                title_override=previous_override,
            )
            self.last_library_organization = previous_organization
            raise SeriesRenameError(self._rename_failure_message(preview))

        organization = self._organize_library_files(root, False, {manga_id})
        if organization.get("organization_blocked") or organization.get("deferred"):
            rollback = self._rollback_series_title_state(
                manga_id,
                previous_metadata_title,
                previous_override,
                root,
            )
            if rollback.get("organization_blocked") or rollback.get("deferred"):
                self.last_library_organization = self._organization_report(
                    warnings=[
                        "Series rename failed and its automatic rollback is incomplete",
                        *list(rollback.get("warnings") or []),
                    ],
                    organization_blocked=True,
                )
                raise SeriesRenameError(
                    "Series rename failed and automatic path rollback is incomplete; "
                    "library mutations are now blocked"
                )
            self.last_library_organization = previous_organization
            raise SeriesRenameError(self._rename_failure_message(organization))

        moved_sidecars: list[tuple[Path, Path]] = []
        try:
            moved_sidecars = self._move_series_sidecars(sidecar_pairs)
        except Exception as exc:
            rollback = self._rollback_series_title_state(
                manga_id,
                previous_metadata_title,
                previous_override,
                root,
            )
            if rollback.get("organization_blocked") or rollback.get("deferred"):
                self.last_library_organization = self._organization_report(
                    warnings=[
                        "Series sidecar relocation failed and path rollback is incomplete",
                        *list(rollback.get("warnings") or []),
                    ],
                    organization_blocked=True,
                )
            else:
                self.last_library_organization = previous_organization
            raise SeriesRenameError(
                f"Unable to relocate series artwork: {type(exc).__name__}: {exc}"
            ) from exc

        old_parents = {source.parent for source, _ in sidecar_pairs}
        old_parents.update(source.parent for source, _ in file_pairs)
        cleanup_warnings: list[str] = []
        try:
            directories_removed = self._remove_empty_directories(old_parents, root)
        except (OSError, UnsafeLibraryPath) as exc:
            directories_removed = 0
            cleanup_warnings.append(
                f"Unable to remove an empty old series directory: {exc}"
            )
        organization["directories_removed"] = (
            int(organization.get("directories_removed") or 0) + directories_removed
        )
        organization["scan_required"] = bool(
            organization.get("scan_required") or moved_sidecars
        )
        self.last_library_organization = organization

        metadata_result = None
        if publish_metadata:
            try:
                metadata_result = self._publish_metadata_to_library_locked(manga_id)
            except Exception as exc:  # noqa: BLE001 - the rename already committed
                metadata_result = {
                    "reader_independent": True,
                    "checked": 0,
                    "updated": 0,
                    "errors": [{"error": f"{type(exc).__name__}: {exc}"[:500]}],
                }
        warnings = cleanup_warnings + [
            str(item.get("error") or "Metadata publication failed")
            for item in (metadata_result or {}).get("errors") or []
        ]
        updated = self.database.get_manga(manga_id)
        return self._series_title_result(
            updated,
            previous_title,
            updated=True,
            sidecars_moved=len(moved_sidecars),
            organization=organization,
            metadata=metadata_result,
            warnings=warnings,
        )

    def _rollback_series_title_state(
        self,
        manga_id: str,
        previous_metadata_title: object,
        previous_override: object,
        root: Path,
    ) -> dict[str, Any]:
        self.database.set_manga_title_state(
            manga_id,
            metadata_title=(
                str(previous_metadata_title)
                if previous_metadata_title is not None
                else None
            ),
            title_override=(
                str(previous_override) if previous_override is not None else None
            ),
        )
        return self._organize_library_files(root, False, {manga_id})

    def _series_title_result(
        self,
        manga: dict[str, Any],
        previous_title: str,
        *,
        updated: bool,
        sidecars_moved: int = 0,
        organization: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        warnings: list[str] | None = None,
    ) -> dict[str, Any]:
        return {
            "updated": updated,
            "manga": manga,
            "previous_title": previous_title,
            "title": manga["title"],
            "sidecars_moved": sidecars_moved,
            "organization": organization or self._organization_report(),
            "metadata": metadata,
            "warnings": warnings or [],
        }

    @staticmethod
    def _rename_failure_message(report: dict[str, Any]) -> str:
        warnings = [str(item) for item in report.get("warnings") or [] if item]
        if warnings:
            return warnings[0]
        if report.get("deferred"):
            return "Series rename is waiting for active downloads to finish"
        return "Series rename could not be completed safely"

    def _series_rename_file_pairs(
        self,
        manga_id: str,
        renamed: dict[str, Any],
        root: Path,
    ) -> list[tuple[Path, Path]]:
        pairs: list[tuple[Path, Path]] = []
        for chapter in self.database.list_all_chapters(manga_id):
            recorded = chapter.get("library_path")
            if not chapter.get("downloaded") or not recorded:
                continue
            source = self._recorded_library_path(str(recorded), root)
            destination = self._confined_library_path(
                final_library_path(root, renamed, chapter), root
            )
            if source != destination:
                pairs.append((source, destination))
        return pairs

    def _series_rename_sidecar_pairs(
        self,
        file_pairs: list[tuple[Path, Path]],
        root: Path,
    ) -> list[tuple[Path, Path]]:
        candidates: dict[Path, Path] = {}
        for source, destination in file_pairs:
            for suffix in sorted(MANAGED_ARTWORK_SUFFIXES):
                source_sidecar = self._confined_artwork_sidecar(
                    source.with_suffix(suffix), root
                )
                destination_sidecar = self._confined_artwork_sidecar(
                    destination.with_suffix(suffix), root
                )
                if source_sidecar.exists() and source_sidecar != destination_sidecar:
                    candidates[source_sidecar] = destination_sidecar
            for suffix in sorted(MANAGED_ARTWORK_SUFFIXES):
                source_cover = self._confined_artwork_sidecar(
                    source.parent / f"cover{suffix}", root
                )
                destination_cover = self._confined_artwork_sidecar(
                    destination.parent / f"cover{suffix}", root
                )
                if source_cover.exists() and source_cover != destination_cover:
                    candidates[source_cover] = destination_cover

        pairs = sorted(candidates.items(), key=lambda item: str(item[0]))
        self._preflight_library_file_deletions([source for source, _ in pairs], root)
        for source, destination in pairs:
            if source.is_symlink() or not source.is_file():
                raise UnsafeLibraryPath(f"Unsafe artwork sidecar: {source}")
            if destination.exists():
                raise FileExistsError(
                    f"Refusing to overwrite series artwork: {destination}"
                )
        return pairs

    def _move_series_sidecars(
        self, pairs: list[tuple[Path, Path]]
    ) -> list[tuple[Path, Path]]:
        moved: list[tuple[Path, Path]] = []
        try:
            for source, destination in pairs:
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.exists():
                    raise FileExistsError(
                        f"Series artwork destination appeared: {destination}"
                    )
                source.rename(destination)
                moved.append((source, destination))
            self._fsync_directories({path.parent for pair in moved for path in pair})
            return moved
        except Exception:
            for source, destination in reversed(moved):
                if destination.exists() and not source.exists():
                    source.parent.mkdir(parents=True, exist_ok=True)
                    destination.rename(source)
            self._fsync_directories({path.parent for pair in moved for path in pair})
            raise

    async def refresh_manga(self, manga_id: str, language: str | None = None) -> dict:
        async with self._mutation_lock:
            self.assert_mutations_allowed()
            current = self.database.get_manga(manga_id)
            if current.get("provider") in NO_REMOTE_PROVIDERS:
                # Local and catalogue series have no primary remote: their
                # chapters come from mapped release sources only. Those rows
                # still need their unit settled, and they are the ones that
                # need it most: a source publishing whole tankobon through a
                # chapter-shaped API (XCOMIC's "Volume 1", 299 pages) leaves a
                # book filed as a chapter with no number, which the queue
                # refuses for ever. Leaving early skipped that for every
                # catalogue series - which is nearly all of them.
                reclassified = await self._reconcile_release_units(manga_id)
                return {
                    "manga_id": manga_id,
                    "language": current["preferred_language"],
                    "monitor_mode_before": current["monitor_mode"],
                    "monitor_mode": current["monitor_mode"],
                    "future_monitoring_allowed": False,
                    "future_monitoring_reason": "No download source linked yet",
                    "seen": 0,
                    "new_chapter_ids": [],
                    "reclassified_providers": reclassified,
                }
            provider = self.provider_for(current.get("provider"))
            selected_language = language or current["preferred_language"]
            try:
                remote = await provider.get_manga(manga_id)
            except Exception as exc:
                exc.add_note(PRIMARY_SOURCE_FAILURE_NOTE)
                self.database.record_primary_source_result(
                    manga_id, error=f"{type(exc).__name__}: {exc}"
                )
                raise
            remote["preferred_language"] = selected_language
            self.database.upsert_manga(remote, selected_language)
            try:
                chapters = await provider.list_chapters(manga_id, selected_language)
            except Exception as exc:
                exc.add_note(PRIMARY_SOURCE_FAILURE_NOTE)
                self.database.record_primary_source_result(
                    manga_id, error=f"{type(exc).__name__}: {exc}"
                )
                raise
            self.database.record_primary_source_result(manga_id)
            mode_before = current["monitor_mode"]
            result = await asyncio.to_thread(
                self.database.upsert_chapters,
                manga_id,
                chapters,
                monitor_new=mode_before in FUTURE_MONITOR_MODES,
            )
            reclassified = await self._reconcile_release_units(manga_id)
            if reclassified:
                result = {**result, "reclassified_providers": reclassified}
            allowed, reason = evaluate_future_monitoring(remote, chapters)
            mode_after = mode_before if allowed else terminal_monitor_mode(mode_before)
            self.database.set_future_monitoring_capability(
                manga_id,
                allowed=allowed,
                reason=reason,
                monitor_mode=mode_after if mode_after != mode_before else None,
            )
            return {
                "manga_id": manga_id,
                "language": selected_language,
                "monitor_mode_before": mode_before,
                "monitor_mode": mode_after,
                "future_monitoring_allowed": allowed,
                "future_monitoring_reason": reason,
                **result,
            }

    async def create_manual_download_job(
        self,
        chapter_id: str,
        *,
        replace: bool = False,
        supersedes: str | None = None,
        quality_override: bool = False,
    ) -> dict[str, Any]:
        """Queue one release by hand.

        ``replace`` (or an explicit ``supersedes``) names the file already in
        the library for the same logical chapter; it is deleted only after
        this download has itself passed every gate, never before. Measured
        live: deleting first and refusing the replacement cost a chapter.
        ``quality_override`` waives the length gate for this job.
        """

        async with self._mutation_lock:
            self.assert_mutations_allowed()
            chapter = self.database.get_chapter(chapter_id)
            manga = self.database.get_manga(chapter["manga_id"])
            if chapter["language"] != manga["preferred_language"]:
                raise ValueError(
                    "Chapter language no longer matches the manga language profile"
                )
            if chapter["downloaded"]:
                raise ValueError("Chapter already downloaded")
            if replace and supersedes is None:
                from tankarr.series_unit import is_volume_release

                siblings = self.database.list_chapters(
                    manga["id"], manga["preferred_language"]
                )
                volume_scoped = volume_scoped_numbering(siblings)
                wanted = (
                    ("volume", canonical_number(chapter.get("volume")))
                    if is_volume_release(chapter)
                    else logical_release_key(chapter, volume_scoped=volume_scoped)
                )
                for sibling in siblings:
                    if not sibling.get("downloaded"):
                        continue
                    key = (
                        ("volume", canonical_number(sibling.get("volume")))
                        if is_volume_release(sibling)
                        else logical_release_key(sibling, volume_scoped=volume_scoped)
                    )
                    if key == wanted:
                        supersedes = str(sibling["id"])
                        break
            # An explicit retry is the operator's answer to a failed release:
            # it clears the block that keeps automation away from it.
            self.database.unblock_release(str(chapter["id"]))
            job = self.database.create_job(
                manga["id"],
                chapter["id"],
                manga["preferred_language"],
                force=True,
                origin="manual",
            )
            if supersedes or quality_override:
                job = self.database.annotate_job(
                    int(job["id"]),
                    supersedes=(
                        supersedes
                        if supersedes and supersedes != str(chapter["id"])
                        else None
                    ),
                    quality_override=quality_override or None,
                )
            return job

    # One audit pass measures at most this many library files. The whole
    # sweep costs a header read per page from the library storage, so it is
    # spread over maintenance cycles instead of stalling one.
    QUALITY_AUDIT_PER_PASS = 800
    # Where the unrestricted sweep resumes next pass: without it, every pass
    # re-walked the library alphabetically from the top, so a few large
    # early-alphabet series (or, once those cleared, whichever came right
    # after) always spent the whole budget before reaching the rest, and
    # late-alphabet series went unmeasured for the library's entire life.
    QUALITY_AUDIT_CURSOR_SETTING = "__runtime_quality_audit_cursor"

    def _settle_degraded_download(
        self,
        job: dict[str, Any],
        manga: dict[str, Any],
        chapter: dict[str, Any],
        quality: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Decide a length refusal: waived, corroborated, or recorded and kept.

        Shape (un-sliced strips) is never waived: nobody can read them. A
        length shortfall is waived when the operator said "download anyway",
        or when another source's copy of the same chapter measures the same
        - two sources agreeing is the chapter being short, not two fragments.
        A refusal that stands is recorded with its measurement, so the next
        source's copy can be compared with it.
        """

        manga_id = str(manga["id"])
        chapter_id = str(chapter["id"])
        shape_refusal = "taller than they are wide" in str(quality.get("reason") or "")
        if shape_refusal:
            self.database.record_page_quality(
                manga_id, chapter_id, verdict=page_quality.REFUSED, assessment=quality
            )
            return None
        if bool(job.get("quality_override")):
            return {
                **quality,
                "verdict": page_quality.OK,
                "reason": "short for this series; accepted by the operator (download anyway)",
            }
        witness = self._length_corroborated(manga_id, chapter, quality)
        if witness is not None:
            return {
                **quality,
                "verdict": page_quality.OK,
                "reason": f"short for this series; {witness} carries the same length",
            }
        self.database.record_page_quality(
            manga_id, chapter_id, verdict=page_quality.REFUSED, assessment=quality
        )
        return None

    def _length_corroborated(
        self, manga_id: str, chapter: dict[str, Any], quality: dict[str, Any]
    ) -> str | None:
        """The other source whose copy of this chapter has the same length."""

        height = float(quality.get("normalized_height") or 0)
        if height <= 0:
            return None
        measured = self.database.page_quality(manga_id)

        def source_of(release: dict[str, Any]) -> str:
            # The named source, not the aggregator: two Suwayomi extensions
            # are two sources, and a witness must be another one.
            return str(release.get("source_name") or _release_source_key(release))

        own_source = source_of(chapter)
        try:
            siblings = self.database.slot_release_ids(manga_id, str(chapter["id"]))
        except KeyError:
            return None
        for sibling_id in siblings:
            if sibling_id == str(chapter["id"]):
                continue
            record = measured.get(sibling_id)
            if not record:
                continue
            try:
                sibling = self.database.get_chapter(sibling_id)
            except KeyError:
                continue
            if source_of(sibling) == own_source:
                continue
            other = float(record.get("normalized_height") or 0)
            if other <= 0:
                continue
            if (
                abs(other - height) / max(other, height)
                <= page_quality.CORROBORATION_TOLERANCE
            ):
                return str(
                    sibling.get("source_name")
                    or sibling.get("provider")
                    or "another source"
                )
        return None

    def _assess_downloaded_pages(
        self, manga_id: str, pages: list[Path], chapter: object = None
    ) -> dict[str, Any]:
        """Compare freshly downloaded pages with this series' own chapters."""

        measurement = page_quality.measure_pages(pages)
        return page_quality.assess(
            measurement,
            self._page_quality_baseline(manga_id),
            compare_length=page_quality.is_measurable(chapter),
        )

    def _chapter_numbers(self, manga_id: str) -> dict[str, object]:
        manga = self.database.get_manga(manga_id)
        return {
            str(release["id"]): release.get("chapter")
            for release in self.database.list_chapters(
                manga_id, str(manga["preferred_language"])
            )
        }

    def _page_quality_baseline(
        self,
        manga_id: str,
        measured: dict[str, Any] | None = None,
        numbers: dict[str, object] | None = None,
    ) -> dict[str, Any] | None:
        """The shape of this series' whole chapters, extras excluded.

        Extras are left out of the baseline as well as out of the judgement:
        a series with a run of two-page omake would otherwise talk its own
        floor down until nothing could ever be too short.
        """

        measured = (
            self.database.page_quality(manga_id) if measured is None else measured
        )
        numbers = self._chapter_numbers(manga_id) if numbers is None else numbers
        return page_quality.baseline_from(
            record
            for chapter_id, record in measured.items()
            if page_quality.is_measurable(numbers.get(chapter_id))
            and str(record.get("verdict")) != page_quality.REFUSED
        )

    async def audit_library_page_quality(
        self, *, limit: int | None = None, manga_ids: Iterable[str] | None = None
    ) -> dict[str, Any]:
        """Measure imported chapters, and report the ones that carry too little.

        The gate at download time can only judge a chapter against chapters it
        has already measured, so a library that predates it has no baseline at
        all. This walks it, newest measurement gaps first, and builds one.

        It never deletes and never re-queues on its own: it records a verdict.
        Acting on a verdict is ``recover_degraded_chapters``' decision, and
        only when another source can actually be asked for the chapter.

        ``manga_ids`` restricts the walk to named series and ignores the
        resume cursor below - crosses the whole library a few hundred files
        at a time, so a series near the end of it is days away; an operator
        looking at one unreadable chapter needs an answer about that series
        now.

        The unrestricted walk resumes after wherever the previous pass left
        off (``QUALITY_AUDIT_CURSOR_SETTING``) instead of always starting
        from the top of the alphabet, so a library too big for one pass
        still reaches every series within a bounded number of passes.
        """

        wanted_ids = {str(item) for item in manga_ids} if manga_ids else None
        measured = 0
        degraded: list[dict[str, Any]] = []
        budget = int(limit or self.QUALITY_AUDIT_PER_PASS)
        try:
            root = self._library_root()
        except LibraryUnavailable:
            return {"measured": 0, "degraded": [], "library_unavailable": True}
        manga_list = self.database.list_manga()
        if wanted_ids is None:
            manga_list = self._rotate_from_quality_audit_cursor(manga_list)
        last_visited: str | None = None
        for manga in manga_list:
            if measured >= budget:
                break
            manga_id = str(manga["id"])
            if wanted_ids is not None and manga_id not in wanted_ids:
                continue
            last_visited = manga_id
            known = self.database.page_quality(manga_id)
            releases = [
                release
                for release in self.database.list_chapters(
                    manga_id, str(manga["preferred_language"])
                )
                if release.get("downloaded") and release.get("library_path")
            ]
            stale = [
                release
                for release in releases
                if str(release["id"]) not in known
                or (
                    known[str(release["id"])].get("library_sha256")
                    not in {"", str(release.get("library_sha256") or "")}
                )
            ]
            for release in stale:
                if measured >= budget:
                    break
                try:
                    path = self._recorded_library_path(
                        str(release["library_path"]), root
                    )
                except (UnsafeLibraryPath, ValueError):
                    continue
                if not path.exists():
                    continue
                try:
                    measurement = await asyncio.to_thread(
                        page_quality.measure_archive, path
                    )
                except (OSError, zipfile.BadZipFile):
                    continue
                measured += 1
                self.database.record_page_quality(
                    manga_id,
                    str(release["id"]),
                    verdict=page_quality.UNKNOWN,
                    assessment={**measurement, "reason": "measured by the audit"},
                    library_sha256=str(release.get("library_sha256") or ""),
                )
            # The verdict is only meaningful once the series has a baseline,
            # so it is decided in a second pass over everything measured -
            # including the rows this pass just wrote.
            known = self.database.page_quality(manga_id)
            numbers = {
                str(release["id"]): release.get("chapter") for release in releases
            }
            baseline = self._page_quality_baseline(manga_id, known, numbers)
            if baseline is None:
                continue
            # The parts of a split chapter are judged together, below.
            groups = page_quality.split_part_groups(releases)
            in_group = {str(part["id"]) for parts in groups.values() for part in parts}
            on_disk = {str(release["id"]) for release in releases}
            for chapter_id, record in known.items():
                if chapter_id in in_group:
                    continue
                if chapter_id not in on_disk:
                    continue  # a refused download, or a file since replaced
                # An extra has no expected length, but a page too tall to fit
                # a screen is unreadable whatever the chapter is, so it is
                # still judged on shape.
                verdict = page_quality.assess(
                    record,
                    baseline,
                    compare_length=page_quality.is_measurable(numbers.get(chapter_id)),
                )
                if verdict["verdict"] == str(record.get("verdict") or ""):
                    continue
                self.database.record_page_quality(
                    manga_id,
                    chapter_id,
                    verdict=str(verdict["verdict"]),
                    assessment=verdict,
                    library_sha256=str(record.get("library_sha256") or ""),
                )
                if verdict["verdict"] == page_quality.DEGRADED:
                    degraded.append(
                        {
                            "manga_id": manga_id,
                            "title": manga.get("title"),
                            "chapter_id": chapter_id,
                            "reason": verdict["reason"],
                        }
                    )
            degraded.extend(
                await self._judge_split_parts(manga, groups, known, baseline, releases)
            )
        if wanted_ids is None and last_visited is not None:
            self.database.save_setting(self.QUALITY_AUDIT_CURSOR_SETTING, last_visited)
        return {"measured": measured, "degraded": degraded}

    def _rotate_from_quality_audit_cursor(
        self, manga_list: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Start the walk after the series the previous pass last visited.

        Wraps to the beginning once the cursor is at or past the end, so the
        walk is a ring: every series comes up again eventually, and none is
        favoured just for sorting first.
        """

        cursor = self.database.get_setting_overrides().get(
            self.QUALITY_AUDIT_CURSOR_SETTING
        )
        if not cursor:
            return manga_list
        ids = [str(item["id"]) for item in manga_list]
        try:
            index = ids.index(cursor)
        except ValueError:
            return manga_list
        return manga_list[index + 1 :] + manga_list[: index + 1]

    async def _judge_split_parts(
        self,
        manga: dict[str, Any],
        groups: dict[str, list[dict[str, Any]]],
        known: dict[str, dict[str, Any]],
        baseline: dict[str, Any],
        releases: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Read the parts of each split chapter as one archive and judge that.

        A split whose parts add up to a chapter is a valid split and is kept.
        One that does not is a fragment however many files it comes in: every
        part is marked with the chapter it belongs to, so the recovery asks
        another source for the whole chapter and, once that is on disk, the
        parts Tankarr itself condemned are retired.
        """

        manga_id = str(manga["id"])
        degraded: list[dict[str, Any]] = []
        wholes_on_disk = {
            canonical_number(release.get("chapter"))
            for release in releases
            if release.get("downloaded")
            and page_quality.is_measurable(release.get("chapter"))
        }
        for whole, parts in groups.items():
            records = [known.get(str(part["id"])) for part in parts]
            if any(record is None for record in records):
                continue  # not every part is measured yet
            if whole in wholes_on_disk:
                condemned = [
                    str(part["id"])
                    for part, record in zip(parts, records, strict=True)
                    if str((record or {}).get("whole_chapter") or "") == whole
                ]
                if len(condemned) != len(parts):
                    # The whole chapter is in and these parts were never
                    # condemned: they are surplus, not a problem, and must
                    # not sit at "unknown" forever.
                    for part, record in zip(parts, records, strict=True):
                        assert record is not None
                        reason = (
                            f"chapter {whole} is on disk whole; this part is surplus"
                        )
                        if str(record.get("verdict")) == page_quality.OK and (
                            str(record.get("reason") or "") == reason
                        ):
                            continue
                        self.database.record_page_quality(
                            manga_id,
                            str(part["id"]),
                            verdict=page_quality.OK,
                            assessment={
                                **record,
                                "reason": reason,
                                "whole_chapter": "",
                            },
                            library_sha256=str(record.get("library_sha256") or ""),
                        )
                    continue
                if len(condemned) == len(parts):
                    try:
                        await self.delete_chapter_files(manga_id, condemned)
                    except Exception as exc:  # noqa: BLE001 - next pass retries
                        logger.info(
                            "Could not retire the parts of chapter %s of %s: %s",
                            whole,
                            manga.get("title"),
                            exc,
                        )
                        continue
                    self.database.forget_page_quality(condemned)
                    logger.info(
                        "Retired %s parts of chapter %s of %s: the whole chapter is in",
                        len(condemned),
                        whole,
                        manga.get("title"),
                    )
                continue
            verdict = page_quality.assess(
                page_quality.combine([record for record in records if record]),
                baseline,
                compare_length=True,
            )
            short = verdict["verdict"] == page_quality.DEGRADED
            total = len(parts)
            for index, (part, record) in enumerate(zip(parts, records, strict=True), 1):
                assert record is not None
                if short:
                    reason = (
                        f"part {index}/{total} of chapter {whole}: "
                        f"together the parts still fall short ({verdict['reason']})"
                    )
                else:
                    reason = (
                        f"part {index}/{total} of chapter {whole}; "
                        "together the parts carry the whole chapter"
                    )
                new_verdict = page_quality.DEGRADED if short else page_quality.OK
                if new_verdict == str(record.get("verdict") or "") and reason == str(
                    record.get("reason") or ""
                ):
                    continue
                self.database.record_page_quality(
                    manga_id,
                    str(part["id"]),
                    verdict=new_verdict,
                    assessment={
                        **record,
                        "reason": reason,
                        "whole_chapter": whole if short else "",
                    },
                    library_sha256=str(record.get("library_sha256") or ""),
                )
                if short:
                    degraded.append(
                        {
                            "manga_id": manga_id,
                            "title": manga.get("title"),
                            "chapter_id": str(part["id"]),
                            "reason": reason,
                        }
                    )
        return degraded

    def surplus_beyond_official_edition(self, manga_id: str) -> dict[str, Any]:
        """Library files numbered past the official edition, with the warrant.

        The verdict is returned whole - frontier, how many official chapters
        placed it, and why it is or is not solid enough to act on - because
        this is the evidence behind deleting a file, and it has to be
        inspectable before and after the fact.
        """

        manga = self.database.get_manga(manga_id)
        metadata_row = self.database.get_series_metadata(manga_id)
        metadata = (metadata_row or {}).get("data") or {}
        releases = self.database.list_chapters(
            manga_id, str(manga["preferred_language"])
        )
        hosts = official_hosts(
            metadata.get("official_links"),
            language=str(manga.get("preferred_language") or ""),
        )
        verdict = official_chapter_frontier(releases, hosts)
        return {
            "manga_id": manga_id,
            "title": manga.get("title"),
            "frontier": verdict.label,
            "anchors": verdict.anchors,
            "enforceable": verdict.enforceable,
            "reason": verdict.reason,
            "surplus": surplus_downloads(releases, verdict),
        }

    async def align_with_official_edition(
        self, manga_id: str, *, dry_run: bool = False
    ) -> dict[str, Any]:
        """Remove library files the official edition has no chapter for.

        Scanlators number from the original edition and run ahead of the
        translation, so a library that follows them drifts away from the
        publication it is supposed to mirror. Those files are not early
        chapters of this edition; they are chapters of another one.

        This deletes, so it only ever runs on an *enforceable* frontier (see
        ``official_numbering``): enough official chapters, an unbroken tail,
        and near-complete coverage of their own range. Deletion goes through
        the same staged quarantine as every other removal, and the download
        gate already refuses to re-acquire anything past the frontier, so the
        alignment converges instead of looping.
        """

        report = self.surplus_beyond_official_edition(manga_id)
        surplus = report["surplus"]
        if not surplus or dry_run:
            return {**report, "deleted": 0, "errors": [], "dry_run": dry_run}
        outcome = await self.delete_chapter_files(
            manga_id, [str(release["id"]) for release in surplus]
        )
        if outcome["deleted"]:
            logger.info(
                "Aligned %s with its official edition: removed %s chapter(s) past %s",
                report["title"],
                outcome["deleted"],
                report["frontier"],
            )
        return {
            **report,
            "deleted": int(outcome["deleted"]),
            "errors": list(outcome["errors"]),
            "dry_run": False,
        }

    async def recover_degraded_chapters(
        self, manga_id: str, *, limit: int = 25
    ) -> dict[str, Any]:
        """Replace unreadable imports with the same chapter from another source.

        Blocking the degraded release first is what makes this converge: the
        ranking then has to choose something else, and a source that keeps
        serving fragments stops being asked for this work at all. When no
        other source carries the chapter there is nothing to replace it with,
        and an unreadable file still beats a hole, so it is kept and left
        visible with its verdict.
        """

        requeued: list[dict[str, Any]] = []
        kept: list[dict[str, Any]] = []
        degraded = self.degraded_chapters(manga_id)
        wholes_handled: set[str] = set()
        for record in degraded[:limit]:
            chapter_id = str(record["chapter_id"])
            try:
                chapter = self.database.get_chapter(chapter_id)
            except KeyError:
                self.database.forget_page_quality([chapter_id])
                continue
            if not chapter.get("downloaded"):
                continue
            whole = str(record.get("whole_chapter") or "")
            if whole:
                # Split parts are one problem with one answer: the whole
                # chapter from a source that did not cut it.
                if whole in wholes_handled:
                    continue
                wholes_handled.add(whole)
                outcome = await self._replace_split_parts(
                    manga_id,
                    whole,
                    [
                        item
                        for item in degraded
                        if str(item.get("whole_chapter") or "") == whole
                    ],
                )
                (requeued if outcome.get("job_id") else kept).append(outcome)
                continue
            self.database.block_release(
                chapter_id, reason=f"Unreadable pages: {record.get('reason') or ''}"
            )
            self.database.record_source_failure(
                manga_id,
                _release_source_key(chapter),
                reason="served far less page than the work's chapters carry",
            )
            alternative = self._best_replacement_for(manga_id, chapter_id)
            if alternative is None:
                self.database.unblock_release(chapter_id)
                kept.append(
                    {
                        "chapter_id": chapter_id,
                        "chapter": chapter.get("chapter"),
                        "reason": "no other source carries this chapter",
                    }
                )
                continue
            try:
                # The degraded file stays until its replacement has passed
                # every gate: the worker deletes it at publish time, not now.
                job = await self.create_manual_download_job(
                    str(alternative["id"]), supersedes=chapter_id
                )
            except Exception as exc:  # noqa: BLE001 - the next pass retries
                logger.info(
                    "Could not replace degraded chapter %s: %s", chapter_id, exc
                )
                continue
            requeued.append(
                {
                    "chapter_id": chapter_id,
                    "chapter": chapter.get("chapter"),
                    "replacement": str(alternative["id"]),
                    "source": alternative.get("source_name")
                    or alternative.get("provider"),
                    "job_id": job.get("id"),
                }
            )
        return {"requeued": requeued, "kept": kept}

    async def _replace_split_parts(
        self, manga_id: str, whole: str, parts: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Queue the whole chapter for a set of fragments that never add up."""

        manga = self.database.get_manga(manga_id)
        part_ids = [str(item["chapter_id"]) for item in parts]
        chapters = []
        for part_id in part_ids:
            try:
                chapters.append(self.database.get_chapter(part_id))
            except KeyError:
                continue
        cut_by = {_release_source_key(chapter) for chapter in chapters}
        blocked = self.database.blocked_releases(manga_id)
        candidates = [
            release
            for release in self.database.list_chapters(
                manga_id, str(manga["preferred_language"])
            )
            if canonical_number(release.get("chapter")) == whole
            and not release.get("downloaded")
            and release.get("monitored")
            and str(release["id"]) not in blocked
            and _release_source_key(release) not in cut_by
        ]
        alternative = self._rank_replacement(manga_id, manga, candidates)
        label = f"{whole} ({len(part_ids)} parts)"
        if alternative is None:
            return {
                "chapter_id": part_ids[0] if part_ids else "",
                "chapter": label,
                "reason": "no other source carries the whole chapter",
            }
        for chapter in chapters:
            self.database.block_release(
                str(chapter["id"]),
                reason=f"Unreadable pages: fragment of chapter {whole}",
            )
            self.database.record_source_failure(
                manga_id,
                _release_source_key(chapter),
                reason="served a chapter in parts that do not add up to it",
            )
        try:
            job = await self.create_manual_download_job(str(alternative["id"]))
        except Exception as exc:  # noqa: BLE001 - the next pass retries
            logger.info("Could not queue whole chapter %s: %s", whole, exc)
            return {
                "chapter_id": part_ids[0] if part_ids else "",
                "chapter": label,
                "reason": f"could not queue the whole chapter: {exc}"[:200],
            }
        return {
            "chapter_id": part_ids[0] if part_ids else "",
            "chapter": label,
            "replacement": str(alternative["id"]),
            "source": alternative.get("source_name") or alternative.get("provider"),
            "job_id": job.get("id"),
        }

    def _rank_replacement(
        self,
        manga_id: str,
        manga: dict[str, Any],
        candidates: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        if not candidates:
            return None
        metadata_row = self.database.get_series_metadata(manga_id)
        metadata = (metadata_row or {}).get("data") or {}
        hosts = official_hosts(
            metadata.get("official_links"),
            language=str(manga.get("preferred_language") or ""),
        )
        demoted = (
            self.database.demoted_sources(manga_id)
            | self.database.global_demoted_sources()
        )
        return self.database.source_ranking.best(
            candidates,
            official_hosts=hosts,
            demoted_sources=demoted,
            health=self.database.source_health_components(),
        )

    def _best_replacement_for(
        self, manga_id: str, chapter_id: str
    ) -> dict[str, Any] | None:
        """The best other release of the same slot, ranked as anything else.

        ``preferred_download_candidates`` cannot answer this: a slot holding a
        downloaded file is satisfied by definition, so it returns nothing for
        exactly the slots that need replacing. The ranking itself has no such
        assumption, so the siblings go straight to it.
        """

        manga = self.database.get_manga(manga_id)
        metadata_row = self.database.get_series_metadata(manga_id)
        metadata = (metadata_row or {}).get("data") or {}
        hosts = official_hosts(
            metadata.get("official_links"),
            language=str(manga.get("preferred_language") or ""),
        )
        blocked = self.database.blocked_releases(manga_id)
        demoted = (
            self.database.demoted_sources(manga_id)
            | self.database.global_demoted_sources()
        )
        by_id = {
            str(release["id"]): release
            for release in self.database.list_chapters(
                manga_id, str(manga["preferred_language"])
            )
        }
        siblings = [
            release
            for release_id in self.database.slot_release_ids(manga_id, chapter_id)
            if release_id != chapter_id
            and release_id not in blocked
            and (release := by_id.get(release_id)) is not None
            and not release.get("downloaded")
            and release.get("monitored")
        ]
        if not siblings:
            return None
        return self.database.source_ranking.best(
            siblings,
            official_hosts=hosts,
            demoted_sources=demoted,
            health=self.database.source_health_components(),
        )

    def degraded_chapters(self, manga_id: str) -> list[dict[str, Any]]:
        """Imported chapters this series' own shape says are unreadable."""

        measured = self.database.page_quality(manga_id)
        return [
            record
            for record in measured.values()
            if str(record.get("verdict")) == page_quality.DEGRADED
        ]

    async def create_missing_download_jobs(
        self, manga_id: str, chapter_ids: list[str] | None = None
    ) -> list[dict[str, Any]]:
        """Select candidates and create jobs atomically against delete operations."""

        async with self._mutation_lock:
            self.assert_mutations_allowed()
            self._rerank_queued_jobs_locked(manga_id)
            manga = self.database.get_manga(manga_id)
            covered = self._chapters_covered_by_books(manga)
            jobs: list[dict[str, Any]] = []
            for chapter in self.database.preferred_download_candidates(
                manga_id, chapter_ids
            ):
                # Persisted indexer offers are visible as releases, but only
                # registered download providers can turn one into a job.
                # A torrent/usenet offer must go through its own acquisition
                # path after the book match has been verified.
                if not self.can_download_release(chapter):
                    continue
                # A fully blocked slot stays visible in Wanted but never
                # re-queues automatically; History retry unblocks explicitly.
                if chapter.get("blocked"):
                    continue
                # A chapter inside a book the library already holds is not
                # missing: fetching it again only builds a second edition.
                if covered and _chapter_int(chapter) in covered:
                    continue
                try:
                    jobs.append(
                        self.database.create_job(
                            manga_id,
                            chapter["id"],
                            manga["preferred_language"],
                            origin="monitor",
                        )
                    )
                except ValueError as exc:
                    # One release the queue refuses (unresolved numbering)
                    # must not cost the series every other slot.
                    logger.warning(
                        "Skipped release %s of %s: %s", chapter["id"], manga_id, exc
                    )
            jobs.extend(self._observed_special_download_jobs_locked(manga, chapter_ids))
            return jobs

    def _observed_special_download_jobs_locked(
        self, manga: dict[str, Any], chapter_ids: list[str] | None
    ) -> list[dict[str, Any]]:
        mode = str(manga.get("monitor_mode") or "none")
        if mode not in BACKLOG_MONITOR_MODES and not (mode == "future" and chapter_ids):
            return []
        manga_id = str(manga["id"])
        language = str(manga["preferred_language"])
        candidates = self._offered_optional_special_candidates_locked(
            manga, chapter_ids
        )
        if not candidates:
            return []
        releases = self.database.list_chapters(manga_id, language)
        scoped = volume_scoped_numbering(releases)
        by_id = {str(release["id"]): release for release in releases}
        active = {
            logical_release_key(by_id[str(job["chapter_id"])], volume_scoped=scoped)
            for job in self.database.list_jobs(
                limit=500,
                statuses=("queued", "running", "downloading", "packaging", "importing"),
                manga_id=manga_id,
            )
            if str(job["chapter_id"]) in by_id
        }
        jobs: list[dict[str, Any]] = []
        for key, chosen in candidates.items():
            if key in active:
                continue
            try:
                jobs.append(
                    self.database.create_job(
                        manga_id, str(chosen["id"]), language, origin="monitor"
                    )
                )
            except (ValueError, ReleaseBlocked) as exc:
                logger.info("Skipped optional special %s: %s", chosen["id"], exc)
        return jobs

    def _offered_optional_special_candidates_locked(
        self,
        manga: dict[str, Any],
        chapter_ids: list[str] | None = None,
        *,
        include_queued_future: bool = False,
    ) -> dict[tuple[str, str], dict[str, Any]]:
        """Rank real extra offers; absence never creates a catalogue slot."""

        if manga.get("library_status_override") == "up_to_date":
            return {}
        mode = str(manga.get("monitor_mode") or "none")
        if mode not in BACKLOG_MONITOR_MODES and mode != "future":
            return {}
        manga_id = str(manga["id"])
        language = str(manga["preferred_language"])
        releases = self.database.list_chapters(manga_id, language)

        def is_optional_offer(release: dict[str, Any]) -> bool:
            label = canonical_number(release.get("chapter"))
            if is_special_title(str(release.get("title") or "")):
                return True
            if label is None:
                return False
            try:
                return Decimal(label) % 1 == Decimal("0.5")
            except InvalidOperation:
                return False

        if not any(is_optional_offer(release) for release in releases):
            return {}
        metadata_row = self.database.get_series_metadata(manga_id)
        metadata = (metadata_row or {}).get("data") or {}
        index = build_chapter_index(
            manga,
            metadata,
            releases,
            chapter_map=self.database.chapter_map(manga_id),
        )
        # Whole books already include their side pages.
        if index["unit"] != "chapter":
            return {}
        canonical_ids = {
            str(release["id"])
            for slot in index["slots"]
            if slot["expected"]
            for release in slot.get("releases") or []
        }
        scoped = volume_scoped_numbering(releases)
        owned = {
            logical_release_key(release, volume_scoped=scoped)
            for release in releases
            if release.get("downloaded") and is_optional_offer(release)
        }
        requested = set(chapter_ids or [])
        blocked = self.database.blocked_releases(manga_id)
        grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for release in releases:
            identifier = str(release["id"])
            if (
                identifier in canonical_ids
                or release.get("downloaded")
                or not is_optional_offer(release)
                or not release.get("monitored")
                or is_unpublished(release)
                or identifier in blocked
                or not self.can_download_release(release)
                or (
                    mode == "future"
                    and not include_queued_future
                    and identifier not in requested
                )
            ):
                continue
            key = logical_release_key(release, volume_scoped=scoped)
            if key not in owned:
                grouped[key].append(release)
        hosts = official_hosts(metadata.get("official_links"), language=language)
        demoted = self.database.demoted_sources(manga_id)
        health = self.database.source_health_components()
        chosen_releases: dict[tuple[str, str], dict[str, Any]] = {}
        for key, candidates in grouped.items():
            chosen = self.database.source_ranking.best(
                candidates,
                official_hosts=hosts,
                demoted_sources=demoted,
                health=health,
            )
            if chosen is not None:
                chosen_releases[key] = chosen
        return chosen_releases

    def _chapters_covered_by_books(self, manga: dict[str, Any]) -> set[int]:
        """Integer chapters that owned books cover (map or declared edition).

        The candidate query knows the catalogue map; it does not know the
        declared edition split, so the canonical index decides here.
        """

        manga_id = str(manga["id"])
        if not effective_edition_book_count(manga):
            return set()
        metadata_row = self.database.get_series_metadata(manga_id)
        index = build_chapter_index(
            manga,
            (metadata_row or {}).get("data") or {},
            self.database.list_chapters(manga_id, str(manga["preferred_language"])),
            chapter_map=self.database.chapter_map(manga_id),
        )
        covered: set[int] = set()
        for slot in index.get("slots") or []:
            if not slot.get("covered_by_volume") or slot.get("downloaded"):
                continue
            number = _chapter_int(slot)
            if number is not None:
                covered.add(number)
        return covered

    def can_download_release(self, chapter: dict[str, Any]) -> bool:
        """Whether this process has a provider that can fetch the release."""

        return (
            not self._strict_provider_registry
            or str(chapter.get("provider") or "") in self.providers
        )

    def _rerank_queued_jobs_locked(self, manga_id: str) -> int:
        queued = self.database.queued_job_targets(manga_id)
        if not queued:
            return 0
        replacements: dict[int, str] = {}
        manual_job_ids = self.database.manual_queued_job_ids(manga_id)
        eligible_job_ids: set[int] = set(manual_job_ids)
        for candidate in self.database.preferred_missing_releases(manga_id):
            if not self.can_download_release(candidate):
                continue
            job_id = candidate.get("queue_job_id")
            if candidate.get("queue_status") != "queued" or job_id is None:
                continue
            eligible_job_ids.add(int(job_id))
            if int(job_id) in manual_job_ids or candidate.get("blocked"):
                continue
            selected_id = str(candidate["id"])
            if queued.get(int(job_id)) != selected_id:
                replacements[int(job_id)] = selected_id
        manga = self.database.get_manga(manga_id)
        optional = self._offered_optional_special_candidates_locked(
            manga, include_queued_future=True
        )
        if optional:
            releases = self.database.list_chapters(
                manga_id, str(manga["preferred_language"])
            )
            scoped = volume_scoped_numbering(releases)
            by_id = {str(release["id"]): release for release in releases}
            for job_id, release_id in queued.items():
                release = by_id.get(release_id)
                if release is None or job_id in manual_job_ids:
                    continue
                key = logical_release_key(release, volume_scoped=scoped)
                if chosen := optional.get(key):
                    eligible_job_ids.add(job_id)
                    selected_id = str(chosen["id"])
                    if release_id != selected_id:
                        replacements[job_id] = selected_id
        changed = self.database.retarget_queued_jobs(manga_id, replacements)
        for job_id in sorted(set(queued) - eligible_job_ids):
            try:
                self.database.cancel_job(job_id)
            except (KeyError, ValueError):
                continue
            changed += 1
        return changed

    def _queued_job_is_acquisition_allowed(
        self,
        manga_id: str,
        chapter_id: str,
        job_id: int,
        *,
        origin: object,
    ) -> bool:
        if str(origin or "automatic").casefold() == "manual":
            return True
        if any(
            self.can_download_release(candidate)
            and candidate.get("queue_status") == "queued"
            and int(candidate.get("queue_job_id") or 0) == job_id
            for candidate in self.database.preferred_missing_releases(
                manga_id, [chapter_id]
            )
        ):
            return True
        manga = self.database.get_manga(manga_id)
        return any(
            str(candidate["id"]) == chapter_id
            for candidate in self._offered_optional_special_candidates_locked(
                manga, include_queued_future=True
            ).values()
        )

    async def rerank_queued_jobs(self, manga_id: str) -> int:
        """Refresh queued release choices without interrupting active work."""

        async with self._mutation_lock:
            self.assert_mutations_allowed()
            return self._rerank_queued_jobs_locked(manga_id)

    # Exception names that indicate the source infrastructure failed, not the
    # release itself: retries were already exhausted at the HTTP layer, and a
    # rebooted or briefly unreachable engine must not condemn a good release.
    TRANSIENT_RETRIES = 3
    TRANSIENT_PAUSE_SECONDS = 30.0

    def _transient_attempt(self, job_id: int) -> int:
        """Read the durable retry ledger, independent of display messages."""
        try:
            return int(self.database.get_job(job_id).get("retry_count") or 0) + 1
        except KeyError:
            return 1

    TRANSIENT_FAILURE_NAMES = frozenset(
        {
            "ProviderUnavailableError",
            "ConnectError",
            "ConnectTimeout",
            "ReadTimeout",
            "ReadError",
            "WriteError",
            "RemoteProtocolError",
            "PoolTimeout",
            "TimeoutException",
            "ProxyError",
        }
    )

    @staticmethod
    def _within_book_count(number: object, volume_count: int | None) -> bool:
        """Whether a book number can belong to an edition of this many books."""

        try:
            value = int(str(number))
        except (TypeError, ValueError):
            return False
        return value >= 1 and (not volume_count or value <= int(volume_count))

    async def _reconcile_release_units(self, manga_id: str) -> list[str]:
        """Reclassify sources that number whole volumes as chapters.

        Evidence comes from the catalogue counts, the chapter map, and the
        page counts of files already on disk. Must run under the mutation lock.
        """

        manga = self.database.get_manga(manga_id, include_logical_counts=False)
        metadata_row = self.database.get_series_metadata(manga_id)
        canonical = (metadata_row or {}).get("data") or {}
        releases = self.database.list_all_chapters(manga_id)
        restored = self.database.restore_reclassified_chapters(
            misclassified_explicit_chapters(
                manga,
                canonical,
                releases,
            )
        )
        if restored:
            logger.info(
                "Restored %s explicit chapters misclassified as books for %s",
                restored,
                manga_id,
            )
            releases = self.database.list_all_chapters(manga_id)
        repaired = self.database.repair_reclassified_releases(manga_id)
        if repaired:
            logger.info(
                "Repaired %s reclassified volume releases of %s", repaired, manga_id
            )
        metadata_hint = canonical
        try:
            hint_volumes = int(metadata_hint.get("volume_count") or 0) or None
            hint_chapters = int(metadata_hint.get("chapter_count") or 0) or None
        except (TypeError, ValueError):
            hint_volumes = hint_chapters = None
        as_books = self.database.reclassify_mapped_ranges_as_volumes(
            manga_id, volume_count=hint_volumes, chapter_total=hint_chapters
        )
        if as_books:
            logger.info(
                "Chapter-range files of %s are the mapped volumes %s",
                manga_id,
                ", ".join(as_books),
            )
            repaired_ranges = True
        else:
            repaired_ranges = False
        volume_count = canonical.get("volume_count")
        chapter_total = canonical.get("chapter_count") or integer_chapter_total(
            self.database.chapter_map(manga_id)
        )
        try:
            volume_count = int(volume_count) if volume_count else None
            chapter_total = int(chapter_total) if chapter_total else None
        except (TypeError, ValueError):
            return []
        if not volume_count or not chapter_total:
            return ["mapped-ranges"] if repaired_ranges else []
        if restored:
            # Webtoon tiles can be longer than a printed book. Once their
            # explicit chapter titles repaired the unit, page count must not
            # immediately convert them back in the same refresh.
            await self._relabel_reclassified_books(manga_id)
            return [f"explicit-chapters:{restored}"]
        releases = self.database.list_all_chapters(manga_id)
        root = self._library_root()
        page_counts: dict[str, list[int]] = {}
        sampled: dict[str, int] = {}
        for release in releases:
            source = release_source(release)
            path = release.get("library_path")
            if (
                not release.get("downloaded")
                or not path
                or str(release.get("release_unit") or "chapter") != "chapter"
                or str(release.get("volume") or "").strip()
                or sampled.get(source, 0) >= 4
            ):
                continue
            try:
                archive = self._recorded_library_path(str(path), root)
                pages = await asyncio.to_thread(count_archive_pages, archive)
            except Exception:  # noqa: BLE001 - evidence is optional
                continue
            sampled[source] = sampled.get(source, 0) + 1
            page_counts.setdefault(source, []).append(pages)
        verdicts = volume_numbered_providers(
            releases,
            volume_count=volume_count,
            chapter_total=chapter_total,
            page_counts=page_counts,
        )
        reclassified: list[str] = []
        # Page counts are the strongest evidence: a 450-page "Ch. 1" is a book.
        from tankarr.release_kind import classify_release

        booked = 0
        for release in releases:
            if str(release.get("release_unit") or "chapter") == "volume":
                continue
            # The page threshold distinguishes most books from chapters, but
            # long-scroll episodes and unusually long printed chapters can
            # exceed it.  Reuse the same explicit-title evidence that repairs
            # persisted mistakes so a later refresh cannot classify the row
            # as a book again.
            explicit_chapter = misclassified_explicit_chapters(
                manga,
                canonical,
                [{**release, "release_unit": "volume"}],
            )
            if explicit_chapter:
                continue
            kind = classify_release(
                title=str(release.get("title") or release.get("chapter") or ""),
                pages=release.get("pages"),
            )
            if kind.kind != "volume" or "pages" not in " ".join(kind.evidence):
                continue
            number = kind.volume
            if number is None and volume_count == 1:
                number = "1"
            if number is None:
                continue
            # "Chapter 235" of a work bound in ten books is not book 235. A
            # number past the edition's last is the chapter's own, misread.
            if not self._within_book_count(number, volume_count):
                continue
            if self.database.set_release_book(str(release["id"]), str(number)):
                booked += 1
        if booked:
            reclassified.append(f"pages:{booked}")
        for verdict in verdicts:
            changed = self.database.reclassify_provider_releases_as_volumes(
                manga_id, verdict.provider, release_ids=verdict.release_ids
            )
            if changed:
                reclassified.append(verdict.provider)
                logger.info(
                    "Reclassified %s releases of %s as volumes: %s",
                    changed,
                    verdict.provider,
                    verdict.reason,
                )
        if repaired_ranges and "mapped-ranges" not in reclassified:
            reclassified = [*reclassified, "mapped-ranges"]
        if reclassified:
            await self._relabel_reclassified_books(manga_id)
        return reclassified

    async def reconcile_persisted_explicit_chapters(self) -> dict[str, int]:
        """Repair historical chapter rows before the library organizer runs.

        Release-unit inference is normally refreshed per series.  On an upgrade,
        however, an old volume classification can make two chapter files claim
        the same book destination before any refresh is possible.  This pass is
        deliberately limited to the strongest, local evidence: the release title
        explicitly says chapter and its number agrees with the stored source
        chapter.  It performs no network or archive work.
        """

        series_checked = 0
        restored = 0
        async with self._mutation_lock:
            for manga in self.database.list_manga():
                manga_id = str(manga["id"])
                metadata_row = self.database.get_series_metadata(manga_id)
                metadata = (metadata_row or {}).get("data") or {}
                matches = misclassified_explicit_chapters(
                    manga,
                    metadata,
                    self.database.list_all_chapters(manga_id),
                )
                series_checked += 1
                restored += self.database.restore_reclassified_chapters(matches)
        if restored:
            logger.info(
                "Restored %s persisted explicit chapters across %s series",
                restored,
                series_checked,
            )
        return {"series_checked": series_checked, "restored": restored}

    async def _relabel_reclassified_books(self, manga_id: str) -> dict[str, Any]:
        """Rewrite the ComicInfo of files whose unit just changed, then tell the reader.

        A chapter-range file that became volume 3 still carries
        "<Title>Chapter 19-27" inside the archive; readers label books from
        that, not from Tankarr's rows. Must run under the mutation lock.
        """

        try:
            report = await asyncio.to_thread(
                self._publish_metadata_to_library_locked, manga_id
            )
        except Exception as exc:  # noqa: BLE001 - labels are best effort
            logger.warning("Could not relabel the books of %s: %s", manga_id, exc)
            return {"updated": 0, "updated_paths": [], "errors": [str(exc)]}
        for updated_path in report.get("updated_paths") or []:
            try:
                await self._sync_imported_path_with_komga(Path(updated_path))
            except Exception as exc:  # noqa: BLE001 - reader rescans on its own
                logger.debug("Reader sync skipped for %s: %s", updated_path, exc)
        if report.get("updated"):
            logger.info(
                "Relabelled %s books of %s after reclassification",
                report["updated"],
                manga_id,
            )
        return report

    async def failover_failed_release(self, job_id: int) -> list[dict[str, Any]]:
        """Blocklist one failed release and queue the slot's next candidate.

        Mirrors Sonarr's failed-download handling: the failing release is
        excluded from automatic selection and the same logical slot falls back
        to the next preferred release, typically from another provider.
        Infrastructure failures are exempt: the job stays failed for History,
        and the next Wanted pass re-queues the same preferred release.
        """

        async with self._mutation_lock:
            self.assert_mutations_allowed()
            try:
                job = self.database.get_job(job_id)
            except KeyError:
                return []
            if job["status"] != "failed":
                return []
            failure_name = str(job.get("failure_code") or "")
            if failure_name in self.TRANSIENT_FAILURE_NAMES:
                # A restarting engine must not condemn a good release, so the
                # release is not blocked. The source is still counted apart:
                # "temporarily unavailable" over and over is a broken source,
                # and it stops being the first choice for this work.
                try:
                    transient_chapter = self.database.get_chapter(
                        str(job["chapter_id"])
                    )
                except KeyError:
                    return []
                self.database.record_source_failure(
                    str(job["manga_id"]),
                    _release_source_key(transient_chapter),
                    reason=str(job.get("message") or ""),
                    transient=True,
                )
                return []
            manga_id = str(job["manga_id"])
            chapter_id = str(job["chapter_id"])
            blocked = self.database.block_release(
                chapter_id, reason=str(job.get("message") or "Download failed")
            )
            if blocked is None:
                return []
            # A source that keeps failing on this work stops being tried first;
            # every healthy source is preferred until it imports again.
            try:
                failed_chapter = self.database.get_chapter(chapter_id)
            except KeyError:
                failed_chapter = None
            if failed_chapter is not None:
                failures = self.database.record_source_failure(
                    manga_id,
                    _release_source_key(failed_chapter),
                    reason=str(job.get("message") or ""),
                )
                if failures == self.database.SOURCE_DEMOTION_FAILURES:
                    logger.info(
                        "Source %s keeps failing on %s: preferring other sources",
                        failed_chapter.get("source_name")
                        or failed_chapter.get("provider"),
                        manga_id,
                    )
            slot_ids = self.database.slot_release_ids(manga_id, chapter_id)
            if not slot_ids:
                return []
            return [
                self.database.create_job(
                    manga_id,
                    candidate["id"],
                    str(job["requested_language"]),
                    origin="failover",
                )
                for candidate in self.database.preferred_download_candidates(
                    manga_id, slot_ids
                )
                if not candidate.get("blocked")
            ]

    def list_wanted(self) -> list[dict[str, Any]]:
        """Every monitored canonical slot absent from the managed library."""

        with self.database.read_snapshot():
            return self._list_wanted_snapshot()

    def _list_wanted_snapshot(self) -> list[dict[str, Any]]:
        now = cache_now()
        inputs = self.database.wanted_inputs()
        all_attempts = inputs["wanted_attempts"]
        active_by_manga: dict[str, dict[str, list[dict[str, Any]]]] = {}
        # Partition once. Walking every active job inside each series made a
        # warm rebuild O(series * jobs), despite reusing the static card cache.
        for active in inputs["active_jobs"].values():
            chapter = canonical_number(active.get("chapter"))
            volume = canonical_number(active.get("volume"))
            slot_key = (
                f"chapter:{chapter}"
                if chapter is not None
                else f"volume:{volume}"
                if volume is not None
                else f"release:{active['chapter_id']}"
            )
            active_by_manga.setdefault(str(active["manga_id"]), {}).setdefault(
                slot_key, []
            ).append(active)
        wanted: list[dict[str, Any]] = []
        seen_manga: set[str] = set()
        for manga in inputs["manga"]:
            manga_id = str(manga["id"])
            seen_manga.add(manga_id)
            metadata_row = inputs["metadata"].get(manga_id)
            metadata = metadata_row["data"] if metadata_row else manga.get("metadata")
            volume_overrides = inputs["volume_overrides"].get(manga_id, {})
            chapter_map = inputs["chapter_maps"].get(manga_id, [])
            active_by_slot = active_by_manga.get(manga_id, {})

            blocked = inputs["blocked"].get(manga_id, {})
            demoted = inputs["demoted_sources"].get(manga_id, frozenset()) | inputs.get(
                "demoted_global", frozenset()
            )
            health = inputs.get("source_health") or {}
            indexer_volumes = inputs["indexer_volumes"].get(manga_id, set())
            pending_volumes = (inputs.get("pending_volumes") or {}).get(manga_id, set())
            release_count, downloaded_count, release_updated_at = inputs[
                "release_revisions"
            ].get(manga_id, (0, 0, ""))
            attempts_by_slot = all_attempts.get(manga_id, {})
            unobtainable_volumes = _unobtainable_volumes(attempts_by_slot)
            static_revision = (
                str(self.settings.preferred_unit),
                repr(self.database.source_ranking),
                str(manga.get("updated_at") or ""),
                release_count,
                downloaded_count,
                release_updated_at,
                str((metadata_row or {}).get("last_enriched_at") or ""),
                tuple(sorted(volume_overrides.items())),
                tuple(
                    (
                        item.source,
                        item.volumes,
                        item.chapters,
                        item.exact,
                        item.release_date,
                    )
                    for item in chapter_map
                ),
                tuple(
                    sorted(
                        (chapter_id, item["reason"], item["created_at"])
                        for chapter_id, item in blocked.items()
                    )
                ),
                tuple(sorted(indexer_volumes)),
                tuple(sorted(pending_volumes)),
                tuple(sorted(unobtainable_volumes)),
            )
            with self._wanted_series_cache_lock:
                cached = self._wanted_series_cache.get(manga_id)
            source_keys = cached[3] if cached is not None else frozenset()
            revision = (
                *static_revision,
                *_wanted_ranking_revision(source_keys, demoted, health),
            )
            if cached is not None and cached[0] == revision and cached[2] > now:
                entry = self._wanted_entry_with_active_jobs(
                    cached[1], active_by_slot, attempts_by_slot
                )
                if entry is not None:
                    wanted.append(entry)
                continue

            # Decoding every release in the library dominated even a warm
            # Wanted request. A cache hit needs only the grouped revision and
            # live-job rows above; fetch summaries solely for a series
            # whose static chapter map actually changed.
            releases = self.database.list_chapter_summaries(
                manga_id, str(manga["preferred_language"])
            )
            source_keys = frozenset(
                key for release in releases for key in release_source_keys(release)
            )
            revision = (
                *static_revision,
                *_wanted_ranking_revision(source_keys, demoted, health),
            )

            indexed_manga = {
                **manga,
                "_unit_context": {
                    "preferred": self.settings.preferred_unit,
                    "indexer_volumes": indexer_volumes,
                    "unobtainable_volumes": unobtainable_volumes,
                    "pending_volumes": pending_volumes,
                },
            }
            chapter_index = build_chapter_index(
                indexed_manga,
                metadata,
                releases,
                volume_overrides,
                chapter_map=chapter_map,
            )

            hosts = official_hosts(
                (metadata or {}).get("official_links"),
                language=str(manga.get("preferred_language") or ""),
            )
            candidates: list[dict[str, Any]] = []
            synthetic: list[dict[str, Any]] = []
            # Under "prefer official" the publisher decides which chapters
            # exist: scanlators running ahead of it are not releases of a
            # chapter the work has, so those slots are not wanted at all.
            frontier = (
                official_frontier(
                    releases,
                    hosts,
                    metadata=metadata,
                    status=publication_summary(manga, metadata)["status"],
                    chapter_total=(
                        manga.get("expected_count_override")
                        if str(
                            manga.get("expected_count_unit_override") or ""
                        ).casefold()
                        == "chapter"
                        else None
                    ),
                )
                if self.database.source_ranking.acquisition_policy == "prefer_official"
                else None
            )
            for slot in chapter_index["slots"]:
                if slot.get("replaceable") and slot["monitored"]:
                    # A hand-built book off the edition's size: the shelf
                    # keeps it, the hunt looks for the edition's own copy.
                    synthetic.append(
                        {
                            "id": f"replace:{manga_id}:{slot['key']}",
                            "manga_id": manga_id,
                            "volume": slot["volume"],
                            "chapter": None,
                            "title": f"Replacing a hand-built book: {slot['replaceable']}",
                            "language": manga["preferred_language"],
                            "provider": "expected",
                            "groups": [],
                            "publish_at": None,
                            "source_url": "",
                            "pages": None,
                            "version": None,
                            "monitored": True,
                            "downloaded": False,
                            "library_path": None,
                            "queue_job_id": None,
                            "queue_status": None,
                            "expected": True,
                            "evidence": "replace_hand_built",
                        }
                    )
                    continue
                if (
                    not slot["expected"]
                    or slot["downloaded"]
                    or not slot["monitored"]
                    or slot.get("covered_by_volume")
                    or slot.get("covered_by_chapters")
                    or slot.get("covered_unmapped")
                ):
                    continue
                if frontier is not None and beyond_frontier(slot, frontier):
                    continue
                if slot["available"]:
                    monitored_releases = [
                        release
                        for release in slot["releases"]
                        if slot.get("volume_monitor_state") == "monitored"
                        or release.get("monitored")
                    ]
                    if not monitored_releases:
                        continue
                    unblocked = [
                        release
                        for release in monitored_releases
                        if str(release["id"]) not in blocked
                    ]
                    if unblocked:
                        chosen = self.database.source_ranking.best(
                            unblocked,
                            official_hosts=hosts,
                            demoted_sources=demoted,
                            health=health,
                        )
                    else:
                        chosen = self.database.source_ranking.best(
                            monitored_releases, official_hosts=hosts, health=health
                        )
                    if chosen is None:
                        unit = (
                            "volume"
                            if slot["chapter"] is None and slot["volume"]
                            else "chapter"
                        )
                        synthetic.append(
                            {
                                "id": f"expected:{manga_id}:{slot['key']}",
                                "manga_id": manga_id,
                                "volume": slot["volume"],
                                "chapter": slot["chapter"],
                                "title": f"Waiting for an official {unit} release",
                                "language": manga["preferred_language"],
                                "provider": "expected",
                                "groups": [],
                                "publish_at": None,
                                "source_url": "",
                                "pages": None,
                                "version": None,
                                "monitored": True,
                                "downloaded": False,
                                "library_path": None,
                                "queue_job_id": None,
                                "queue_status": None,
                                "expected": True,
                                "evidence": "official_only_policy",
                            }
                        )
                        continue
                    selected = dict(chosen)
                    if unblocked:
                        selected["blocked"] = False
                        selected["block_reason"] = None
                    else:
                        selected["blocked"] = True
                        selected["block_reason"] = blocked.get(
                            str(selected["id"]), {}
                        ).get("reason")
                    selected["queue_job_id"] = None
                    selected["queue_status"] = None
                    selected["_wanted_slot_key"] = str(slot["key"])
                    candidates.append(selected)
                    continue
                unit = (
                    "volume"
                    if slot["chapter"] is None and slot["volume"]
                    else "chapter"
                )
                synthetic.append(
                    {
                        "id": f"expected:{manga_id}:{slot['key']}",
                        "manga_id": manga_id,
                        "volume": slot["volume"],
                        "chapter": slot["chapter"],
                        "title": f"Expected {unit} — release not indexed",
                        "language": manga["preferred_language"],
                        "provider": "expected",
                        "groups": [],
                        "publish_at": None,
                        "source_url": "",
                        "pages": None,
                        "version": None,
                        "monitored": True,
                        "downloaded": False,
                        "library_path": None,
                        "queue_job_id": None,
                        "queue_status": None,
                        "expected": True,
                        "evidence": slot["evidence"],
                    }
                )
            unresolved = (
                int(chapter_index["unresolved_expected_count"])
                if manga["monitor_mode"] in BACKLOG_MONITOR_MODES
                else 0
            )
            # A paywalled episode at the official source is not missing: the
            # library is exactly as far as the publisher lets it be, the way a
            # not-yet-aired episode is not missing in Sonarr. It never joins
            # Wanted - the series page and the calendar already show the lock
            # - and it must not inflate the missing counter that tells the
            # operator how much work the library still has.
            locked = min(
                unresolved,
                int((chapter_index.get("dropped_releases") or {}).get("locked") or 0),
            )
            unresolved -= locked
            # A monitored work that no source lists and whose size no
            # catalogue knows has nothing to put in a row - and is exactly
            # the series that would otherwise vanish from Wanted while its
            # library stays empty. It gets a series-level entry so the
            # indexers are asked for it and the operator sees the verdict.
            no_sources = (
                not releases
                and not unresolved
                and not candidates
                and not synthetic
                and manga["monitor_mode"] in BACKLOG_MONITOR_MODES
            )
            # Keep the full Wanted contract without decoding the import and
            # numbering history of every release of every monitored work.
            full_candidates = self.database.chapters_by_ids(
                str(chapter["id"]) for chapter in candidates
            )
            candidates = [
                {**full_candidates[str(chapter["id"])], **chapter}
                for chapter in candidates
            ]
            static_entry = (
                None
                if not candidates
                and not synthetic
                and not unresolved
                and not no_sources
                else {
                    "manga": {
                        "id": manga["id"],
                        "title": manga["title"],
                        "provider": manga.get("provider", "catalogue"),
                        "source_name": manga.get("source_name"),
                        "cover_url": manga.get("cover_url")
                        or (
                            series_artwork_url(
                                str(manga["id"]), metadata_row.get("artwork_sha256")
                            )
                            if metadata_row and metadata_row.get("artwork_sha256")
                            else None
                        ),
                        "preferred_language": manga["preferred_language"],
                        "monitor_mode": manga["monitor_mode"],
                    },
                    "chapters": sorted([*candidates, *synthetic], key=_wanted_sort_key),
                    "unmapped_expected_count": unresolved,
                    "unmapped_locked_count": locked,
                    "no_sources": no_sources,
                    "expected_count": chapter_index["expected_count"],
                    "expected_source": chapter_index["expected_source"],
                }
            )
            with self._wanted_series_cache_lock:
                self._wanted_series_cache[manga_id] = (
                    revision,
                    static_entry,
                    next_publication(releases, since=now, include_ranking=True),
                    source_keys,
                )
            entry = self._wanted_entry_with_active_jobs(
                static_entry, active_by_slot, attempts_by_slot
            )
            if entry is not None:
                wanted.append(entry)
        with self._wanted_series_cache_lock:
            for manga_id in self._wanted_series_cache.keys() - seen_manga:
                del self._wanted_series_cache[manga_id]
        return wanted

    @staticmethod
    def _wanted_entry_with_active_jobs(
        entry: dict[str, Any] | None,
        active_by_slot: dict[str, list[dict[str, Any]]],
        attempts_by_slot: dict[str, list[dict[str, Any]]] | None = None,
    ) -> dict[str, Any] | None:
        """Attach the live facts a cached Wanted entry cannot hold.

        The entry is cached against a revision of the series' releases, but a
        recovery attempt changes neither the releases nor that revision: what
        each channel answered has to be joined here, next to the active jobs,
        or a cache hit would keep showing a slot as never searched.
        """

        from tankarr.wanted_recovery import slot_key_for, slot_verdict

        if entry is None:
            return None
        attempts_by_slot = attempts_by_slot or {}
        chapters: list[dict[str, Any]] = []
        for cached in entry["chapters"]:
            chapter = dict(cached)
            slot_key = str(chapter.pop("_wanted_slot_key", "")) or slot_key_for(chapter)
            active = min(
                active_by_slot.get(slot_key, []),
                key=lambda job: (
                    job["status"] == "queued",
                    int(job["job_id"]),
                ),
                default=None,
            )
            if active is not None:
                chapter["queue_job_id"] = int(active["job_id"])
                chapter["queue_status"] = str(active["status"])
            chapter["slot_key"] = slot_key
            chapter["recovery"] = slot_verdict(attempts_by_slot.get(slot_key, []))
            chapters.append(chapter)
        result = {**entry, "chapters": chapters}
        if entry.get("no_sources"):
            result["recovery"] = slot_verdict(attempts_by_slot.get(SERIES_SLOT, []))
        return result

    async def remove_release_source(
        self, manga_id: str, provider: str, provider_manga_id: str
    ) -> dict[str, Any]:
        """Unmap one source entry from a series and forget what it offered.

        Files it already delivered stay: they are the library's. Only its
        undownloaded releases go, so a side work mapped by mistake stops
        competing for the series' slots at once.
        """

        async with self._mutation_lock:
            self.assert_mutations_allowed(db_only=True)
            manga = self.database.get_manga(manga_id)
            mapping = next(
                (
                    item
                    for item in self.database.list_release_sources(manga_id)
                    if str(item.get("provider")) == provider
                    and str(item.get("provider_manga_id")) == provider_manga_id
                ),
                None,
            )
            if mapping is None:
                raise KeyError(provider_manga_id)
            release_ids: list[str] = []
            source = self.providers.get(provider)
            if source is not None:
                try:
                    listed = await source.list_chapters(
                        provider_manga_id, str(manga["preferred_language"])
                    )
                    release_ids = [str(item["id"]) for item in listed if item.get("id")]
                except Exception as exc:  # noqa: BLE001 - the mapping still goes
                    logger.info(
                        "Could not list %s before unmapping it: %s",
                        provider_manga_id,
                        exc,
                    )
            forgotten = self.database.forget_source_releases(
                manga_id,
                provider=provider,
                source_name=str(mapping.get("source_name") or "") or None,
                release_ids=release_ids,
            )
            self.database.delete_release_source(manga_id, provider, provider_manga_id)
            self.database.add_release_source_rejection(
                manga_id, provider, provider_manga_id, "unmapped by the operator"
            )
            # The series payload and the Wanted cache are keyed by the
            # releases' revision, which the forgotten rows just changed.
            return {
                "manga_id": manga_id,
                "provider": provider,
                "provider_manga_id": provider_manga_id,
                "title": mapping.get("title"),
                "source_name": mapping.get("source_name"),
                "releases_forgotten": forgotten,
            }

    async def delete_manga(
        self,
        manga_id: str,
        *,
        delete_files: bool = False,
        confirmation_snapshot: str | None = None,
    ) -> dict[str, Any]:
        async with self._mutation_lock:
            self.assert_mutations_allowed(db_only=not delete_files)
            manga = self.database.get_manga(manga_id)
            chapters = self.database.list_all_chapters(manga_id)
            paths: list[Path] = []
            if delete_files:
                preview, paths = self._build_manga_deletion_preview(manga, chapters)
                if not confirmation_snapshot:
                    raise StaleMangaDeletionError(
                        "Deleting series files requires a fresh server preview and "
                        "its confirmation snapshot."
                    )
                if not hmac.compare_digest(
                    confirmation_snapshot, str(preview["snapshot"])
                ):
                    raise StaleMangaDeletionError(
                        "Series or library files changed since confirmation; "
                        "review the deletion preview and confirm again."
                    )
            self.database.assert_manga_deletion_ready(manga_id)
            quarantine = self._stage_library_files(paths)
            try:
                deleted = self.database.delete_manga_record(
                    manga_id, quarantine.operation_id
                )
            except Exception as exc:
                self._handle_deletion_database_exception(quarantine, exc)
                raise
            file_result = self._commit_file_quarantine(quarantine)
            result: dict[str, Any] = {
                "manga_id": manga_id,
                "deleted": True,
                "delete_files": delete_files,
                **file_result,
                "chapters_deleted": deleted["chapters_deleted"],
                "jobs_deleted": deleted["jobs_deleted"],
            }
        result["komga_scan"] = await self._request_komga_reconciliation(
            bool(paths)
            and not result["cleanup_errors"]
            and not result["quarantine_files_remaining"]
        )
        return result

    async def enqueue_manga_deletion(
        self, manga_id: str, *, delete_files: bool = False
    ) -> dict[str, Any]:
        """Commit the removal and its cleanup scope without touching the library."""
        self.assert_mutations_allowed(db_only=True)
        request_id = uuid.uuid4().hex
        deleted = await self._finish_import_thread(
            lambda: self.database.delete_manga_record(
                manga_id,
                deferred_scope=(request_id, str(self.settings.library_dir))
                if delete_files
                else None,
            )
        )
        return {
            "manga_id": manga_id,
            "deleted": True,
            "delete_files": delete_files,
            "cleanup_pending": delete_files,
            "deletion_id": request_id if delete_files else None,
            "files_deleted": 0,
            "files_missing": 0,
            "directories_removed": 0,
            "cleanup_errors": [],
            "quarantine_files_remaining": 0,
            "quarantine_path": None,
            "komga_scan": {"triggered": False},
            **deleted,
        }

    async def process_series_deletions(self) -> None:
        """Retry durable filesystem work; reader reconciliation has its own journal."""
        for request in self.database.pending_series_deletions():
            if request["retry_after"] > datetime.now(UTC).timestamp():
                continue
            try:
                async with self._mutation_lock:
                    self.assert_mutations_allowed()
                    await self._finish_import_thread(
                        self._finish_series_deletion, request
                    )
                await self._request_komga_reconciliation(True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - durable retry for offline storage
                self.database.retry_series_deletion(
                    request["id"],
                    f"{type(exc).__name__}: {exc}",
                    datetime.now(UTC).timestamp()
                    + min(3600, 30 * 2 ** min(request["attempts"], 7)),
                )
                logger.warning("Series cleanup %s deferred: %s", request["id"], exc)

    def _finish_series_deletion(self, request: dict[str, Any]) -> None:
        scope = request["scope"]
        if scope["library_root"] != str(self.settings.library_dir):
            raise UnsafeLibraryPath("Library configuration changed since removal")
        try:
            self.database.get_manga(request["manga_id"], include_logical_counts=False)
        except KeyError:
            pass
        else:
            raise UnsafeLibraryPath("Series was added again; its files are protected")
        _, paths = self._build_manga_deletion_preview(
            scope["manga"], scope["chapters"], job_paths=scope["job_paths"]
        )
        for path in paths:
            if path.exists() and path.stat().st_mtime_ns > scope["requested_at_ns"]:
                raise UnsafeLibraryPath("Library file changed after series removal")
        quarantine = self._stage_library_files(paths)
        try:
            self.database.finish_series_deletion(request["id"], quarantine.operation_id)
        except Exception as exc:
            self._handle_deletion_database_exception(quarantine, exc)
            raise
        self._commit_file_quarantine(quarantine)

    async def preview_manga_deletion(self, manga_id: str) -> dict[str, Any]:
        """Return the exact, freshness-bound scope of a destructive series delete."""

        self.assert_mutations_allowed()
        # This is an optimistic, read-only snapshot.  It must not wait behind a
        # provider refresh, metadata publication, or another long mutation: the
        # destructive endpoint rebuilds the same snapshot while holding the
        # mutation lock and rejects it if the database or filesystem changed.
        # Running the library scan in a worker also keeps unrelated API
        # requests responsive if the library storage is briefly slow.
        return await asyncio.to_thread(self._preview_manga_deletion_snapshot, manga_id)

    def _preview_manga_deletion_snapshot(self, manga_id: str) -> dict[str, Any]:
        manga = self.database.get_manga(manga_id)
        chapters = self.database.list_all_chapters(manga_id)
        self.database.assert_manga_deletion_ready(manga_id)
        preview, _ = self._build_manga_deletion_preview(manga, chapters)
        return preview

    def _build_manga_deletion_preview(
        self,
        manga: dict[str, Any],
        chapters: list[dict[str, Any]],
        *,
        job_paths: dict[str, list[str]] | None = None,
    ) -> tuple[dict[str, Any], list[Path]]:
        """Build a deletion scope and token while the caller holds the mutation lock."""

        manga_id = str(manga["id"])
        root = self._library_root()
        paths = self._library_paths_for_chapters(manga, chapters, job_paths=job_paths)
        for directory in {path.parent for path in paths if path.suffix == ".cbz"}:
            for suffix in MANAGED_ARTWORK_SUFFIXES:
                candidate = directory / f"cover{suffix}"
                if candidate.exists():
                    paths.append(self._confined_artwork_sidecar(candidate, root))
        paths = sorted(set(paths), key=str)
        self._assert_library_paths_not_shared(
            manga_id,
            {str(chapter["id"]) for chapter in chapters},
            paths,
        )

        path_states = [self._deletion_path_state(path, root) for path in paths]
        languages: list[dict[str, Any]] = []
        for language in sorted(
            {str(chapter.get("language") or "unknown") for chapter in chapters}
        ):
            selected = [
                chapter
                for chapter in chapters
                if str(chapter.get("language") or "unknown") == language
            ]
            language_paths = self._library_paths_for_chapters(manga, selected)
            language_states = [
                self._deletion_path_state(path, root) for path in language_paths
            ]
            downloaded_keys = {
                self._deletion_logical_chapter_key(chapter)
                for chapter in selected
                if chapter.get("downloaded")
            }
            languages.append(
                {
                    "language": language,
                    "chapters": len(
                        {
                            self._deletion_logical_chapter_key(chapter)
                            for chapter in selected
                        }
                    ),
                    "chapter_releases": len(selected),
                    "downloaded_chapters": len(downloaded_keys),
                    "files": len(language_states),
                    "existing_files": sum(
                        bool(state["exists"]) for state in language_states
                    ),
                    "missing_files": sum(
                        not bool(state["exists"]) for state in language_states
                    ),
                }
            )

        snapshot_scope = {
            "version": 1,
            "manga_id": manga_id,
            "chapters": [
                {
                    "id": str(chapter["id"]),
                    "language": str(chapter.get("language") or "unknown"),
                    "volume": chapter.get("volume"),
                    "chapter": chapter.get("chapter"),
                    "downloaded": bool(chapter.get("downloaded")),
                    "library_path": chapter.get("library_path"),
                }
                for chapter in sorted(chapters, key=lambda item: str(item["id"]))
            ],
            "paths": path_states,
        }
        snapshot = hashlib.sha256(
            json.dumps(
                snapshot_scope,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        downloaded_keys = {
            (
                str(chapter.get("language") or "unknown"),
                *self._deletion_logical_chapter_key(chapter),
            )
            for chapter in chapters
            if chapter.get("downloaded")
        }
        preview = {
            "manga_id": manga_id,
            "title": str(manga.get("title") or manga_id),
            "snapshot": snapshot,
            "chapters": sum(int(item["chapters"]) for item in languages),
            "chapter_releases": len(chapters),
            "downloaded_chapters": len(downloaded_keys),
            "files": len(path_states),
            "existing_files": sum(bool(state["exists"]) for state in path_states),
            "missing_files": sum(not bool(state["exists"]) for state in path_states),
            "languages": languages,
        }
        return preview, paths

    @staticmethod
    def _deletion_logical_chapter_key(
        chapter: dict[str, Any],
    ) -> tuple[str, str]:
        number = str(chapter.get("chapter") or "").strip()
        if not number:
            return ("id", str(chapter["id"]))
        return (str(chapter.get("volume") or ""), number)

    @staticmethod
    def _deletion_path_state(path: Path, root: Path) -> dict[str, Any]:
        relative = path.relative_to(root).as_posix()
        try:
            stat = path.stat()
        except FileNotFoundError:
            return {"path": relative, "exists": False}
        return {
            "path": relative,
            "exists": True,
            "size": stat.st_size,
            "modified_ns": stat.st_mtime_ns,
            "changed_ns": stat.st_ctime_ns,
            "device": stat.st_dev,
            "inode": stat.st_ino,
        }

    async def delete_chapter_file(
        self, manga_id: str, chapter_id: str
    ) -> dict[str, Any]:
        async with self._mutation_lock:
            # Keep the lock until the filesystem transaction finishes, even
            # when the HTTP client disconnects during a slow library audit.
            result = await self._finish_import_thread(
                self._delete_chapter_file_locked, manga_id, chapter_id
            )
        result["komga_scan"] = await self._request_komga_reconciliation(
            result.pop("_has_paths")
            and not result["cleanup_errors"]
            and not result["quarantine_files_remaining"]
        )
        return result

    def _delete_chapter_file_locked(
        self, manga_id: str, chapter_id: str
    ) -> dict[str, Any]:
        self.assert_mutations_allowed()
        manga = self.database.get_manga(manga_id)
        chapter = self.database.get_chapter(chapter_id)
        if chapter["manga_id"] != manga_id:
            raise KeyError(chapter_id)
        paths = self._library_paths_for_chapters(manga, [chapter])
        self._assert_library_paths_not_shared(manga_id, {chapter_id}, paths)
        self.database.assert_chapter_file_deletion_ready(manga_id, chapter_id)
        quarantine = self._stage_library_files(paths)
        try:
            reset = self.database.reset_chapter_file(
                manga_id, chapter_id, quarantine.operation_id
            )
        except Exception as exc:
            self._handle_deletion_database_exception(quarantine, exc)
            raise
        file_result = self._commit_file_quarantine(quarantine)
        return {
            "manga_id": manga_id,
            "chapter_id": chapter_id,
            **file_result,
            "chapters_reset": reset["chapters_reset"],
            "jobs_deleted": reset["jobs_deleted"],
            "_has_paths": bool(paths),
        }

    def align_chapter_contents(self, manga_id: str) -> dict[str, Any]:
        """Prove, page by page, which book each loose chapter file is inside.

        The map and the sources' volume tags are claims about numbers; the
        pages are not. Every downloaded chapter is compared with the book the
        map (then the source's tag, then any other book on the shelf) says
        holds it, and the verdict is recorded: ``inside`` with the book,
        ``outside`` of every book tried, ``ambiguous`` in between,
        ``unreadable`` when either archive has no readable pages. Signatures
        of a book are cached by its content hash, so a shelf is read once.
        Nothing is deleted here; the retirement rules read the verdicts.
        """

        from tankarr import content_alignment as content
        from tankarr.source_numbering import title_volume

        manga = self.database.get_manga(manga_id)
        releases = self.database.list_chapters(manga_id, manga["preferred_language"])
        books: dict[str, dict[str, Any]] = {}
        loose: list[dict[str, Any]] = []
        for release in releases:
            if not release.get("downloaded") or not release.get("library_path"):
                continue
            if str(release.get("release_unit") or "chapter") == "volume":
                volume = canonical_number(release.get("volume")) or str(
                    release.get("volume") or ""
                )
                if volume:
                    books.setdefault(volume, release)
            elif release.get("chapter"):
                loose.append(release)
        summary = {"manga_id": manga_id, "checked": 0, "verdicts": {}}
        if not books or not loose:
            return summary
        root = self._library_root()
        metadata_row = self.database.get_series_metadata(manga_id)
        index = build_chapter_index(
            manga,
            (metadata_row or {}).get("data") or {},
            releases,
            chapter_map=self.database.chapter_map(manga_id),
        )
        mapped: dict[str, str] = {}
        for slot in [
            *(index.get("slots") or []),
            *(index.get("mapped_chapter_slots") or []),
        ]:
            volume = slot.get("duplicate_of_volume") or slot.get("volume")
            if not volume:
                continue
            for release in slot.get("releases") or []:
                mapped[str(release["id"])] = canonical_number(volume) or str(volume)
        existing = self.database.content_alignment(manga_id)
        cache: dict[str, list[int]] = {}

        def content_key(release: dict[str, Any]) -> str:
            """The content hash of a file, or its path, size and mtime when a
            legacy row never recorded one: enough to notice a replacement."""

            sha = str(release.get("library_sha256") or "")
            if sha:
                return sha
            try:
                path = self._recorded_library_path(str(release["library_path"]), root)
                stat = path.stat()
            except (OSError, UnsafeLibraryPath, ValueError):
                return ""
            return f"{path}:{stat.st_size}:{stat.st_mtime_ns}"

        keys = {volume: content_key(release) for volume, release in books.items()}
        shelf_hash = hashlib.sha256(
            "|".join(f"{volume}:{keys[volume]}" for volume in sorted(books)).encode()
        ).hexdigest()

        def book_signatures(volume: str) -> list[int]:
            key = keys[volume]
            if key in cache:
                return cache[key]
            signatures = self.database.book_page_signatures(key) if key else None
            if signatures is None:
                signatures = content.page_signatures(
                    self._recorded_library_path(
                        str(books[volume]["library_path"]), root
                    )
                )
                if key and signatures:
                    self.database.record_book_page_signatures(key, signatures)
            cache[key] = signatures
            return signatures

        rank = {content.INSIDE: 3, content.AMBIGUOUS: 2, content.OUTSIDE: 1}
        for release in loose:
            release_id = str(release["id"])
            chapter_key = content_key(release)
            previous = existing.get(release_id)
            if (
                previous
                and chapter_key
                and previous.get("chapter_sha256") == chapter_key
                and (
                    (
                        previous.get("verdict") == content.INSIDE
                        and previous.get("book_sha256")
                        == keys.get(str(previous.get("volume")), "")
                    )
                    or (
                        previous.get("verdict") != content.INSIDE
                        and previous.get("book_sha256") == shelf_hash
                    )
                )
            ):
                summary["verdicts"][release_id] = previous["verdict"]
                continue
            candidates: list[str] = []
            tagged = title_volume(release.get("title"))
            for volume in (mapped.get(release_id), str(tagged) if tagged else None):
                if volume and volume in books and volume not in candidates:
                    candidates.append(volume)
            candidates.extend(
                volume for volume in sorted(books) if volume not in candidates
            )
            chapter_signatures = content.page_signatures(
                self._recorded_library_path(str(release["library_path"]), root)
            )
            best: tuple[str, Any] | None = None
            for volume in candidates:
                alignment = content.align(chapter_signatures, book_signatures(volume))
                if alignment.verdict == content.INSIDE:
                    best = (volume, alignment)
                    break
                if best is None or rank.get(alignment.verdict, 0) > rank.get(
                    best[1].verdict, 0
                ):
                    best = (volume, alignment)
            volume, alignment = best if best else ("", content.align([], []))
            self.database.record_content_alignment(
                manga_id,
                release_id,
                volume=volume if alignment.verdict == content.INSIDE else "",
                alignment=alignment.as_dict(),
                chapter_sha256=chapter_key,
                book_sha256=(
                    keys[volume] if alignment.verdict == content.INSIDE else shelf_hash
                ),
            )
            summary["checked"] += 1
            summary["verdicts"][release_id] = alignment.verdict
        return summary

    def content_blocks_retirement(
        self, manga_id: str, chapter_ids: Iterable[str]
    ) -> set[str]:
        """The chapter files whose pages were not found inside a book.

        A verdict of ``outside`` or ``ambiguous`` vetoes the automatic
        retirement of that file whatever the map says; ``inside`` allows it;
        an unreadable archive, or a chapter never checked, leaves the map's
        word as it was.
        """

        from tankarr import content_alignment as content

        rows = self.database.content_alignment(manga_id)
        return {
            str(chapter_id)
            for chapter_id in chapter_ids
            if (row := rows.get(str(chapter_id)))
            and row.get("verdict") in {content.OUTSIDE, content.AMBIGUOUS}
        }

    async def retire_redundant_chapter_files(self, manga_id: str) -> dict[str, Any]:
        """Once every book of a series is on disk, its loose chapter files are
        redundant by definition and go, so the reader shows the books in
        order and nothing else (Billy Bat: 20 books beside 147 chapter files).
        Must be called outside the mutation lock."""

        from tankarr.chapter_mapping import build_chapter_index

        manga = self.database.get_manga(manga_id)
        releases = self.database.list_chapters(
            manga_id, str(manga["preferred_language"])
        )
        loose = [
            r
            for r in releases
            if r.get("downloaded")
            and str(r.get("release_unit") or "chapter") != "volume"
            and r.get("chapter")
        ]
        if not loose:
            return {
                "manga_id": manga_id,
                "deleted": 0,
                "errors": [],
                "reason": "no chapter files",
            }
        metadata_row = self.database.get_series_metadata(manga_id)
        index = build_chapter_index(
            manga,
            (metadata_row or {}).get("data") or {},
            releases,
            chapter_map=self.database.chapter_map(manga_id),
        )
        if index.get("unit") != "volume":
            return {
                "manga_id": manga_id,
                "deleted": 0,
                "errors": [],
                "reason": "chapter unit",
            }
        books = [
            slot
            for slot in index.get("slots") or []
            if slot.get("expected") and not slot.get("chapter")
        ]
        if not books or not all(slot.get("downloaded") for slot in books):
            return {
                "manga_id": manga_id,
                "deleted": 0,
                "errors": [],
                "reason": "books missing",
            }
        # The books are on the shelf; the pages say whether each chapter file
        # is in one of them. A file whose pages no book holds is not
        # redundant, whatever the map says (Nana: 81-84 never collected).
        await asyncio.to_thread(self.align_chapter_contents, manga_id)
        vetoed = self.content_blocks_retirement(
            manga_id, [str(release["id"]) for release in loose]
        )
        if vetoed:
            logger.info(
                "Kept %d chapter files of %s: their pages are not inside any book",
                len(vetoed),
                manga.get("title"),
            )
        keep = [release for release in loose if str(release["id"]) not in vetoed]
        if not keep:
            return {
                "manga_id": manga_id,
                "deleted": 0,
                "errors": [],
                "reason": "pages not inside the books",
            }
        outcome = await self.delete_chapter_files(
            manga_id, [str(release["id"]) for release in keep]
        )
        for error in outcome.get("errors") or []:
            logger.info(
                "Could not retire a chapter of %s: %s", manga.get("title"), error
            )
        if outcome.get("deleted"):
            logger.info(
                "Retired %d chapter files of %s: every book is in the library",
                outcome["deleted"],
                manga.get("title"),
            )
        return {"manga_id": manga_id, **outcome, "reason": None}

    async def delete_chapter_files(
        self, manga_id: str, chapter_ids: list[str], *, recycle: bool = False
    ) -> dict[str, Any]:
        """Delete several chapter files of one series with a single safety scan.

        The shared-path guard walks the whole library and the pending queue, so
        deleting hundreds of chapters one call at a time is quadratic. Failures
        are collected per chapter: one unsafe file never blocks the others.
        """

        async with self._mutation_lock:
            self.assert_mutations_allowed()
            manga = self.database.get_manga(manga_id)
            chapters = []
            for chapter_id in chapter_ids:
                chapter = self.database.get_chapter(chapter_id)
                if chapter["manga_id"] != manga_id:
                    raise KeyError(chapter_id)
                chapters.append(chapter)
            if not chapters:
                return {"deleted": 0, "errors": [], "any_file_removed": False}
            self._assert_library_paths_not_shared(
                manga_id,
                {str(chapter["id"]) for chapter in chapters},
                self._library_paths_for_chapters(manga, chapters),
            )
            deleted = 0
            errors: list[str] = []
            any_file_removed = False
            for chapter in chapters:
                chapter_id = str(chapter["id"])
                try:
                    paths = self._library_paths_for_chapters(manga, [chapter])
                    self.database.assert_chapter_file_deletion_ready(
                        manga_id, chapter_id
                    )
                    quarantine = self._stage_library_files(
                        paths, disposition="retain" if recycle else "delete"
                    )
                    try:
                        self.database.reset_chapter_file(
                            manga_id, chapter_id, quarantine.operation_id
                        )
                    except Exception as exc:
                        self._handle_deletion_database_exception(quarantine, exc)
                        raise
                    file_result = self._commit_file_quarantine(quarantine)
                except Exception as exc:  # noqa: BLE001 - keep retiring the rest
                    errors.append(f"{chapter_id}: {exc}")
                    continue
                deleted += 1
                any_file_removed = any_file_removed or (
                    bool(paths)
                    and not file_result["cleanup_errors"]
                    and not file_result["quarantine_files_remaining"]
                )
        await self._request_komga_reconciliation(any_file_removed)
        return {
            "deleted": deleted,
            "errors": errors,
            "any_file_removed": any_file_removed,
        }

    async def delete_volume_files(
        self,
        manga_id: str,
        volume: str,
        language: str | None = None,
        expected_downloaded_count: int | None = None,
        expected_downloaded_chapter_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        async with self._mutation_lock:
            self.assert_mutations_allowed()
            manga = self.database.get_manga(manga_id)
            selected_language = language or manga["preferred_language"]
            chapters = self.database.list_volume_chapters(
                manga_id, volume, selected_language
            )
            downloaded_count = sum(bool(chapter["downloaded"]) for chapter in chapters)
            if (
                expected_downloaded_count is not None
                and downloaded_count != expected_downloaded_count
            ):
                raise StaleVolumeDeletionError(
                    "Volume changed since confirmation: expected "
                    f"{expected_downloaded_count} downloaded files, found "
                    f"{downloaded_count}. Review and confirm again."
                )
            if expected_downloaded_count is not None and (
                expected_downloaded_chapter_ids is None
            ):
                raise StaleVolumeDeletionError(
                    "Volume confirmation snapshot is incomplete; review and "
                    "confirm again."
                )
            if expected_downloaded_chapter_ids is not None:
                expected_snapshot = sorted(expected_downloaded_chapter_ids)
                current_snapshot = sorted(
                    str(chapter["id"]) for chapter in chapters if chapter["downloaded"]
                )
                if expected_snapshot != current_snapshot:
                    raise StaleVolumeDeletionError(
                        "Volume changed since confirmation; review and confirm again."
                    )
            paths = self._library_paths_for_chapters(manga, chapters)
            self._assert_library_paths_not_shared(
                manga_id,
                {chapter["id"] for chapter in chapters},
                paths,
            )
            self.database.assert_volume_file_deletion_ready(
                manga_id, volume, selected_language
            )
            quarantine = self._stage_library_files(paths)
            try:
                reset = self.database.reset_volume_files(
                    manga_id,
                    volume,
                    selected_language,
                    quarantine.operation_id,
                )
            except Exception as exc:
                self._handle_deletion_database_exception(quarantine, exc)
                raise
            file_result = self._commit_file_quarantine(quarantine)
            result: dict[str, Any] = {
                "manga_id": manga_id,
                "volume": volume,
                "language": selected_language,
                "chapters_matched": len(chapters),
                **file_result,
                "chapters_reset": reset["chapters_reset"],
                "jobs_deleted": reset["jobs_deleted"],
            }
        result["komga_scan"] = await self._request_komga_reconciliation(
            bool(paths)
            and not result["cleanup_errors"]
            and not result["quarantine_files_remaining"]
        )
        return result

    def series_calendar(self, manga_id: str) -> dict[str, Any]:
        """What one work released lately and when its next chapters are expected.

        The dates come from the work's own release history (MangaUpdates):
        a median cadence projected forward. No publisher announces future
        dates, so every expected entry is an estimate and says so; a chapter
        whose date has passed stays listed as overdue instead of vanishing.
        """

        from tankarr.release_cadence import (
            MIN_SAMPLES,
            expected_releases,
            release_timeline,
        )
        from tankarr.series_summary import publication_summary
        from tankarr.source_ranking import (
            is_official_release,
            official_hosts,
            source_class,
        )

        manga = self.database.get_manga(manga_id)
        manga["publication_signals"] = self.database.publication_signals(manga_id)
        metadata_row = self.database.get_series_metadata(manga_id)
        metadata = metadata_row["data"] if metadata_row else manga.get("metadata")
        publication = publication_summary(manga, metadata)
        releases = self.database.list_chapters(manga_id, manga["preferred_language"])
        # The publisher's own release dates come first, as on the Calendar
        # page: they carry the series' canonical numbering. The catalogue
        # history is the fallback, and it may count differently (MangaUpdates
        # had The World After the Fall at 250 while every source and the
        # library said 235): its numbers are re-anchored to the series.
        hosts = official_hosts(
            (metadata or {}).get("official_links"),
            language=str(manga.get("preferred_language") or ""),
        )
        official_history = [
            {"chapter": row.get("chapter"), "release_date": row.get("publish_at")}
            for row in releases
            if hosts
            and str(row.get("numbering_status") or "mapped") == "mapped"
            and row.get("chapter")
            and is_official_release(row, hosts)
        ]
        # The platform states the exact moment (Tapas: 16:00 UTC); keep it
        # for the announced episodes, the date alone is not the whole answer.
        official_moments: dict[int, str] = {}
        for item in official_history:
            number = _chapter_int({"chapter": item["chapter"]})
            raw = str(item.get("release_date") or "").strip()
            if number is not None and raw and number not in official_moments:
                official_moments[number] = raw
        official_timeline = release_timeline(official_history)
        catalogue_history = self.database.list_release_history(manga_id)
        catalogue_timeline = release_timeline(catalogue_history)
        renumber = 0
        if len(official_timeline) >= MIN_SAMPLES:
            history, timeline, source = (
                official_history,
                official_timeline,
                "official platform release dates",
            )
        else:
            history, timeline = catalogue_history, catalogue_timeline
            source = "MangaUpdates release history" if catalogue_history else None
            listed = [
                number
                for number in (
                    _chapter_int(
                        {"chapter": row.get("canonical_chapter") or row.get("chapter")}
                    )
                    for row in releases
                )
                if number is not None
            ]
            if timeline and listed:
                last_catalogue = int(timeline[-1][0])
                renumber = max(listed) - last_catalogue
                if renumber > 0:
                    renumber = 0  # the catalogue lags the sources: numbers agree
        today = datetime.now(UTC).date()
        # A publisher platform lists the next chapter ahead of time (Tapas
        # shows "🔒 252" a week early, dated). That date is announced, not
        # estimated: it heads the expected list and is not release history.
        announced = (
            [(number, released) for number, released in timeline if released > today]
            if source == "official platform release dates"
            else []
        )
        past_timeline = [
            (number, released) for number, released in timeline if released <= today
        ]
        recent = [
            {
                "chapter": str(int(number) + renumber),
                "released_at": released.isoformat(),
            }
            for number, released in past_timeline[-6:]
        ][::-1]

        known: dict[int, dict[str, bool]] = {}
        for release in releases:
            number = _chapter_int(
                {"chapter": release.get("canonical_chapter") or release.get("chapter")}
            )
            if number is None:
                continue
            state = known.setdefault(
                number, {"available": False, "downloaded": False, "official": False}
            )
            state["available"] = True
            if release.get("downloaded"):
                state["downloaded"] = True
            if (
                source_class(
                    provider=release.get("provider"),
                    source_key=release.get("source_key"),
                    source_name=release.get("source_name"),
                )
                == "official"
            ):
                state["official"] = True

        predictions = (
            []
            if publication["status"] == "ended" or publication.get("paused")
            else expected_releases(history, today=today, horizon_days=60)
        )
        expected = []
        announced_numbers: set[int] = set()
        for number_decimal, released in announced:
            number = int(number_decimal) + renumber
            announced_numbers.add(number)
            state = known.get(
                number,
                {"available": False, "downloaded": False, "official": False},
            )
            expected.append(
                {
                    "chapter": str(number),
                    "expected_at": released.isoformat(),
                    "cadence_days": predictions[0].cadence_days if predictions else 0,
                    "cadence_label": predictions[0].cadence_label
                    if predictions
                    else "",
                    "overdue_days": 0,
                    "estimated": False,
                    "published_at": official_moments.get(int(number_decimal)),
                    # Listed but locked until that day: not yet obtainable.
                    "available": bool(state["downloaded"]),
                    "downloaded": bool(state["downloaded"]),
                    "official": True,
                }
            )
        for prediction in predictions:
            number = (_chapter_int({"chapter": prediction.chapter}) or 0) + renumber
            if number in announced_numbers:
                continue
            state = known.get(
                number,
                {"available": False, "downloaded": False, "official": False},
            )
            expected.append(
                {
                    "chapter": str(number),
                    "expected_at": prediction.expected_at.isoformat(),
                    "cadence_days": prediction.cadence_days,
                    "cadence_label": prediction.cadence_label,
                    "overdue_days": prediction.overdue_days,
                    "estimated": True,
                    **state,
                }
            )
        cadence = None
        if predictions:
            last_chapter = int(predictions[0].last_chapter)
            last_release_at = predictions[0].last_release_at
            if announced and past_timeline:
                last_chapter, last_release_at = (
                    int(past_timeline[-1][0]),
                    past_timeline[-1][1],
                )
            cadence = {
                "days": predictions[0].cadence_days,
                "label": predictions[0].cadence_label,
                "last_chapter": str(last_chapter + renumber),
                "last_release_at": last_release_at.isoformat(),
                "renumbered_by": renumber,
            }
        if publication.get("paused"):
            reason = "paused"
        elif publication["status"] == "ended":
            reason = "ended"
        elif not history:
            reason = "no_history"
        elif not predictions:
            reason = "no_rhythm"
        else:
            reason = None
        return {
            "manga_id": manga_id,
            "source": source if timeline else None,
            "history_count": len(past_timeline),
            "recent": recent,
            "cadence": cadence,
            "expected": expected,
            "reason": reason,
        }

    def duplicate_chapter_files(self, manga_id: str) -> list[dict[str, Any]]:
        """Downloaded chapter files whose content is also inside an owned volume."""

        manga = self.database.get_manga(manga_id)
        releases = self.database.list_chapters(manga_id, manga["preferred_language"])
        metadata_row = self.database.get_series_metadata(manga_id)
        metadata = metadata_row["data"] if metadata_row else manga.get("metadata")
        volume_overrides = {
            item["volume_key"]: item["state"]
            for item in self.database.list_volume_monitor_overrides(manga_id)
        }
        index = build_chapter_index(
            manga,
            metadata,
            releases,
            volume_overrides,
            chapter_map=self.database.chapter_map(manga_id),
        )
        duplicates: list[dict[str, Any]] = []
        for slot in index["slots"]:
            volume = slot.get("duplicate_of_volume")
            if not volume:
                continue
            for release in slot["releases"]:
                if release.get("downloaded"):
                    duplicates.append({**release, "duplicate_of_volume": volume})

        def is_whole_book(release: dict[str, Any]) -> bool:
            """A tankobon, not merely a file with no chapter number on it.

            Treating any unnumbered release as a whole book let a 263 KB
            fragment named "cUnknown-<id>" stand in for V.B. Rose volume 9 and
            condemn the six real chapters filed under it. Only a release the
            provider actually delivered as a volume covers anything.
            """

            return str(release.get("release_unit") or "chapter") == "volume"

        owns_whole_books = any(
            release.get("downloaded") and is_whole_book(release) for release in releases
        )
        if owns_whole_books:
            # What matters is owning the book, not what the series is counted
            # in: Baby Steps is measured in chapters and still holds 44 whole
            # tankobon, whose 425 chapter files sat beside them unnoticed
            # because this ran only for a volumes index. Chapter files inside
            # an owned book (Yawara: chapters 138-174 next to volumes 13-15)
            # are found from the rows themselves, with the map's exact entries
            # only. A book the map does not describe, or a suspect one, covers
            # nothing here.
            from tankarr.chapter_map import coverage_for_chapter

            map_entries = self.database.chapter_map(manga_id)
            reasons = self.suspect_covering_volumes(manga_id)
            # A book Tankarr did not download itself is not allowed to condemn
            # a chapter file: it cannot know what is inside it. Unless somebody
            # looked. A reading of its pages ("ocr") is exactly that evidence,
            # and it is why the reading exists, so a hand-imported book that
            # was read covers its chapters like any other.
            read_volumes = {
                str(volume)
                for entry in map_entries
                if entry.source == "ocr" and entry.exact
                for volume in entry.volumes
            }
            suspect = {
                volume
                for volume, reason in reasons.items()
                if reason != "Imported by hand" or str(volume) not in read_volumes
            }
            owned = {
                str(canonical_number(release.get("volume")))
                for release in releases
                if release.get("downloaded")
                and is_whole_book(release)
                and canonical_number(release.get("volume"))
            } - suspect
            seen = {str(item.get("id")) for item in duplicates}
            for release in releases:
                if not release.get("downloaded") or str(release.get("id")) in seen:
                    continue
                if (
                    str(release.get("release_unit") or "chapter") == "volume"
                    or not str(release.get("chapter") or "").strip()
                ):
                    continue
                coverage = coverage_for_chapter(
                    release.get("chapter"), map_entries, owned
                )
                if coverage.covered and coverage.exact and coverage.volume:
                    duplicates.append(
                        {**release, "duplicate_of_volume": coverage.volume}
                    )
        # Split chapter files retain their source labels (for example 28.1 and
        # 28.2). An assembled book records the exact source IDs it contains,
        # which proves those files too when its saved boundaries still match.
        from tankarr.assembly_provenance import proven_assembly

        suspect = set(self.suspect_covering_volumes(manga_id))
        proven_ids: dict[str, str] = {}
        for release in releases:
            proof = proven_assembly(release)
            if release.get("downloaded") and proof and proof["volume"] not in suspect:
                proven_ids.update(
                    (identifier, proof["volume"]) for identifier in proof["chapter_ids"]
                )
        seen = {str(release["id"]) for release in duplicates}
        for release in releases:
            identifier = str(release["id"])
            if (
                release.get("downloaded")
                and identifier in proven_ids
                and identifier not in seen
            ):
                duplicates.append(
                    {**release, "duplicate_of_volume": proven_ids[identifier]}
                )
        return duplicates

    SUSPECT_BOOK_PAGES = 600  # no single manga volume; a pack imported as "v1"

    def suspect_covering_volumes(self, manga_id: str) -> dict[str, str]:
        """Owned books whose contents cannot prove chapter coverage."""

        from tankarr.chapter_mapping import suspect_volume_reasons

        manga = self.database.get_manga(manga_id)
        return suspect_volume_reasons(
            self.database.list_chapters(manga_id, manga["preferred_language"]),
            page_limit=self.SUSPECT_BOOK_PAGES,
            chapter_map=self.database.chapter_map(manga_id),
        )

    async def delete_duplicate_chapter_files(
        self,
        manga_id: str,
        expected_chapter_ids: list[str] | None = None,
        *,
        only: set[str] | None = None,
        recycle: bool = False,
        volume: str | None = None,
    ) -> dict[str, Any]:
        async with self._mutation_lock:
            pending = asyncio.create_task(
                asyncio.to_thread(
                    self._delete_duplicate_chapter_files_locked,
                    manga_id,
                    expected_chapter_ids,
                    only=only,
                    recycle=recycle,
                    volume=volume,
                )
            )
            cancelled = False
            while not pending.done():
                try:
                    await asyncio.shield(pending)
                except asyncio.CancelledError:
                    cancelled = True
            result = pending.result()
            if cancelled:
                raise asyncio.CancelledError
        has_paths = result.pop("_has_paths")
        result["komga_scan"] = await self._request_komga_reconciliation(
            has_paths
            and not result["cleanup_errors"]
            and not result["quarantine_files_remaining"]
        )
        return result

    def _delete_duplicate_chapter_files_locked(
        self,
        manga_id: str,
        expected_chapter_ids: list[str] | None = None,
        *,
        only: set[str] | None = None,
        recycle: bool = False,
        volume: str | None = None,
        expected_file_sha256: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Retire proven duplicates while the caller holds the mutation lock.

        A book-scoped confirmation compares the complete reviewed set for that
        book. The internal ``only`` filter is applied after confirmation, as
        before, for callers that review the whole series.
        """

        self.assert_mutations_allowed()
        manga = self.database.get_manga(manga_id)
        chapters = self.duplicate_chapter_files(manga_id)
        if volume is not None:
            chapters = [
                chapter
                for chapter in chapters
                if canonical_number(chapter["duplicate_of_volume"])
                == canonical_number(volume)
            ]
        current = sorted(str(chapter["id"]) for chapter in chapters)
        if expected_chapter_ids is not None and (
            sorted(expected_chapter_ids) != current
        ):
            raise StaleVolumeDeletionError(
                "Duplicates changed since confirmation; review and confirm again."
            )
        if only is not None:
            chapters = [chapter for chapter in chapters if str(chapter["id"]) in only]
            current = sorted(str(chapter["id"]) for chapter in chapters)
        if not chapters:
            return {
                "manga_id": manga_id,
                "chapters_matched": 0,
                "files_deleted": 0,
                "chapters_reset": 0,
                "jobs_deleted": 0,
                "cleanup_errors": [],
                "quarantine_files_remaining": 0,
                "komga_scan": {"requested": False, "triggered": False},
                "_has_paths": False,
            }
        paths = self._library_paths_for_chapters(manga, chapters)
        self._assert_library_paths_not_shared(manga_id, set(current), paths)
        quarantine = self._stage_library_files(
            paths, disposition="retain" if recycle else "delete"
        )
        try:
            if expected_file_sha256 is not None:
                moved = {str(original): staged for original, staged in quarantine.moved}
                if any(
                    original not in moved
                    or not hmac.compare_digest(sha256(moved[original]), digest)
                    for original, digest in expected_file_sha256.items()
                ):
                    raise UnsafeLibraryPath(
                        "Chapter bytes changed before retirement; source files were restored."
                    )
            reset = self.database.reset_chapter_files(
                manga_id, current, quarantine.operation_id
            )
        except Exception as exc:
            self._handle_deletion_database_exception(quarantine, exc)
            raise
        file_result = self._commit_file_quarantine(quarantine)
        result: dict[str, Any] = {
            "manga_id": manga_id,
            "chapters_matched": len(chapters),
            "volumes": sorted(
                {str(chapter["duplicate_of_volume"]) for chapter in chapters},
                key=lambda value: (len(value), value),
            ),
            **file_result,
            "chapters_reset": reset["chapters_reset"],
            "jobs_deleted": reset["jobs_deleted"],
        }
        result["_has_paths"] = bool(paths)
        return result

    def _library_paths_for_chapters(
        self,
        manga: dict[str, Any],
        chapters: list[dict[str, Any]],
        *,
        job_paths: dict[str, list[str]] | None = None,
    ) -> list[Path]:
        result_paths = (
            job_paths
            if job_paths is not None
            else self.database.list_job_result_paths(
                chapter["id"] for chapter in chapters
            )
        )
        paths: set[Path] = set()
        root = self._library_root()
        for chapter in chapters:
            library_path = chapter.get("library_path")
            job_paths = result_paths.get(chapter["id"], [])
            if not chapter.get("downloaded") and not library_path and not job_paths:
                continue
            expected = self._confined_library_path(
                final_library_path(root, manga, chapter), root
            )
            if library_path:
                managed_path = self._recorded_library_path(str(library_path), root)
            else:
                managed_path = expected
            paths.add(managed_path)
            for suffix in MANAGED_ARTWORK_SUFFIXES:
                sidecar = managed_path.with_suffix(suffix)
                if sidecar.exists():
                    paths.add(self._confined_artwork_sidecar(sidecar, root))
            for raw_path in job_paths:
                job_path = self._recorded_library_path(raw_path, root)
                if job_path != managed_path:
                    raise UnsafeLibraryPath(
                        "Refusing download job result path that does not match "
                        f"the chapter library path: {raw_path}"
                    )
        return sorted(paths, key=str)

    def _assert_library_paths_not_shared(
        self,
        manga_id: str,
        target_chapter_ids: set[str],
        target_paths: list[Path],
    ) -> None:
        if not target_paths:
            return
        targets = set(target_paths)
        root = self._library_root()
        snapshot = _FileReferenceSnapshot(
            root, (*LEGACY_LIBRARY_ROOTS, *self.settings.legacy_library_root_paths)
        )
        for other_manga, chapter, job_paths in self.database.library_file_references():
            if other_manga["id"] == manga_id and chapter["id"] in target_chapter_ids:
                continue
            managed = snapshot.file(
                chapter.get("library_path")
                or final_library_path(root, other_manga, chapter)
            )
            for raw in job_paths:
                if snapshot.file(raw) != managed:
                    raise UnsafeLibraryPath(
                        "Refusing download job result path that does not match "
                        f"the chapter library path: {raw}"
                    )
            references = {managed}
            for suffix in MANAGED_ARTWORK_SUFFIXES:
                sidecar = managed.with_suffix(suffix)
                if snapshot.exists(sidecar):
                    references.add(snapshot.file(sidecar, MANAGED_ARTWORK_SUFFIXES))
            shared = targets.intersection(references)
            if shared:
                paths = ", ".join(str(path) for path in sorted(shared, key=str))
                raise UnsafeLibraryPath(
                    "Refusing to delete library files referenced by another "
                    f"chapter release: {paths}"
                )

        # One queue can hold thousands of jobs; resolving each job's series
        # again (with its chapter coverage) turned this guard quadratic.
        manga_cache: dict[str, dict[str, Any]] = {}
        pending_jobs = self.database.list_pending_jobs()
        pending_chapters = self.database.chapters_by_ids(
            job["chapter_id"]
            for job in pending_jobs
            if job["status"] == "queued" and not job.get("planned_path")
        )
        for job in pending_jobs:
            if job["chapter_id"] in target_chapter_ids:
                continue
            if job.get("planned_path"):
                destination = snapshot.file(str(job["planned_path"]))
            elif job["status"] == "queued":
                try:
                    chapter = pending_chapters[job["chapter_id"]]
                    other_id = str(chapter["manga_id"])
                    manga = manga_cache.get(other_id)
                    if manga is None:
                        manga = self.database.get_manga(
                            other_id, include_logical_counts=False
                        )
                        manga_cache[other_id] = manga
                except KeyError as exc:
                    raise UnsafeLibraryPath(
                        f"Refusing deletion while pending job {job['id']} has no "
                        "resolvable manga/chapter destination"
                    ) from exc
                destination = snapshot.file(final_library_path(root, manga, chapter))
            else:
                raise UnsafeLibraryPath(
                    f"Refusing deletion while active job {job['id']} has no "
                    "recorded planned path"
                )
            if destination in targets:
                raise UnsafeLibraryPath(
                    "Refusing to delete a library file targeted by another "
                    f"{job['status']} download job {job['id']}: {destination}"
                )

    def _recorded_library_path(self, raw_path: str, root: Path) -> Path:
        recorded = Path(raw_path)
        try:
            return self._confined_library_path(recorded, root)
        except UnsafeLibraryPath as original_error:
            legacy_roots = (
                *LEGACY_LIBRARY_ROOTS,
                *self.settings.legacy_library_root_paths,
            )
            for legacy_root in legacy_roots:
                try:
                    relative = recorded.relative_to(legacy_root)
                except ValueError:
                    continue
                # Old installations persisted the container or host mount root.
                # Preserve the recorded relative path; never infer from a suffix.
                return self._confined_library_path(root / relative, root)
            raise original_error

    def _library_root(self) -> Path:
        try:
            root = self.settings.library_dir.resolve(strict=True)
        except FileNotFoundError as exc:
            raise LibraryUnavailable(
                f"Configured library root is unavailable: {self.settings.library_dir}"
            ) from exc
        if not root.is_dir():
            raise LibraryUnavailable(f"Library root is not a directory: {root}")
        self._validate_library_identity(root)
        return root

    def library_status(self) -> dict[str, Any]:
        try:
            root = self._library_root()
        except LibraryUnavailable as exc:
            return {"available": False, "reason": str(exc)}
        return {"available": True, "root": str(root)}

    async def publish_metadata_to_library(self, manga_id: str) -> dict[str, Any]:
        """Write enriched portable metadata into managed CBZ files.

        Reader applications are intentionally not contacted. They discover the
        atomic file updates through their own normal library scan.
        """

        async with self._mutation_lock:
            self.assert_mutations_allowed()
            return await asyncio.to_thread(
                self._publish_metadata_to_library_locked, manga_id
            )

    def _metadata_artwork_path(self, record: dict[str, Any] | None) -> Path | None:
        artwork_path = (record or {}).get("artwork_path")
        if not artwork_path:
            return None
        source = (self.settings.data_dir / str(artwork_path)).resolve()
        data_root = self.settings.data_dir.resolve()
        if (
            data_root not in source.parents
            or source.is_symlink()
            or not source.is_file()
            or source.suffix.casefold() not in READER_ARTWORK_SUFFIXES
        ):
            raise UnsafeLibraryPath("Unsafe metadata artwork cache path")
        return source

    @staticmethod
    def _replace_artwork_sidecar(source: Path, destination: Path) -> bool:
        """Install or update a small metadata sidecar without touching the CBZ."""

        if destination.is_symlink() or (
            destination.exists() and not destination.is_file()
        ):
            raise UnsafeLibraryPath(f"Unsafe artwork sidecar: {destination}")
        source_digest = sha256(source)
        if destination.is_file() and sha256(destination) == source_digest:
            return False
        partial = destination.with_name(
            f".{destination.name}.artwork-{uuid.uuid4().hex}.partial"
        )
        try:
            with source.open("rb") as reader, partial.open("xb") as writer:
                shutil.copyfileobj(reader, writer, length=1024 * 1024)
                writer.flush()
                os.fsync(writer.fileno())
            if sha256(partial) != source_digest or sha256(source) != source_digest:
                raise ValueError("Metadata artwork changed during publication")
            os.replace(partial, destination)
            fsync_directory(destination.parent)
            return True
        finally:
            partial.unlink(missing_ok=True)

    @staticmethod
    def _remove_alternate_sidecars(destination: Path) -> None:
        for suffix in (*READER_ARTWORK_SUFFIXES, ".tbn"):
            alternate = destination.with_suffix(suffix)
            if (
                alternate != destination
                and alternate.is_file()
                and not alternate.is_symlink()
            ):
                alternate.unlink()
        fsync_directory(destination.parent)

    def _publish_metadata_to_library_locked(self, manga_id: str) -> dict[str, Any]:
        manga = self.database.get_manga(manga_id)
        root = self._library_root()
        metadata_record = self.database.get_series_metadata(manga_id)
        canonical = (metadata_record or {}).get("data") or {}
        volumes = {
            item["volume_key"]: item
            for item in self.database.list_volume_metadata(manga_id)
        }
        series_directories: set[Path] = set()
        checked = 0
        updated = 0
        series_covers_written = 0
        book_covers_written = 0
        covers_unchanged = 0
        updated_paths: list[str] = []
        errors: list[dict[str, str]] = []
        for chapter in self.database.list_all_chapters(manga_id):
            if not chapter.get("downloaded") or not chapter.get("library_path"):
                continue
            path = self._recorded_library_path(str(chapter["library_path"]), root)
            if path.suffix.casefold() != ".cbz":
                errors.append(
                    {
                        "chapter_id": str(chapter["id"]),
                        "error": f"Portable metadata requires CBZ: {path.name}",
                    }
                )
                continue
            series_directories.add(path.parent)
            checked += 1
            try:
                snapshot = read_comic_info(path)
                enriched_chapter = dict(chapter)
                volume_record = volumes.get(
                    publication_metadata_key(chapter, canonical)
                )
                enriched_chapter["metadata"] = (volume_record or {}).get("data", {})
                comic_info = build_comic_info(
                    manga, enriched_chapter, int(snapshot["page_count"])
                )
                result = replace_comic_info(path, comic_info)
                if result["changed"]:
                    self.database.mark_chapter_downloaded(
                        str(chapter["id"]), path, str(result["sha256"])
                    )
                    updated += 1
                    updated_paths.append(str(path))
                volume_artwork = self._metadata_artwork_path(volume_record)
                if volume_artwork is not None:
                    destination = path.with_suffix(".jpg")
                    if self._replace_artwork_sidecar(volume_artwork, destination):
                        book_covers_written += 1
                    else:
                        covers_unchanged += 1
                    self._remove_alternate_sidecars(destination)
            except Exception as exc:  # noqa: BLE001 - report one bad book
                errors.append(
                    {
                        "chapter_id": str(chapter["id"]),
                        "error": f"{type(exc).__name__}: {exc}"[:500],
                    }
                )
        try:
            series_artwork = self._metadata_artwork_path(metadata_record)
            if series_artwork is not None:
                for directory in sorted(series_directories, key=str):
                    destination = directory / "cover.jpg"
                    if self._replace_artwork_sidecar(series_artwork, destination):
                        series_covers_written += 1
                    else:
                        covers_unchanged += 1
                    self._remove_alternate_sidecars(destination)
        except Exception as exc:  # noqa: BLE001 - preserve book metadata
            errors.append(
                {
                    "chapter_id": "series-cover",
                    "error": f"{type(exc).__name__}: {exc}"[:500],
                }
            )
        covers_written = series_covers_written + book_covers_written
        return {
            "reader_independent": True,
            "checked": checked,
            "updated": updated,
            "updated_paths": updated_paths,
            "covers_written": covers_written,
            "series_covers_written": series_covers_written,
            "book_covers_written": book_covers_written,
            "covers_unchanged": covers_unchanged,
            "errors": errors,
        }

    def komga_reconciliation_status(self) -> dict[str, Any]:
        operations = [
            operation
            for operation in self.database.list_deletion_operations()
            if operation.get("state") == "committed"
        ]
        errors = [
            str(operation["komga_last_error"])
            for operation in operations
            if operation.get("komga_last_error")
        ]
        return {
            "configured": bool(getattr(self.komga, "configured", False)),
            "pending_operations": len(operations),
            "pending_paths": sum(
                len(operation.get("library_paths") or []) for operation in operations
            ),
            "attempts": sum(
                int(operation.get("komga_attempts") or 0) for operation in operations
            ),
            "last_error": errors[-1] if errors else None,
            "last_result": self.last_komga_reconciliation,
            "library": self.last_komga_library_alignment,
        }

    def set_komga_refresh_post_scan(
        self, callback: Callable[[], Awaitable[dict[str, Any]]] | None
    ) -> None:
        """Attach the canonical metadata publisher run after a full scan."""

        self._komga_refresh_post_scan = callback

    def reset_komga_refresh_schedule(self) -> None:
        """Restart the periodic interval after configuration changes."""

        self._komga_refresh_completed_monotonic = monotonic()

    def komga_periodic_refresh_due(self, *, now: float | None = None) -> bool:
        if bool(getattr(self.komga, "standalone", False)) or not bool(
            getattr(self.komga, "configured", False)
        ):
            return False
        elapsed = (monotonic() if now is None else now) - (
            self._komga_refresh_completed_monotonic
        )
        return elapsed >= self.settings.komga_refresh_interval_minutes * 60

    def komga_refresh_status(self) -> dict[str, Any]:
        configured = bool(getattr(self.komga, "configured", False)) and not bool(
            getattr(self.komga, "standalone", False)
        )
        interval_seconds = self.settings.komga_refresh_interval_minutes * 60
        age = max(0.0, monotonic() - self._komga_refresh_completed_monotonic)
        return {
            "enabled": configured,
            "interval_minutes": self.settings.komga_refresh_interval_minutes,
            "due": configured and age >= interval_seconds,
            "next_due_in_seconds": (
                max(0, round(interval_seconds - age)) if configured else None
            ),
            "last_result": self.last_komga_refresh,
        }

    async def refresh_komga_library(self, *, reason: str) -> dict[str, Any]:
        """Run one coalesced full scan, then reapply canonical Tankarr metadata."""

        requested_at = monotonic()
        async with self._komga_refresh_lock:
            if (
                self.last_komga_refresh is not None
                and self.last_komga_refresh.get("ready")
                and self._komga_refresh_completed_monotonic >= requested_at
            ):
                return {
                    **self.last_komga_refresh,
                    "coalesced": True,
                    "requested_reason": reason,
                }

            attempted_at = datetime.now(UTC).isoformat()
            if bool(getattr(self.komga, "standalone", False)) or not bool(
                getattr(self.komga, "configured", False)
            ):
                result = {
                    "configured": False,
                    "ready": True,
                    "triggered": False,
                    "refresh_reason": reason,
                    "attempted_at": attempted_at,
                }
                self.last_komga_refresh = result
                return result

            try:
                expected = await asyncio.to_thread(self._tracked_library_relative_paths)
                force_scan = getattr(self.komga, "force_scan", None)
                if reason == "manual" and callable(force_scan):
                    scan = await force_scan(expected)
                else:
                    scan = await self.komga.scan(expected)
                result = {
                    "ready": True,
                    **scan,
                    "refresh_reason": reason,
                    "refreshed_at": datetime.now(UTC).isoformat(),
                }
                if self._komga_refresh_post_scan is not None:
                    try:
                        result["metadata_sync"] = await self._komga_refresh_post_scan()
                    except Exception as exc:  # noqa: BLE001 - metadata retries later
                        result["metadata_sync_error"] = f"{type(exc).__name__}: {exc}"[
                            :1000
                        ]
                self._komga_refresh_completed_monotonic = monotonic()
            except Exception as exc:  # noqa: BLE001 - expose and retry when due
                self._komga_refresh_completed_monotonic = monotonic() - (
                    self.settings.komga_refresh_interval_minutes * 60
                )
                result = {
                    "configured": bool(getattr(self.komga, "configured", False)),
                    "ready": False,
                    "triggered": False,
                    "refresh_reason": reason,
                    "attempted_at": attempted_at,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            self.last_komga_refresh = result
            self.last_komga_library_alignment = result
            return result

    def count_gap_series(self) -> list[dict[str, Any]]:
        """Ended works whose catalogue count exceeds every source's numbering.

        Each is either a real gap (the sources stop early) or a count that
        includes what the sources call an extra. Both deserve a name.
        """

        gaps: list[dict[str, Any]] = []
        for entry in self.list_wanted():
            phantom = [
                row
                for row in entry.get("chapters") or []
                if row.get("expected")
                and str(row.get("provider") or "") == "expected"
                and str(row.get("evidence") or "").startswith(
                    ("provider_final", "metadata_total")
                )
            ]
            unmapped = int(entry.get("unmapped_expected_count") or 0)
            if not phantom and not unmapped:
                continue
            numbered = 0
            manga_id = str(entry["manga"]["id"])
            manga = self.database.get_manga(manga_id)
            for release in self.database.list_chapters(
                manga_id, str(manga["preferred_language"])
            ):
                number = canonical_number(release.get("chapter"))
                try:
                    value = (
                        int(Decimal(number))
                        if number and Decimal(number) == int(Decimal(number))
                        else None
                    )
                except (InvalidOperation, ValueError):
                    value = None
                if value is not None:
                    numbered = max(numbered, value)
            gaps.append(
                {
                    "manga_id": manga_id,
                    "title": entry["manga"]["title"],
                    "expected": entry.get("expected_count"),
                    "numbered": numbered,
                    "phantom": len(phantom),
                    "unmapped": unmapped,
                }
            )
        return gaps

    ORPHAN_SUMMARY_MAX_AGE_SECONDS = 600.0

    def library_orphans_summary(self) -> dict[str, Any]:
        """The orphan scan for alerts, at most once every ten minutes.

        The full scan stats every tracked file (about a second per 15,000
        files) and the System page polls the status that carries the alert.
        An explicit scan or a change to the library refreshes it at once.
        """

        cached = self._orphan_summary
        if cached is not None and monotonic() - cached[0] < (
            self.ORPHAN_SUMMARY_MAX_AGE_SECONDS
        ):
            return cached[1]
        return self.library_orphans(folder_limit=5)

    def invalidate_orphan_summary(self) -> None:
        self._orphan_summary = None

    def library_orphans(self, *, folder_limit: int = 100) -> dict[str, Any]:
        """Library files no Tankarr release claims, grouped by folder.

        A series deleted from Tankarr without its files leaves them on disk;
        the reader keeps serving them and the two views of the library drift
        apart silently. Measured live: 27 chapters of a work removed from
        Tankarr stayed in Stump for two days as "stale books". This names
        them so the drift is always visible - the files themselves are only
        removed when the operator says so.
        """

        try:
            root = self._library_root()
        except LibraryUnavailable as exc:
            self._orphan_summary = None
            return {"available": False, "reason": str(exc), "count": 0, "folders": []}
        tracked: set[Path] = set()
        for recorded in self.database.list_tracked_library_paths():
            try:
                tracked.add(self._recorded_library_path(recorded, root))
            except (UnsafeLibraryPath, ValueError):
                continue
        by_folder: dict[str, dict[str, Any]] = {}
        total = 0
        total_bytes = 0
        for path in root.rglob("*.cbz"):
            if any(part.startswith(".") for part in path.relative_to(root).parts):
                continue  # quarantine and hidden staging directories
            if path in tracked or not path.is_file():
                continue
            folder = path.parent.relative_to(root).as_posix() or "."
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            entry = by_folder.setdefault(
                folder, {"folder": folder, "files": 0, "bytes": 0, "sample": []}
            )
            entry["files"] += 1
            entry["bytes"] += size
            if len(entry["sample"]) < 3:
                entry["sample"].append(path.name)
            total += 1
            total_bytes += size
        folders = sorted(by_folder.values(), key=lambda item: -item["files"])
        report = {
            "available": True,
            "count": total,
            "bytes": total_bytes,
            "folders": folders[:folder_limit],
            "truncated": len(folders) > folder_limit,
        }
        # Every scan is the freshest summary there is.
        self._orphan_summary = (
            monotonic(),
            {**report, "folders": folders[:5], "truncated": len(folders) > 5},
        )
        return report

    async def delete_library_orphans(
        self, folders: list[str] | None = None
    ) -> dict[str, Any]:
        """Remove untracked library files, by folder, and re-align the reader."""

        async with self._mutation_lock:
            self.assert_mutations_allowed()
            root = self._library_root()
            report = self.library_orphans(folder_limit=10_000)
            wanted = {str(item) for item in folders} if folders else None
            targets: list[Path] = []
            for entry in report["folders"]:
                if wanted is not None and entry["folder"] not in wanted:
                    continue
                base = root if entry["folder"] == "." else root / entry["folder"]
                targets.extend(sorted(base.glob("*.cbz")))
            # Re-check: only files still untracked at this instant are removed.
            fresh = {
                path
                for entry in self.library_orphans(folder_limit=10_000)["folders"]
                for path in (
                    root if entry["folder"] == "." else root / entry["folder"]
                ).glob("*.cbz")
            }
            targets = [path for path in targets if path in fresh]
            if not targets:
                return {"deleted": 0, "folders": [], "bytes": 0, "errors": []}
            quarantine = self._stage_library_files(targets)
            result = self._commit_file_quarantine(quarantine)
            removed_folders = sorted(
                {path.parent.relative_to(root).as_posix() or "." for path in targets}
            )
            self._remove_empty_directories({path.parent for path in targets}, root)
            self._orphan_summary = None
            scan = await self._request_komga_reconciliation(
                not result["cleanup_errors"]
                and not result["quarantine_files_remaining"]
            )
            return {
                "deleted": len(targets),
                "folders": removed_folders,
                "bytes": sum(entry["bytes"] for entry in report["folders"]),
                "errors": result["cleanup_errors"],
                "reader_scan": scan,
            }

    def _tracked_library_relative_paths(self) -> tuple[str, ...]:
        root = self._library_root()
        relative_paths: list[str] = []
        for chapter in self.database.list_downloaded_chapters():
            recorded = chapter.get("library_path")
            if not recorded:
                raise UnsafeLibraryPath(
                    f"Downloaded chapter {chapter['id']} has no library path"
                )
            path = self._recorded_library_path(str(recorded), root)
            if path.is_symlink() or not path.is_file():
                raise FileNotFoundError(f"Tracked library file is missing: {path}")
            relative_paths.append(path.relative_to(root).as_posix())
        return tuple(dict.fromkeys(relative_paths))

    @staticmethod
    def _series_names_books(chapters: list[dict[str, Any]]) -> bool:
        """Whether this series' filenames carry the book number.

        The name follows the majority of its own files. A series filed into
        books keeps that number even where one extra, a prologue say, sits
        outside every book; a series that is not filed into books drops the
        tag one source happened to attach to a handful of chapters, which
        would otherwise sort those few files away from all their neighbours.
        """

        downloaded = [
            chapter
            for chapter in chapters
            if chapter.get("downloaded") and chapter.get("library_path")
        ]
        if not downloaded:
            return True
        tagged = sum(
            1 for chapter in downloaded if str(chapter.get("volume") or "").strip()
        )
        return tagged * 2 > len(downloaded)

    async def _purge_stale_komga_trash(
        self, expected: tuple[str, ...]
    ) -> dict[str, Any]:
        root = self._library_root()

        def path_exists(relative: str) -> bool:
            candidate = Path(relative)
            if candidate.is_absolute() or ".." in candidate.parts:
                return True  # never treat an unsafe path as safely gone
            target = root / candidate
            if target.is_dir():
                # A series directory left behind with nothing inside (or only
                # Tankarr's own hidden .partial leftovers) is not a series;
                # Komga's record for it is debris like any other.
                return any(not entry.name.startswith(".") for entry in target.iterdir())
            return target.exists()

        try:
            result = await self.komga.purge_stale_trash(
                expected, path_exists=path_exists
            )
        except Exception as exc:  # noqa: BLE001 - trash cleanup is best effort
            # Best effort is not the same as unreported: a reader that has no
            # such call, or refuses it, leaves a record for every file ever
            # renamed away, and the shelf fills with entries whose file is
            # gone. Nobody sees that until they open the reader, so say it.
            logger.warning(
                "Could not empty the reader's trash: %s: %s", type(exc).__name__, exc
            )
            return {"purged": False, "error": f"{type(exc).__name__}: {exc}"}
        if result.get("reason"):
            logger.warning("Reader trash was left alone: %s", result["reason"])
        if result.get("purged"):
            logger.info(
                "Emptied Komga trash: %s renamed and %s removed book record(s), "
                "%s series record(s)",
                result.get("renamed_books"),
                result.get("removed_books"),
                result.get("stale_series"),
            )
        return result

    async def reconcile_komga_library(self) -> dict[str, Any]:
        """Audit managed files; legacy reader integrations remain testable."""

        if bool(getattr(self.komga, "standalone", False)):
            alignment = {
                "configured": bool(getattr(self.komga, "configured", False)),
                "reader_independent": True,
                "ready": True,
                "triggered": False,
            }
            self.last_komga_library_alignment = alignment
            return alignment
        try:
            expected = await asyncio.to_thread(self._tracked_library_relative_paths)
            result = await self.komga.ensure_present(expected)
            alignment = {"ready": True, **result}
            if hasattr(self.komga, "purge_stale_trash"):
                # ensure_present proved every tracked file is mounted and
                # imported, so leftover trash is rename/removal debris.
                alignment["trash"] = await self._purge_stale_komga_trash(expected)
        except Exception as exc:  # noqa: BLE001 - expose and retry drift
            alignment = {
                "configured": bool(getattr(self.komga, "configured", False)),
                "ready": False,
                "triggered": False,
                "error": f"{type(exc).__name__}: {exc}",
            }
        self.last_komga_library_alignment = alignment
        return alignment

    async def organize_library(
        self,
        *,
        dry_run: bool = False,
        confirmation_snapshot: str | None = None,
        reconcile_reader: bool = True,
    ) -> dict[str, Any]:
        """Normalize every tracked download without overwriting library content."""
        if not dry_run:
            self._orphan_summary = None

        if not dry_run and getattr(self.settings, "restored_safe_mode", False):
            self.assert_mutations_allowed()
        komga_prepare: dict[str, Any] | None = None
        komga_prepare_error: str | None = None
        async with self._mutation_lock:
            if confirmation_snapshot is not None:
                preview = await asyncio.to_thread(
                    self._organize_library_files, self._library_root(), True
                )
                if (
                    not preview.get("snapshot")
                    or preview["snapshot"] != confirmation_snapshot
                ):
                    raise ValueError(
                        "Library repair preview is stale; review a new preview before applying."
                    )
                if (
                    preview.get("organization_blocked")
                    or preview.get("deferred")
                    or preview.get("planned_duplicate_removals")
                    or preview.get("replacement_files_deferred")
                    or any(
                        not action["safe"] for action in preview.get("actions") or []
                    )
                ):
                    raise ValueError(
                        "Library repair needs manual review; no file was changed."
                    )
            if (
                reconcile_reader
                and not dry_run
                and bool(getattr(self.komga, "configured", False))
            ):
                try:
                    komga_prepare = await self.komga.prepare_for_moves()
                except Exception as exc:  # noqa: BLE001 - fail closed only for moves
                    komga_prepare_error = f"{type(exc).__name__}: {exc}"
            recovery = self.last_deletion_recovery
            if recovery is not None and recovery.get("recovery_blocked"):
                result = self._organization_report(
                    warnings=["Library organization is waiting for deletion recovery"],
                    organization_blocked=True,
                )
            else:
                try:
                    root = self._library_root()
                    if komga_prepare_error:
                        preview = await asyncio.to_thread(
                            self._organize_library_files, root, True
                        )
                        if preview.get("planned_moves"):
                            result = self._organization_report(
                                chapters_scanned=preview["chapters_scanned"],
                                downloaded_files=preview["downloaded_files"],
                                warnings=[
                                    "Library organization needs to rename files, but "
                                    "Komga file hashing could not be verified: "
                                    f"{komga_prepare_error}"
                                ],
                                organization_blocked=True,
                            )
                        else:
                            result = await asyncio.to_thread(
                                self._organize_library_files, root, dry_run
                            )
                    else:
                        result = await asyncio.to_thread(
                            self._organize_library_files, root, dry_run
                        )
                except Exception as exc:  # noqa: BLE001 - expose a durable block
                    result = self._organization_report(
                        warnings=[
                            f"Library organization failed: {type(exc).__name__}: {exc}"
                        ],
                        organization_blocked=True,
                    )
            if not dry_run:
                self.last_library_organization = result

        if reconcile_reader:
            result["komga_scan"] = await self._request_komga_scan(
                bool(result.get("scan_required")) and not dry_run
            )
        else:
            result["komga_scan"] = {
                "requested": bool(result.get("scan_required")) and not dry_run,
                "triggered": False,
                "deferred": True,
                "reason": "Reader maintenance reconciles the index after startup",
            }
        if komga_prepare is not None:
            result["komga_prepare"] = komga_prepare
        if not dry_run:
            self.last_library_organization = result
        return result

    def _organize_library_files(
        self,
        root: Path,
        dry_run: bool = False,
        manga_ids: set[str] | None = None,
    ) -> dict[str, Any]:
        pending_jobs = [
            job
            for job in self.database.list_pending_jobs()
            if (manga_ids is None or str(job["manga_id"]) in manga_ids)
        ]
        active_jobs = [
            job
            for job in pending_jobs
            if job["status"] in {"running", "downloading", "packaging", "importing"}
        ]
        if active_jobs:
            summary = ", ".join(f"{job['id']} ({job['status']})" for job in active_jobs)
            return self._organization_report(
                warnings=[
                    "Library organization was deferred while downloads are active: "
                    f"{summary}"
                ],
                deferred=True,
            )

        chapters_scanned = self.database.count_chapters(manga_ids)
        downloaded_files = 0
        unchanged = 0
        hashed = 0
        warnings: list[str] = []
        actions: list[dict[str, Any]] = []
        candidates: list[tuple[dict[str, Any], dict[str, Any], Path, Path]] = []
        desired_owners: dict[Path, list[str]] = {}

        downloaded_by_manga: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for chapter in self.database.list_downloaded_chapters(manga_ids):
            downloaded_by_manga[str(chapter["manga_id"])].append(chapter)
        job_paths = self.database.chapter_job_paths_by_release()
        for manga in self.database.list_manga():
            if manga_ids is not None and str(manga["id"]) not in manga_ids:
                continue
            chapters = downloaded_by_manga.get(str(manga["id"]), [])
            names_books = self._series_names_books(chapters)
            for chapter in chapters:
                downloaded_files += 1
                recorded = chapter.get("library_path")
                if not recorded:
                    warnings.append(
                        f"Downloaded chapter {chapter['id']} has no recorded library path"
                    )
                    continue
                try:
                    source = self._recorded_library_path(str(recorded), root)
                    named = (
                        chapter
                        if names_books or not chapter.get("volume")
                        else {**chapter, "volume": None}
                    )
                    destination = self._confined_library_path(
                        final_library_path(root, manga, named), root
                    )
                except (UnsafeLibraryPath, ValueError) as exc:
                    warnings.append(f"Chapter {chapter['id']}: {exc}")
                    continue
                desired_owners.setdefault(destination, []).append(chapter["id"])
                candidates.append((manga, chapter, source, destination))
        snapshot_candidates = list(candidates)

        # After a restart a replacement may be durable but not yet retired or
        # normalized. It is not an accidental duplicate of the old release.
        # Its queued worker owns both paths and must recover before maintenance
        # touches either of them; unrelated chapters are still organized.
        downloaded_chapter_ids = {str(item[1]["id"]) for item in candidates}
        pending_replacements = [
            job
            for job in pending_jobs
            if str(job.get("supersedes_chapter_id") or "") in downloaded_chapter_ids
        ]
        replacement_chapters = {
            str(chapter_id)
            for job in pending_replacements
            for chapter_id in (job["chapter_id"], job["supersedes_chapter_id"])
        }
        replacement_paths: set[Path] = set()
        for job in pending_replacements:
            if job.get("planned_path"):
                try:
                    replacement_paths.add(
                        self._recorded_library_path(str(job["planned_path"]), root)
                    )
                except (UnsafeLibraryPath, ValueError) as exc:
                    warnings.append(f"Replacement job {job['id']}: {exc}")
        for _manga, chapter, source, destination in candidates:
            if chapter["id"] in replacement_chapters:
                replacement_paths.update((source, destination))
        eligible_candidates = [
            item
            for item in candidates
            if item[1]["id"] not in replacement_chapters
            and item[2] not in replacement_paths
            and item[3] not in replacement_paths
        ]
        replacement_files_deferred = len(candidates) - len(eligible_candidates)
        candidates = eligible_candidates

        # Two sources can describe the same canonical chapter with different
        # volume metadata. Their final filenames then differ even though the
        # library owns duplicate content, so a destination-only collision
        # check never sees them. Resolve the logical chapter first and keep
        # the best measured/source-ranked copy independent of its old name.
        logical_claims: dict[
            tuple[str, str, str],
            list[tuple[dict[str, Any], Path]],
        ] = defaultdict(list)
        for _manga, chapter, source, _destination in candidates:
            if str(chapter.get("release_unit") or "chapter") == "volume":
                continue
            chapter_number = str(
                chapter.get("canonical_chapter") or chapter.get("chapter") or ""
            )
            if not chapter_number:
                continue
            logical_claims[
                (
                    str(chapter["manga_id"]),
                    str(chapter.get("language") or ""),
                    chapter_number,
                )
            ].append((chapter, source))

        duplicates_removed = 0
        planned_duplicate_removals = 0
        logical_losers: set[str] = set()
        for claims in logical_claims.values():
            if len(claims) < 2:
                continue
            keeper = self._preferred_duplicate_download_keeper(claims)
            if keeper is None:
                continue
            logical_losers.update(
                str(chapter["id"])
                for chapter, _source in claims
                if chapter["id"] != keeper["id"]
            )
        retained_paths = {
            source
            for _manga, chapter, source, _destination in candidates
            if str(chapter["id"]) not in logical_losers
        }
        logical_loser_paths = {
            source
            for _manga, chapter, source, _destination in candidates
            if str(chapter["id"]) in logical_losers
        }
        for manga, chapter, source, destination in list(candidates):
            if str(chapter["id"]) not in logical_losers:
                continue
            planned_duplicate_removals += 1
            if dry_run:
                actions.append(
                    self._repair_action(
                        chapter["id"],
                        "duplicate",
                        source,
                        destination,
                        "Another tracked release owns this canonical chapter; review both files before removal.",
                        safe=False,
                    )
                )
                continue
            self.database.forget_duplicate_download(chapter["id"])
            if source.exists() and source not in retained_paths:
                try:
                    quarantine = self._stage_library_files([source])
                    self._commit_file_quarantine(quarantine)
                except Exception as exc:  # noqa: BLE001 - fall back to a plain removal
                    logger.info(
                        "Logical duplicate %s removed without the deletion journal: %s",
                        source,
                        exc,
                    )
                    source.unlink(missing_ok=True)
            logger.info(
                "Removed logical duplicate %s (%s): chapter %s of %s",
                chapter["id"],
                source,
                chapter.get("chapter"),
                manga.get("title") or manga.get("id"),
            )
            duplicates_removed += 1
        candidates = [
            item for item in candidates if str(item[1]["id"]) not in logical_losers
        ]

        desired_owners = {}
        for _manga, chapter, _source, destination in candidates:
            desired_owners.setdefault(destination, []).append(chapter["id"])

        # Aligning a source to the official numbering can leave two downloaded
        # files claiming the same chapter: the copy that already sits at the
        # destination was named under the trusted numbering and stays; the
        # other is the same chapter fetched from a second source, so it is a
        # duplicate the library never needs.
        for destination, owners in desired_owners.items():
            if len(owners) < 2:
                continue
            claims = [
                (chapter, source)
                for _manga, chapter, source, wanted in candidates
                if wanted == destination
            ]
            keeper = self._duplicate_download_keeper(claims, destination)
            if keeper is None:
                warnings.append(
                    "Naming collision for tracked chapters "
                    f"{', '.join(sorted(owners))}: {destination}"
                )
                continue
            other_paths = {
                source for chapter, source in claims if chapter["id"] != keeper["id"]
            }
            for chapter, source in claims:
                if chapter["id"] == keeper["id"]:
                    continue
                planned_duplicate_removals += 1
                if dry_run:
                    actions.append(
                        self._repair_action(
                            chapter["id"],
                            "duplicate",
                            source,
                            destination,
                            "Another tracked release claims this destination; review both files before removal.",
                            safe=False,
                        )
                    )
                    continue
                self.database.forget_duplicate_download(chapter["id"])
                if source != destination and source.exists() and source in other_paths:
                    # Same journaled two-phase deletion as every other file
                    # Tankarr removes: a crash between the ledger and the
                    # disk is recovered at startup instead of leaving a
                    # stray file, and the operation is on record.
                    try:
                        quarantine = self._stage_library_files([source])
                        self._commit_file_quarantine(quarantine)
                    except Exception as exc:  # noqa: BLE001 - fall back to a plain removal
                        logger.info(
                            "Duplicate %s removed without the deletion journal: %s",
                            source,
                            exc,
                        )
                        source.unlink(missing_ok=True)
                logger.info(
                    "Removed duplicate download %s (%s): chapter %s of %s is kept as %s",
                    chapter["id"],
                    source,
                    chapter.get("chapter"),
                    chapter.get("manga_id"),
                    keeper["id"],
                )
                duplicates_removed += 1
            candidates = [
                item
                for item in candidates
                if item[3] != destination or item[1]["id"] == keeper["id"]
            ]

        # Renumbering a source shifts a whole run of files, so a destination
        # is legitimate when the file sitting there is itself moving away.
        moving_sources = {
            source
            for _manga, _chapter, source, destination in candidates
            if source != destination and source.exists()
        }
        plans: list[LibraryRelocation] = []
        if not warnings:
            for manga, chapter, source, destination in candidates:
                del manga  # the destination already contains the metadata snapshot
                recorded = str(chapter["library_path"])
                expected_digest = self._expected_library_digest(chapter)
                source_exists = source.exists()
                destination_exists = destination.exists()

                if source == destination:
                    if not source_exists:
                        warnings.append(f"Tracked library file is missing: {source}")
                        continue
                    stored_digest = chapter.get("library_sha256")
                    if isinstance(stored_digest, str) and self._valid_sha256(
                        stored_digest
                    ):
                        digest = stored_digest
                    else:
                        try:
                            digest = sha256(source)
                        except OSError as exc:
                            warnings.append(f"Unable to hash {source}: {exc}")
                            continue
                        if expected_digest is not None and digest != expected_digest:
                            warnings.append(
                                "Tracked library hash does not match persisted "
                                f"import evidence: {source}"
                            )
                            continue
                        hashed += 1
                    job_updates = self._job_path_updates_for_relocation(
                        chapter["id"],
                        source,
                        destination,
                        root,
                        jobs=job_paths.get(str(chapter["id"]), []),
                    )
                    if (
                        stored_digest in {None, ""}
                        or recorded != str(destination)
                        or job_updates
                    ):
                        plans.append(
                            LibraryRelocation(
                                manga_id=chapter["manga_id"],
                                chapter_id=chapter["id"],
                                recorded_library_path=recorded,
                                source=source,
                                destination=destination,
                                digest=digest,
                                job_path_updates=job_updates,
                                records_new_hash=stored_digest in {None, ""},
                            )
                        )
                    unchanged += 1
                    continue

                if (
                    source_exists
                    and destination_exists
                    and destination not in moving_sources
                    and destination not in logical_loser_paths
                ):
                    warnings.append(
                        "Refusing to overwrite an existing standardized path: "
                        f"{destination} (source: {source})"
                    )
                    continue
                if not source_exists and not destination_exists:
                    warnings.append(f"Tracked library file is missing: {source}")
                    continue

                candidate_path = source if source_exists else destination
                if candidate_path.is_symlink() or not candidate_path.is_file():
                    warnings.append(f"Refusing unsafe library entry: {candidate_path}")
                    continue
                try:
                    actual_digest = sha256(candidate_path)
                except OSError as exc:
                    warnings.append(f"Unable to hash {candidate_path}: {exc}")
                    continue
                if expected_digest is not None and actual_digest != expected_digest:
                    warnings.append(
                        "Tracked library hash does not match persisted import evidence: "
                        f"{candidate_path}"
                    )
                    continue
                if not source_exists and expected_digest is None:
                    warnings.append(
                        "Refusing to adopt a standardized file without persisted "
                        f"identity evidence: {destination}"
                    )
                    continue
                if expected_digest is None:
                    hashed += 1
                try:
                    job_updates = self._job_path_updates_for_relocation(
                        chapter["id"],
                        source,
                        destination,
                        root,
                        jobs=job_paths.get(str(chapter["id"]), []),
                    )
                except UnsafeLibraryPath as exc:
                    warnings.append(f"Chapter {chapter['id']}: {exc}")
                    continue
                plans.append(
                    LibraryRelocation(
                        manga_id=chapter["manga_id"],
                        chapter_id=chapter["id"],
                        recorded_library_path=recorded,
                        source=source,
                        destination=destination,
                        digest=actual_digest,
                        job_path_updates=job_updates,
                        adopt_existing=not source_exists,
                        records_new_hash=expected_digest is None,
                    )
                )

        if warnings:
            return self._organization_report(
                chapters_scanned=chapters_scanned,
                downloaded_files=downloaded_files,
                unchanged=unchanged,
                hashes_to_record=hashed,
                warnings=warnings,
                organization_blocked=True,
            )

        if dry_run:
            _ordered, cycles = self._relocation_order(plans)
            cyclic_ids = {plan.chapter_id for plan in cycles}
            for plan in plans:
                kind = (
                    "adopt"
                    if plan.adopt_existing
                    else "move"
                    if plan.source != plan.destination
                    else "record_hash"
                )
                actions.append(
                    self._repair_action(
                        plan.chapter_id,
                        kind,
                        plan.source,
                        plan.destination,
                        "Restore the tracked managed path and integrity record."
                        if kind != "move"
                        else "Normalize the filename without replacing existing content.",
                        safe=plan.chapter_id not in cyclic_ids,
                    )
                )
            paths = {root, root / ".tankarr-library-id"}
            for _manga, _chapter, source, destination in snapshot_candidates:
                paths.update((source, destination))
            files = []
            for path in sorted(paths):
                try:
                    stat = path.stat(follow_symlinks=False)
                    identity = (
                        stat.st_dev,
                        stat.st_ino,
                        stat.st_mode,
                        stat.st_size,
                        stat.st_mtime_ns,
                        stat.st_ctime_ns,
                    )
                except FileNotFoundError:
                    identity = None
                files.append((str(path), identity))
            snapshot = hashlib.sha256(
                json.dumps(
                    {
                        "root": str(root),
                        "revision": self.database.library_revision(),
                        "files": files,
                        "actions": actions,
                        "jobs": [
                            (
                                job["id"],
                                job["chapter_id"],
                                job["status"],
                                job.get("planned_path"),
                            )
                            for job in pending_jobs
                        ],
                        "digests": [
                            (plan.chapter_id, plan.digest, plan.job_path_updates)
                            for plan in plans
                        ],
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                ).encode("utf-8")
            ).hexdigest()
            return self._organization_report(
                dry_run=True,
                actions=actions,
                snapshot=snapshot,
                replacement_files_deferred=replacement_files_deferred,
                chapters_scanned=chapters_scanned,
                downloaded_files=downloaded_files,
                unchanged=unchanged,
                planned_moves=sum(
                    not plan.adopt_existing and plan.source != plan.destination
                    for plan in plans
                ),
                planned_adoptions=sum(plan.adopt_existing for plan in plans),
                planned_duplicate_removals=planned_duplicate_removals,
                hashes_to_record=hashed,
            )

        moved = 0
        adopted = 0
        normalized_records = 0
        hashes_recorded = 0
        source_parents: set[Path] = set()
        execution_warnings: list[str] = []
        ordered, cycles = self._relocation_order(plans)
        for plan in cycles:
            # Two files that want each other's name cannot both move without a
            # temporary name; that is rare and worth a look, not a blocked
            # library: the rest of the pass still runs.
            execution_warnings.append(
                f"Chapter {plan.chapter_id}: destination {plan.destination} is "
                "held by another file that wants this one's name (swap); "
                "left in place"
            )
        blocked_by_failure = False
        for plan in ordered:
            try:
                changed = self._commit_library_relocation(plan)
            except Exception as exc:  # noqa: BLE001 - retain every safe prior move
                execution_warnings.append(
                    f"Chapter {plan.chapter_id}: {type(exc).__name__}: {exc}"
                )
                blocked_by_failure = True
                break
            if plan.adopt_existing:
                adopted += 1
            elif plan.source != plan.destination:
                moved += 1
                source_parents.add(plan.source.parent)
            elif changed:
                normalized_records += 1
            if plan.records_new_hash:
                hashes_recorded += 1

        directories_removed = 0
        if source_parents:
            try:
                directories_removed = self._remove_empty_directories(
                    source_parents, root
                )
            except (OSError, UnsafeLibraryPath) as exc:
                execution_warnings.append(
                    f"Unable to remove empty legacy directories: {exc}"
                )

        return self._organization_report(
            chapters_scanned=chapters_scanned,
            replacement_files_deferred=replacement_files_deferred,
            downloaded_files=downloaded_files,
            moved=moved,
            adopted=adopted,
            unchanged=unchanged,
            normalized_records=normalized_records,
            hashes_recorded=hashes_recorded,
            directories_removed=directories_removed,
            duplicates_removed=duplicates_removed,
            scan_required=bool(moved or adopted or duplicates_removed),
            warnings=execution_warnings,
            organization_blocked=blocked_by_failure,
        )

    @staticmethod
    def _repair_action(
        chapter_id: str,
        kind: str,
        source: Path,
        destination: Path,
        reason: str,
        *,
        safe: bool,
    ) -> dict[str, Any]:
        identity = json.dumps([chapter_id, kind, str(source), str(destination)])
        return {
            "id": hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24],
            "kind": kind,
            "path": str(source),
            "destination": str(destination),
            "reason": reason,
            "safe": safe,
        }

    @staticmethod
    def _relocation_order(
        plans: list[LibraryRelocation],
    ) -> tuple[list[LibraryRelocation], list[LibraryRelocation]]:
        """Order moves so a destination is free when its plan runs.

        Renumbering shifts a run of files onto each other's names; running the
        plan whose destination is already free first turns that into a simple
        cascade. Returns the runnable order and the plans left in a true cycle
        (a swap), which need a temporary name and are reported instead.
        """

        occupied = {plan.source for plan in plans if plan.source.exists()}
        occupied.update(plan.destination for plan in plans if plan.destination.exists())
        pending = list(plans)
        ordered: list[LibraryRelocation] = []
        progress = True
        while pending and progress:
            progress = False
            waiting: list[LibraryRelocation] = []
            for plan in pending:
                # An adoption records a file already sitting at its
                # destination; nothing moves, so nothing has to be free first.
                moves = plan.source != plan.destination and not plan.adopt_existing
                if moves and plan.destination in occupied:
                    waiting.append(plan)
                    continue
                ordered.append(plan)
                occupied.discard(plan.source)
                occupied.add(plan.destination)
                progress = True
            pending = waiting
        return ordered, pending

    def _commit_library_relocation(self, plan: LibraryRelocation) -> bool:
        if plan.source != plan.destination:
            candidate = plan.destination if plan.adopt_existing else plan.source
            if sha256(candidate) != plan.digest:
                raise RuntimeError(
                    f"Library file changed after preflight validation: {candidate}"
                )
        self.database.record_chapter_library_sha256(
            plan.chapter_id, plan.recorded_library_path, plan.digest
        )
        moved = False
        if plan.source != plan.destination and not plan.adopt_existing:
            plan.destination.parent.mkdir(parents=True, exist_ok=True)
            if plan.destination.exists():
                raise FileExistsError(
                    f"Standardized destination appeared during migration: {plan.destination}"
                )
            publish_without_overwrite(plan.source, plan.destination)
            moved = True
            try:
                self._fsync_directories({plan.source.parent, plan.destination.parent})
            except Exception:
                publish_without_overwrite(plan.destination, plan.source)
                self._fsync_directories({plan.source.parent, plan.destination.parent})
                raise
        try:
            self.database.relocate_chapter_library_file(
                plan.chapter_id,
                plan.recorded_library_path,
                plan.destination,
                plan.digest,
                plan.job_path_updates,
            )
        except Exception:
            if moved and plan.destination.exists() and not plan.source.exists():
                publish_without_overwrite(plan.destination, plan.source)
                self._fsync_directories({plan.source.parent, plan.destination.parent})
            raise
        return plan.recorded_library_path != str(plan.destination) or bool(
            plan.job_path_updates
        )

    def _organize_imported_chapter_locked(self, chapter_id: str, digest: str) -> Path:
        """Normalize a recovered or metadata-renamed import before Komga scans it."""

        root = self._library_root()
        chapter = self.database.get_chapter(chapter_id)
        manga = self.database.get_manga(chapter["manga_id"])
        recorded = chapter.get("library_path")
        if not recorded:
            raise RuntimeError(
                f"Downloaded chapter {chapter_id} has no recorded library path"
            )
        source = self._recorded_library_path(str(recorded), root)
        destination = self._confined_library_path(
            final_library_path(root, manga, chapter), root
        )
        if source == destination and str(recorded) == str(destination):
            return destination
        if not source.exists() or source.is_symlink() or not source.is_file():
            raise FileNotFoundError(f"Imported library file is unavailable: {source}")
        if destination.exists() and destination != source:
            raise FileExistsError(
                f"Refusing to overwrite standardized library file: {destination}"
            )
        self._assert_library_paths_not_shared(manga["id"], {chapter_id}, [destination])
        plan = LibraryRelocation(
            manga_id=manga["id"],
            chapter_id=chapter_id,
            recorded_library_path=str(recorded),
            source=source,
            destination=destination,
            digest=digest,
            job_path_updates=self._job_path_updates_for_relocation(
                chapter_id, source, destination, root
            ),
        )
        self._commit_library_relocation(plan)
        if source.parent != destination.parent:
            self._remove_empty_directories({source.parent}, root)
        return destination

    def _expected_library_digest(self, chapter: dict[str, Any]) -> str | None:
        stored = chapter.get("library_sha256")
        if isinstance(stored, str) and self._valid_sha256(stored):
            return stored
        if stored not in {None, ""}:
            raise UnsafeLibraryPath(
                f"Invalid stored library hash for chapter {chapter['id']}"
            )
        return self.database.chapter_archive_sha256(chapter["id"])

    def _job_path_updates_for_relocation(
        self,
        chapter_id: str,
        source: Path,
        destination: Path,
        root: Path,
        *,
        jobs: list[dict[str, Any]] | None = None,
    ) -> list[tuple[int, str, str]]:
        updates: list[tuple[int, str, str]] = []
        for job in (
            jobs
            if jobs is not None
            else self.database.list_chapter_job_paths(chapter_id)
        ):
            for field in ("planned_path", "result_path"):
                raw_path = job.get(field)
                if not raw_path:
                    continue
                canonical = self._recorded_library_path(str(raw_path), root)
                if canonical == destination:
                    continue
                if canonical == source:
                    updates.append((int(job["id"]), field, str(raw_path)))
                elif field == "result_path" or job["status"] in {
                    "queued",
                    "running",
                    "downloading",
                    "packaging",
                    "importing",
                }:
                    raise UnsafeLibraryPath(
                        f"Download job {job['id']} {field} points to a different "
                        f"library file: {raw_path}"
                    )
        return updates

    @staticmethod
    def _valid_sha256(value: str) -> bool:
        return len(value) == 64 and all(
            character in "0123456789abcdef" for character in value
        )

    def _duplicate_download_keeper(
        self, claims: list[tuple[dict[str, Any], Path]], destination: Path
    ) -> dict[str, Any] | None:
        """The download that keeps a contested library name, or None."""

        # Only copies of one chapter can be duplicates of each other. When the
        # claims name different chapters the filename collapsed instead, and
        # deleting the loser would destroy content nothing else holds: report
        # it and leave both alone. An unnumbered claim is never resolved here
        # either, because every unnumbered file wears the same fallback name.
        numbers = {
            str(chapter.get("canonical_chapter") or chapter.get("chapter") or "")
            for chapter, _source in claims
        }
        if len(numbers) > 1 or "" in numbers:
            return None

        settled = [
            chapter
            for chapter, source in claims
            if source == destination and source.exists()
        ]
        if len(settled) == 1:
            return settled[0]
        if settled:
            return None
        return self._preferred_duplicate_download_keeper(claims)

    def _preferred_duplicate_download_keeper(
        self, claims: list[tuple[dict[str, Any], Path]]
    ) -> dict[str, Any] | None:
        """Choose the best present copy of one canonical chapter."""

        present = [(chapter, source) for chapter, source in claims if source.exists()]
        if not present:
            return None
        manga_id = str(present[0][0]["manga_id"])
        demoted = set(self.database.demoted_sources(manga_id))
        quality = self.database.page_quality(manga_id)
        present.sort(
            key=lambda item: self._duplicate_keeper_key(item[0], demoted, quality)
        )
        return present[0][0]

    @staticmethod
    def _duplicate_keeper_key(
        chapter: dict[str, Any],
        demoted: set[str],
        quality: dict[str, dict[str, Any]],
    ) -> tuple[Any, ...]:
        """Order two files of the same chapter: the better one sorts first.

        A demoted source loses. Then the file measured as degraded loses to
        one measured fine, wider pages win (a scan at 1600 px beats the same
        chapter at 800 px), then the source class (a group's own release,
        curated scanlation, official platform, aggregator), then the file
        with more pages, then the older id so the result is stable.
        """

        from tankarr.source_ranking import source_class

        measured = quality.get(str(chapter["id"])) or {}
        verdict = str(measured.get("verdict") or "")
        class_rank = {
            "origin": 0,
            "scan_hq": 1,
            "official": 2,
            "aggregator": 3,
            "unknown": 4,
        }
        klass = source_class(
            provider=chapter.get("provider"),
            source_key=chapter.get("source_key"),
            source_name=chapter.get("source_name"),
        )
        try:
            pages = int(chapter.get("pages") or 0)
        except (TypeError, ValueError):
            pages = 0
        return (
            str(chapter.get("source_key") or "") in demoted,
            verdict == "degraded",
            -int(measured.get("median_width") or 0),
            class_rank.get(klass, 4),
            -pages,
            str(chapter["id"]),
        )

    @staticmethod
    def _organization_report(**updates: Any) -> dict[str, Any]:
        result: dict[str, Any] = {
            "naming_version": LIBRARY_NAMING_VERSION,
            "naming_format": LIBRARY_NAMING_FORMAT,
            "chapters_scanned": 0,
            "downloaded_files": 0,
            "dry_run": False,
            "planned_moves": 0,
            "planned_adoptions": 0,
            "hashes_to_record": 0,
            "moved": 0,
            "adopted": 0,
            "unchanged": 0,
            "normalized_records": 0,
            "hashes_recorded": 0,
            "directories_removed": 0,
            "duplicates_removed": 0,
            "planned_duplicate_removals": 0,
            "scan_required": False,
            "deferred": False,
            "warnings": [],
            "organization_blocked": False,
            "actions": [],
            "snapshot": None,
        }
        result.update(updates)
        return result

    def _validate_library_identity(self, root: Path) -> None:
        """Bind the configuration to one library and refuse another.

        The same identity is kept in two places: a token in the data
        directory and a marker at the library root. They agree while the
        configured volume is the one the database describes. A first start
        (no files recorded yet) provisions both; a library that carries a
        marker but whose configuration was recreated is adopted. Two cases
        stay closed because the volume may be unmounted, and writing into
        the mount point would silently file the library into the container:
        a token without a marker, and a configuration that already records
        files but finds no identity at all.
        """

        token_path = self.settings.data_dir / ".tankarr-library-id"
        marker_path = root / ".tankarr-library-id"
        if token_path.is_symlink() or marker_path.is_symlink():
            raise LibraryUnavailable("Library identity files must not be symlinks")

        if not token_path.exists() and not marker_path.exists():
            if self.database.has_downloaded_chapters():
                # Files are recorded but neither side carries the identity:
                # this is not a first start, and nothing proves that the
                # mounted folder is the library those records describe.
                raise LibraryUnavailable(
                    "Library identity is not provisioned, yet this configuration "
                    f"already records downloaded files. Mount the library they "
                    f"live in at {root}, or start with an empty data directory "
                    "to adopt a new library."
                )
            self._provision_library_identity(root, token_path, marker_path)
        elif not token_path.exists():
            self._adopt_library_identity(root, token_path, marker_path)
        if not marker_path.exists():
            raise LibraryUnavailable(
                f"Library identity marker is missing at {marker_path}; the library "
                "volume may be unmounted. Mount the library that this configuration "
                f"was created for, or remove {token_path} to adopt a new, empty "
                "library on the next start."
            )
        if not token_path.is_file() or not marker_path.is_file():
            raise LibraryUnavailable("Library identity paths must be regular files")
        expected = self._read_library_identity(token_path)
        actual = self._read_library_identity(marker_path)
        if actual != expected:
            raise LibraryUnavailable(
                "Library identity marker does not match configured storage"
            )

    def _provision_library_identity(
        self, root: Path, token_path: Path, marker_path: Path
    ) -> None:
        """First start: mint an identity and write it to both places."""

        self._assert_identity_writes_allowed(root)
        identity = uuid.uuid4().hex
        # The marker first: it proves the library is mounted and writable
        # before the configuration commits to it.
        self._write_library_identity(marker_path, identity, root)
        self._write_library_identity(token_path, identity, root)
        logger.info("Adopted new library %s (identity %s)", root, identity[:8])

    def _adopt_library_identity(
        self, root: Path, token_path: Path, marker_path: Path
    ) -> None:
        """The library carries a marker but the configuration lost its token
        (a recreated config volume): the marker proves the mount, so the
        configuration adopts it."""

        self._assert_identity_writes_allowed(root)
        identity = self._read_library_identity(marker_path)
        self._write_library_identity(token_path, identity, root)
        logger.info(
            "Adopted existing library %s (identity %s) into this configuration",
            root,
            identity[:8],
        )

    def _assert_identity_writes_allowed(self, root: Path) -> None:
        if self.settings.restored_safe_mode:
            raise LibraryUnavailable(
                "Library identity is not provisioned and restored safe mode "
                f"forbids writing to {root}. Verify the restored installation, "
                "then restart with TANKARR_RESTORED_SAFE_MODE=false."
            )

    @staticmethod
    def _write_library_identity(path: Path, identity: str, root: Path) -> None:
        temporary = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with open(temporary, "w", encoding="utf-8") as handle:
                handle.write(f"{identity}\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except OSError as exc:
            try:
                temporary.unlink()
            except OSError:
                pass
            where = "library directory" if path.parent == root else "data directory"
            raise LibraryUnavailable(
                f"Cannot write the library identity file {path} ({exc.strerror}). "
                f"Make the {where} writable by the user Tankarr runs as "
                "(PUID/PGID in Docker), then restart."
            ) from exc

    @staticmethod
    def _read_library_identity(path: Path) -> str:
        try:
            identity = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise LibraryUnavailable(
                f"Unable to read library identity: {path}"
            ) from exc
        if len(identity) != 32 or any(
            character not in "0123456789abcdef" for character in identity.lower()
        ):
            raise LibraryUnavailable(f"Invalid library identity marker: {path}")
        return identity.lower()

    @staticmethod
    def _confined_library_path(path: Path, root: Path) -> Path:
        resolved = TankarrService._confined_regular_file(path, root)
        if resolved.suffix.lower() != ".cbz":
            raise UnsafeLibraryPath(f"Refusing non-CBZ library path: {path}")
        return resolved

    @staticmethod
    def _confined_artwork_sidecar(path: Path, root: Path) -> Path:
        resolved = TankarrService._confined_regular_file(path, root)
        if resolved.suffix.lower() not in MANAGED_ARTWORK_SUFFIXES:
            raise UnsafeLibraryPath(f"Refusing non-artwork sidecar path: {path}")
        return resolved

    @staticmethod
    def _confined_regular_file(path: Path, root: Path) -> Path:
        absolute = Path(os.path.abspath(path))
        try:
            lexical_relative = absolute.relative_to(root)
        except ValueError as exc:
            raise UnsafeLibraryPath(
                f"Refusing path outside configured library root: {path}"
            ) from exc
        current = root
        for part in lexical_relative.parts:
            current /= part
            if current.is_symlink():
                raise UnsafeLibraryPath(
                    f"Refusing symlinked library path component: {current}"
                )
        resolved = absolute.resolve(strict=False)
        try:
            relative = resolved.relative_to(root)
        except ValueError as exc:
            raise UnsafeLibraryPath(
                f"Refusing path outside configured library root: {path}"
            ) from exc
        if not relative.parts:
            raise UnsafeLibraryPath("Refusing to delete the library root")
        if resolved.exists() and not resolved.is_file():
            raise UnsafeLibraryPath(f"Refusing non-file library path: {path}")
        return resolved

    def _confined_deletion_path(self, path: Path, root: Path) -> Path:
        if path.suffix.casefold() == ".cbz":
            return self._confined_library_path(path, root)
        return self._confined_artwork_sidecar(path, root)

    def _stage_library_files(
        self, paths: list[Path], *, disposition: str = "delete"
    ) -> FileQuarantine:
        """Move a complete batch aside, rolling it back if any move fails."""

        if disposition not in {"delete", "retain"}:
            raise ValueError("Invalid deletion disposition")
        if not paths:
            return FileQuarantine(None, None, None, [], 0, set())
        root = self._library_root()
        validated = list(
            dict.fromkeys(self._confined_deletion_path(path, root) for path in paths)
        )
        self._preflight_library_file_deletions(validated, root)
        parents = {path.parent for path in validated}
        existing = [path for path in validated if path.exists()]
        quarantine = FileQuarantine(
            operation_id=None,
            directory=None,
            manifest_path=None,
            moved=[],
            missing=len(validated) - len(existing),
            source_parents=parents,
            disposition=disposition,
        )
        if not os.access(root, os.W_OK | os.X_OK):
            raise PermissionError(f"Library root is not writable: {root}")
        quarantine.operation_id = uuid.uuid4().hex
        quarantine.directory = root / f".tankarr-delete-{quarantine.operation_id}"
        quarantine.directory.mkdir(mode=0o700)
        self._fsync_directories({root})
        quarantine.manifest_path = quarantine.directory / "manifest.json"
        planned = [
            (original, quarantine.directory / f"{index:06d}.quarantined")
            for index, original in enumerate(existing)
        ]
        quarantine.moved = list(planned)
        try:
            # The durable manifest always precedes the first destructive rename.
            self._write_quarantine_manifest(quarantine, "pre-db")
            self.database.create_deletion_operation(
                quarantine.operation_id,
                quarantine.manifest_path,
                (str(path.relative_to(root)) for path in validated),
                disposition=disposition,
            )
            quarantine.moved = []
            for original, staged in planned:
                try:
                    os.replace(original, staged)
                except FileNotFoundError:
                    quarantine.missing += 1
                    continue
                quarantine.moved.append((original, staged))
            self._fsync_directories(
                {quarantine.directory, *(path.parent for path, _ in planned)}
            )
            # Compact the journal after all renames. A crash before this write is
            # still recoverable because rollback tolerates untouched originals.
            self._write_quarantine_manifest(quarantine, "pre-db")
        except Exception as move_error:
            try:
                self._rollback_file_quarantine(quarantine)
            except Exception as rollback_error:  # noqa: BLE001 - preserve residue
                raise RuntimeError(
                    "Unable to stage library deletion and rollback was incomplete: "
                    f"{rollback_error}"
                ) from move_error
            raise
        return quarantine

    def _write_quarantine_manifest(
        self, quarantine: FileQuarantine, state: str
    ) -> None:
        if (
            quarantine.operation_id is None
            or quarantine.directory is None
            or quarantine.manifest_path is None
        ):
            return
        root = self._library_root()
        payload = {
            "version": 1,
            "operation_id": quarantine.operation_id,
            "state": state,
            "missing": quarantine.missing,
            "disposition": quarantine.disposition,
            "retired_at": quarantine.retired_at,
            "files": [
                {
                    "original": str(original.relative_to(root)),
                    "staged": staged.name,
                }
                for original, staged in quarantine.moved
            ],
        }
        temporary = quarantine.directory / "manifest.json.tmp"
        if quarantine.directory.is_symlink() or quarantine.directory.parent != root:
            raise UnsafeLibraryPath("Unsafe quarantine directory")
        if temporary.is_symlink() or (temporary.exists() and not temporary.is_file()):
            raise UnsafeLibraryPath("Unsafe temporary deletion manifest")
        temporary.unlink(missing_ok=True)
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, quarantine.manifest_path)
        directory_fd = os.open(quarantine.directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def _handle_deletion_database_exception(
        self, quarantine: FileQuarantine, database_error: Exception
    ) -> None:
        """Resolve filesystem state only when the DB journal proves it is safe."""

        if quarantine.operation_id is None:
            return
        try:
            operation = self.database.get_deletion_operation(quarantine.operation_id)
        except Exception as state_error:  # noqa: BLE001 - ambiguity is fail-closed
            message = (
                "Unable to determine deletion transaction state; quarantine was "
                f"left untouched: {type(state_error).__name__}: {state_error}"
            )
            self._persist_ambiguous_quarantine(quarantine, message)
            self._mark_recovery_blocked(message)
            raise RuntimeError(message) from database_error
        if operation is None:
            message = (
                "Deletion transaction state is missing; quarantine was left "
                "untouched for recovery"
            )
            self._persist_ambiguous_quarantine(quarantine, message)
            self._mark_recovery_blocked(message)
            raise RuntimeError(message) from database_error

        state = str(operation.get("state") or "")
        if state == "prepared":
            self._rollback_file_quarantine(quarantine)
            return
        if state == "committed":
            message = (
                "Deletion transaction committed but its caller failed; quarantine "
                "was retained for startup recovery"
            )
            self._mark_recovery_blocked(message)
            raise RuntimeError(message) from database_error

        message = (
            f"Unknown deletion transaction state {state!r}; quarantine was left "
            "untouched for recovery"
        )
        self._persist_ambiguous_quarantine(quarantine, message)
        self._mark_recovery_blocked(message)
        raise RuntimeError(message) from database_error

    def _persist_ambiguous_quarantine(
        self, quarantine: FileQuarantine, message: str
    ) -> None:
        if quarantine.directory is None:
            return
        try:
            root = self._library_root()
            if (
                quarantine.directory.parent != root
                or quarantine.directory.is_symlink()
                or not quarantine.directory.is_dir()
            ):
                raise UnsafeLibraryPath(
                    f"Unsafe quarantine directory: {quarantine.directory}"
                )
            marker = quarantine.directory / "AMBIGUOUS"
            try:
                with marker.open("x", encoding="utf-8") as handle:
                    handle.write(f"{message}\n")
                    handle.flush()
                    os.fsync(handle.fileno())
            except FileExistsError:
                if marker.is_symlink() or not marker.is_file():
                    raise UnsafeLibraryPath(f"Unsafe ambiguity marker: {marker}")
            self._fsync_directories({quarantine.directory})
        except Exception as exc:  # noqa: BLE001 - best-effort durable fail-closed mark
            self._mark_recovery_blocked(
                "Unable to persist ambiguous quarantine marker: "
                f"{type(exc).__name__}: {exc}"
            )

    def _rollback_file_quarantine(self, quarantine: FileQuarantine) -> None:
        if quarantine.directory is None:
            return
        errors: list[str] = []
        for original, staged in reversed(quarantine.moved):
            staged_exists = staged.exists()
            original_exists = original.exists()
            if staged_exists and original_exists:
                errors.append(
                    f"Cannot restore {staged}: destination already exists: {original}"
                )
                continue
            if not staged_exists and not original_exists:
                errors.append(
                    "Cannot prove rollback state because both paths are missing: "
                    f"{original}, {staged}"
                )
                continue
            if not staged_exists:
                # Either this entry had not been renamed when the process died,
                # or rollback had already restored it before a crash.
                continue
            try:
                os.replace(staged, original)
            except OSError as exc:
                errors.append(f"{staged} -> {original}: {exc}")
        try:
            self._fsync_directories(
                {
                    quarantine.directory,
                    *(original.parent for original, _ in quarantine.moved),
                }
            )
        except OSError as exc:
            errors.append(f"Unable to sync rollback directories: {exc}")
        if errors:
            message = "; ".join(errors)
            self._mark_recovery_blocked(message)
            raise RuntimeError(message)

        temporary = quarantine.directory / "manifest.json.tmp"
        allowed_journal_files = {temporary}
        if quarantine.manifest_path is not None:
            allowed_journal_files.add(quarantine.manifest_path)
        try:
            unexpected = [
                entry
                for entry in quarantine.directory.iterdir()
                if entry not in allowed_journal_files
            ]
        except FileNotFoundError:
            unexpected = []
        if unexpected:
            message = (
                "Refusing to discard quarantine journal with unexpected entries: "
                + ", ".join(str(path) for path in unexpected)
            )
            self._mark_recovery_blocked(message)
            raise RuntimeError(message)
        for journal_file in (temporary, quarantine.manifest_path):
            if journal_file is None:
                continue
            try:
                journal_file.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                errors.append(f"{journal_file}: {exc}")
        if errors:
            message = "; ".join(errors)
            self._mark_recovery_blocked(message)
            raise RuntimeError(message)

        try:
            quarantine.directory.rmdir()
            self._fsync_directories({quarantine.directory.parent})
        except FileNotFoundError:
            pass
        except OSError as exc:
            message = f"{quarantine.directory}: {exc}"
            self._mark_recovery_blocked(message)
            raise RuntimeError(message) from exc
        if quarantine.operation_id is not None:
            self.database.delete_deletion_operation(quarantine.operation_id)

    def _commit_file_quarantine(
        self, quarantine: FileQuarantine, *, purge_retained: bool = False
    ) -> dict[str, Any]:
        if quarantine.directory is None:
            return {
                "files_deleted": 0,
                "files_missing": quarantine.missing,
                "directories_removed": 0,
                "quarantine_files_remaining": 0,
                "quarantine_path": None,
                "cleanup_errors": [],
                "cleanup_warning": None,
            }

        cleanup_errors: list[str] = []
        files_deleted = 0
        files_missing = quarantine.missing
        if quarantine.disposition == "retain" and quarantine.retired_at is None:
            quarantine.retired_at = datetime.now(UTC).isoformat()
        try:
            for _, staged in quarantine.moved:
                if staged.is_symlink() or (staged.exists() and not staged.is_file()):
                    raise UnsafeLibraryPath("Unsafe staged deletion artifact")
            self._write_quarantine_manifest(quarantine, "committed")
        except Exception as exc:  # noqa: BLE001 - DB mutation is already committed
            cleanup_errors.append(
                f"Unable to commit quarantine manifest: {type(exc).__name__}: {exc}"
            )
        if cleanup_errors:
            remaining = sum(1 for _, staged in quarantine.moved if staged.exists())
            warning = (
                "Quarantine cleanup is incomplete; startup recovery will retry it."
            )
            if not purge_retained:
                self._mark_recovery_blocked(f"{warning} {'; '.join(cleanup_errors)}")
            return {
                "files_deleted": 0,
                "files_missing": files_missing,
                "directories_removed": 0,
                "quarantine_files_remaining": remaining,
                "quarantine_path": str(quarantine.directory),
                "cleanup_errors": cleanup_errors,
                "cleanup_warning": warning,
            }

        if quarantine.disposition == "retain" and not purge_retained:
            return {
                "files_deleted": 0,
                "files_retired": sum(
                    staged.is_file() for _, staged in quarantine.moved
                ),
                "files_missing": files_missing,
                "directories_removed": 0,
                # Retained files are completed work, not incomplete cleanup.
                "quarantine_files_remaining": 0,
                "quarantine_path": str(quarantine.directory),
                "cleanup_errors": [],
                "cleanup_warning": None,
            }

        for _, staged in quarantine.moved:
            if staged.is_symlink() or (staged.exists() and not staged.is_file()):
                cleanup_errors.append(
                    f"Refusing unexpected quarantine entry type: {staged}"
                )
                continue
            try:
                staged.unlink()
                files_deleted += 1
            except FileNotFoundError:
                files_missing += 1
            except OSError as exc:
                cleanup_errors.append(f"{staged}: {exc}")

        try:
            self._fsync_directories({quarantine.directory})
        except OSError as exc:
            cleanup_errors.append(f"Unable to sync quarantine cleanup: {exc}")
        try:
            directories_removed = self._remove_empty_directories(
                quarantine.source_parents, self._library_root()
            )
        except (OSError, UnsafeLibraryPath, LibraryUnavailable) as exc:
            directories_removed = 0
            cleanup_errors.append(f"Unable to clean empty library directories: {exc}")

        remaining = sum(1 for _, staged in quarantine.moved if staged.exists())
        if remaining == 0:
            temporary = quarantine.directory / "manifest.json.tmp"
            unexpected = []
            try:
                unexpected = [
                    entry
                    for entry in quarantine.directory.iterdir()
                    if entry not in {quarantine.manifest_path, temporary}
                ]
            except OSError as exc:
                cleanup_errors.append(f"Unable to inspect quarantine directory: {exc}")
            if unexpected:
                cleanup_errors.append(
                    "Refusing to remove quarantine containing unexpected entries: "
                    + ", ".join(str(path) for path in unexpected)
                )
            elif not cleanup_errors:
                journal_removed = False
                try:
                    temporary.unlink(missing_ok=True)
                    if quarantine.manifest_path is not None:
                        quarantine.manifest_path.unlink(missing_ok=True)
                    journal_removed = True
                    quarantine.directory.rmdir()
                    self._fsync_directories({quarantine.directory.parent})
                except OSError as exc:
                    cleanup_errors.append(
                        f"Unable to remove completed quarantine journal: {exc}"
                    )
                    if journal_removed and quarantine.directory.exists():
                        try:
                            self._write_quarantine_manifest(quarantine, "committed")
                        except Exception as journal_error:  # noqa: BLE001
                            cleanup_errors.append(
                                "Unable to restore committed quarantine manifest: "
                                f"{journal_error}"
                            )

        remaining = sum(1 for _, staged in quarantine.moved if staged.exists())
        quarantine_exists = quarantine.directory.exists()
        warning = None
        if remaining or cleanup_errors:
            warning = (
                "Quarantine cleanup is incomplete; startup recovery will retry it."
            )
        result = {
            "files_deleted": files_deleted,
            "files_missing": files_missing,
            "directories_removed": directories_removed,
            "quarantine_files_remaining": remaining,
            "quarantine_path": (
                str(quarantine.directory) if quarantine_exists else None
            ),
            "cleanup_errors": cleanup_errors,
            "cleanup_warning": warning,
        }
        if warning is not None and not purge_retained:
            details = "; ".join(cleanup_errors) or (
                f"{remaining} quarantined file(s) remain"
            )
            self._mark_recovery_blocked(f"{warning} {details}")
        return result

    @staticmethod
    def _fsync_directories(directories: set[Path]) -> None:
        for directory in sorted(directories, key=str):
            try:
                descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            except FileNotFoundError:
                continue
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

    async def recover_file_quarantines(self) -> dict[str, Any]:
        """Finish or roll back crash-interrupted library deletions."""

        async with self._mutation_lock:
            try:
                root = self._library_root()
            except LibraryUnavailable as exc:
                result: dict[str, Any] = {
                    "library_available": False,
                    "rolled_back": 0,
                    "purged": 0,
                    "files_deleted": 0,
                    "warnings": [str(exc)],
                    "scan_required": False,
                    "recovery_blocked": True,
                }
                self.last_deletion_recovery = result
                return result

            rolled_back = 0
            purged = 0
            files_deleted = 0
            warnings: list[str] = []
            scan_required = False
            discovered_ids: set[str] = set()
            for directory in sorted(root.glob(".tankarr-delete-*"), key=str):
                if directory.is_symlink() or not directory.is_dir():
                    warnings.append(f"Unsafe deletion quarantine ignored: {directory}")
                    continue
                directory_operation_id = directory.name.removeprefix(".tankarr-delete-")
                if len(directory_operation_id) != 32 or any(
                    character not in "0123456789abcdef"
                    for character in directory_operation_id
                ):
                    warnings.append(
                        f"Malformed deletion quarantine ignored: {directory}"
                    )
                    continue
                manifest_path = directory / "manifest.json"
                if not manifest_path.exists():
                    try:
                        entries = list(directory.iterdir())
                        if entries:
                            warnings.append(
                                "Deletion quarantine has no manifest and is not "
                                f"empty: {directory}"
                            )
                        else:
                            directory.rmdir()
                            self._fsync_directories({root})
                    except OSError as exc:
                        warnings.append(
                            f"Unable to reconcile manifestless quarantine "
                            f"{directory}: {exc}"
                        )
                    continue
                try:
                    quarantine, manifest_state = self._load_quarantine_manifest(
                        directory, root
                    )
                    assert quarantine.operation_id is not None
                    discovered_ids.add(quarantine.operation_id)
                    operation = self.database.get_deletion_operation(
                        quarantine.operation_id
                    )
                    if (
                        operation is not None
                        and Path(str(operation["manifest_path"])) != manifest_path
                    ):
                        raise UnsafeLibraryPath(
                            "Deletion operation manifest path does not match its journal"
                        )
                    operation_state = (
                        str(operation["state"]) if operation is not None else None
                    )
                    if operation is not None and (
                        operation.get("disposition", "delete") != quarantine.disposition
                    ):
                        raise RuntimeError(
                            "Deletion disposition differs from its journal"
                        )
                    if operation_state not in {None, "prepared", "committed"}:
                        raise RuntimeError(
                            f"Unknown deletion operation state: {operation_state}"
                        )

                    ambiguity_marker = directory / "AMBIGUOUS"
                    if ambiguity_marker.is_symlink() or ambiguity_marker.exists():
                        if (
                            ambiguity_marker.is_symlink()
                            or not ambiguity_marker.is_file()
                        ):
                            raise UnsafeLibraryPath(
                                f"Unsafe ambiguity marker: {ambiguity_marker}"
                            )
                        if operation_state is None:
                            warnings.append(
                                "Deletion quarantine has an unresolved ambiguous "
                                f"database state: {directory}"
                            )
                            continue
                        ambiguity_marker.unlink()
                        self._fsync_directories({directory})

                    committed = operation_state == "committed" or (
                        operation_state is None and manifest_state == "committed"
                    )
                    if committed:
                        if quarantine.disposition == "delete" or operation is not None:
                            scan_required = scan_required or bool(quarantine.moved)
                        if (
                            quarantine.disposition == "retain"
                            and manifest_state == "committed"
                        ):
                            continue
                        cleanup = self._commit_file_quarantine(quarantine)
                        files_deleted += int(cleanup["files_deleted"])
                        if cleanup["cleanup_errors"]:
                            warnings.extend(cleanup["cleanup_errors"])
                        if (
                            cleanup["quarantine_files_remaining"]
                            or cleanup["cleanup_errors"]
                        ):
                            warnings.append(
                                "Committed quarantine cleanup remains incomplete: "
                                f"{directory}"
                            )
                        elif quarantine.disposition == "delete":
                            purged += 1
                    else:
                        self._rollback_file_quarantine(quarantine)
                        rolled_back += 1
                except Exception as exc:  # noqa: BLE001 - leave invalid journal intact
                    warnings.append(
                        f"Unable to recover deletion quarantine {directory}: "
                        f"{type(exc).__name__}: {exc}"
                    )

            # A journal is removed before its DB operation during normal cleanup.
            # Clear rows left behind by a crash only when no staged artifact remains.
            for operation in self.database.list_deletion_operations():
                operation_id = str(operation["id"])
                if operation_id in discovered_ids:
                    continue
                expected_directory = root / f".tankarr-delete-{operation_id}"
                expected_manifest = expected_directory / "manifest.json"
                if Path(str(operation["manifest_path"])) != expected_manifest:
                    warnings.append(
                        "Refusing deletion operation with an unexpected manifest path: "
                        f"{operation_id}"
                    )
                    continue
                if operation.get("state") == "committed":
                    # The filesystem journal is complete. Keep this durable row
                    # as the Komga deletion receipt until reconciliation succeeds.
                    continue
                try:
                    if expected_directory.is_symlink():
                        raise UnsafeLibraryPath(
                            f"Refusing symlinked quarantine: {expected_directory}"
                        )
                    if expected_directory.exists():
                        entries = list(expected_directory.iterdir())
                        if entries:
                            raise RuntimeError(
                                "manifest is missing but quarantine is not empty"
                            )
                        expected_directory.rmdir()
                        self._fsync_directories({root})
                    self.database.delete_deletion_operation(operation_id)
                except Exception as exc:  # noqa: BLE001 - retain ambiguous operation
                    warnings.append(
                        f"Unable to reconcile deletion operation {operation_id}: "
                        f"{type(exc).__name__}: {exc}"
                    )

            result = {
                "library_available": True,
                "rolled_back": rolled_back,
                "purged": purged,
                "files_deleted": files_deleted,
                "warnings": warnings,
                "scan_required": scan_required,
                "recovery_blocked": bool(warnings),
            }
            self.last_deletion_recovery = result

        pending_reconciliation = any(
            operation.get("state") == "committed"
            for operation in self.database.list_deletion_operations()
        )
        if pending_reconciliation and not result["warnings"]:
            result["komga_scan"] = await self._request_komga_reconciliation(True)
        else:
            result["komga_scan"] = await self._request_komga_scan(scan_required)
        self.last_deletion_recovery = result
        return result

    def _load_quarantine_manifest(
        self, directory: Path, root: Path
    ) -> tuple[FileQuarantine, str]:
        if directory.is_symlink() or directory.parent != root:
            raise UnsafeLibraryPath(f"Unsafe deletion quarantine: {directory}")
        manifest_path = directory / "manifest.json"
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise UnsafeLibraryPath(f"Unsafe deletion manifest: {manifest_path}")
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Unable to read deletion manifest: {exc}") from exc
        if payload.get("version") != 1:
            raise RuntimeError("Unsupported deletion manifest version")
        operation_id = str(payload.get("operation_id") or "")
        if (
            len(operation_id) != 32
            or any(character not in "0123456789abcdef" for character in operation_id)
            or directory.name != f".tankarr-delete-{operation_id}"
        ):
            raise UnsafeLibraryPath("Invalid deletion operation identifier")
        state = str(payload.get("state") or "")
        if state not in {"pre-db", "committed"}:
            raise RuntimeError(f"Invalid deletion manifest state: {state}")
        disposition = payload.get("disposition", "delete")
        if disposition not in {"delete", "retain"}:
            raise RuntimeError("Invalid deletion manifest disposition")
        retired_at = payload.get("retired_at")
        if retired_at is not None:
            try:
                retired = datetime.fromisoformat(retired_at)
                if retired.utcoffset() is None:
                    raise ValueError("Timezone is required")
            except (TypeError, ValueError) as exc:
                raise RuntimeError("Invalid recycle bin retirement timestamp") from exc
        elif disposition == "retain" and state == "committed":
            raise RuntimeError(
                "Committed recycle bin entry has no retirement timestamp"
            )
        missing = payload.get("missing", 0)
        if not isinstance(missing, int) or isinstance(missing, bool) or missing < 0:
            raise RuntimeError("Invalid deletion manifest missing-file count")
        files = payload.get("files")
        if not isinstance(files, list):
            raise RuntimeError("Invalid deletion manifest file list")

        moved: list[tuple[Path, Path]] = []
        originals: set[Path] = set()
        staged_paths: set[Path] = set()
        for entry in files:
            if not isinstance(entry, dict):
                raise RuntimeError("Invalid deletion manifest entry")
            relative = Path(str(entry.get("original") or ""))
            staged_name = str(entry.get("staged") or "")
            if (
                relative.is_absolute()
                or not relative.parts
                or ".." in relative.parts
                or Path(staged_name).name != staged_name
                or not staged_name.endswith(".quarantined")
            ):
                raise UnsafeLibraryPath("Unsafe path in deletion manifest")
            original = self._confined_deletion_path(root / relative, root)
            staged = directory / staged_name
            if staged.is_symlink() or (staged.exists() and not staged.is_file()):
                raise UnsafeLibraryPath(f"Unsafe staged deletion artifact: {staged}")
            if original in originals or staged in staged_paths:
                raise RuntimeError("Duplicate path in deletion manifest")
            originals.add(original)
            staged_paths.add(staged)
            moved.append((original, staged))
        return (
            FileQuarantine(
                operation_id,
                directory,
                manifest_path,
                moved,
                missing,
                {original.parent for original, _ in moved},
                disposition,
                retired_at,
            ),
            state,
        )

    async def purge_recycle_bin(self, *, now: datetime | None = None) -> dict[str, Any]:
        """Expire only proven, committed retirements; leave reader receipts intact."""

        current = now or datetime.now(UTC)
        if current.utcoffset() is None:
            raise ValueError("Recycle bin expiry requires a timezone")
        retention = timedelta(days=self.settings.recycle_bin_retention_days)
        result: dict[str, Any] = {"files_deleted": 0, "purged": 0, "errors": []}
        async with self._mutation_lock:
            self.assert_mutations_allowed()
            root = self._library_root()
            for directory in sorted(root.glob(".tankarr-delete-*"), key=str):
                try:
                    if (directory / "AMBIGUOUS").exists() or (
                        directory / "AMBIGUOUS"
                    ).is_symlink():
                        continue
                    quarantine, state = self._load_quarantine_manifest(directory, root)
                    if quarantine.disposition != "retain" or state != "committed":
                        continue
                    operation = self.database.get_deletion_operation(
                        str(quarantine.operation_id)
                    )
                    if operation is not None and (
                        operation["state"] != "committed"
                        or operation.get("disposition") != "retain"
                        or Path(operation["manifest_path"]) != quarantine.manifest_path
                    ):
                        continue
                    assert quarantine.retired_at is not None
                    retired = datetime.fromisoformat(quarantine.retired_at)
                    if current < retired + retention:
                        continue
                    permitted = {
                        quarantine.manifest_path,
                        directory / "manifest.json.tmp",
                        *(staged for _, staged in quarantine.moved),
                    }
                    if any(entry not in permitted for entry in directory.iterdir()):
                        raise UnsafeLibraryPath("Unexpected file in recycle bin entry")
                    cleanup = self._commit_file_quarantine(
                        quarantine, purge_retained=True
                    )
                    result["files_deleted"] += cleanup["files_deleted"]
                    result["errors"].extend(cleanup["cleanup_errors"])
                    if (
                        not cleanup["cleanup_errors"]
                        and not cleanup["quarantine_files_remaining"]
                    ):
                        result["purged"] += 1
                except Exception as exc:  # noqa: BLE001 - preserve unproven artifacts
                    result["errors"].append(
                        f"Recycle bin entry could not be purged: {type(exc).__name__}: {exc}"
                    )
        return result

    @staticmethod
    def _preflight_library_file_deletions(paths: list[Path], root: Path) -> None:
        """Check every existing file can be unlinked before deleting the first one."""

        for path in paths:
            if not path.exists():
                continue
            current = path.parent
            while True:
                required = os.X_OK | (os.W_OK if current == path.parent else 0)
                if not current.exists() or not os.access(current, required):
                    raise PermissionError(
                        f"Library directory is not writable/searchable: {current}"
                    )
                if current == root:
                    break
                current = current.parent

    @staticmethod
    def _remove_empty_directories(parents: set[Path], root: Path) -> int:
        removed = 0
        for initial in sorted(parents, key=lambda path: len(path.parts), reverse=True):
            current = initial
            while current != root:
                try:
                    current.resolve(strict=False).relative_to(root)
                except ValueError as exc:
                    raise UnsafeLibraryPath(
                        f"Refusing directory cleanup outside library root: {current}"
                    ) from exc
                try:
                    current.rmdir()
                except FileNotFoundError:
                    pass
                except OSError:
                    break
                else:
                    removed += 1
                current = current.parent
        return removed

    async def _request_komga_scan(self, requested: bool) -> dict[str, Any]:
        if not requested:
            return {"requested": False, "triggered": False}
        if bool(getattr(self.komga, "standalone", False)):
            return {
                "requested": False,
                "triggered": False,
                "reader_independent": True,
                "reason": "Reader applications scan the shared filesystem independently",
            }
        try:
            return {"requested": True, **await self.komga.scan()}
        except Exception as exc:  # noqa: BLE001 - deletion has already committed
            return {
                "requested": True,
                "triggered": False,
                "error": f"{type(exc).__name__}: {exc}",
            }

    async def _sync_imported_path_with_komga(self, path: Path) -> dict[str, Any]:
        root = self._library_root()
        relative = self._confined_library_path(path, root).relative_to(root).as_posix()
        if bool(getattr(self.komga, "standalone", False)):
            return {
                "configured": False,
                "triggered": False,
                "reader_independent": True,
                "path": relative,
            }
        try:
            sync_imported_path = getattr(self.komga, "sync_imported_path", None)
            if callable(sync_imported_path):
                result = await sync_imported_path(relative)
            else:
                result = await self.komga.scan([relative])
            self.last_komga_library_alignment = {
                "ready": True,
                **result,
            }
            return result
        except Exception as exc:  # noqa: BLE001 - the DB-derived audit retries it
            error = f"{type(exc).__name__}: {exc}"
            self.last_komga_library_alignment = {
                "configured": bool(getattr(self.komga, "configured", False)),
                "ready": False,
                "triggered": False,
                "error": error,
            }
            return {
                "configured": bool(getattr(self.komga, "configured", False)),
                "triggered": False,
                "sync_pending": True,
                "error": error,
            }

    async def retry_pending_komga_reconciliation(self) -> dict[str, Any]:
        recovery = self.last_deletion_recovery
        if recovery is not None and recovery.get("recovery_blocked"):
            return {
                "requested": False,
                "triggered": False,
                "reason": "Deletion recovery must complete before Komga cleanup",
            }
        pending = any(
            operation.get("state") == "committed"
            for operation in self.database.list_deletion_operations()
        )
        return await self._request_komga_reconciliation(pending)

    def _replacement_receipt_path(
        self, operation: dict[str, Any], *, organize: bool
    ) -> Path | None:
        """Verify a replacement ledger before treating an occupied path as valid."""
        identifier = operation.get("replacement_chapter_id")
        expected = operation.get("replacement_sha256")
        if (
            not isinstance(identifier, str)
            or not identifier
            or not isinstance(expected, str)
            or not self._valid_sha256(expected)
            or operation.get("disposition") != "retain"
        ):
            raise UnsafeLibraryPath(
                "Replacement retirement receipt has no valid ledger"
            )
        try:
            chapter = self.database.get_chapter(identifier)
        except KeyError:
            return None
        if not chapter.get("downloaded"):
            # A later explicit deletion may have removed the replacement too.
            # The ordinary absent-path checks still guard that receipt's ack.
            return None
        if chapter.get("library_sha256") != expected or not chapter.get("library_path"):
            raise UnsafeLibraryPath(
                "Replacement book changed before reader reconciliation"
            )
        root = self._library_root()
        path = self._recorded_library_path(str(chapter["library_path"]), root)
        if not path.is_file() or sha256(path) != expected:
            raise UnsafeLibraryPath(
                "Replacement book bytes differ from the committed ledger"
            )
        if organize:
            path = self._organize_imported_chapter_locked(identifier, expected)
        return path

    async def _reconcile_replacement_receipt(self, operation: dict[str, Any]):
        async with self._mutation_lock:
            path = await self._finish_import_thread(
                lambda: self._replacement_receipt_path(operation, organize=True)
            )
        if path is None:
            return None
        result = await self._sync_imported_path_with_komga(path)
        if (
            result.get("error")
            or result.get("sync_pending")
            or (
                result.get("configured") is False
                and not result.get("reader_independent")
            )
        ):
            raise RuntimeError(
                result.get("error") or "Reader has not accepted the replacement book"
            )
        async with self._mutation_lock:
            after = await self._finish_import_thread(
                lambda: self._replacement_receipt_path(operation, organize=False)
            )
            if after != path:
                raise UnsafeLibraryPath(
                    "Replacement path changed during reader reconciliation"
                )
        return result

    async def _request_komga_reconciliation(self, requested: bool) -> dict[str, Any]:
        """Drain committed deletion receipts only after a guarded Komga scan."""

        if not requested:
            return {"requested": False, "triggered": False}
        async with self._komga_reconciliation_lock:
            operations = [
                operation
                for operation in self.database.list_deletion_operations()
                if operation.get("state") == "committed"
            ]
            if not operations:
                result = {
                    "requested": True,
                    "triggered": False,
                    "purged": False,
                    "pending_operations": 0,
                }
                self.last_komga_reconciliation = result
                return result

            operation_ids = [str(operation["id"]) for operation in operations]
            try:
                deletions = []
                replacement_syncs = []
                for operation in operations:
                    if (
                        operation.get("replacement_chapter_id") is not None
                        or operation.get("replacement_sha256") is not None
                    ):
                        synced = await self._reconcile_replacement_receipt(operation)
                        if synced is not None:
                            replacement_syncs.append(synced)
                            continue
                    deletions.append(operation)
                if not deletions:
                    for operation_id in operation_ids:
                        self.database.delete_deletion_operation(operation_id)
                    result = {
                        "requested": not bool(getattr(self.komga, "standalone", False)),
                        "triggered": any(
                            item.get("triggered") for item in replacement_syncs
                        ),
                        "purged": False,
                        "pending_operations": 0,
                        "completed_operations": len(operation_ids),
                    }
                    self.last_komga_reconciliation = result
                    return result
                root = self._library_root()
                relative_paths: list[str] = []
                for operation in deletions:
                    stored_paths = operation.get("library_paths")
                    if not isinstance(stored_paths, list) or not stored_paths:
                        raise UnsafeLibraryPath(
                            "Committed deletion receipt has no authorized library "
                            f"paths: {operation['id']}"
                        )
                    for raw in stored_paths:
                        if not isinstance(raw, str):
                            raise UnsafeLibraryPath(
                                "Deletion receipt contains a non-string path: "
                                f"{operation['id']}"
                            )
                        relative = Path(raw)
                        if (
                            relative.is_absolute()
                            or not relative.parts
                            or ".." in relative.parts
                        ):
                            raise UnsafeLibraryPath(
                                f"Unsafe path in deletion receipt: {raw}"
                            )
                        self._confined_deletion_path(root / relative, root)
                        relative_paths.append(relative.as_posix())

                unique_relative_paths = tuple(dict.fromkeys(relative_paths))
                # A receipt is obsolete once the library owns a book at the
                # name it emptied: the path did not come back, a different
                # book was filed there. Telling the reader to drop it would
                # remove a book that exists, and refusing to drain the queue
                # over it strands every other receipt with it - one stale
                # name kept 582 of them unreconciled for a week.
                owned = self.database.owned_library_paths(
                    str(root / Path(raw)) for raw in unique_relative_paths
                )
                obsolete = {
                    raw
                    for raw in unique_relative_paths
                    if str(root / Path(raw)) in owned
                }
                if obsolete:
                    logger.info(
                        "Deletion receipts name %d path(s) the library owns "
                        "again; reconciling the rest: %s",
                        len(obsolete),
                        ", ".join(sorted(obsolete)[:3]),
                    )
                purgeable = tuple(
                    raw for raw in unique_relative_paths if raw not in obsolete
                )
                deleted_books = tuple(
                    path for path in purgeable if Path(path).suffix.casefold() == ".cbz"
                )

                def deletion_paths_still_absent() -> None:
                    # Only the books are ever reported to the reader, so only
                    # a book coming back can make that report wrong. The
                    # sidecar cover next to a book is written by Tankarr
                    # itself and reappears on its own; letting one hold the
                    # queue stranded 641 receipts behind a .jpg.
                    current_root = self._library_root()
                    for raw in deleted_books:
                        candidate = self._confined_deletion_path(
                            current_root / Path(raw), current_root
                        )
                        if candidate.exists():
                            raise UnsafeLibraryPath(
                                "A deleted library path was restored before "
                                f"cleanup completed: {raw}"
                            )

                reader_independent = bool(getattr(self.komga, "standalone", False))
                legacy_scan_only = not hasattr(self.komga, "reconcile_deleted")
                if reader_independent:
                    deletion_paths_still_absent()
                    reconciliation = {
                        "configured": False,
                        "reader_independent": True,
                        "triggered": False,
                        "purged": False,
                    }
                elif not legacy_scan_only:
                    reconciliation = await self.komga.reconcile_deleted(
                        deleted_books,
                        safety_check=deletion_paths_still_absent,
                    )
                else:
                    # Simple test doubles and legacy clients still exercise the
                    # deletion transaction through the scan-only contract.
                    reconciliation = await self.komga.scan()

                if reconciliation.get("configured") is False and not reader_independent:
                    raise RuntimeError(
                        reconciliation.get("reason")
                        or "Komga is not configured; deletion receipt retained"
                    )
                if reconciliation.get("sync_pending"):
                    raise RuntimeError(
                        reconciliation.get("error")
                        or "Reader deletion synchronization is still pending"
                    )
                for operation_id in operation_ids:
                    self.database.delete_deletion_operation(operation_id)
                if reader_independent:
                    result = {
                        "requested": False,
                        **reconciliation,
                        "pending_operations": 0,
                        "completed_operations": len(operation_ids),
                    }
                elif legacy_scan_only:
                    result = {"requested": True, **reconciliation}
                else:
                    result = {
                        "requested": True,
                        **reconciliation,
                        "pending_operations": 0,
                        "completed_operations": len(operation_ids),
                    }
            except Exception as exc:  # noqa: BLE001 - deletion already committed
                error = f"{type(exc).__name__}: {exc}"
                try:
                    self.database.record_deletion_reconciliation_failure(
                        operation_ids, error
                    )
                except Exception as database_error:  # noqa: BLE001
                    error = (
                        f"{error}; unable to persist Komga retry: "
                        f"{type(database_error).__name__}: {database_error}"
                    )
                result = {
                    "requested": True,
                    "triggered": False,
                    "purged": False,
                    "pending_operations": len(operation_ids),
                    "error": error,
                }
            self.last_komga_reconciliation = result
            return result

    @staticmethod
    def _logical_chapter_count(chapters: list[dict[str, Any]]) -> int:
        return len(
            {
                (
                    str(chapter.get("volume") or ""),
                    str(chapter.get("chapter") or chapter["id"]),
                )
                for chapter in chapters
            }
        )

    @staticmethod
    def _latest_chapter(chapters: list[dict[str, Any]]) -> str | None:
        numbered: list[tuple[Decimal, str]] = []
        for chapter in chapters:
            raw = chapter.get("chapter")
            if raw is None:
                continue
            try:
                numbered.append((Decimal(str(raw)), str(raw)))
            except (InvalidOperation, ValueError):
                continue
        return max(numbered, default=(Decimal("-1"), ""))[1] or None

    def _publish_download_import_locked(
        self,
        chapter_id: str,
        staged: Path,
        destination: Path,
        archive_sha256: str,
        supersedes_chapter_id: str | None = None,
    ) -> Path:
        """Publish one prepared archive while the async mutation lock is held.

        Copying, hashing, fsync and post-import organization are intentionally
        synchronous here so the caller can run the complete consistency unit
        in the bounded blocking pool without yielding halfway through it.
        """

        install_atomically(staged, destination, expected_sha256=archive_sha256)
        self.database.mark_chapter_downloaded(chapter_id, destination, archive_sha256)
        if supersedes_chapter_id:
            self._retire_superseded_download_locked(chapter_id, supersedes_chapter_id)
        return self._organize_imported_chapter_locked(chapter_id, archive_sha256)

    def _retire_superseded_download_locked(
        self, chapter_id: str, supersedes_chapter_id: str
    ) -> None:
        """Retire the old release only after its replacement is durably published.

        The caller holds the mutation lock and has installed/hash-verified and
        recorded the replacement at its own journalled path. Use the existing
        deletion journal for retirement, then the relocation journal to give
        the new file its canonical name. A failure at either boundary leaves
        at least the new, independently addressable archive recoverable.
        """

        if chapter_id == supersedes_chapter_id:
            return
        replacement = self.database.get_chapter(chapter_id)
        try:
            old = self.database.get_chapter(supersedes_chapter_id)
        except KeyError:
            return
        if (
            old["manga_id"] != replacement["manga_id"]
            or old["language"] != replacement["language"]
        ):
            raise UnsafeLibraryPath(
                "Replacement refers to a different series or language"
            )
        if not old.get("downloaded"):
            return
        if not replacement.get("downloaded") or not replacement.get("library_path"):
            raise RuntimeError(
                "Replacement must be published before retiring the old file"
            )
        from tankarr.assembly_provenance import proven_assembly
        from tankarr.series_unit import is_volume_release

        retiring_assembly = bool(proven_assembly(old))
        if retiring_assembly:
            if (
                not is_volume_release(replacement)
                or canonical_number(old.get("volume"))
                != canonical_number(replacement.get("volume"))
                or replacement.get("provider") == "assembled"
            ):
                raise UnsafeLibraryPath(
                    "Assembly replacement must be an external release of the same book"
                )
            self._replacement_receipt_path(
                {
                    "replacement_chapter_id": chapter_id,
                    "replacement_sha256": replacement.get("library_sha256"),
                    "disposition": "retain",
                },
                organize=False,
            )
        manga_id = str(replacement["manga_id"])
        manga = self.database.get_manga(manga_id)
        paths = self._library_paths_for_chapters(manga, [old])
        self._assert_library_paths_not_shared(manga_id, {supersedes_chapter_id}, paths)
        self.database.assert_chapter_file_deletion_ready(
            manga_id, supersedes_chapter_id
        )
        quarantine = self._stage_library_files(
            paths, disposition="retain" if retiring_assembly else "delete"
        )
        try:
            if retiring_assembly:
                if any(
                    sha256(staged) != old.get("library_sha256")
                    for _original, staged in quarantine.moved
                ):
                    raise UnsafeLibraryPath("Assembled book changed before retirement")
                with self.database.write_snapshot():
                    current = self.database.get_chapter(supersedes_chapter_id)
                    if any(
                        current.get(key) != old.get(key)
                        for key in (
                            "provider",
                            "volume",
                            "language",
                            "release_unit",
                            "downloaded",
                            "library_path",
                            "library_sha256",
                            "assembled_from",
                        )
                    ):
                        raise UnsafeLibraryPath(
                            "Assembled book changed before retirement commit"
                        )
                    self.database.mark_deletion_replacement(
                        quarantine.operation_id,
                        chapter_id,
                        replacement["library_sha256"],
                    )
                    self.database.reset_chapter_file(
                        manga_id, supersedes_chapter_id, quarantine.operation_id
                    )
            else:
                self.database.reset_chapter_file(
                    manga_id, supersedes_chapter_id, quarantine.operation_id
                )
        except Exception as exc:
            self._handle_deletion_database_exception(quarantine, exc)
            raise
        self._commit_file_quarantine(quarantine)
        self.assert_mutations_allowed()

    def _recover_download_import_locked(
        self,
        job_id: int,
        chapter_id: str,
        destination: Path,
        archive_sha256: str,
        evidence: dict[str, Any],
        supersedes_chapter_id: str | None = None,
    ) -> Path:
        self.database.mark_chapter_downloaded(chapter_id, destination, archive_sha256)
        self.database.update_job(
            job_id,
            status="importing",
            progress=0.92,
            message="Recovering previously imported library file",
            language_evidence=evidence,
        )
        if supersedes_chapter_id:
            self._retire_superseded_download_locked(chapter_id, supersedes_chapter_id)
        return self._organize_imported_chapter_locked(chapter_id, archive_sha256)

    def _replan_legacy_replacement_locked(
        self, queued: dict[str, Any], chapter: dict[str, Any], planned_path: Path
    ) -> Path | None:
        """Migrate only a queued plan proven to still point at its old archive."""

        superseded = str(queued.get("supersedes_chapter_id") or "")
        if not superseded or chapter.get("downloaded"):
            return planned_path
        try:
            old = self.database.get_chapter(superseded)
        except KeyError:
            return planned_path
        if (
            not old.get("downloaded")
            or old["manga_id"] != chapter["manga_id"]
            or old["language"] != chapter["language"]
            or not old.get("library_path")
            or self._recorded_library_path(
                str(old["library_path"]), self._library_root()
            )
            != planned_path
            or not planned_path.is_file()
            or planned_path.is_symlink()
        ):
            return planned_path
        digest = sha256(planned_path)
        archive = (queued.get("language_evidence") or {}).get("archive")
        if isinstance(archive, dict) and archive.get("sha256") == digest:
            # The new file is already present: preserve its persisted recovery
            # ledger, including jobs interrupted between installation and mark.
            return planned_path
        if digest != self._expected_library_digest(old):
            return planned_path
        replacement_path = planned_path.with_name(
            f"tankarr-replacement-{int(queued['id'])}.cbz"
        )
        if not self.database.replan_queued_replacement(
            int(queued["id"]),
            supersedes_chapter_id=superseded,
            expected_path=str(queued["planned_path"]),
            planned_path=replacement_path,
        ):
            return None
        return replacement_path

    async def process_download_job(self, job_id: int) -> None:
        work_dir: Path | None = None
        destination: Path | None = None
        # Read the retry ledger while the job is still queued. Claiming the job
        # replaces its operator-facing message with "Preparing download".
        transient_attempt = self._transient_attempt(job_id)
        async with self._mutation_lock:
            self.assert_mutations_allowed()
            try:
                queued = self.database.get_job(job_id)
                if queued["status"] != "queued":
                    return
                chapter = self.database.get_chapter(queued["chapter_id"])
                manga = self.database.get_manga(queued["manga_id"])
                acquisition_allowed = self._queued_job_is_acquisition_allowed(
                    str(manga["id"]),
                    str(chapter["id"]),
                    job_id,
                    origin=queued.get("origin"),
                )
                canonical_record = self.database.get_series_metadata(manga["id"])
                canonical = (canonical_record or {}).get("data") or {}
                chapter["metadata"] = next(
                    (
                        item["data"]
                        for item in self.database.list_volume_metadata(manga["id"])
                        if item["volume_key"]
                        == publication_metadata_key(chapter, canonical)
                    ),
                    {},
                )
            except KeyError:
                return
            if chapter["manga_id"] != manga["id"]:
                return
            try:
                library_root = self._library_root()
            except LibraryUnavailable as exc:
                # The volume is gone for the moment: the job stays queued and
                # the worker backs off; it is downloaded when the library is
                # back, instead of the worker dying with it.
                self.database.update_job(
                    job_id, message=f"Waiting for the library: {exc}"[:300]
                )
                return
            if queued.get("planned_path"):
                planned_path = self._recorded_library_path(
                    str(queued["planned_path"]), library_root
                )
                try:
                    planned_path = await asyncio.to_thread(
                        self._replan_legacy_replacement_locked,
                        queued,
                        chapter,
                        planned_path,
                    )
                except (OSError, UnsafeLibraryPath, ValueError) as exc:
                    self.database.update_job(
                        job_id,
                        status="failed",
                        message=f"Unable to verify replacement path: {type(exc).__name__}: {exc}",
                    )
                    return
                if planned_path is None:
                    # A concurrent claim/cancellation changed the durable plan;
                    # leave the job to its current owner without any file I/O.
                    return
            else:
                try:
                    planned_path = self._confined_library_path(
                        final_library_path(library_root, manga, chapter), library_root
                    )
                except (UnsafeLibraryPath, ValueError) as exc:
                    self.database.update_job(
                        job_id,
                        status="failed",
                        message=f"Unable to plan the library path: {type(exc).__name__}: {exc}"[
                            :300
                        ],
                    )
                    return
                if queued.get("supersedes_chapter_id"):
                    # Both releases normally share a canonical filename. Keep
                    # the current book available while copying/fsyncing the new
                    # one, and persist this distinct path for crash recovery.
                    planned_path = planned_path.with_name(
                        f"tankarr-replacement-{job_id}.cbz"
                    )
            recovering_published_file = bool(
                chapter.get("downloaded")
                and queued.get("planned_path")
                and planned_path.exists()
            )
            job = self.database.claim_queued_job(
                job_id,
                planned_path
                if acquisition_allowed or recovering_published_file
                else None,
                acquisition_allowed=acquisition_allowed or recovering_published_file,
            )
        if job is None:
            # A queued job can be deliberately removed by a delete operation
            # while its stale ID is still waiting in DownloadWorker's queue.
            return
        try:
            if chapter["language"] != job["requested_language"]:
                raise ValueError(
                    f"Chapter language {chapter['language']} does not match requested "
                    f"language {job['requested_language']}"
                )
            destination = self._confined_library_path(
                Path(job["planned_path"]), self._library_root()
            )
            if destination.exists():
                await self._complete_recovered_import(job_id, job, chapter, destination)
                return
            work_dir = self.settings.staging_dir / f"job-{job_id}"
            pages_dir = work_dir / "pages"
            work_dir.mkdir(parents=True, exist_ok=True)

            last_progress_update: float | None = None

            async def progress(completed: int, total: int) -> None:
                nonlocal last_progress_update
                now = monotonic()
                if (
                    completed < total
                    and last_progress_update is not None
                    and now - last_progress_update < 0.5
                ):
                    return
                last_progress_update = now
                value = 0.05 + (completed / max(total, 1)) * 0.58
                await asyncio.to_thread(
                    self.database.update_job,
                    job_id,
                    status="downloading",
                    progress=value,
                    message=f"Downloaded {completed}/{total} pages",
                )

            download_started = monotonic()
            pages = await self.provider_for(chapter.get("provider")).download_pages(
                chapter["id"],
                pages_dir,
                progress,
                concurrency=self.settings.download_concurrency,
            )
            download_seconds = monotonic() - download_started
            self.database.record_source_circuit(
                source_gate_key(
                    chapter.get("provider"),
                    chapter.get("source_url"),
                    chapter.get("source_name"),
                )
            )

            # A source can serve a well-formed archive of thumbnails, or one
            # episode chopped into fragments. The pages are the only evidence
            # of that, and this is the last moment they exist as pages, so the
            # judgement happens here rather than after a bad file is published.
            quality = await asyncio.to_thread(
                self._assess_downloaded_pages,
                str(manga["id"]),
                pages,
                chapter.get("chapter"),
            )

            evidence = {
                "provider": {
                    "name": chapter.get("provider"),
                    "declared_language": chapter["language"],
                    "requested_language": job["requested_language"],
                    "verdict": "confirmed",
                },
                "pages": quality,
            }
            if quality["verdict"] == page_quality.DEGRADED and (
                "taller than they are wide" in str(quality.get("reason") or "")
            ):
                # Un-sliced strips are not refused any more: they are cut
                # into pages on the quietest rows, then judged again.
                pages, sliced = await asyncio.to_thread(
                    page_quality.slice_strips, pages
                )
                quality = await asyncio.to_thread(
                    self._assess_downloaded_pages,
                    str(manga["id"]),
                    pages,
                    chapter.get("chapter"),
                )
                quality = {**quality, "sliced_strips": sliced}
                evidence["pages"] = quality
            if quality["verdict"] == page_quality.DEGRADED:
                verdict = self._settle_degraded_download(job, manga, chapter, quality)
                if verdict is None:
                    raise DegradedPagesError(
                        f"{chapter.get('source_name') or chapter.get('provider')} "
                        f"{quality['reason']}"
                    )
                quality = verdict
                evidence["pages"] = quality

            self.database.update_job(
                job_id,
                status="packaging",
                progress=0.76,
                message="Creating CBZ and ComicInfo.xml",
                language_evidence=evidence,
            )
            cbz_path = work_dir / chapter_filename(manga, chapter, chapter["language"])
            _path, archive_info = await asyncio.to_thread(
                package_cbz_validated, cbz_path, pages, manga, chapter
            )
            evidence["archive"] = archive_info
            cbz_bytes = _path.stat().st_size

            self.database.update_job(
                job_id,
                status="importing",
                progress=0.88,
                message="Importing into manga library",
                language_evidence=evidence,
            )
            destination = self._confined_library_path(
                Path(job["planned_path"]), self._library_root()
            )
            superseded = str(job.get("supersedes_chapter_id") or "")
            async with self._mutation_lock:
                self.assert_mutations_allowed()
                destination = await asyncio.to_thread(
                    self._publish_download_import_locked,
                    str(chapter["id"]),
                    cbz_path,
                    destination,
                    str(archive_info["sha256"]),
                    superseded or None,
                )

            komga_result = await self._sync_imported_path_with_komga(destination)
            evidence["komga"] = komga_result
            # One good import proves the source works on this series again,
            # and feeds the perennial ranking with the measured speed.
            self.database.clear_source_failures(
                str(chapter["manga_id"]), _release_source_key(chapter)
            )
            self.database.record_source_health(
                _release_source_key(chapter),
                ok=True,
                bytes_downloaded=cbz_bytes,
                seconds=max(0.001, download_seconds),
            )
            # Only a chapter that was accepted feeds the baseline, so a series
            # can never be talked into thinking fragments are its normal size.
            self.database.record_page_quality(
                str(manga["id"]),
                str(chapter["id"]),
                verdict=str(quality["verdict"]),
                assessment=quality,
                library_sha256=str(archive_info["sha256"]),
            )
            async with self._mutation_lock:
                # The file and chapter snapshot are already published. Completing
                # the job is an operational transition, not a new mutation.
                self.database.update_job(
                    job_id,
                    status="completed",
                    progress=1.0,
                    message=(
                        "Download imported; Komga sync pending"
                        if komga_result.get("sync_pending")
                        else "Download imported successfully"
                    ),
                    result_path=destination,
                    language_evidence=evidence,
                )
            await self.notifier.chapter_imported(manga, chapter)
        except RecoveryBlocked:
            async with self._mutation_lock:
                self.database.update_job(
                    job_id,
                    status="queued",
                    progress=0.0,
                    message="Paused for deletion recovery",
                )
        except Exception as exc:  # noqa: BLE001 - persist every worker failure
            if isinstance(
                exc, (ProviderUnavailableError, ProviderRequestError, httpx.HTTPError)
            ):
                self.database.record_source_circuit(
                    source_gate_key(
                        chapter.get("provider"),
                        chapter.get("source_url"),
                        chapter.get("source_name"),
                    ),
                    error=type(exc).__name__,
                )
            failed_detail: str | None = None
            try:
                published_state = (
                    await asyncio.to_thread(
                        self._published_import_matches_persisted_hash,
                        job_id,
                        destination,
                    )
                    if destination is not None
                    else False
                )
                async with self._mutation_lock:
                    # Once claimed, a job must always leave an active status.
                    # This operational terminal transition is allowed even when
                    # recovery became blocked while the provider was running.
                    if published_state is not False:
                        self.database.update_job(
                            job_id,
                            status="queued",
                            progress=0.0,
                            message=(
                                "Paused for imported-file recovery: "
                                f"{type(exc).__name__}: {exc}"
                            ),
                        )
                    elif (
                        isinstance(exc, ProviderUnavailableError)
                        or type(exc).__name__ in self.TRANSIENT_FAILURE_NAMES
                    ) and transient_attempt <= self.TRANSIENT_RETRIES:
                        # The source engine was unreachable (a restart, a
                        # timeout): the release is fine. Give it a moment and
                        # queue the job again instead of failing it.
                        self.database.update_job(
                            job_id,
                            status="queued",
                            progress=0.0,
                            retry_count=transient_attempt,
                            next_retry_at=datetime.now(UTC).timestamp()
                            + min(
                                600,
                                self.TRANSIENT_PAUSE_SECONDS
                                * 2 ** (transient_attempt - 1),
                            ),
                            failure_code=type(exc).__name__,
                            failure_scope="service",
                            message=(
                                f"Source unreachable (attempt {transient_attempt}/{self.TRANSIENT_RETRIES}), "
                                f"retrying: {type(exc).__name__}: {exc}"
                            )[:500],
                        )
                    else:
                        self.database.update_job(
                            job_id,
                            status="failed",
                            failure_code=type(exc).__name__,
                            failure_scope="service"
                            if type(exc).__name__ in self.TRANSIENT_FAILURE_NAMES
                            else "release",
                            next_retry_at=0,
                            message=f"{type(exc).__name__}: {exc}",
                        )
                        failed_detail = f"{type(exc).__name__}: {exc}"
            except KeyError:
                # Deletion is allowed to remove queued history. A defensive
                # no-op keeps a stale worker ID from killing the worker task.
                failed_detail = None
            if failed_detail is not None:
                await self.notifier.job_failed(manga["title"], failed_detail)
        finally:
            if work_dir and work_dir.exists():
                await asyncio.to_thread(shutil.rmtree, work_dir, ignore_errors=True)

    async def _complete_recovered_import(
        self,
        job_id: int,
        job: dict[str, Any],
        chapter: dict[str, Any],
        destination: Path,
    ) -> None:
        evidence = dict(job.get("language_evidence") or {})
        archive_evidence = evidence.get("archive")
        expected_hash = (
            archive_evidence.get("sha256")
            if isinstance(archive_evidence, dict)
            else None
        )
        if (
            not isinstance(expected_hash, str)
            or len(expected_hash) != 64
            or any(character not in "0123456789abcdef" for character in expected_hash)
        ):
            raise FileExistsError(
                "Refusing existing library file without persisted archive hash: "
                f"{destination}"
            )
        archive_info = await asyncio.to_thread(validate_cbz, destination)
        if archive_info["sha256"] != expected_hash:
            raise FileExistsError(
                "Refusing existing library file whose hash differs from the "
                f"persisted import: {destination}"
            )
        evidence["archive"] = archive_info
        evidence["recovery"] = {
            "recovered_existing_import": True,
            "sha256_verified": True,
        }
        try:
            await asyncio.to_thread(fsync_directory, destination.parent)
        except OSError as exc:
            raise ImportDurabilityError(
                "Recovered library file exists but directory durability could not "
                f"be confirmed: {destination}"
            ) from exc
        async with self._mutation_lock:
            self.assert_mutations_allowed()
            destination = await asyncio.to_thread(
                self._recover_download_import_locked,
                job_id,
                str(chapter["id"]),
                destination,
                str(archive_info["sha256"]),
                evidence,
                str(job.get("supersedes_chapter_id") or "") or None,
            )

        komga_result = await self._sync_imported_path_with_komga(destination)
        evidence["komga"] = komga_result
        async with self._mutation_lock:
            self.database.update_job(
                job_id,
                status="completed",
                progress=1.0,
                message=(
                    "Recovered import; Komga sync pending"
                    if komga_result.get("sync_pending")
                    else "Recovered imported file"
                ),
                result_path=destination,
                language_evidence=evidence,
            )
        manga = self.database.get_manga(str(job["manga_id"]))
        await self.notifier.chapter_imported(manga, chapter)

    def _published_import_matches_persisted_hash(
        self, job_id: int, destination: Path
    ) -> bool | None:
        try:
            root = self._library_root()
        except LibraryUnavailable:
            return None
        try:
            candidate = self._confined_library_path(destination, root)
            job = self.database.get_job(job_id)
            planned = self._recorded_library_path(str(job["planned_path"]), root)
        except (KeyError, TypeError, UnsafeLibraryPath, ValueError):
            return False
        if candidate != planned or not candidate.exists():
            return False
        archive_evidence = (job.get("language_evidence") or {}).get("archive")
        expected_hash = (
            archive_evidence.get("sha256")
            if isinstance(archive_evidence, dict)
            else None
        )
        if (
            not isinstance(expected_hash, str)
            or len(expected_hash) != 64
            or any(character not in "0123456789abcdef" for character in expected_hash)
        ):
            return False
        try:
            actual_hash = validate_cbz(candidate)["sha256"]
        except OSError:
            return None
        except Exception:  # noqa: BLE001 - invalid archive is a deterministic mismatch
            return False
        return actual_hash == expected_hash
