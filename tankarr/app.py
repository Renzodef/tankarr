from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import mimetypes
import platform
import shutil
import sqlite3
import zipfile
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, suppress
from datetime import UTC, date, datetime, timedelta
from functools import partial
from pathlib import Path
from time import monotonic
from typing import Any
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import Headers
from starlette.staticfiles import NotModifiedResponse

from tankarr import __version__, series_unit
from tankarr.artwork_thumbnails import (
    ARTWORK_THUMBNAIL_VERSION,
    SUPPORTED_ARTWORK_THUMBNAIL_WIDTHS,
    ArtworkThumbnailCache,
)
from tankarr.artwork_urls import (
    artwork_cache_headers,
    normalized_artwork_sha256,
    series_artwork_url,
    volume_artwork_url,
)
from tankarr.assemble import register_assemble_routes
from tankarr.assembly_batch import BookAssemblyBatch, register_assembly_batch_routes
from tankarr.audit_actions import register_audit_routes
from tankarr.auth import (
    SESSION_COOKIE,
    AuthenticationManager,
    AuthenticationMiddleware,
    CrossOriginProtectionMiddleware,
    RestoreSafetyMiddleware,
    SecurityHeadersMiddleware,
    client_address,
    ensure_login,
    too_many_attempts,
)
from tankarr.authors import AuthorRegistry
from tankarr.backups import ApplicationBackups, create_pre_migration_backup
from tankarr.catalogue import (
    CATALOGUE_PROVIDER,
    CATALOGUE_SOURCE,
    catalogue_card,
    parse_catalogue_id,
)
from tankarr.chapter_mapping import build_chapter_index, canonical_number
from tankarr.config import Settings, get_settings
from tankarr.database import ActiveDownloadJobsError, Database
from tankarr.http import async_client
from tankarr.importer import LanguageReviewRequired, LibraryImporter
from tankarr.internet_archive import InternetArchiveClient
from tankarr.komga import KomgaClient
from tankarr.languages import normalize_language_code
from tankarr.library_reader import ReaderIndependentLibrary
from tankarr.library_snapshot import changed_inputs, manga_revisions
from tankarr.logs import (
    apply_log_level,
    log_directory,
    log_files,
    normalize_log_level,
    tail_log,
)
from tankarr.maintenance import MaintenanceWorker
from tankarr.metadata import build_metadata_sources
from tankarr.metadata.service import MAX_ARTWORK_BYTES, MetadataService
from tankarr.models import (
    AddMangaRequest,
    AddReleaseSourceRequest,
    ArtworkSelectionRequest,
    CancelJobsRequest,
    ChapterSearchRequest,
    DismissAlertRequest,
    DownloadRequest,
    ImportRequest,
    LibraryOrphanDeleteRequest,
    LoginRequest,
    MangaDeletionPreview,
    ManualImportRequest,
    MetadataCorrelationsRequest,
    MonitorChapterRequest,
    MonitorVolumeRequest,
    RenameMangaRequest,
    TorrentGrabRequest,
    TorrentImportRequest,
    UpdateMangaRequest,
)
from tankarr.monitor import ReleaseMonitor
from tankarr.monitoring import BACKLOG_MONITOR_MODES, FUTURE_MONITOR_MODES
from tankarr.native_reader import NativeReaderLibrary, register_native_reader_routes
from tankarr.notify import CHANNEL_SETTINGS, Notifier
from tankarr.official_platforms import extensions_to_install, official_platforms_for
from tankarr.operations import register_operations_routes
from tankarr.operator_map import register_chapter_map_routes
from tankarr.providers import build_providers, replace_configurable_providers
from tankarr.providers.suwayomi import suwayomi_id_is_current
from tankarr.prowlarr import ProwlarrClient
from tankarr.qbittorrent import QBitTorrentClient, QBitTorrentError
from tankarr.read_model_cache import (
    SnapshotValidators,
    cache_now,
    cache_time_revision,
    next_publication,
)
from tankarr.reader_discovery import discover_reader as discover_local_reader
from tankarr.readers import READER_LABELS, effective_reader_kind, resolve_reader_link
from tankarr.redaction import redact_secrets
from tankarr.release_cadence import expected_releases
from tankarr.release_sources import ReleaseSourceManager
from tankarr.response_snapshots import ResponseSnapshots
from tankarr.sabnzbd import SABnzbdClient
from tankarr.series_summary import decorate_series_summary, publication_summary
from tankarr.series_unit import select_releases
from tankarr.series_units import load_series_units
from tankarr.service import (
    FutureMonitoringUnavailable,
    LibraryUnavailable,
    RecoveryBlocked,
    SeriesRenameError,
    StaleMangaDeletionError,
    StaleVolumeDeletionError,
    TankarrService,
    UnsafeLibraryPath,
)
from tankarr.settings_store import (
    SPINE_SOURCE_FLAGS,
    apply_setting_overrides,
    load_metadata_secret_overrides,
    preview_settings,
    settings_view,
    update_settings,
)
from tankarr.source_alerts import source_outages
from tankarr.source_numbering import is_not_yet_released
from tankarr.source_ranking import is_official_release, official_hosts
from tankarr.stump import StumpLibraryClient
from tankarr.suwayomi_bootstrap import (
    FLARESOLVERR_EXTENSIONS,
    WEBVIEW_EXTENSIONS,
)
from tankarr.suwayomi_runtime import (
    CHALLENGE_PATTERN,
    SuwayomiRuntime,
    SuwayomiRuntimeBusy,
    SuwayomiRuntimeError,
    default_java_executable,
)
from tankarr.tasks import ScheduledTask, TaskBusy, TaskNotRunnable, TaskRegistry
from tankarr.torrents import ORPHAN_SWEEP_INTERVAL_SECONDS, TorrentManager
from tankarr.updates import UpdateChecker
from tankarr.url_base import UrlBaseMiddleware
from tankarr.worker import DownloadWorker


def _torrent_client_url(download: dict, settings: Settings) -> str | None:
    """A browser-reachable link to the download's client, if one is set.

    ``qbittorrent_url``/``sabnzbd_url`` are what Tankarr uses internally
    (often a Docker service name a browser cannot resolve); the public
    variants are the LAN address, configured separately under Settings.
    """

    if str(download.get("protocol") or "torrent") == "usenet":
        url = settings.sabnzbd_public_url
    else:
        url = settings.qbittorrent_public_url
    return url or None


def _torrent_review_reason(download: dict) -> str | None:
    """Why a download stopped for review: the two causes need two actions.

    OCR could not confirm the pages are English ("language"), or the release
    carries books whose volume/chapter cannot be derived ("content").
    Confirming the language does nothing for the latter — the numbering check
    runs first and would refuse the import again.
    """

    if str(download.get("status") or "") != "review":
        return None
    evidence = download.get("language_evidence")
    if isinstance(evidence, dict) and evidence.get("verdict") == "review":
        return "language"
    return "content"


def _public_torrent_download(download: dict, settings: Settings) -> dict:
    """Hide server-side reacquisition data from operational API responses."""

    return {
        **{key: value for key, value in download.items() if key != "torrent_url"},
        "client_url": _torrent_client_url(download, settings),
        "review_reason": _torrent_review_reason(download),
    }


def _public_match_review(review: dict) -> dict:
    """Hide the resolved download reference a release review carries.

    A release review keeps ``download_ref`` in its stored payload so
    accepting it later can grab the same result; the browser must never
    see it, the same rule ``_public_torrent_download`` applies to a job.
    """

    payload = review.get("payload")
    if not isinstance(payload, dict) or "download_ref" not in payload:
        return review
    return {
        **review,
        "payload": {k: v for k, v in payload.items() if k != "download_ref"},
    }


logger = logging.getLogger(__name__)


class VersionedStaticFiles(StaticFiles):
    """Serve Vite assets once, precompressed and immutable."""

    def file_response(
        self,
        full_path: str | Path,
        stat_result: object,
        scope: dict[str, Any],
        status_code: int = 200,
    ) -> Response:
        requested = Path(full_path)
        served = requested
        response_headers = {
            "Cache-Control": "public, max-age=31536000, immutable",
        }
        accepted = Headers(scope=scope).get("accept-encoding", "").casefold()
        compressed = Path(f"{requested}.gz")
        if "gzip" in accepted and compressed.is_file():
            served = compressed
            stat_result = compressed.stat()
            response_headers.update(
                {"Content-Encoding": "gzip", "Vary": "Accept-Encoding"}
            )
        response = FileResponse(
            served,
            status_code=status_code,
            stat_result=stat_result,  # type: ignore[arg-type]
            media_type=mimetypes.guess_type(requested.name)[0],
            headers=response_headers,
        )
        request_headers = Headers(scope=scope)
        if self.is_not_modified(response.headers, request_headers):
            return NotModifiedResponse(response.headers)
        return response


def _metadata_api_message(error: httpx.HTTPStatusError) -> str:
    """The provider's own explanation, when it sends a JSON error body."""

    try:
        payload = error.response.json()
    except Exception:  # noqa: BLE001 - a body is a bonus, never a requirement
        return ""
    message = ""
    if isinstance(payload, dict):
        detail = payload.get("error")
        if isinstance(detail, dict):
            message = str(detail.get("message") or "")
        elif isinstance(detail, str):
            message = detail
        message = message or str(payload.get("message") or "")
    return f" — {message.strip()}" if message.strip() else ""


def _whole_number(value: object) -> int:
    """The whole number a label carries, or zero when it carries none."""

    try:
        number = float(str(value))
    except (TypeError, ValueError):
        return 0
    return int(number) if number == int(number) and number > 0 else 0


def _tell_both_counts(
    decorated: dict, item: dict, metadata_data: dict | None, chapter_index: dict
) -> None:
    """Say the same work measured the other way, on the card and on the page.

    A shelf of 47 books also holds 455 chapters, and a run of 257 chapters
    makes 26 books: both places can say both instead of making the reader
    guess. The unit the series is already counted in is not counted again
    here — it is the very number the header shows, so a total typed by hand
    in Edit reads the same on every screen. Only the other unit is derived:
    from the books the map holds, or from what the catalogues agree on.
    """

    counts = decorated.get("library_count") or {}
    counted_in_books = str(counts.get("unit") or "chapter") != "chapter"
    shown_total = _whole_number(counts.get("total_count"))

    # Two counts can both be right — FLCL is two Japanese tankobon that Dark
    # Horse printed as one omnibus — so the derived book count takes whichever
    # is larger, unless an edition pins it.
    mapped_books = len(
        {
            str(slot.get("volume"))
            for slot in chapter_index.get("slots") or []
            if slot.get("volume")
        }
    )
    derived_books = _whole_number(item.get("edition_book_count")) or max(
        int(chapter_index.get("expected_volume_count") or 0), mapped_books
    )
    # The catalogues agree on how many chapters a work has even when nothing
    # has numbered its last one: A Drunken Dream is one book of ten chapters
    # that MangaBaka and Kitsu both count, and reading only the last chapter
    # seen left the card silent about it.
    derived_chapters = (
        _whole_number(chapter_index.get("read_chapter_count"))
        or _whole_number((metadata_data or {}).get("chapter_count"))
        or _whole_number(item.get("last_chapter"))
    )

    books_total = shown_total if counted_in_books else derived_books
    chapters_total = derived_chapters if counted_in_books else shown_total
    decorated["book_total_count"] = books_total or None
    decorated["chapter_total_count"] = chapters_total or None


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    settings.ensure_directories()
    blocking_executor = ThreadPoolExecutor(
        max_workers=4, thread_name_prefix="tankarr-blocking"
    )
    api_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="tankarr-api")
    revision_executor = ThreadPoolExecutor(
        max_workers=1, thread_name_prefix="tankarr-api-revision"
    )

    async def run_api_blocking(
        function: Callable[..., Any], /, *args: Any, **kwargs: Any
    ) -> Any:
        call = partial(function, *args, **kwargs)
        return await asyncio.get_running_loop().run_in_executor(api_executor, call)

    async def run_revision(function: Callable[..., Any], /, *args: Any) -> Any:
        # A warm request must not queue behind two cold, CPU-heavy renders.
        return await asyncio.get_running_loop().run_in_executor(
            revision_executor, partial(function, *args)
        )

    load_metadata_secret_overrides(settings)
    database = Database(settings.database_path)
    database.initialize(before_migration=lambda: create_pre_migration_backup(settings))
    apply_setting_overrides(settings, database)
    ensure_login(settings)
    database.provider_priority = settings.provider_priority_order
    database.source_ranking = settings.source_ranking

    def retire_stale_suwayomi_identities() -> None:
        if not settings.suwayomi_enabled:
            return
        token = settings.suwayomi_instance_token
        retired = database.retire_stale_provider_identities(
            "suwayomi", lambda value: suwayomi_id_is_current(value, token)
        )
        if any(retired.values()):
            logger.info(
                "Retired Suwayomi identities from a previous instance: %s", retired
            )

    retire_stale_suwayomi_identities()
    providers = build_providers(settings)
    retired_providers = []
    provider_reload_lock = asyncio.Lock()
    provider = providers.get("suwayomi") or providers["local"]
    komga_links = KomgaClient(settings)

    def managed_library_reader():
        """The reader Tankarr keeps in sync with the library, if any.

        Komga is mirrored through its own settings; Stump is driven through
        the generic reader settings; every other reader scans on its own.
        """

        kind = effective_reader_kind(settings)
        if kind == "tankarr":
            return NativeReaderLibrary()
        if settings.komga_link_enabled:
            return komga_links
        if kind == "stump":
            stump = StumpLibraryClient(settings)
            if stump.configured:
                return stump
        return ReaderIndependentLibrary()

    def managed_metadata_reader(reader):
        if callable(getattr(reader, "catalogue_for_paths", None)) and callable(
            getattr(reader, "apply_catalogue_metadata", None)
        ):
            return reader
        return None

    library_reader = managed_library_reader()
    service = TankarrService(settings, database, providers, library_reader)
    metadata = MetadataService(
        settings,
        database,
        managed_metadata_reader(library_reader),
        providers,
        build_metadata_sources(settings),
        library_publisher=service.publish_metadata_to_library,
        origin_refresher=service.refresh_metadata,
        canonical_title_publisher=service.apply_metadata_title,
    )
    catalogue_metadata = next(
        source
        for source in metadata.sources
        if getattr(source, "name", "") == CATALOGUE_SOURCE
    )
    authors = AuthorRegistry(settings, database, catalogue_metadata)
    artwork_thumbnails = ArtworkThumbnailCache(
        settings.data_dir / "metadata" / "thumbnails"
    )
    service.set_komga_refresh_post_scan(lambda: metadata.sync_all_to_komga(force=False))
    worker = DownloadWorker(database, service)
    release_sources = ReleaseSourceManager(database, providers)
    from tankarr.translation import TranslationManager

    translations = TranslationManager(settings, database, service, release_sources)
    monitor = ReleaseMonitor(
        settings, database, service, worker, release_sources=release_sources
    )
    monitor.translations = translations
    importer = LibraryImporter(settings, database, service)
    backups = ApplicationBackups(settings)
    maintenance = MaintenanceWorker(
        settings,
        database,
        service,
        backups,
        import_running=lambda: bool(importer.state.get("running")),
        ready=lambda: bool(worker.status()["running"]),
    )
    discovery_tasks: set[asyncio.Task] = set()
    author_refresh_pending: set[str] = set()

    def schedule_author_refresh(author_ids: list[str]) -> None:
        if settings.restored_safe_mode:
            return
        pending = sorted(set(author_ids) - author_refresh_pending)
        if not pending:
            return
        author_refresh_pending.update(pending)

        async def run() -> None:
            for author_id in pending:
                try:
                    await authors.refresh(author_id)
                except Exception:  # noqa: BLE001 - the periodic loop retries it
                    logger.warning(
                        "Add-time MangaBaka author refresh failed for %s",
                        author_id,
                        exc_info=True,
                    )

        task = asyncio.create_task(run(), name="tankarr-add-author-refresh")
        discovery_tasks.add(task)

        def completed(done: asyncio.Task) -> None:
            author_refresh_pending.difference_update(pending)
            discovery_tasks.discard(done)

        task.add_done_callback(completed)

    def schedule_release_source_discovery(
        manga_id: str,
        *,
        monitor_new: bool,
        queue_backlog: bool,
        before: Callable[[], Awaitable[Any]] | None = None,
    ) -> None:
        """Correlate a newly added series with every other provider right away.

        The Wanted cycle repeats the same discovery on its own schedule; doing
        it at add time only removes the initial single-source window. ``before``
        runs first (the catalogue identity pin and enrichment) so the chapter
        map exists before any source is searched — off the request path.
        """

        async def run() -> None:
            if before is not None:
                try:
                    await before()
                except Exception:  # noqa: BLE001 - enrichment retries on its own
                    logging.getLogger(__name__).warning(
                        "Catalogue identity pin failed for %s", manga_id, exc_info=True
                    )
            try:
                await ensure_official_extensions(manga_id)
            except Exception:  # noqa: BLE001 - discovery still runs
                logging.getLogger(__name__).debug(
                    "official extension install failed for %s", manga_id, exc_info=True
                )
            try:
                await release_sources.discover_and_refresh(
                    manga_id, monitor_new=monitor_new
                )
                if queue_backlog:
                    await monitor.queue_missing(manga_id)
            except Exception:  # noqa: BLE001 - per-source states are persisted
                logging.getLogger(__name__).warning(
                    "Add-time release source discovery failed for %s",
                    manga_id,
                    exc_info=True,
                )
                return
            if not queue_backlog:
                return
            try:
                # What the sources do not list is asked of the indexers and
                # the archives now, not at the next six-hourly Wanted pass.
                await monitor.search_wanted_series(manga_id, trigger="add")
            except Exception:  # noqa: BLE001 - the Wanted pass repeats it
                logging.getLogger(__name__).warning(
                    "Add-time Wanted search failed for %s", manga_id, exc_info=True
                )

        task = asyncio.create_task(run(), name=f"tankarr-add-discovery-{manga_id}")
        discovery_tasks.add(task)
        task.add_done_callback(discovery_tasks.discard)

    prowlarr = ProwlarrClient(settings)
    internet_archive = InternetArchiveClient(settings)
    qbittorrent = QBitTorrentClient(settings)
    sabnzbd = SABnzbdClient(settings)
    suwayomi_runtime = SuwayomiRuntime(
        settings.suwayomi_runtime_dir,
        heap_mb=settings.suwayomi_managed_heap_mb,
        credentials=settings.suwayomi_managed_credentials,
        extension_store=settings.suwayomi_extension_store or None,
        java_executable=default_java_executable(),
    )

    suwayomi_maintenance: dict[str, Any] = {"last_run_at": None, "result": None}

    async def maintain_managed_suwayomi(*, force: bool = False) -> dict[str, Any]:
        """Daily: update the server JAR when a release is out, then the
        installed extensions. The JVM restart waits for an idle queue."""

        if not settings.suwayomi_managed or not suwayomi_runtime.installed():
            return {"skipped": "not managed"}
        result: dict[str, Any] = {}
        try:
            check = await suwayomi_runtime.check_update()
            result["server"] = check
            busy = any(
                job["status"] in {"running", "downloading", "packaging", "importing"}
                for job in database.list_jobs(
                    statuses=("running", "downloading", "packaging", "importing"),
                    limit=1,
                )
            )
            if check.get("update_available") and (force or not busy):
                await suwayomi_runtime.install()
                await suwayomi_runtime.wait_ready()
                result["server"]["updated_to"] = check["latest"]
            elif check.get("update_available"):
                result["server"]["deferred"] = "downloads in progress"
            if suwayomi_runtime.running and (
                force or await suwayomi_runtime.wait_ready(timeout=5)
            ):
                result[
                    "extensions_updated"
                ] = await suwayomi_runtime.update_installed_extensions()
                result["catalogue_installed"] = await install_language_catalogue()
            result["challenged_sources"] = challenged_sources()
        except Exception as exc:  # noqa: BLE001 - maintenance is best effort
            result["error"] = f"{type(exc).__name__}: {exc}"[:300]
        suwayomi_maintenance["last_run_at"] = datetime.now(UTC).isoformat(
            timespec="seconds"
        )
        suwayomi_maintenance["result"] = result
        return result

    def challenged_sources() -> list[str]:
        """Sources that answered with an anti-bot challenge.

        The managed runtime has no bypass: a Chromium solver does not fit
        this host, and of the English catalogue measured on this network only
        a couple of sources are gated this way. They are reported so the
        operator knows why those sources never return anything, and the
        perennial ranking sinks them on its own.
        """

        names = list(suwayomi_runtime.challenged_sources)
        for mapping in database.list_all_release_sources():
            error = str(mapping.get("last_error") or "")
            if CHALLENGE_PATTERN.search(error) and mapping.get("source_name"):
                names.append(str(mapping["source_name"]))
        return sorted(set(names))

    async def suwayomi_maintenance_loop() -> None:
        await asyncio.sleep(600)  # let startup settle
        await refresh_installed_sources(force=True)
        await install_language_catalogue()
        while True:
            await maintain_managed_suwayomi()
            await asyncio.sleep(24 * 3600)

    async def start_managed_suwayomi() -> None:
        try:
            await suwayomi_runtime.start()
            if await suwayomi_runtime.wait_ready():
                logger.info("Managed Suwayomi is ready at %s", suwayomi_runtime.url)
            else:
                logger.warning(
                    "Managed Suwayomi did not become ready: %s",
                    suwayomi_runtime.status().get("last_error"),
                )
        except Exception as exc:  # noqa: BLE001 - the runtime reports itself
            logger.error("Managed Suwayomi failed to start: %s", exc)

    torrents = TorrentManager(
        database,
        service,
        importer,
        metadata,
        prowlarr,
        qbittorrent,
        sabnzbd=sabnzbd,
        internet_archive=internet_archive,
    )
    monitor.torrents = torrents  # automatic volume search in the Wanted cycle
    from tankarr.translation_acquisition import TranslationAcquisition

    translations.acquisition = TranslationAcquisition(translations, torrents)
    torrents.translations = translations.acquisition
    authentication = AuthenticationManager(settings)
    authentication.api_key()
    updates = UpdateChecker(__version__, enabled=settings.update_check_enabled)
    snapshot_validators = SnapshotValidators()
    started_at = datetime.now(UTC).isoformat()

    def attach_series_artwork(item: dict, enriched: dict | None) -> dict | None:
        raw_metadata = (
            enriched.get("data") if enriched is not None else item.get("metadata")
        )
        metadata_data = dict(raw_metadata) if isinstance(raw_metadata, dict) else None
        digest = normalized_artwork_sha256(
            enriched.get("artwork_sha256") if enriched is not None else None
        )
        if enriched is not None and enriched.get("artwork_path"):
            artwork_url = series_artwork_url(str(item["id"]), digest)
        else:
            artwork_url = (
                str(metadata_data.get("cover_url"))
                if metadata_data and metadata_data.get("cover_url")
                else item.get("cover_url")
            )
        if metadata_data is not None:
            metadata_data["cover_url"] = artwork_url
            item["metadata"] = metadata_data
        item["artwork_url"] = artwork_url
        item["artwork_sha256"] = digest
        return metadata_data

    def attach_volume_artwork(manga_id: str, record: dict) -> dict:
        decorated = dict(record)
        raw_data = decorated.get("data")
        data = dict(raw_data) if isinstance(raw_data, dict) else {}
        digest = normalized_artwork_sha256(decorated.get("artwork_sha256"))
        if decorated.get("artwork_path"):
            artwork_url = volume_artwork_url(
                manga_id, str(decorated["volume_key"]), digest
            )
        else:
            artwork_url = data.get("cover_url")
        data["cover_url"] = artwork_url
        decorated["data"] = data
        decorated["artwork_url"] = artwork_url
        decorated["artwork_sha256"] = digest
        return decorated

    installed_sources_cache: dict[str, Any] = {"at": 0.0, "sources": []}

    def _acquisition_policy() -> str:
        """The release policy the download gate is applying right now."""

        return (
            str(getattr(database.source_ranking, "acquisition_policy", "") or "")
            .strip()
            .casefold()
        )

    def _unit_context(manga_id: str) -> dict[str, Any]:
        return {
            "preferred": getattr(settings, "preferred_unit", "volumes"),
            "indexer_volumes": database.list_indexer_volumes(manga_id),
            "unobtainable_volumes": database.unobtainable_indexer_volumes(manga_id),
            "pending_volumes": database.pending_book_volumes([manga_id]).get(
                manga_id, set()
            ),
            "acquisition_policy": _acquisition_policy(),
        }

    series_unit.unit_context = _unit_context

    async def refresh_installed_sources(*, force: bool = False) -> list[dict[str, Any]]:
        """Suwayomi sources with their home URLs, refreshed hourly."""

        import time as _time

        now = _time.monotonic()
        if (
            not force
            and installed_sources_cache["sources"]
            and now - installed_sources_cache["at"] < 3600
        ):
            return installed_sources_cache["sources"]
        if not (settings.suwayomi_managed and suwayomi_runtime.running):
            return installed_sources_cache["sources"]
        try:
            previous = {
                f"suwayomi:{source['id']}".casefold()
                for source in installed_sources_cache["sources"]
                if source.get("id")
            }
            installed_sources_cache["sources"] = await suwayomi_runtime.list_sources()
            installed_sources_cache["at"] = now
            current = {
                f"suwayomi:{source['id']}".casefold()
                for source in installed_sources_cache["sources"]
                if source.get("id")
            }
            # A source that is gone must not keep offering releases: they
            # would be chosen and fail one by one. A list that shrank by more
            # than an uninstall or two is a runtime mid-restart, not a
            # removal, and is left alone.
            if current and (not previous or len(current) >= len(previous) - 2):
                retired = database.retire_uninstalled_sources(current)
                if retired["releases"] or retired["mappings"]:
                    logger.info(
                        "Retired %s releases and %s mappings of uninstalled sources",
                        retired["releases"],
                        retired["mappings"],
                    )
            # An unmaintained extension is the last choice everywhere: it can
            # still be the only place a work is published, and it stays a
            # valid reference for counts and dates, but a maintained source
            # is always tried first for downloads.
            database.deprioritised_sources = frozenset(
                f"suwayomi:{source['id']}".casefold()
                for source in installed_sources_cache["sources"]
                if source.get("obsolete") and source.get("id")
            )
        except Exception:  # noqa: BLE001 - keep the previous snapshot
            pass
        return installed_sources_cache["sources"]

    async def ensure_official_extensions(manga_id: str) -> list[str]:
        """Install the free official platforms' extensions for a work."""

        if not (
            settings.suwayomi_managed
            and settings.suwayomi_auto_install_official
            and suwayomi_runtime.running
        ):
            return []
        manga = database.get_manga(manga_id)
        metadata_row = database.get_series_metadata(manga_id)
        sources = await refresh_installed_sources()
        platforms = official_platforms_for(
            manga, (metadata_row or {}).get("data") or {}, installed_sources=sources
        )
        installed: list[str] = []
        for pkg_name in extensions_to_install(platforms):
            try:
                await suwayomi_runtime.set_extension(pkg_name, installed=True)
                installed.append(pkg_name)
            except Exception:  # noqa: BLE001 - the catalogue may lack it
                logging.getLogger(__name__).info(
                    "Could not install %s for %s", pkg_name, manga_id, exc_info=True
                )
        if installed:
            await refresh_installed_sources(force=True)
        return installed

    async def install_language_catalogue() -> list[str]:
        """Install every safe store extension for the enabled languages.

        Tankarr does not curate membership: the perennial health ranking
        orders sources by their measured failures and download speed, so
        the catalogue can be broad. What stays out cannot work here at all:
        NSFW, the WebView-dependent ones, and the Cloudflare-gated ones -
        this host has no bypass to offer them. Unmaintained extensions are
        never uninstalled: the runtime's obsolete flag and their own failures
        rank them last.
        """

        if not (settings.suwayomi_managed and suwayomi_runtime.running):
            return []
        if settings.suwayomi_source_id_set:
            # The operator picked the sources by hand (or a measured run did).
            # Re-installing the whole store on every start would undo that
            # choice and pay its memory cost again, so a chosen catalogue is
            # left exactly as it is.
            return []
        excluded = set(WEBVIEW_EXTENSIONS) | set(FLARESOLVERR_EXTENSIONS)
        try:
            installed = await suwayomi_runtime.install_catalogue(
                settings.search_language_codes, exclude=excluded
            )
        except Exception as exc:  # noqa: BLE001 - retried on the next pass
            logger.info("Suwayomi catalogue install failed: %s", exc)
            return []
        if installed:
            logger.info(
                "Installed %s new extensions for languages %s",
                len(installed),
                ", ".join(settings.search_language_codes),
            )
            await refresh_installed_sources(force=True)
            await seed_source_standings(set(installed))
        return installed

    async def seed_source_standings(pkg_names: set[str]) -> None:
        """First standings for sources that just joined the catalogue.

        A quick probe records one success or failure per new source, so a
        newcomer enters the perennial ranking with a measured position
        instead of a blank one.
        """

        sources = [
            source
            for source in installed_sources_cache["sources"]
            if source.get("pkg_name") in pkg_names
        ]
        if not sources:
            return
        try:
            results = await suwayomi_runtime.test_sources(
                sources,
                language=settings.default_language,
                timeout=float(settings.suwayomi_search_timeout_seconds),
            )
        except Exception:  # noqa: BLE001 - seeding is best effort
            logger.info("Standings seeding failed", exc_info=True)
            return
        for item in results:
            database.record_source_health(
                f"suwayomi:{item['id']}",
                ok=item["verdict"] != "unreachable",
                reason=str(item.get("error") or ""),
            )

    monitor.before_discovery = ensure_official_extensions

    def decorate_manga(item: dict) -> dict:
        item["author_entities"] = database.list_manga_authors(str(item["id"]))
        enriched = database.get_series_metadata(str(item["id"]))
        metadata_data = attach_series_artwork(item, enriched)
        item["official_platforms"] = [
            platform
            for platform in official_platforms_for(
                item,
                (enriched or {}).get("data") or {},  # the API copy hides official_links
                installed_sources=installed_sources_cache["sources"],
                release_sources=item.get("release_sources")
                or database.list_release_sources(str(item["id"])),
            )
            if platform["free"]
        ]
        if enriched is not None:
            item["metadata_last_enriched_at"] = enriched["last_enriched_at"]
        overrides = database.list_volume_monitor_overrides(str(item["id"]))
        item["volume_monitor_overrides"] = overrides
        item["publication_signals"] = database.publication_signals(str(item["id"]))
        override_map = {entry["volume_key"]: entry["state"] for entry in overrides}
        chapter_index = item.get("chapter_index") or build_chapter_index(
            item,
            metadata_data,
            item.get("chapters")
            or database.list_chapters(str(item["id"]), item["preferred_language"]),
            override_map,
            chapter_map=database.chapter_map(str(item["id"])),
        )
        decorated = decorate_series_summary(item, metadata_data, chapter_index)
        _tell_both_counts(decorated, item, metadata_data, chapter_index)
        return decorated

    library_metadata_fields = frozenset(
        {
            "alternate_titles",
            "authors",
            "content_kind",
            "cover_url",
            "creator_links",
            "display_title",
            "editions",
            "status",
            "title",
            "year",
        }
    )
    response_snapshots = ResponseSnapshots(
        settings.data_dir / "cache" / "responses",
        settings.database_path,
        scope=json.dumps(
            [str(settings.library_dir.resolve()), settings.preferred_unit]
        ),
    )
    saved_library = response_snapshots.load("library")
    library_cache: dict[str, Any] = {
        "revision": None,
        "payload": saved_library.get("payload"),
        "refresh_task": None,
    }
    library_cache_lock = asyncio.Lock()
    library_cards: dict[str, dict[str, Any]] = {}

    def decorate_library_manga(item: dict, inputs: dict[str, Any]) -> dict:
        """Build the exact card counts without returning detail-only metadata."""

        item["author_entities"] = inputs["authors"]
        enriched = inputs["metadata"]
        metadata_data = attach_series_artwork(item, enriched)
        item["publication_signals"] = inputs["publication_signals"]
        item["_unit_context"] = {
            "preferred": settings.preferred_unit,
            "indexer_volumes": inputs["indexer_volumes"],
            "unobtainable_volumes": inputs["unobtainable_volumes"],
            "pending_volumes": inputs.get("pending_volumes", set()),
            "acquisition_policy": _acquisition_policy(),
        }
        chapter_index = build_chapter_index(
            item,
            metadata_data,
            inputs["releases"],
            inputs["overrides"],
            chapter_map=inputs["chapter_map"],
        )
        item.pop("_unit_context", None)
        decorated = decorate_series_summary(item, metadata_data, chapter_index)
        _tell_both_counts(decorated, item, metadata_data, chapter_index)
        effective_unit = chapter_index.get("series_unit")
        decorated["effective_series_unit"] = (
            effective_unit
            if effective_unit in {"chapters", "volumes"}
            else "volumes"
            if chapter_index.get("unit") == "volume"
            else "chapters"
        )
        if metadata_data is not None:
            decorated["metadata"] = {
                key: metadata_data[key]
                for key in library_metadata_fields
                if key in metadata_data
            }
        return decorated

    def render_library_payload() -> bytes:
        with database.read_snapshot():
            revisions = {
                manga_id: (*revision, str(settings.preferred_unit))
                for manga_id, revision in manga_revisions(database).items()
            }
            now = cache_now()
            changed = [
                manga_id
                for manga_id, revision in revisions.items()
                if manga_id not in library_cards
                or library_cards[manga_id]["revision"] != revision
                or library_cards[manga_id]["expires_at"] <= now
            ]
            inputs = changed_inputs(database, changed)
            for manga_id, entry in inputs.items():
                card = decorate_library_manga(entry["manga"], entry)
                library_cards[manga_id] = {
                    "revision": revisions[manga_id],
                    "expires_at": next_publication(entry["releases"], since=now),
                    "payload": json.dumps(
                        card, ensure_ascii=False, separators=(",", ":")
                    ).encode("utf-8"),
                }
            for manga_id in library_cards.keys() - revisions.keys():
                del library_cards[manga_id]
            payload = (
                b"["
                + b",".join(
                    library_cards[manga_id]["payload"] for manga_id in revisions
                )
                + b"]"
            )
            response_snapshots.save("library", {"payload": payload})
            return payload

    async def refresh_library_payload() -> bytes:
        async with library_cache_lock:
            revision = (
                *await run_revision(database.library_revision),
                str(settings.preferred_unit),
                cache_time_revision(),
            )
            if (
                library_cache["revision"] == revision
                and library_cache["payload"] is not None
            ):
                return library_cache["payload"]
            payload = await run_api_blocking(render_library_payload)
            # Publish a complete snapshot even if the queue changed while it
            # was rendered. Keeping the starting revision guarantees that the
            # next request notices that race and schedules another refresh.
            library_cache["revision"] = revision
            library_cache["payload"] = payload
            return payload

    def schedule_library_refresh() -> None:
        current = library_cache.get("refresh_task")
        if current is not None and not current.done():
            return

        async def refresh() -> None:
            try:
                await refresh_library_payload()
            except Exception:  # noqa: BLE001 - the next request retries it
                logger.warning("Unable to refresh the Library response", exc_info=True)

        task = asyncio.create_task(refresh(), name="tankarr-library-cache-refresh")
        library_cache["refresh_task"] = task
        discovery_tasks.add(task)

        def completed(done: asyncio.Task[None]) -> None:
            discovery_tasks.discard(done)
            if library_cache.get("refresh_task") is done:
                library_cache["refresh_task"] = None

        task.add_done_callback(completed)

    async def cached_library_payload() -> bytes:
        revision = (
            *await run_revision(database.library_revision),
            str(settings.preferred_unit),
            cache_time_revision(),
        )
        cached_revision = library_cache.get("revision")
        cached_payload = library_cache.get("payload")
        if cached_revision == revision and cached_payload is not None:
            return cached_payload
        if cached_payload is not None and await run_revision(
            database.has_pending_download_jobs
        ):
            # Imports update both chapter and series timestamps. Serve the last
            # complete snapshot while exact counts are rebuilt off the event
            # loop only while a download queue is active; callers performing a
            # mutation can request ``fresh=true``.
            schedule_library_refresh()
            return cached_payload
        return await refresh_library_payload()

    series_cache: OrderedDict[tuple[str, bool], dict[str, Any]] = OrderedDict()
    series_cache_lock = asyncio.Lock()
    series_cache_limit = 32
    compact_chapter_fields = {
        "id",
        "manga_id",
        "volume",
        "chapter",
        "title",
        "language",
        "provider",
        "groups",
        "source_name",
        "publish_at",
        "source_url",
        "pages",
        "version",
        "monitored",
        "downloaded",
        "library_path",
        "queue_job_id",
        "queue_status",
        "blocked",
        "block_reason",
    }

    def compact_chapter_releases(chapter_index: dict[str, Any]) -> None:
        """Keep the series overview small; release search loads alternatives."""

        for slot in chapter_index.get("slots") or []:
            releases = slot.get("releases") or []
            slot["release_count"] = len(releases)
            slot["all_releases_blocked"] = bool(releases) and all(
                bool(release.get("blocked")) for release in releases
            )
            selected: list[dict[str, Any]] = []
            seen: set[str] = set()
            for release in [
                *releases[:1],
                *(item for item in releases[1:] if item.get("downloaded")),
            ]:
                release_id = str(release.get("id") or "")
                if not release_id or release_id in seen:
                    continue
                seen.add(release_id)
                selected.append(
                    {
                        field: release[field]
                        for field in compact_chapter_fields
                        if field in release
                    }
                )
            slot["releases"] = selected

    def render_series_payload(manga_id: str, compact: bool) -> bytes:
        with database.read_snapshot():
            return render_series_snapshot(manga_id, compact)

    def render_series_snapshot(manga_id: str, compact: bool) -> bytes:
        manga = database.get_manga(manga_id)
        manga["author_entities"] = database.list_manga_authors(manga_id)
        manga["chapters"] = database.list_chapters(
            manga_id, manga["preferred_language"]
        )
        blocked = database.blocked_releases(manga_id)
        quality = database.page_quality(manga_id)
        for chapter in manga["chapters"]:
            block = blocked.get(str(chapter["id"]))
            chapter["blocked"] = block is not None
            chapter["block_reason"] = block["reason"] if block else None
            measured = quality.get(str(chapter["id"]))
            chapter["page_quality"] = (
                {
                    "verdict": measured["verdict"],
                    "reason": measured.get("reason") or "",
                    "whole_chapter": measured.get("whole_chapter") or "",
                }
                if measured
                else None
            )
        enriched = database.get_series_metadata(manga_id)
        if enriched is not None:
            manga["metadata"] = enriched["data"]
            manga["metadata_last_enriched_at"] = enriched["last_enriched_at"]
            manga["metadata_last_synced_at"] = enriched["last_synced_at"]
            manga["metadata_last_error"] = enriched["last_error"]
            manga["metadata_source_status"] = enriched["source_status"]
        metadata_data = attach_series_artwork(manga, enriched)
        manga["volume_metadata"] = [
            attach_volume_artwork(manga_id, record)
            for record in database.list_volume_metadata(manga_id)
        ]
        overrides = database.list_volume_monitor_overrides(manga_id)
        manga["volume_monitor_overrides"] = overrides
        manga["chapter_index"] = build_chapter_index(
            manga,
            metadata_data,
            manga["chapters"],
            {entry["volume_key"]: entry["state"] for entry in overrides},
            chapter_map=database.chapter_map(manga_id),
        )
        manga["release_sources"] = database.list_release_sources(manga_id)
        manga["publication_signals"] = database.publication_signals(manga_id)
        manga["publication_pause"] = database.publication_pause(manga_id)
        manga["official_platforms"] = [
            platform
            for platform in official_platforms_for(
                manga,
                (enriched or {}).get("data") or {},
                installed_sources=installed_sources_cache["sources"],
                release_sources=manga["release_sources"],
            )
            if platform["free"]
        ]
        decorated = decorate_series_summary(
            manga, metadata_data, manga["chapter_index"]
        )
        _tell_both_counts(decorated, manga, metadata_data, manga["chapter_index"])
        if compact:
            # The chapter index already contains every logical slot and its
            # preferred release. Alternatives are fetched by the existing
            # chapter-search endpoint only when the operator asks for them.
            compact_chapter_releases(decorated["chapter_index"])
            decorated.pop("chapters", None)
        return json.dumps(decorated, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )

    async def current_series_revision(manga_id: str) -> tuple[str, ...]:
        source_revision = repr(
            sorted(
                (
                    str(source.get("id") or source.get("pkg_name") or ""),
                    bool(source.get("obsolete")),
                )
                for source in installed_sources_cache["sources"]
            )
        )
        return (
            *await run_revision(database.manga_revision, manga_id),
            str(settings.preferred_unit),
            repr(database.source_ranking),
            source_revision,
            cache_time_revision(),
        )

    def series_etag(revision: tuple[str, ...]) -> str:
        serialized = json.dumps(revision, separators=(",", ":")).encode("utf-8")
        return f'"series-{hashlib.sha256(serialized).hexdigest()[:24]}"'

    def operational_series_revision_change(
        previous: tuple[str, ...] | None, current: tuple[str, ...]
    ) -> bool:
        if previous is None or len(previous) != len(current):
            return False
        # Database.manga_revision fields 1, 7 and 9 represent chapter rows,
        # release blocks and active jobs. These change continuously while the
        # worker runs; metadata, mappings, source identities and settings do not.
        operational_fields = {1, 7, 9}
        return all(
            old == new
            for index, (old, new) in enumerate(zip(previous, current, strict=True))
            if index not in operational_fields
        )

    async def refresh_series_payload(
        manga_id: str, revision: tuple[str, ...], compact: bool
    ) -> tuple[bytes, tuple[str, ...]]:
        key = (manga_id, compact)
        payload = await run_api_blocking(render_series_payload, manga_id, compact)
        async with series_cache_lock:
            series_cache[key] = {
                "revision": revision,
                "payload": payload,
                "refresh_task": None,
                "refresh_revision": None,
            }
            series_cache.move_to_end(key)
            while len(series_cache) > series_cache_limit:
                series_cache.popitem(last=False)
        return payload, revision

    async def cached_series_payload(
        manga_id: str,
        revision: tuple[str, ...],
        compact: bool,
        *,
        fresh: bool,
    ) -> tuple[bytes, tuple[str, ...]]:
        key = (manga_id, compact)
        retry_after: asyncio.Task | None = None
        async with series_cache_lock:
            cached = series_cache.get(key)
            if (
                cached is not None
                and cached.get("payload") is not None
                and cached.get("revision") == revision
            ):
                series_cache.move_to_end(key)
                return cached["payload"], revision

            task = cached.get("refresh_task") if cached is not None else None
            task_revision = (
                cached.get("refresh_revision") if cached is not None else None
            )
            if task is not None and not task.done() and task_revision != revision:
                retry_after = task
            elif task is None or task.done():
                task = asyncio.create_task(
                    refresh_series_payload(manga_id, revision, compact),
                    name=f"tankarr-series-cache-{manga_id}",
                )
                discovery_tasks.add(task)
                task.add_done_callback(discovery_tasks.discard)
                if cached is None:
                    cached = {"revision": None, "payload": None}
                    series_cache[key] = cached
                cached["refresh_task"] = task
                cached["refresh_revision"] = revision

            if (
                not fresh
                and cached is not None
                and cached.get("payload") is not None
                and operational_series_revision_change(cached.get("revision"), revision)
            ):
                series_cache.move_to_end(key)
                return cached["payload"], cached["revision"]

        if retry_after is not None:
            try:
                await asyncio.shield(retry_after)
            except Exception:  # noqa: BLE001 - retry the caller's newer revision
                pass
            return await cached_series_payload(manga_id, revision, compact, fresh=fresh)
        assert task is not None
        return await asyncio.shield(task)

    wanted_chapter_fields = (
        "id",
        "volume",
        "chapter",
        "title",
        "provider",
        "source_name",
        "publish_at",
        "queue_status",
        "blocked",
        "block_reason",
        "slot_key",
        "recovery",
    )
    saved_wanted = response_snapshots.load("wanted")
    try:
        saved_wanted_revision = tuple(
            str(value) for value in json.loads(saved_wanted["revision"])
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        saved_wanted_revision = ()
    wanted_cache: dict[str, Any] = {
        "revision": None,
        "persisted_revision": saved_wanted_revision,
        "payload": saved_wanted.get("payload"),
        "compact_payload": saved_wanted.get("compact_payload"),
        "refresh_task": None,
        "startup_ready": False,
        "startup_snapshot": False,
    }
    wanted_cache_lock = asyncio.Lock()

    def compact_wanted_chapter(chapter: dict[str, Any]) -> dict[str, Any]:
        compact = {
            key: chapter.get(key) for key in wanted_chapter_fields if key in chapter
        }
        recovery = compact.get("recovery")
        # A slot nobody has searched yet carries the same boilerplate verdict
        # as every other one; the page says as much when the key is absent.
        # For a freshly added library it was half of the Wanted payload.
        if (
            isinstance(recovery, dict)
            and recovery.get("verdict") == "unsearched"
            and not recovery.get("channels")
        ):
            del compact["recovery"]
        return compact

    def render_wanted_payloads(revision: tuple[str, ...]) -> tuple[bytes, bytes]:
        records = service.list_wanted()
        compact_records = [
            {
                **{key: value for key, value in entry.items() if key != "chapters"},
                "chapters": [
                    compact_wanted_chapter(chapter) for chapter in entry["chapters"]
                ],
            }
            for entry in records
        ]
        payloads = (
            json.dumps(records, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            ),
            json.dumps(
                compact_records, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8"),
        )
        response_snapshots.save(
            "wanted",
            {
                "payload": payloads[0],
                "compact_payload": payloads[1],
                "revision": json.dumps(revision, separators=(",", ":")).encode("utf-8"),
            },
        )
        return payloads

    async def current_wanted_revision() -> tuple[str, ...]:
        return (
            *await run_revision(database.wanted_revision),
            str(settings.preferred_unit),
            repr(database.source_ranking),
            repr(sorted(database.deprioritised_sources)),
            cache_time_revision(),
        )

    async def refresh_wanted_payloads() -> None:
        async with wanted_cache_lock:
            revision = await current_wanted_revision()
            persisted_revision = wanted_cache.get("persisted_revision") or ()
            if (
                wanted_cache["payload"] is not None
                and wanted_cache["compact_payload"] is not None
                and persisted_revision
            ):
                # A provider refresh rewrites timestamps even when its releases
                # did not change, so the raw table clocks almost never survive a
                # process restart. The snapshot is already tied to this database
                # inode, library scope and a bounded age. Adopt it for first paint
                # and reconcile it in the background on the first HTTP read.
                wanted_cache["revision"] = None
                wanted_cache["persisted_revision"] = ()
                wanted_cache["startup_ready"] = True
                wanted_cache["startup_snapshot"] = True
                return
            if (
                wanted_cache["revision"] == revision
                and wanted_cache["payload"] is not None
                and wanted_cache["compact_payload"] is not None
            ):
                wanted_cache["startup_ready"] = True
                wanted_cache["startup_snapshot"] = False
                return
            payload, compact_payload = await run_api_blocking(
                render_wanted_payloads, revision
            )
            # The starting revision deliberately stays attached to this
            # complete snapshot. A concurrent import then schedules one more
            # refresh instead of discarding useful work.
            wanted_cache["revision"] = revision
            wanted_cache["payload"] = payload
            wanted_cache["compact_payload"] = compact_payload
            wanted_cache["startup_ready"] = True
            wanted_cache["startup_snapshot"] = False

    def schedule_wanted_refresh() -> asyncio.Task[None]:
        current = wanted_cache.get("refresh_task")
        if current is not None and not current.done():
            return current

        async def refresh() -> None:
            try:
                await refresh_wanted_payloads()
            except Exception:  # noqa: BLE001 - the next request retries it
                logger.warning("Unable to refresh the Wanted response", exc_info=True)

        task = asyncio.create_task(refresh(), name="tankarr-wanted-cache-refresh")
        wanted_cache["refresh_task"] = task
        discovery_tasks.add(task)

        def completed(done: asyncio.Task[None]) -> None:
            discovery_tasks.discard(done)
            if wanted_cache.get("refresh_task") is done:
                wanted_cache["refresh_task"] = None

        task.add_done_callback(completed)
        return task

    async def cached_wanted_payload(*, compact: bool, fresh: bool) -> bytes:
        payload_key = "compact_payload" if compact else "payload"
        cached_payload = wanted_cache.get(payload_key)
        if cached_payload is not None and wanted_cache.get("startup_snapshot"):
            # First paint uses the saved snapshot. An explicit fresh request
            # joins the reconciliation below, so it cannot report a stale
            # empty Wanted list as the current state after a restart.
            schedule_wanted_refresh()
            if not fresh:
                return cached_payload
        if fresh:
            # A paint-first request may already have started a refresh for an
            # older revision. Wait until the cache reaches the revision seen
            # by this caller instead of merely waiting for that older task.
            for _attempt in range(3):
                requested_revision = await current_wanted_revision()
                if (
                    wanted_cache.get("revision") == requested_revision
                    and wanted_cache.get(payload_key) is not None
                ):
                    return wanted_cache[payload_key]
                await asyncio.shield(schedule_wanted_refresh())
            await refresh_wanted_payloads()
            return wanted_cache[payload_key]
        if cached_payload is not None and await run_revision(
            database.has_pending_download_jobs
        ):
            # Under sustained imports the revision necessarily changes every
            # few seconds. Paint first; the frontend's fresh request waits for
            # this coalesced rebuild without delaying the initial page.
            schedule_wanted_refresh()
            return cached_payload
        revision = await current_wanted_revision()
        if wanted_cache.get("revision") == revision and cached_payload is not None:
            return cached_payload
        if cached_payload is None:
            await refresh_wanted_payloads()
            return wanted_cache[payload_key]
        await refresh_wanted_payloads()
        return wanted_cache[payload_key]

    def enabled_search_language(raw: str | None) -> str:
        try:
            language = normalize_language_code(raw or settings.default_language)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if language not in settings.search_language_set:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"{language} is disabled. Enable it under Settings > "
                    "General > Search languages."
                ),
            )
        return language

    # A full alignment lists every book the reader holds (nineteen pages of
    # 500 for this library) and walks every tracked row: 1 700 GraphQL calls
    # in three hours when it ran each minute, and the event-loop stalls that
    # made the health probe time out. Imports and deletions still reconcile
    # at once through the outbox; the periodic audit only has to catch drift.
    ALIGNMENT_INTERVAL_SECONDS = 1800.0

    async def series_deletion_loop() -> None:
        while True:
            try:
                await service.process_series_deletions()
            except Exception:  # noqa: BLE001 - keep the durable worker alive
                logger.exception("Unable to process pending series cleanup")
            await asyncio.sleep(5)

    async def library_maintenance_loop() -> None:
        alignment_failures = 0
        next_alignment_at = 0.0
        while True:
            await asyncio.sleep(60)
            try:
                await service.retry_pending_komga_reconciliation()
            except Exception:  # noqa: BLE001 - the durable outbox remains retryable
                pass
            if service.komga_periodic_refresh_due():
                await service.refresh_komga_library(reason="periodic")
            elif monotonic() >= next_alignment_at:
                alignment = await service.reconcile_komga_library()
                if alignment.get("ready") or not alignment.get("configured", True):
                    alignment_failures = 0
                    next_alignment_at = monotonic() + ALIGNMENT_INTERVAL_SECONDS
                else:
                    # A failed alignment means Komga is still analyzing or
                    # unreachable. Retrying every minute only re-queues its
                    # work; back off exponentially (2 min → 30 min).
                    alignment_failures += 1
                    next_alignment_at = monotonic() + min(
                        60.0 * 2**alignment_failures, 1800.0
                    )
                if (
                    alignment.get("ready")
                    and metadata.komga is not None
                    and metadata.komga_sync_pending()
                ):
                    try:
                        alignment["metadata_sync"] = await metadata.sync_all_to_komga()
                    except Exception as exc:  # noqa: BLE001 - retry next run
                        alignment["metadata_sync_error"] = (
                            f"{type(exc).__name__}: {exc}"[:1000]
                        )
                    service.last_komga_library_alignment = alignment

    async def author_maintenance_loop() -> None:
        """Refresh one persisted MangaBaka staff page at a time."""

        next_credit_sync_at = 0.0
        await asyncio.sleep(10)
        while True:
            if monotonic() >= next_credit_sync_at:
                try:
                    await run_api_blocking(authors.sync_library)
                except Exception:  # noqa: BLE001 - retry without blocking the API
                    logger.warning(
                        "Unable to synchronize MangaBaka authors", exc_info=True
                    )
                next_credit_sync_at = monotonic() + 3600
            try:
                due = await run_api_blocking(
                    database.list_authors_due,
                    datetime.now(UTC).isoformat(),
                    limit=1,
                )
                if due and str(due[0]["id"]) not in author_refresh_pending:
                    await authors.refresh(str(due[0]["id"]))
            except Exception:  # noqa: BLE001 - per-author backoff is persisted
                logger.warning(
                    "Periodic MangaBaka author refresh failed", exc_info=True
                )
            await asyncio.sleep(30)

    async def initialization_loop() -> None:
        """Recover and organize in the background until mutation safety is proven.

        Every step is guarded: an unhandled error here used to kill the task
        silently, and with it the worker that starts at the end - the app
        stayed up, answered every request, and never downloaded anything,
        reporting only "worker has not completed safe startup".
        """

        while True:
            try:
                try:
                    recovery = await service.recover_file_quarantines()
                except Exception as exc:  # noqa: BLE001 - expose and retry startup state
                    recovery = {
                        "library_available": False,
                        "warnings": [
                            f"Startup deletion recovery failed: {type(exc).__name__}: {exc}"
                        ],
                        "recovery_blocked": True,
                        "initializing": True,
                    }
                    service.last_deletion_recovery = recovery
                if not recovery["recovery_blocked"]:
                    # Repair release units before deriving destinations.  A
                    # historical chapter classified as a book can otherwise
                    # collide with another source at startup and prevent both
                    # the worker and imports from ever starting.
                    await service.reconcile_persisted_explicit_chapters()
                    reader_gates_startup = getattr(
                        service.komga, "gates_readiness", True
                    )
                    organization = await service.organize_library(
                        reconcile_reader=reader_gates_startup
                    )
                    if not organization["organization_blocked"]:
                        metadata.last_title_migration = (
                            await metadata.apply_cached_titles()
                        )
                        # The second pass exists to move files under titles
                        # the migration just changed. With nothing changed
                        # it only repeats a full library walk at startup.
                        if int(metadata.last_title_migration.get("updated") or 0):
                            organization = await service.organize_library(
                                reconcile_reader=reader_gates_startup
                            )
                    if not organization["organization_blocked"]:
                        alignment = service.last_komga_library_alignment
                        if reader_gates_startup:
                            alignment = await service.reconcile_komga_library()
                        if not reader_gates_startup or alignment["ready"]:
                            # Reader metadata is cache warming, not a mutation
                            # safety gate.  The maintenance loop owns that
                            # potentially long synchronization after startup;
                            # waiting here used to leave the API unhealthy and
                            # could overlap a second full sync after 60s.
                            service.last_komga_library_alignment = alignment
                            database.prune_duplicate_queued_jobs()
                            reranked = 0
                            for manga_id in database.list_queued_manga_ids():
                                reranked += await service.rerank_queued_jobs(manga_id)
                            if reranked:
                                logger.info(
                                    "Re-ranked %d queued downloads to current sources",
                                    reranked,
                                )
                            # Warm Wanted before readiness opens. Its cold build is
                            # CPU-bound and used to land on the first browser request,
                            # making an otherwise ready application appear frozen.
                            # The worker starts immediately afterwards, so acquisition
                            # still cannot race the snapshot being published here.
                            await refresh_wanted_payloads()
                            await worker.start()
                            await translations.start()
                            await monitor.start()
                            await metadata.start()
                            await torrents.start()
                            await importer.resume_pending()
                            try:
                                await run_api_blocking(authors.sync_library)
                            except Exception:  # noqa: BLE001 - maintenance retries it
                                logger.warning(
                                    "Unable to build the MangaBaka author index",
                                    exc_info=True,
                                )
                            try:
                                await cached_library_payload()
                            except Exception:  # noqa: BLE001 - first request can rebuild it
                                logger.warning(
                                    "Unable to prebuild the Library response",
                                    exc_info=True,
                                )
                            return
            except Exception:  # noqa: BLE001 - never let startup die in silence
                logger.exception("Startup initialization failed; retrying in 30s")
            await asyncio.sleep(30)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        asyncio.get_running_loop().set_default_executor(blocking_executor)
        # Make the API available immediately, but fail readiness and every file
        # mutation closed until crash recovery and filesystem organization have
        # completed. Reader applications remain outside this lifecycle.
        service.last_deletion_recovery = {
            "library_available": False,
            "warnings": ["Startup deletion recovery is in progress"],
            "recovery_blocked": True,
            "initializing": True,
        }
        service.last_library_organization = service._organization_report(
            warnings=["Startup library organization is in progress"],
            organization_blocked=True,
            deferred=True,
            initializing=True,
        )
        native_reader_ready = isinstance(library_reader, NativeReaderLibrary)
        service.last_komga_library_alignment = {
            "configured": library_reader.configured,
            "ready": native_reader_ready,
            "triggered": False,
            "initializing": not native_reader_ready,
            **({"reader_independent": True} if native_reader_ready else {}),
        }
        lifecycle_tasks: list[asyncio.Task] = []
        if settings.restored_safe_mode:
            service.last_deletion_recovery["warnings"] = [
                "Restored safe mode: no background automation is running. Verify storage and configuration before restarting with TANKARR_RESTORED_SAFE_MODE=false."
            ]
        else:
            initialization_task = asyncio.create_task(
                initialization_loop(), name="tankarr-initialization"
            )
            lifecycle_tasks.extend(
                [
                    initialization_task,
                    asyncio.create_task(library_maintenance_loop()),
                    asyncio.create_task(
                        series_deletion_loop(), name="tankarr-series-cleanup"
                    ),
                    asyncio.create_task(
                        author_maintenance_loop(), name="tankarr-author-maintenance"
                    ),
                    asyncio.create_task(
                        suwayomi_maintenance_loop(), name="tankarr-suwayomi-maintenance"
                    ),
                    asyncio.create_task(updates.run(), name="tankarr-update-check"),
                    asyncio.create_task(
                        maintenance.run(), name="tankarr-nightly-maintenance"
                    ),
                ]
            )
            if settings.suwayomi_managed and suwayomi_runtime.installed():
                lifecycle_tasks.append(
                    asyncio.create_task(
                        start_managed_suwayomi(), name="tankarr-suwayomi-start"
                    )
                )
            # Small libraries retain their zero-race startup path.
            await asyncio.wait({initialization_task}, timeout=0.25)
        yield
        lifecycle_tasks.extend(discovery_tasks)
        for task in lifecycle_tasks:
            task.cancel()
        for task in lifecycle_tasks:
            with suppress(asyncio.CancelledError):
                await task
        await monitor.stop()
        await translations.stop()
        await torrents.stop()
        await metadata.stop()
        await importer.stop()
        await worker.stop()
        await suwayomi_runtime.aclose()
        closed: set[int] = set()
        for registered in [*providers.values(), *retired_providers]:
            if id(registered) in closed:
                continue
            closed.add(id(registered))
            await registered.aclose()
        api_executor.shutdown(wait=True, cancel_futures=True)
        revision_executor.shutdown(wait=True, cancel_futures=True)
        blocking_executor.shutdown(wait=True, cancel_futures=True)

    app = FastAPI(
        title="Tankarr",
        version=__version__,
        description="Headless comics monitor, downloader, and library organizer",
        lifespan=lifespan,
    )
    app.add_middleware(RestoreSafetyMiddleware, settings=settings)
    app.add_middleware(AuthenticationMiddleware, manager=authentication)
    app.add_middleware(CrossOriginProtectionMiddleware)
    app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=4)
    app.add_middleware(SecurityHeadersMiddleware)
    if settings.url_base:
        # Outermost: every other middleware and route sees the base as the
        # ASGI root_path and keeps working with root-relative paths.
        app.add_middleware(UrlBaseMiddleware, url_base=settings.url_base)
    app.state.settings = settings
    app.state.database = database
    app.state.provider = provider
    app.state.providers = providers
    app.state.service = service
    app.state.release_sources = release_sources
    app.state.worker = worker
    app.state.maintenance = maintenance
    app.state.monitor = monitor
    app.state.translations = translations
    app.state.importer = importer
    app.state.metadata = metadata
    app.state.artwork_thumbnails = artwork_thumbnails
    app.state.prowlarr = prowlarr
    app.state.qbittorrent = qbittorrent
    app.state.suwayomi_runtime = suwayomi_runtime
    app.state.komga_links = komga_links
    app.state.torrents = torrents
    app.state.authentication = authentication
    app.state.api_executor = api_executor
    app.state.revision_executor = revision_executor

    @app.get("/api/auth/status")
    async def authentication_status(request: Request):
        return authentication.status(request.scope)

    @app.post("/api/auth/login")
    async def login(request: Request, credentials: LoginRequest):
        if authentication.method != "forms":
            raise HTTPException(
                status_code=409,
                detail="Forms authentication is not enabled",
            )
        client = client_address(request.scope)
        wait = authentication.throttle.retry_after(client)
        if wait > 0:
            return too_many_attempts(wait)
        if not authentication.credentials_valid(
            credentials.username, credentials.password
        ):
            authentication.throttle.failed(client)
            logger.warning(
                "Sign-in failed for user %r from %s",
                credentials.username[:64],
                client,
            )
            raise HTTPException(status_code=401, detail="Invalid username or password")
        authentication.throttle.succeeded(client)
        logger.info("User %r signed in from %s", credentials.username[:64], client)
        response = JSONResponse(
            {
                "authenticated": True,
                "method": authentication.method,
                "username": settings.auth_username,
            },
            headers={"Cache-Control": "no-store"},
        )
        forwarded_scheme = request.headers.get("x-forwarded-proto", "").split(",")[0]
        cookie_options: dict[str, object] = {
            "key": SESSION_COOKIE,
            "value": authentication.create_session(remember=credentials.remember_me),
            "httponly": True,
            "secure": (
                request.url.scheme == "https"
                or forwarded_scheme.strip().casefold() == "https"
            ),
            "samesite": "lax",
            "path": settings.url_base or "/",
        }
        if credentials.remember_me:
            cookie_options["max_age"] = 30 * 24 * 60 * 60
        response.set_cookie(**cookie_options)
        return response

    @app.post("/api/auth/logout")
    async def logout(request: Request):
        await run_api_blocking(
            authentication.revoke_session, request.cookies.get(SESSION_COOKIE)
        )
        response = JSONResponse(
            {"authenticated": False}, headers={"Cache-Control": "no-store"}
        )
        response.delete_cookie(
            SESSION_COOKIE, path=settings.url_base or "/", samesite="lax"
        )
        return response

    @app.get("/api/auth/api-key")
    async def read_api_key():
        key = await run_api_blocking(authentication.api_key)
        return JSONResponse({"api_key": key}, headers={"Cache-Control": "no-store"})

    @app.post("/api/auth/api-key/regenerate")
    async def regenerate_api_key(request: Request):
        key = await run_api_blocking(authentication.regenerate_api_key)
        logger.info("API key regenerated from %s", client_address(request.scope))
        return JSONResponse({"api_key": key}, headers={"Cache-Control": "no-store"})

    def reader_alignment_status() -> dict[str, Any]:
        kind = effective_reader_kind(settings)
        return {
            **(service.last_komga_library_alignment or {}),
            "reader": kind,
            "reader_label": READER_LABELS.get(kind, "Reader"),
        }

    def render_health() -> dict[str, Any]:
        return {
            "status": "ok",
            "restored_safe_mode": settings.restored_safe_mode,
            "version": __version__,
            "provider": provider.name,
            "providers": [
                {
                    "name": item.name,
                    "label": item.label,
                    "search_mode": item.search_mode,
                }
                for item in providers.values()
            ],
            "default_language": settings.default_language,
            "search_languages": list(settings.search_language_codes),
            "library_dir": str(settings.library_dir),
            "auth_configured": settings.auth_configured,
            "auth_method": settings.auth_method,
            "library_alignment": reader_alignment_status(),
            "komga_refresh": service.komga_refresh_status(),
            "ntfy_configured": service.notifier.configured,
            "notifications": service.notifier.configured_channels(),
            "library": service.library_status(),
            "deletion_recovery": service.last_deletion_recovery,
            "library_organization": service.last_library_organization,
            "download_worker": worker.status(),
            "download_tuning": (
                provider.page_concurrency.status()
                if hasattr(provider, "page_concurrency")
                else None
            ),
            "monitor": monitor.status(),
            "metadata": metadata.status(),
            "torrents": torrents.status(),
        }

    @app.get("/api/health")
    async def health():
        return {"status": "ok", "auth_configured": settings.auth_configured}

    @app.get("/api/system/health")
    async def system_health():
        return await run_api_blocking(render_health)

    @app.get("/api/ready")
    async def ready():
        try:
            await system_ready()
        except HTTPException:
            return JSONResponse({"status": "not_ready"}, status_code=503)
        return {"status": "ready"}

    @app.get("/api/system/ready")
    async def system_ready():
        library = await run_api_blocking(service.library_status)
        if not library["available"]:
            raise HTTPException(status_code=503, detail=library["reason"])
        recovery = service.last_deletion_recovery
        if recovery is None:
            raise HTTPException(
                status_code=503, detail="Deletion recovery has not completed"
            )
        if recovery is not None and recovery.get("recovery_blocked"):
            raise HTTPException(
                status_code=503,
                detail={
                    "message": "Deletion recovery is incomplete",
                    "warnings": recovery["warnings"],
                },
            )
        organization = service.last_library_organization
        if organization is None:
            raise HTTPException(
                status_code=503, detail="Library organization has not completed"
            )
        if organization.get("organization_blocked"):
            raise HTTPException(
                status_code=503,
                detail={
                    "message": "Library organization is incomplete",
                    "warnings": organization["warnings"],
                },
            )
        alignment = service.last_komga_library_alignment
        if getattr(service.komga, "gates_readiness", True) and (
            alignment is None or not alignment.get("ready")
        ):
            raise HTTPException(
                status_code=503,
                detail={
                    "message": "Library filesystem audit is incomplete",
                    "alignment": alignment,
                },
            )
        if worker.task is None or worker.task.done():
            raise HTTPException(
                status_code=503,
                detail="Download worker has not completed safe startup",
            )
        if not wanted_cache["startup_ready"]:
            raise HTTPException(
                status_code=503,
                detail="Wanted cache has not completed startup warming",
            )
        return {
            "status": "ready",
            "library": library,
            "recovery": recovery,
            "organization": organization,
            "library_alignment": alignment,
        }

    def catalogue_source():
        for source in metadata.sources:
            if getattr(source, "name", "") == CATALOGUE_SOURCE:
                return source
        raise HTTPException(
            status_code=503, detail="The MangaBaka catalogue is not available"
        )

    async def catalogue_record(identifier: str) -> dict[str, Any]:
        external_id = parse_catalogue_id(identifier)
        if external_id is None:
            raise HTTPException(status_code=404, detail="Unknown catalogue id")
        try:
            record = await catalogue_source().get_series(external_id)
        except Exception as exc:  # noqa: BLE001 - upstream failure is the answer
            raise HTTPException(
                status_code=502, detail=f"MangaBaka lookup failed: {exc}"
            ) from exc
        if not record.get("external_id"):
            raise HTTPException(status_code=404, detail="Catalogue record not found")
        return record

    @app.get("/api/search")
    async def search(
        q: str = Query(min_length=2, max_length=2_000),
        language: str | None = Query(default=None, min_length=2, max_length=16),
        provider_name: str = Query(default="all", alias="provider", max_length=32),
    ):
        language = enabled_search_language(language)
        parsed_query = urlsplit(q.strip())
        query_mode = (
            "url"
            if parsed_query.scheme.casefold() in {"http", "https"}
            and bool(parsed_query.hostname)
            else "title"
        )
        pasted_catalogue_id = parse_catalogue_id(q) if query_mode == "url" else None
        if query_mode == "url" and not pasted_catalogue_id:
            raise HTTPException(
                status_code=400,
                detail="Paste a MangaBaka series URL, or search by title",
            )
        # Identity-first: Add New searches the catalogue, never a download
        # site. Sources are mapped after the work is added.
        try:
            if pasted_catalogue_id:
                records = [await catalogue_source().get_series(pasted_catalogue_id)]
            else:
                records = await catalogue_source().search_series(q.strip(), limit=25)
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001 - surface upstream state
            raise HTTPException(
                status_code=502, detail=f"MangaBaka search failed: {exc}"
            ) from exc
        cards = [
            catalogue_card(record) for record in records if record.get("external_id")
        ]
        # A work already in the library must not be offered again: Add would
        # either duplicate it or fail on the identity conflict.
        in_library = database.catalogue_identity_owners()
        for card in cards:
            existing = in_library.get(str(card.get("external_id") or ""))
            card["in_library"] = existing is not None
            card["library_manga_id"] = existing
        return {
            "results": cards,
            "works": cards,
            "errors": [],
            "releases": [],
            "release_matches": {},
        }

    @app.get("/api/authors")
    async def list_authors():
        records = await run_api_blocking(database.list_author_identities)
        return {
            "authors": [
                {
                    "id": record["id"],
                    "name": record["display_name"],
                    "aliases": [alias["name"] for alias in record["aliases"]],
                    "work_count": record["work_count"],
                    "library_manga_count": record["library_manga_count"],
                    "last_refreshed_at": record["last_refreshed_at"],
                    "last_refresh_error": record["last_refresh_error"],
                }
                for record in records
            ],
            "potential_duplicates": await run_api_blocking(authors.audit_duplicates),
        }

    @app.get("/api/authors/{author_id}")
    async def author_page(author_id: str):
        page = await run_api_blocking(authors.page, author_id)
        if page is None:
            raise HTTPException(status_code=404, detail="Author not found")
        # Works already in the library carry their library state (status,
        # counts, unit, language) so the author page answers "what do I
        # have of this author and is it complete" without opening each one.
        library_ids = {
            str(work.get("library_manga_id") or "")
            for work in page.get("works") or []
            if work.get("library_manga_id")
        }
        if library_ids:
            try:
                library_items = {
                    str(item["id"]): item
                    for item in json.loads(await cached_library_payload())
                }
            except Exception:  # noqa: BLE001 - the page stands without it
                library_items = {}
            for work in page.get("works") or []:
                item = library_items.get(str(work.get("library_manga_id") or ""))
                if not item:
                    continue
                work["library"] = {
                    key: item.get(key)
                    for key in (
                        "publication",
                        "library_count",
                        "library_status_override",
                        "status_override",
                        "preferred_language",
                        "monitor_mode",
                        "effective_series_unit",
                        "chapter_count",
                        "downloaded_count",
                    )
                }
        next_refresh_at = str(page.get("next_refresh_at") or "")
        if page["last_refreshed_at"] is None and (
            not next_refresh_at or next_refresh_at <= datetime.now(UTC).isoformat()
        ):
            schedule_author_refresh([str(page["id"])])
        return page

    @app.post("/api/authors/{author_id}/refresh")
    async def refresh_author_page(author_id: str):
        try:
            return await authors.refresh(author_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Author not found") from exc
        except Exception as exc:  # noqa: BLE001 - surface the provider state
            raise HTTPException(
                status_code=502, detail=f"MangaBaka author refresh failed: {exc}"
            ) from exc

    @app.get("/api/manga")
    async def list_manga(request: Request, fresh: bool = False, cached: bool = False):
        if cached and not fresh and library_cache["payload"] is not None:
            # The UI follows its initial paint with an exact freshness read.
            # Source refreshes invalidate counts even without active downloads.
            payload = library_cache["payload"]
            schedule_library_refresh()
        else:
            payload = (
                await refresh_library_payload()
                if fresh
                else await cached_library_payload()
            )
        etag = snapshot_validators.etag("library", payload)
        headers = {"ETag": etag, "Cache-Control": "private, no-cache"}
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers=headers)
        return Response(
            content=payload,
            media_type="application/json",
            headers=headers,
        )

    @app.get("/api/manga/{manga_id}/preview")
    async def preview_manga(
        manga_id: str,
        language: str | None = Query(default=None, min_length=2, max_length=16),
        provider_name: str = Query(
            default="catalogue", alias="provider", max_length=32
        ),
    ):
        language = enabled_search_language(language)
        if provider_name == CATALOGUE_PROVIDER or manga_id.startswith("mb:"):
            record = await catalogue_record(manga_id)
            return service.preview_catalogue(record, language)
        try:
            return await service.preview_manga(
                manga_id,
                language,
                provider_name,
                metadata.preview_chapter_completeness,
            )
        except KeyError as exc:
            raise HTTPException(
                status_code=404, detail=f"Unknown provider: {provider_name}"
            ) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=502, detail=f"Unable to preview manga: {exc}"
            ) from exc

    @app.post("/api/manga", status_code=201)
    async def add_manga(request: AddMangaRequest):
        language = enabled_search_language(request.language)
        if request.provider == CATALOGUE_PROVIDER or request.manga_id.startswith("mb:"):
            record = await catalogue_record(request.manga_id)
            try:
                manga = await service.add_catalogue_series(
                    record,
                    language,
                    request.monitor_mode,
                    series_unit=request.series_unit,
                )
            except FutureMonitoringUnavailable as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except RecoveryBlocked as exc:
                raise HTTPException(status_code=503, detail=str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            manga_id = str(manga["id"])
            catalogue_id = str(record["external_id"])
            author_ids = authors.register_catalogue_series(manga_id, record)
            schedule_author_refresh(author_ids)

            async def pin_identity() -> Any:
                # Pin the identity so enrichment chains MU/AniList by ID and
                # builds the chapter map before any source is searched. This
                # is the slow part of adding a work, so it runs after the
                # response: the series appears at once and fills in.
                return await metadata.update_manual_correlations(
                    manga_id, {CATALOGUE_SOURCE: catalogue_id}
                )

            schedule_release_source_discovery(
                manga_id,
                monitor_new=request.monitor_mode in BACKLOG_MONITOR_MODES,
                queue_backlog=request.search_now
                and request.monitor_mode in BACKLOG_MONITOR_MODES,
                before=pin_identity,
            )
            result = decorate_manga(database.get_manga(manga_id))
            result["queued"] = 0
            return result
        try:
            manga = await service.add_manga(
                request.manga_id,
                language,
                request.monitor_mode,
                request.provider,
            )
            queued = 0
            if request.monitor_mode in BACKLOG_MONITOR_MODES:
                queued = await monitor.queue_missing(request.manga_id)
            if str(manga.get("provider") or "") != "local":
                schedule_release_source_discovery(
                    request.manga_id,
                    monitor_new=request.monitor_mode in BACKLOG_MONITOR_MODES,
                    queue_backlog=request.monitor_mode in BACKLOG_MONITOR_MODES,
                )
            manga = decorate_manga(database.get_manga(request.manga_id))
            manga["queued"] = queued
            return manga
        except FutureMonitoringUnavailable as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except RecoveryBlocked as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=502, detail=f"Unable to add manga: {exc}"
            ) from exc

    # Upstream cover fetches pay a rate limiter and a TLS handshake; a small
    # in-memory LRU keeps the library grid's first paint fast.
    cover_cache: OrderedDict[tuple[str, str, str], tuple[bytes, str]] = OrderedDict()
    cover_cache_limit = 256

    async def _proxy_cover(provider_name: str, manga_id: str, filename: str):
        selected = providers.get(provider_name)
        if selected is None:
            raise HTTPException(status_code=404, detail="Unknown provider")
        key = (provider_name, manga_id, filename)
        cached = cover_cache.get(key)
        if cached is not None:
            cover_cache.move_to_end(key)
            content, media_type = cached
        else:
            try:
                content, media_type = await selected.get_cover(manga_id, filename)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            except Exception as exc:
                raise HTTPException(
                    status_code=502, detail="Unable to load cover"
                ) from exc
            cover_cache[key] = (content, media_type)
            if len(cover_cache) > cover_cache_limit:
                cover_cache.popitem(last=False)
        return Response(
            content=content,
            media_type=media_type,
            headers={"Cache-Control": "public, max-age=86400, immutable"},
        )

    @app.get(
        "/api/covers/{provider_name}/{manga_id}/{filename}", include_in_schema=False
    )
    async def manga_cover(provider_name: str, manga_id: str, filename: str):
        return await _proxy_cover(provider_name, manga_id, filename)

    @app.get("/api/manga/{manga_id}")
    async def get_manga(
        manga_id: str,
        request: Request,
        compact: bool = Query(default=False),
        fresh: bool = Query(default=False),
    ):
        try:
            revision = await current_series_revision(manga_id)
            current_etag = series_etag((*revision, f"compact={compact}"))
            requested_etag = request.headers.get("if-none-match")
            if requested_etag == current_etag:
                return Response(
                    status_code=304,
                    headers={
                        "ETag": current_etag,
                        "Cache-Control": "private, no-cache",
                    },
                )
            payload, payload_revision = await cached_series_payload(
                manga_id, revision, compact, fresh=fresh
            )
            etag = series_etag((*payload_revision, f"compact={compact}"))
            if requested_etag == etag:
                return Response(
                    status_code=304,
                    headers={"ETag": etag, "Cache-Control": "private, no-cache"},
                )
            return Response(
                content=payload,
                media_type="application/json",
                headers={"ETag": etag, "Cache-Control": "private, no-cache"},
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Manga not found") from exc

    @app.get("/api/manga/{manga_id}/reader-link")
    async def get_manga_reader_link(manga_id: str):
        """Resolve the optional reader shortcut for one series."""

        try:
            return await resolve_reader_link(settings, database, komga_links, manga_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Manga not found") from exc
        except Exception as exc:  # noqa: BLE001 - reader failures are not ours
            raise HTTPException(
                status_code=502, detail=f"Unable to resolve the reader series: {exc}"
            ) from exc

    @app.get("/api/metadata/status")
    async def metadata_status():
        return metadata.status()

    @app.get("/api/manga/{manga_id}/metadata")
    async def get_manga_metadata(manga_id: str):
        try:
            return metadata.metadata_for(manga_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Manga not found") from exc

    @app.post("/api/manga/{manga_id}/metadata/refresh")
    async def refresh_manga_metadata(manga_id: str):
        try:
            await service.refresh_metadata(manga_id)
            return await metadata.enrich_series(manga_id, force=True)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Manga not found") from exc
        except Exception as exc:
            raise HTTPException(
                status_code=502, detail=f"Metadata refresh failed: {exc}"
            ) from exc

    @app.get("/api/manga/{manga_id}/metadata/correlations")
    async def get_metadata_correlations(manga_id: str):
        try:
            return metadata.correlations_for(manga_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Manga not found") from exc

    @app.put("/api/manga/{manga_id}/metadata/correlations")
    async def update_metadata_correlations(
        manga_id: str, request: MetadataCorrelationsRequest
    ):
        try:
            return await metadata.update_manual_correlations(
                manga_id, request.correlations
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Manga not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail=f"Metadata correlation update failed ({type(exc).__name__})",
            ) from exc

    @app.get("/api/manga/{manga_id}/artwork-candidates")
    async def get_artwork_candidates(manga_id: str):
        try:
            return metadata.artwork_candidates_for(manga_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Manga not found") from exc

    @app.put("/api/manga/{manga_id}/artwork-selection")
    async def update_artwork_selection(manga_id: str, request: ArtworkSelectionRequest):
        try:
            return await metadata.select_series_artwork(manga_id, request.candidate_id)
        except KeyError as exc:
            raise HTTPException(
                status_code=404, detail="Manga or artwork candidate not found"
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail=f"Artwork selection failed ({type(exc).__name__})",
            ) from exc

    @app.post("/api/manga/{manga_id}/artwork-upload")
    async def upload_artwork(manga_id: str, request: Request):
        content_type = (
            request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        )
        if content_type and not (
            content_type.startswith("image/")
            or content_type == "application/octet-stream"
        ):
            raise HTTPException(
                status_code=415,
                detail="Upload a JPEG, PNG, WebP, GIF, or AVIF image",
            )
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > MAX_ARTWORK_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail="Cover image exceeds the 20 MiB safety limit",
                    )
            except ValueError:
                pass
        content = bytearray()
        async for chunk in request.stream():
            if len(content) + len(chunk) > MAX_ARTWORK_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail="Cover image exceeds the 20 MiB safety limit",
                )
            content.extend(chunk)
        try:
            return await metadata.upload_series_artwork(manga_id, bytes(content))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Manga not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail=f"Artwork upload failed ({type(exc).__name__})",
            ) from exc

    @app.get(
        "/api/manga/{manga_id}/artwork-candidates/{candidate_id}",
        include_in_schema=False,
    )
    async def artwork_candidate(
        manga_id: str,
        candidate_id: str,
        v: str | None = Query(default=None, max_length=128),
    ):
        try:
            path, media_type, digest = metadata.artwork_candidate_response(
                manga_id, candidate_id
            )
            return FileResponse(
                path,
                media_type=media_type,
                headers=artwork_cache_headers(v, digest),
            )
        except (KeyError, FileNotFoundError) as exc:
            raise HTTPException(status_code=404, detail="Artwork not found") from exc

    @app.post("/api/library/adopt-catalogue")
    async def adopt_catalogue():
        """Promote identified local-import series to catalogue works and map
        their download sources."""

        result = service.adopt_catalogue_identities()
        for manga_id in result["adopted"]:
            schedule_release_source_discovery(
                manga_id, monitor_new=False, queue_backlog=False
            )
        return {"adopted": len(result["adopted"]), "skipped": result["skipped"]}

    @app.post("/api/metadata/refresh", status_code=202)
    async def refresh_all_metadata(force: bool = Query(default=True)):
        return metadata.start_bulk_refresh(force=force)

    @app.get("/api/metadata/artwork/{manga_id}/series", include_in_schema=False)
    async def series_metadata_artwork(
        manga_id: str,
        v: str | None = Query(default=None, max_length=128),
    ):
        try:
            path, media_type, digest = metadata.artwork_response(manga_id)
            return FileResponse(
                path,
                media_type=media_type,
                headers=artwork_cache_headers(v, digest),
            )
        except (KeyError, FileNotFoundError) as exc:
            raise HTTPException(status_code=404, detail="Artwork not found") from exc

    async def metadata_artwork_thumbnail(
        manga_id: str,
        width: int,
        *,
        volume: str | None,
        requested_version: str | None,
    ) -> FileResponse:
        if width not in SUPPORTED_ARTWORK_THUMBNAIL_WIDTHS:
            raise HTTPException(status_code=404, detail="Thumbnail size not found")
        try:
            source, _media_type, digest = metadata.artwork_response(manga_id, volume)
            path, digest = await artwork_thumbnails.get(source, digest, width)
        except (KeyError, FileNotFoundError) as exc:
            raise HTTPException(status_code=404, detail="Artwork not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        headers = artwork_cache_headers(requested_version, digest)
        headers["ETag"] = (
            f'"sha256-{digest}-thumb-w{width}-v{ARTWORK_THUMBNAIL_VERSION}"'
        )
        return FileResponse(path, media_type="image/webp", headers=headers)

    @app.get(
        "/api/metadata/artwork/{manga_id}/series/thumbnail/{width}",
        include_in_schema=False,
    )
    async def series_metadata_artwork_thumbnail(
        manga_id: str,
        width: int,
        v: str | None = Query(default=None, max_length=128),
    ):
        return await metadata_artwork_thumbnail(
            manga_id, width, volume=None, requested_version=v
        )

    @app.get(
        "/api/metadata/artwork/{manga_id}/volumes/{volume}",
        include_in_schema=False,
    )
    async def volume_metadata_artwork(
        manga_id: str,
        volume: str,
        v: str | None = Query(default=None, max_length=128),
    ):
        try:
            path, media_type, digest = metadata.artwork_response(manga_id, volume)
            return FileResponse(
                path,
                media_type=media_type,
                headers=artwork_cache_headers(v, digest),
            )
        except (KeyError, FileNotFoundError) as exc:
            raise HTTPException(status_code=404, detail="Artwork not found") from exc

    @app.get(
        "/api/metadata/artwork/{manga_id}/volumes/{volume}/thumbnail/{width}",
        include_in_schema=False,
    )
    async def volume_metadata_artwork_thumbnail(
        manga_id: str,
        volume: str,
        width: int,
        v: str | None = Query(default=None, max_length=128),
    ):
        return await metadata_artwork_thumbnail(
            manga_id, width, volume=volume, requested_version=v
        )

    @app.get(
        "/api/manga/{manga_id}/delete-preview",
        response_model=MangaDeletionPreview,
    )
    async def preview_manga_deletion(manga_id: str):
        try:
            return await service.preview_manga_deletion(manga_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Manga not found") from exc
        except (LibraryUnavailable, RecoveryBlocked) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except (ActiveDownloadJobsError, UnsafeLibraryPath) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=500, detail=f"Unable to preview manga deletion: {exc}"
            ) from exc

    @app.delete("/api/manga/{manga_id}")
    async def delete_manga(
        manga_id: str,
        delete_files: bool = Query(default=False),
        background: bool | None = Query(default=None),
        confirmation_snapshot: str | None = Query(
            default=None, min_length=64, max_length=64, pattern="^[0-9a-f]{64}$"
        ),
    ):
        try:
            if background is True or (
                background is None and confirmation_snapshot is None
            ):
                return await service.enqueue_manga_deletion(
                    manga_id, delete_files=delete_files
                )
            return await service.delete_manga(
                manga_id,
                delete_files=delete_files,
                confirmation_snapshot=confirmation_snapshot,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Manga not found") from exc
        except (LibraryUnavailable, RecoveryBlocked) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except (
            ActiveDownloadJobsError,
            StaleMangaDeletionError,
            UnsafeLibraryPath,
        ) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=500, detail=f"Unable to delete manga: {exc}"
            ) from exc

    @app.delete("/api/manga/{manga_id}/chapters/{chapter_id}/file")
    async def delete_chapter_file(manga_id: str, chapter_id: str):
        try:
            return await service.delete_chapter_file(manga_id, chapter_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Chapter not found") from exc
        except (LibraryUnavailable, RecoveryBlocked) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except (ActiveDownloadJobsError, UnsafeLibraryPath) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=500, detail=f"Unable to delete chapter file: {exc}"
            ) from exc

    @app.delete("/api/manga/{manga_id}/chapters/files")
    async def delete_chapter_files_bulk(
        manga_id: str, chapter_id: list[str] = Query(default=[])
    ):
        """Delete several chapter files of one series in one operation: one
        safety scan and one reader reconciliation instead of one per file."""

        if not chapter_id:
            raise HTTPException(status_code=422, detail="chapter_id is required")
        try:
            return await service.delete_chapter_files(manga_id, list(chapter_id))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Series not found") from exc
        except (LibraryUnavailable, RecoveryBlocked) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except (ActiveDownloadJobsError, UnsafeLibraryPath) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.delete("/api/manga/{manga_id}/volumes/{volume}/files")
    async def delete_volume_files(
        manga_id: str,
        volume: str,
        language: str | None = Query(default=None, min_length=2, max_length=16),
        expected_downloaded_count: int | None = Query(default=None, ge=0),
        expected_chapter_id: list[str] | None = Query(default=None),
    ):
        try:
            return await service.delete_volume_files(
                manga_id,
                volume,
                language,
                expected_downloaded_count,
                expected_chapter_id,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Volume not found") from exc
        except (LibraryUnavailable, RecoveryBlocked) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except (
            ActiveDownloadJobsError,
            StaleVolumeDeletionError,
            UnsafeLibraryPath,
        ) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=500, detail=f"Unable to delete volume files: {exc}"
            ) from exc

    @app.get("/api/manga/{manga_id}/calendar")
    async def series_calendar(manga_id: str):
        """Recent dated releases and the estimated next chapters of one work."""

        try:
            return await run_api_blocking(service.series_calendar, manga_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Series not found") from exc

    @app.get("/api/manga/{manga_id}/units")
    async def series_units(manga_id: str):
        try:
            return await run_api_blocking(
                load_series_units,
                database,
                manga_id,
                str(getattr(settings, "preferred_unit", "volumes") or "volumes"),
                sync=True,
                specials_outside_books=bool(
                    getattr(settings, "special_chapters_outside_books", False)
                ),
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Series not found") from exc

    @app.get("/api/manga/{manga_id}/duplicates")
    async def list_duplicate_files(
        manga_id: str, volume: str | None = Query(default=None, max_length=64)
    ):
        try:
            chapters = await run_api_blocking(service.duplicate_chapter_files, manga_id)
            if volume is not None:
                chapters = [
                    chapter
                    for chapter in chapters
                    if canonical_number(chapter["duplicate_of_volume"])
                    == canonical_number(volume)
                ]
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Series not found") from exc
        return {"manga_id": manga_id, "chapters": chapters, "count": len(chapters)}

    @app.delete("/api/manga/{manga_id}/duplicates/files")
    async def delete_duplicate_files(
        manga_id: str,
        expected_chapter_id: list[str] | None = Query(default=None),
        volume: str | None = Query(default=None, max_length=64),
        recycle: bool = False,
    ):
        try:
            if (volume is not None or recycle) and expected_chapter_id is None:
                raise StaleVolumeDeletionError(
                    "Preview the duplicate files and confirm their IDs before retirement."
                )
            return await service.delete_duplicate_chapter_files(
                manga_id, expected_chapter_id, volume=volume, recycle=recycle
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Series not found") from exc
        except (LibraryUnavailable, RecoveryBlocked) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except (
            ActiveDownloadJobsError,
            StaleVolumeDeletionError,
            UnsafeLibraryPath,
        ) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=500, detail=f"Unable to delete duplicate files: {exc}"
            ) from exc

    @app.patch("/api/manga/{manga_id}")
    async def update_manga(manga_id: str, request: UpdateMangaRequest):
        try:
            service.assert_mutations_allowed()
            updates = request.model_dump(exclude_none=True)
            queued = 0
            direct_updates = {
                key: updates[key]
                for key in (
                    "preferred_language",
                    "status_override",
                    "library_status_override",
                    "verified_chapter_count",
                    "verified_chapter_source",
                    "expected_count_override",
                    "edition_book_count",
                    "assemble_books_automatically",
                    "translation_enabled",
                    "translation_source_languages",
                    "reader_mode_override",
                    "reader_direction_override",
                )
                if key in updates
            }
            if "authors_override" in updates:
                chosen = updates["authors_override"]
                database.set_manga_authors_override(
                    manga_id, None if chosen == "automatic" else list(chosen)
                )
            if "series_unit" in updates:
                chosen = updates["series_unit"]
                direct_updates["series_unit_override"] = (
                    None if chosen == "automatic" else chosen
                )
            if "reader_mode" in updates:
                chosen = updates["reader_mode"]
                direct_updates["reader_mode_override"] = (
                    None if chosen == "automatic" else chosen
                )
            if "reader_direction" in updates:
                chosen = updates["reader_direction"]
                direct_updates["reader_direction_override"] = (
                    None if chosen == "automatic" else chosen
                )
            expected_override = direct_updates.get("expected_count_override")
            if expected_override is not None and expected_override != "automatic":
                current = decorate_manga(database.get_manga(manga_id))
                counts = current["library_count"]
                expected_count = int(expected_override)
                # Decimal extras and split files do not raise the minimum
                # number of main chapters in a manually corrected edition.
                minimum_count = (
                    current.get("logical_downloaded_count", counts["downloaded_count"])
                    if counts["unit"] == "chapter"
                    else counts["downloaded_count"]
                )
                if expected_count < int(minimum_count):
                    raise ValueError(
                        "Expected edition count cannot be lower than the number "
                        "of imported files"
                    )
                direct_updates["expected_count_unit_override"] = counts["unit"]
            if direct_updates:
                database.update_manga(manga_id, direct_updates)
            settle_books = (
                "series_unit" in updates and updates["series_unit"] == "volumes"
            )
            if "authors_override" in updates and settings.metadata_enabled:
                # Rebuild the canonical record now so the corrected credits
                # reach the series page and the reader's library at once.
                await metadata.enrich_series(manga_id, force=False)
            if "preferred_language" in updates:
                await monitor.refresh_one(manga_id)
            if "monitor_mode" in updates:
                configured = await monitor.configure_monitor_mode(
                    manga_id, updates["monitor_mode"]
                )
                queued += int(configured.pop("queued", 0))
            elif "preferred_language" in updates:
                manga = database.get_manga(manga_id)
                if manga["monitor_mode"] in BACKLOG_MONITOR_MODES:
                    queued += await monitor.queue_missing(manga_id)
            manga = database.get_manga(manga_id)
            manga["queued"] = queued
            if settle_books:
                try:
                    await service.retire_redundant_chapter_files(manga_id)
                except Exception as exc:  # noqa: BLE001 - the monitor pass retries
                    logger.info("Could not settle books of %s: %s", manga_id, exc)
            return decorate_manga(manga)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Manga not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except RecoveryBlocked as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=502, detail=f"Unable to update manga: {exc}"
            ) from exc

    @app.post("/api/manga/{manga_id}/rename")
    async def rename_manga(manga_id: str, request: RenameMangaRequest):
        try:
            result = await service.rename_manga(manga_id, request.title)
            if result.get("updated") and metadata.komga is not None:
                result["komga"] = await metadata.sync_to_komga(manga_id, force=True)
            return result
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Manga not found") from exc
        except (LibraryUnavailable, RecoveryBlocked) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except (
            FileExistsError,
            SeriesRenameError,
            UnsafeLibraryPath,
            ValueError,
        ) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail=f"Unable to rename manga: {type(exc).__name__}: {exc}",
            ) from exc

    @app.post("/api/manga/{manga_id}/search")
    async def search_missing(manga_id: str):
        """Refresh and correlate direct sources, then queue verified releases."""

        # A person asked: the Wanted pass looks at this series again next time
        # instead of waiting out the back-off ladder, and the indexers are
        # asked again now rather than in seven days (their last "nothing" or
        # "needs review" was given under rules that may since have changed).
        try:
            database.reset_wanted_search_state(manga_id)
            database.forget_indexer_answers(manga_id)
        except Exception:  # noqa: BLE001 - cadence is an optimisation
            pass

        try:
            service.assert_mutations_allowed()
            manga = database.get_manga(manga_id)
            origin = await service.refresh_manga(manga_id, manga["preferred_language"])
            discovered = await release_sources.discover_and_refresh(
                manga_id,
                monitor_new=manga["monitor_mode"] in BACKLOG_MONITOR_MODES,
            )
            # The whole ladder, as on add and in the Wanted pass: sources,
            # then the indexers by chapter and by book, then the archives.
            searched = await monitor.search_wanted_series(manga_id, trigger="manual")
            queued = int(searched.get("queued") or 0)
            origin_adapter = providers.get(str(manga.get("provider") or ""))
            origin_status = {
                "provider": str(manga.get("provider") or "local"),
                "label": str(
                    manga.get("source_name")
                    or getattr(origin_adapter, "label", None)
                    or manga.get("provider")
                    or "Local"
                ),
                "state": "matched",
                "provider_manga_id": manga_id,
                "title": manga["title"],
                "releases": int(origin.get("seen") or 0),
                "new": len(origin.get("new_chapter_ids") or []),
                "confidence": 1.0,
                "reason": "Origin download source",
            }
            return {
                "manga_id": manga_id,
                "queued": queued,
                "searched": int(searched.get("searched") or 0),
                "seen": int(origin.get("seen") or 0) + int(discovered["seen"]),
                "new": len(origin.get("new_chapter_ids") or [])
                + int(discovered["new"]),
                "sources": [origin_status, *discovered["sources"]],
                "errors": discovered["errors"],
            }
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Manga not found") from exc
        except RecoveryBlocked as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=502, detail=f"Missing release search failed: {exc}"
            ) from exc

    @app.delete(
        "/api/manga/{manga_id}/release-sources/{provider}/{provider_manga_id:path}"
    )
    async def remove_release_source(
        manga_id: str, provider: str, provider_manga_id: str
    ):
        try:
            return await service.remove_release_source(
                manga_id, provider, provider_manga_id
            )
        except KeyError as exc:
            raise HTTPException(
                status_code=404, detail="Release source not found"
            ) from exc

    @app.post("/api/manga/{manga_id}/release-sources", status_code=201)
    async def add_release_source(manga_id: str, request: AddReleaseSourceRequest):
        # The operator mapping it by hand outranks an earlier refusal.
        database.clear_release_source_rejection(
            manga_id, request.provider, request.provider_manga_id
        )
        """Manually map one provider series as a verified release source.

        Automatic discovery refuses ambiguous candidates by design (equal-title
        editions on one source, for example). This is the operator's explicit,
        audited override for exactly that case.
        """

        try:
            service.assert_mutations_allowed()
            manga = database.get_manga(manga_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Manga not found") from exc
        except RecoveryBlocked as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        if request.provider in {"local", str(manga.get("provider") or "")}:
            raise HTTPException(
                status_code=409,
                detail="This provider already anchors the series",
            )
        adapter = providers.get(request.provider)
        if adapter is None:
            raise HTTPException(
                status_code=404,
                detail=f"Unknown or disabled provider: {request.provider}",
            )
        language = str(manga["preferred_language"])
        try:
            detail = await adapter.get_manga(request.provider_manga_id)
            chapters = await adapter.list_chapters(request.provider_manga_id, language)
        except Exception as exc:  # noqa: BLE001 - surfaced as one gateway error
            raise HTTPException(
                status_code=502,
                detail=f"Unable to inspect the source series: {exc}",
            ) from exc
        mapping = database.upsert_release_source(
            manga_id,
            provider=request.provider,
            provider_manga_id=str(request.provider_manga_id),
            title=str(detail.get("title") or request.provider_manga_id),
            source_url=str(detail.get("source_url") or "") or None,
            source_name=str(detail.get("source_name") or "") or None,
            language=language,
            match_confidence=1.0,
            match_reason="Manual operator mapping",
            verified_by="manual",
        )
        monitor_new = manga["monitor_mode"] in BACKLOG_MONITOR_MODES
        inserted = database.upsert_chapters(manga_id, chapters, monitor_new=monitor_new)
        database.record_release_source_result(
            manga_id, request.provider, str(request.provider_manga_id)
        )
        queued = await monitor.queue_missing(manga_id) if monitor_new else 0
        return {
            "mapping": mapping,
            "releases": int(inserted["seen"]),
            "new": len(inserted["new_chapter_ids"]),
            "queued": queued,
        }

    @app.post("/api/manga/{manga_id}/chapters/search")
    async def search_chapter_releases(manga_id: str, request: ChapterSearchRequest):
        """Search one canonical chapter slot without grabbing any result."""

        if request.chapter is None and request.volume is None:
            raise HTTPException(
                status_code=400, detail="A chapter or volume number is required"
            )
        try:
            service.assert_mutations_allowed()
            manga = database.get_manga(manga_id)
            # Manual search reads the index as it is: the mapped sources were
            # refreshed by the monitor (or the toolbar Refresh). Re-querying
            # every source here took minutes on long works; only the torrent
            # indexers are searched live.
            direct = {
                "sources": [
                    {
                        "provider": str(mapping.get("provider") or ""),
                        "label": str(
                            mapping.get("source_name") or mapping.get("provider") or ""
                        ),
                        "state": "matched",
                        "provider_manga_id": str(
                            mapping.get("provider_manga_id") or ""
                        ),
                        "title": mapping.get("title"),
                        "confidence": mapping.get("match_confidence"),
                        "reason": mapping.get("match_reason"),
                        "error": mapping.get("last_error"),
                    }
                    for mapping in database.list_release_sources(manga_id)
                ]
            }
            origin_status = {
                "provider": str(manga.get("provider") or "local"),
                "label": str(
                    manga.get("source_name") or manga.get("provider") or "Local"
                ),
                "state": "matched",
                "provider_manga_id": manga_id,
                "title": manga["title"],
                "confidence": 1.0,
                "reason": "Indexed releases",
            }
            direct_releases = release_sources.releases_for_slot(
                manga_id, chapter=request.chapter, volume=request.volume
            )
            query = " ".join(
                item
                for item in (
                    str(manga["title"]),
                    f"chapter {request.chapter}" if request.chapter else None,
                    f"volume {request.volume}"
                    if request.volume and request.chapter is None
                    else None,
                )
                if item
            )
            torrent_result: dict = {
                "provider": "aggregate",
                "providers": {},
                "errors": [],
                "query": query,
                "results": [],
            }
            if manga["preferred_language"] == "en" and (
                prowlarr.enabled and prowlarr.configured
            ):
                try:
                    torrent_result = await torrents.search(manga_id, query, limit=50)
                    has_chapter_match = request.chapter is not None and any(
                        canonical_number(item.get("chapter"))
                        == canonical_number(request.chapter)
                        for item in torrent_result["results"]
                    )
                    if request.chapter and request.volume and not has_chapter_match:
                        volume_query = f"{manga['title']} volume {request.volume}"
                        volume_result = await torrents.search(
                            manga_id, volume_query, limit=50
                        )
                        combined = {
                            (item["provider"], item["id"]): item
                            for item in [
                                *torrent_result["results"],
                                *volume_result["results"],
                            ]
                        }
                        torrent_result["results"] = list(combined.values())[:50]
                        torrent_result["errors"] = [
                            *torrent_result["errors"],
                            *volume_result["errors"],
                        ]
                        torrent_result["providers"] = {
                            name: int(torrent_result["providers"].get(name, 0))
                            + int(volume_result["providers"].get(name, 0))
                            for name in {
                                *torrent_result["providers"],
                                *volume_result["providers"],
                            }
                        }
                        torrent_result["query"] = f"{query} · fallback {volume_query}"
                except Exception as exc:  # noqa: BLE001 - direct results remain usable
                    torrent_result["errors"].append(
                        {
                            "provider": "torrent",
                            "error": f"{type(exc).__name__}: {exc}"[:500],
                        }
                    )
            from tankarr.selection import explain_releases

            direct_releases = explain_releases(database, manga_id, direct_releases)
            return {
                "manga_id": manga_id,
                "chapter": request.chapter,
                "volume": request.volume,
                "direct_releases": direct_releases,
                "direct_sources": [origin_status, *direct["sources"]],
                "torrent": torrent_result,
            }
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Manga not found") from exc
        except RecoveryBlocked as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=502, detail=f"Chapter release search failed: {exc}"
            ) from exc

    @app.post("/api/manga/{manga_id}/chapters/search/automatic")
    async def automatic_search_chapter(manga_id: str, request: ChapterSearchRequest):
        """Queue the best verified release for one canonical chapter slot."""

        if request.chapter is None and request.volume is None:
            raise HTTPException(
                status_code=400, detail="A chapter or volume number is required"
            )
        try:
            service.assert_mutations_allowed()
            manga = database.get_manga(manga_id)
            direct_releases = release_sources.releases_for_slot(
                manga_id, chapter=request.chapter, volume=request.volume
            )
            response = {
                "manga_id": manga_id,
                "chapter": request.chapter,
                "volume": request.volume,
                "state": "no_match",
                "queued": 0,
                "grabbed": 0,
                "message": "No verified automatic match was found. Use Interactive Search to review every result.",
                "errors": [],
            }
            if any(release.get("downloaded") for release in direct_releases):
                response.update(
                    state="downloaded",
                    message="This chapter is already in the library.",
                )
                return response

            direct_ids = [str(release["id"]) for release in direct_releases]
            preferred = (
                database.preferred_missing_releases(manga_id, direct_ids)
                if direct_ids
                else []
            )
            active = next(
                (release for release in preferred if release.get("queue_status")),
                None,
            )
            if active is not None:
                response.update(
                    state="already_queued",
                    message=f"This chapter is already {str(active['queue_status']).replace('_', ' ')}.",
                )
                return response
            if preferred and all(release.get("blocked") for release in preferred):
                response.update(
                    state="blocked",
                    message="Every verified release is blocked after a previous failure. Use Interactive Search or retry it from History.",
                )
                return response
            if preferred:
                queued = await monitor.queue_missing(manga_id, direct_ids)
                if queued:
                    response.update(
                        state="queued",
                        queued=queued,
                        message="The best verified direct release was queued.",
                    )
                    return response

            if direct_releases:
                # Three different refusals used to share one sentence, and it
                # named the one cause that was often false: a chapter every
                # source still offers, monitored throughout, was reported as
                # unmonitored. Say which of them it actually is.
                if not any(release.get("monitored") for release in direct_releases):
                    reason = (
                        "This chapter is not monitored, so it is not downloaded "
                        "automatically."
                    )
                    state = "not_monitored"
                elif not preferred:
                    reason = (
                        "No release for this chapter passed verification, so none "
                        "was chosen automatically."
                    )
                    state = "no_verified_release"
                else:
                    reason = (
                        "The queue turned down every verified release for this chapter."
                    )
                    state = "not_queued"
                response.update(
                    state=state,
                    message=f"{reason} Interactive Search is still available.",
                )
                return response

            # Whole-volume releases can be selected from Prowlarr by the same
            # deterministic identity and coverage planner used by Wanted.
            if request.chapter is None and request.volume is not None:
                hunted = await monitor._hunt_missing_volumes(
                    {
                        "manga": {"id": manga_id, "title": manga["title"]},
                        "chapters": [
                            {
                                "chapter": None,
                                "volume": request.volume,
                                "queue_status": None,
                                "blocked": False,
                            }
                        ],
                    }
                )
                grabbed = len(hunted.get("grabbed") or [])
                errors = [str(error) for error in hunted.get("errors") or []]
                if grabbed:
                    response.update(
                        state="queued",
                        grabbed=grabbed,
                        message="The best verified indexer release was sent to the download client.",
                        errors=errors,
                    )
                    return response
                response["errors"] = errors
            return response
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Manga not found") from exc
        except RecoveryBlocked as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=502, detail=f"Automatic chapter search failed: {exc}"
            ) from exc

    @app.post("/api/manga/{manga_id}/refresh")
    async def refresh_manga(manga_id: str):
        try:
            return await monitor.refresh_one(manga_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Manga not found") from exc
        except RecoveryBlocked as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=502, detail=f"Refresh failed: {exc}"
            ) from exc

    @app.get("/api/manga/{manga_id}/chapters")
    async def list_chapters(manga_id: str, language: str | None = None):
        try:
            manga = database.get_manga(manga_id)
            return database.list_chapters(
                manga_id, language or manga["preferred_language"]
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Manga not found") from exc

    @app.post("/api/chapters/{chapter_id}/download", status_code=202)
    async def download_chapter(chapter_id: str, request: DownloadRequest):
        try:
            job = await service.create_manual_download_job(
                chapter_id,
                replace=request.replace,
                quality_override=request.quality_override,
            )
            if job["status"] == "queued":
                await worker.enqueue(job["id"])
            return job
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Chapter not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except RecoveryBlocked as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.get("/api/manga/{manga_id}/torrent-search")
    async def search_torrent_releases(
        manga_id: str,
        q: str | None = Query(default=None, min_length=2, max_length=200),
        limit: int = Query(default=50, ge=1, le=75),
    ):
        try:
            return await torrents.search(manga_id, q, limit=limit)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Manga not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=502, detail=f"Torrent search failed: {exc}"
            ) from exc

    @app.post("/api/manga/{manga_id}/torrents", status_code=202)
    async def grab_torrent_release(manga_id: str, request: TorrentGrabRequest):
        try:
            download = await torrents.grab(
                manga_id, request.provider, request.release_id
            )
            return _public_torrent_download(download, settings)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Manga not found") from exc
        except RecoveryBlocked as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except QBitTorrentError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=502, detail=f"Unable to add torrent release: {exc}"
            ) from exc

    @app.post("/api/manga/{manga_id}/torrents/discovered", status_code=202)
    async def grab_discovered_torrent_release(
        manga_id: str, request: TorrentGrabRequest
    ):
        try:
            download = await torrents.grab_discovered(
                manga_id, request.provider, request.release_id
            )
            return _public_torrent_download(download, settings)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Manga not found") from exc
        except RecoveryBlocked as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except QBitTorrentError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=502, detail=f"Unable to add Prowlarr release: {exc}"
            ) from exc

    @app.get("/api/torrents")
    async def list_torrent_downloads(
        manga_id: str | None = Query(default=None),
        status: list[str] | None = Query(default=None),
        limit: int = Query(default=200, ge=1, le=500),
    ):
        downloads = await run_api_blocking(
            database.list_torrent_downloads,
            manga_id=manga_id,
            statuses=status,
            limit=limit,
        )
        return [_public_torrent_download(download, settings) for download in downloads]

    @app.post("/api/torrents/{download_id}/import")
    async def import_torrent_download(download_id: int, request: TorrentImportRequest):
        try:
            download = await torrents.import_now(
                download_id,
                confirm_language=request.confirm_language,
                skip_unnumbered=request.skip_unnumbered,
                selected_paths=(
                    set(request.selected_paths)
                    if request.selected_paths is not None
                    else None
                ),
                assigned={
                    book.path: {"volume": book.volume, "chapter": book.chapter}
                    for book in request.assigned or []
                }
                or None,
            )
            return _public_torrent_download(download, settings)
        except KeyError as exc:
            raise HTTPException(
                status_code=404, detail="Torrent job not found"
            ) from exc
        except (ValueError, FileNotFoundError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except RecoveryBlocked as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=500, detail=f"Torrent import failed: {exc}"
            ) from exc

    @app.post("/api/torrents/{download_id}/retry")
    async def retry_torrent_download(download_id: int):
        try:
            return _public_torrent_download(await torrents.retry(download_id), settings)
        except KeyError as exc:
            raise HTTPException(
                status_code=404, detail="Torrent job not found"
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except RecoveryBlocked as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=502, detail=f"Torrent retry failed: {exc}"
            ) from exc

    @app.get("/api/torrents/{download_id}/contents")
    async def list_torrent_contents(download_id: int):
        """The files in a completed release, for the operator to choose from."""

        try:
            return await torrents.contents(download_id)
        except KeyError as exc:
            raise HTTPException(
                status_code=404, detail="Torrent job not found"
            ) from exc
        except (ValueError, FileNotFoundError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.delete("/api/torrents/{download_id}")
    async def discard_torrent_download(
        download_id: int, delete_files: bool = Query(default=True)
    ):
        try:
            return await torrents.discard(download_id, delete_files=delete_files)
        except KeyError as exc:
            raise HTTPException(
                status_code=404, detail="Torrent job not found"
            ) from exc
        except QBitTorrentError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/jobs")
    async def list_jobs(
        limit: int = Query(default=100, ge=1, le=500),
        status: list[str] | None = Query(default=None),
        manga_id: str | None = Query(default=None),
    ):
        return await run_api_blocking(
            database.list_jobs, limit, status, manga_id=manga_id
        )

    @app.post("/api/jobs/prune-duplicates")
    async def prune_duplicate_jobs():
        return {"removed": database.prune_duplicate_queued_jobs()}

    @app.get("/api/reviews")
    async def list_reviews():
        return [_public_match_review(item) for item in database.list_match_reviews()]

    @app.post("/api/reviews/{review_id}/accept")
    async def accept_review(review_id: int):
        """A human confirms a near-miss: map the source or grab the release."""

        reviews = {item["id"]: item for item in database.list_match_reviews()}
        review = reviews.get(review_id)
        if review is None:
            raise HTTPException(status_code=404, detail="Review not found")
        if review["kind"] == "source":
            request = AddReleaseSourceRequest(
                provider=str(review["provider"]),
                provider_manga_id=str(
                    review["payload"].get("provider_manga_id") or review["candidate_id"]
                ),
            )
            result = await add_release_source(str(review["manga_id"]), request)
        else:
            payload = dict(review["payload"] or {})
            payload.setdefault("id", review["candidate_id"])
            result = _public_torrent_download(
                await torrents.grab_known_release(str(review["manga_id"]), payload),
                settings,
            )
        database.resolve_match_review(review_id, "accepted")
        return {"review": review_id, "resolution": "accepted", "result": result}

    @app.post("/api/reviews/{review_id}/reject")
    async def reject_review(review_id: int):
        try:
            return database.resolve_match_review(review_id, "rejected")
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Review not found") from exc

    @app.post("/api/system/alerts/{key}/dismiss")
    async def dismiss_alert(key: str, request: DismissAlertRequest):
        """Acknowledge one alert; it returns if what it reports changes."""

        database.dismiss_alert(key, request.signature)
        return {"key": key, "dismissed": True}

    @app.post("/api/jobs/prune-recovered")
    async def prune_recovered_jobs():
        """Forget failed downloads whose chapter arrived from another source."""

        return {"forgotten": database.prune_recovered_failures()}

    @app.get("/api/jobs/summary")
    async def jobs_summary():
        def render() -> dict[str, Any]:
            counts = database.job_status_counts()
            active = sum(
                counts.get(status, 0)
                for status in (
                    "queued",
                    "running",
                    "downloading",
                    "packaging",
                    "importing",
                )
            )
            return {
                "counts": counts,
                "active": active,
                "series": database.job_series_summary(),
            }

        return await run_api_blocking(render)

    @app.get("/api/jobs/{job_id}")
    async def get_job(job_id: int):
        try:
            return database.get_job(job_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Job not found") from exc

    @app.post("/api/jobs/{job_id}/retry")
    async def retry_job(job_id: int, override_quality: bool = Query(default=False)):
        try:
            service.assert_mutations_allowed()
            job = database.retry_job(job_id, quality_override=override_quality)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Job not found") from exc
        except RecoveryBlocked as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except (ValueError, sqlite3.IntegrityError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        await worker.enqueue(job["id"])
        return job

    @app.post("/api/jobs/cancel")
    async def cancel_jobs(request: CancelJobsRequest):
        removed: list[dict] = []
        failed: list[dict[str, object]] = []
        for job_id in dict.fromkeys(request.job_ids):
            try:
                removed.append(await worker.remove(job_id))
            except KeyError:
                failed.append({"id": job_id, "error": "Job not found"})
            except ValueError as exc:
                failed.append({"id": job_id, "error": str(exc)})
        return {"removed": removed, "failed": failed}

    @app.delete("/api/jobs/{job_id}")
    async def delete_job(job_id: int):
        try:
            return await worker.remove(job_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Job not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/wanted")
    async def list_wanted(
        request: Request,
        compact: bool = False,
        fresh: bool = False,
        cached: bool = False,
    ):
        previous = wanted_cache["compact_payload" if compact else "payload"]
        if cached and not fresh and previous is not None:
            payload = previous
            schedule_wanted_refresh()
        else:
            payload = await cached_wanted_payload(compact=compact, fresh=fresh)
        # Wanted can be a multi-megabyte snapshot. A stable validator lets the
        # paint-first request and its idle freshness check share one parsed JS
        # object when nothing changed, instead of transferring and decoding it
        # twice. Hash the cached bytes so stale-while-revalidate responses keep
        # the validator of the exact snapshot they actually serve.
        etag = snapshot_validators.etag("wanted", payload)
        headers = {"ETag": etag, "Cache-Control": "private, no-cache"}
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers=headers)
        return Response(
            content=payload,
            media_type="application/json",
            headers=headers,
        )

    @app.post("/api/wanted/search")
    async def search_wanted():
        try:
            service.assert_mutations_allowed()
        except RecoveryBlocked as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return await monitor.search_wanted(trigger="manual")

    @app.get("/api/manga/{manga_id}/official-alignment")
    async def official_alignment(manga_id: str):
        """What this series holds past its official edition, and the warrant."""

        try:
            report = service.surplus_beyond_official_edition(manga_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Series not found") from exc
        return {
            **report,
            "surplus": [
                {
                    "id": str(release["id"]),
                    "chapter": release.get("chapter"),
                    "title": release.get("title"),
                    "source_name": release.get("source_name"),
                    "library_path": release.get("library_path"),
                }
                for release in report["surplus"]
            ],
        }

    @app.post("/api/manga/{manga_id}/official-alignment")
    async def apply_official_alignment(manga_id: str, dry_run: bool = False):
        try:
            service.assert_mutations_allowed()
        except RecoveryBlocked as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        try:
            outcome = await service.align_with_official_edition(
                manga_id, dry_run=dry_run
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Series not found") from exc
        return {**outcome, "surplus": len(outcome["surplus"])}

    @app.get("/api/manga/{manga_id}/page-quality")
    async def page_quality_report(manga_id: str):
        """Every measured chapter of a series, worst first."""

        measured = database.page_quality(manga_id)
        return {
            "manga_id": manga_id,
            "measured": len(measured),
            "degraded": sorted(
                (
                    dict(record)
                    for record in measured.values()
                    if str(record.get("verdict")) == "degraded"
                ),
                key=lambda item: int(item.get("normalized_height") or 0),
            ),
        }

    @app.get("/api/manga/{manga_id}/content-alignment")
    async def content_alignment_report(manga_id: str):
        """What the pages say about each chapter file and the books."""
        try:
            database.get_manga(manga_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Unknown series") from exc
        rows = database.content_alignment(manga_id)
        return {
            "manga_id": manga_id,
            "chapters": sorted(rows.values(), key=lambda r: str(r.get("chapter_id"))),
        }

    @app.post("/api/manga/{manga_id}/content-alignment")
    async def align_content_now(manga_id: str):
        """Read this series' shelf now instead of waiting for the sweep."""
        try:
            database.get_manga(manga_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Unknown series") from exc
        outcome = await asyncio.to_thread(service.align_chapter_contents, manga_id)
        rows = database.content_alignment(manga_id)
        return {
            **outcome,
            "chapters": sorted(rows.values(), key=lambda r: str(r.get("chapter_id"))),
        }

    @app.post("/api/manga/{manga_id}/page-quality/recover")
    async def recover_page_quality(manga_id: str, measure: bool = True):
        try:
            service.assert_mutations_allowed()
        except RecoveryBlocked as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        if measure:
            # The background sweep may be days from this series; an operator
            # asking about it now gets it measured now.
            await service.audit_library_page_quality(
                limit=100_000, manga_ids=[manga_id]
            )
        outcome = await service.recover_degraded_chapters(manga_id)
        for item in outcome["requeued"]:
            await worker.enqueue(int(item["job_id"]))
        return outcome

    @app.get("/api/monitor/status")
    async def monitor_status():
        return monitor.status()

    def system_alerts(
        manga_list: list[dict[str, Any]], jobs: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Faults only a person can fix, worst first.

        Failed downloads are retried by the worker and listed in History;
        near-miss matches are refused by the rules and kept as history;
        catalogue counts beyond the numbering and imports without a catalogue
        identity show on the series page. None of them is an alert.
        """

        alerts: list[dict[str, Any]] = []
        dismissed = database.dismissed_alerts()
        # An individual source failure feeds ranking and retries. Only a
        # persistent outage of every source for work still Wanted is actionable.
        outages = source_outages(
            manga_list, database.list_all_release_sources(), providers
        )
        if outages:
            wanted_ids = {str(entry["manga"]["id"]) for entry in service.list_wanted()}
            outages = [item for item in outages if str(item["id"]) in wanted_ids]
        if outages:
            alerts.append(
                {
                    "level": "warn",
                    "key": "sources",
                    "title": f"{len(outages)} Wanted series without a working source for over 24 hours",
                    "detail": ", ".join(sorted(str(item["title"]) for item in outages))[
                        :200
                    ],
                    "href": "#/wanted",
                }
            )
        # Files on disk that no Tankarr release claims: the library and the
        # reader must always show the same thing.
        try:
            orphans = service.library_orphans(folder_limit=5)
        except Exception:  # noqa: BLE001 - a scan failure is not an alert
            orphans = {"count": 0, "folders": []}
        if orphans.get("count"):
            names = ", ".join(item["folder"] for item in orphans["folders"])
            alerts.append(
                {
                    "level": "warn",
                    "key": "library_orphans",
                    "title": (
                        f"{orphans['count']} library file"
                        f"{'s' if orphans['count'] != 1 else ''} Tankarr does not track"
                    ),
                    "detail": (
                        "The reader still serves them. They are left from a series "
                        f"deleted without its files: {names}"[:400]
                    ),
                    "href": "#/system",
                }
            )
        # Torrents in Tankarr's category that Tankarr never added are left
        # alone by the orphan sweep; only a person can move or remove them.
        sweep = torrents.last_orphan_sweep or {}
        if sweep.get("foreign"):
            count = int(sweep["foreign"])
            alerts.append(
                {
                    "level": "info",
                    "key": "foreign_torrents",
                    "title": (
                        f"{count} torrent{'s' if count != 1 else ''} in the "
                        f"{settings.qbittorrent_category!r} category that Tankarr "
                        "did not add"
                    ),
                    "detail": (
                        "They are never removed by Tankarr. Move them to another "
                        "category or delete them in qBittorrent: "
                        + ", ".join(sweep.get("foreign_names") or [])
                    )[:400],
                }
            )
        for name, path in (
            ("library", settings.library_dir),
            ("config", settings.data_dir),
        ):
            try:
                usage = shutil.disk_usage(path)
                if usage.total and usage.free / usage.total < 0.05:
                    alerts.append(
                        {
                            "level": "danger",
                            "key": f"disk_{name}",
                            "title": f"{name.title()} storage below 5% free",
                            "detail": f"{usage.free // (1024**3)} GiB free of {usage.total // (1024**3)} GiB at {path}",
                        }
                    )
            except OSError:
                pass
        if (
            settings.suwayomi_managed
            and suwayomi_runtime.installed()
            and not suwayomi_runtime.running
        ):
            alerts.append(
                {
                    "level": "danger",
                    "key": "suwayomi",
                    "title": "Suwayomi runtime is not running",
                    "detail": suwayomi_runtime.last_error
                    or "Start it from System → Suwayomi.",
                    "href": "#/system",
                }
            )
        if (
            settings.suwayomi_managed
            and suwayomi_runtime.installed()
            and not settings.suwayomi_extension_store
        ):
            alerts.append(
                {
                    "level": "warn",
                    "key": "suwayomi_extension_store",
                    "title": "No extension repository configured",
                    "detail": "Suwayomi has no repository to install sources "
                    "from. Enter one under Settings → Sources → Suwayomi.",
                    "href": "#/settings?tab=sources",
                }
            )
        challenged = challenged_sources()
        if challenged:
            alerts.append(
                {
                    "level": "info",
                    "key": "challenged_sources",
                    "title": f"{len(challenged)} source(s) behind an anti-bot challenge",
                    "detail": "Unreadable from this host and ranked last: "
                    + ", ".join(challenged)[:160],
                }
            )
        metadata_status = metadata.status() if hasattr(metadata, "status") else {}
        if isinstance(metadata_status, dict) and metadata_status.get(
            "last_cycle_error"
        ):
            alerts.append(
                {
                    "level": "warn",
                    "key": "metadata",
                    "title": "Metadata refresh reported errors",
                    "detail": str(metadata_status.get("last_cycle_error"))[:200],
                }
            )
        update = updates.status()
        if update.get("update_available"):
            alert = {
                "level": "info",
                "key": "update_available",
                "title": f"Tankarr {update['latest']} is available",
                "detail": (
                    f"This installation runs {update['current']}. Pull the new "
                    "image and restart the container; the release notes say "
                    "what changed."
                ),
            }
            if update.get("url"):
                alert["href"] = str(update["url"])
            alerts.append(alert)
        order = {"danger": 0, "warn": 1, "info": 2}
        alerts.sort(key=lambda item: order.get(item["level"], 3))
        # An acknowledged alert stays hidden until its own content changes,
        # so dismissing "12 failed downloads" does not hide the thirteenth.
        for alert in alerts:
            alert.setdefault("signature", str(alert.get("title") or alert["key"]))
        return [
            alert
            for alert in alerts
            if dismissed.get(str(alert["key"])) != alert["signature"]
        ]

    @app.get("/api/system/status")
    async def system_status():
        def render() -> dict[str, Any]:
            def disk(path: Path) -> dict:
                try:
                    usage = shutil.disk_usage(path)
                    return {
                        "path": str(path),
                        "total": usage.total,
                        "free": usage.free,
                        "used": usage.total - usage.free,
                    }
                except OSError:
                    return {
                        "path": str(path),
                        "total": None,
                        "free": None,
                        "used": None,
                    }

            manga_list = database.list_manga()
            jobs = database.list_jobs(500)
            return {
                "version": __version__,
                "update": updates.status(),
                "python": platform.python_version(),
                "platform": platform.platform(),
                "started_at": started_at,
                "database_path": str(settings.database_path),
                "database_size": (
                    settings.database_path.stat().st_size
                    if settings.database_path.exists()
                    else 0
                ),
                "storage": {
                    "data": disk(settings.data_dir),
                    "library": disk(settings.library_dir),
                },
                "totals": {
                    "series": len(manga_list),
                    "monitored": sum(1 for item in manga_list if item["monitored"]),
                    "chapters": sum(item["chapter_count"] for item in manga_list),
                    "downloaded": sum(item["downloaded_count"] for item in manga_list),
                    "jobs": len(jobs),
                    "failed_jobs": sum(1 for job in jobs if job["status"] == "failed"),
                },
                "providers": [
                    {
                        "name": item.name,
                        "label": item.label,
                        "search_mode": item.search_mode,
                    }
                    for item in providers.values()
                ],
                "alerts": system_alerts(manga_list, jobs),
                "library_alignment": reader_alignment_status(),
                "suwayomi": (
                    {
                        **_suwayomi_runtime_status(),
                        "maintenance": suwayomi_maintenance,
                        "challenged_sources": challenged_sources(),
                    }
                    if settings.suwayomi_managed
                    else None
                ),
                "komga_refresh": service.komga_refresh_status(),
                "ntfy_configured": service.notifier.configured,
                "notifications": service.notifier.configured_channels(),
                "monitor": monitor.status(),
                "metadata": metadata.status(),
                "torrents": torrents.status(),
                "library": service.library_status(),
                "backups": _list_backups(),
            }

        return await run_api_blocking(render)

    @app.post("/api/system/komga/refresh")
    async def refresh_komga_now():
        if not service.komga_refresh_status()["enabled"]:
            raise HTTPException(status_code=409, detail="Komga is not configured")
        result = await service.refresh_komga_library(reason="manual")
        if not result.get("ready"):
            raise HTTPException(status_code=502, detail=result)
        return result

    backup_lock = asyncio.Lock()

    def _list_backups() -> list[dict]:
        return backups.list(limit=settings.backup_retention_count)

    @app.get("/api/system/logs")
    async def list_system_logs():
        return {
            "level": settings.log_level,
            "directory": str(log_directory(settings)),
            "files": await run_api_blocking(log_files, settings),
        }

    @app.get("/api/system/logs/tail")
    async def tail_system_log(
        lines: int = Query(default=200, ge=1, le=2000),
        level: str | None = Query(default=None, max_length=10),
    ):
        try:
            minimum = normalize_log_level(level) if level else None
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"lines": await run_api_blocking(tail_log, settings, lines, minimum)}

    @app.get("/api/system/logs/{name}/download")
    async def download_system_log(name: str):
        # Only the names the listing shows: no path can reach outside the folder.
        known = {item["name"] for item in await run_api_blocking(log_files, settings)}
        path = log_directory(settings) / name
        if name not in known or not path.is_file():
            raise HTTPException(status_code=404, detail="Unknown log file")
        return FileResponse(
            path,
            media_type="text/plain; charset=utf-8",
            filename=name,
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/api/system/backup")
    async def backup_database():
        """Verified private control-plane backup; library media stay separate."""

        try:
            async with backup_lock:
                return await asyncio.to_thread(backups.create)
        except (sqlite3.Error, OSError, ValueError, TimeoutError) as exc:
            raise HTTPException(
                status_code=500,
                detail=f"Backup failed ({type(exc).__name__}); check storage and permissions",
            ) from exc

    @app.post("/api/system/backups/{name}/verify")
    async def verify_backup(name: str):
        try:
            async with backup_lock:
                return await asyncio.to_thread(backups.verify, name)
        except (ValueError, OSError, sqlite3.Error, zipfile.BadZipFile) as exc:
            raise HTTPException(409, "Backup is missing or failed validation") from exc

    @app.get("/api/system/backups/{name}/download")
    async def download_backup(name: str):
        # Bundles contain credentials: normal authenticated access only, never
        # attach their contents to the redacted diagnostics export.
        try:
            path = backups.export_path(name)
        except (ValueError, OSError) as exc:
            raise HTTPException(404, "Backup not found") from exc
        return FileResponse(
            path,
            filename=path.name,
            media_type="application/zip",
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/providers/status")
    async def providers_status():
        """Probe each provider using its own lightweight health contract."""

        async def probe(item):
            loop = asyncio.get_running_loop()
            start = loop.time()
            try:
                details = await asyncio.wait_for(item.healthcheck(), timeout=30)
                result = {
                    "name": item.name,
                    "label": item.label,
                    "search_mode": item.search_mode,
                    "ok": True,
                    "latency_ms": round((loop.time() - start) * 1000),
                }
                if isinstance(details, dict):
                    result.update(
                        {
                            key: value
                            for key, value in details.items()
                            if key not in result and key != "ok"
                        }
                    )
                return result
            except Exception as exc:  # noqa: BLE001 - probe failures are data
                return {
                    "name": item.name,
                    "label": item.label,
                    "search_mode": item.search_mode,
                    "ok": False,
                    "latency_ms": None,
                    "error": f"{type(exc).__name__}: {exc}"[:200],
                }

        return await asyncio.gather(*(probe(item) for item in providers.values()))

    @app.get("/api/settings")
    async def get_app_settings():
        return settings_view(settings, database)

    @app.put("/api/settings")
    async def put_app_settings(changes: dict[str, object]):
        metadata_was_enabled = settings.metadata_enabled
        monitor_was_enabled = settings.monitor_enabled
        try:
            applied = update_settings(settings, database, changes)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        refresh = None
        if "log_level" in applied:
            apply_log_level(settings)
        if settings.metadata_enabled and not metadata_was_enabled:
            refresh = metadata.start_bulk_refresh(force=True)
        if {
            "provider_priority",
            "source_priority_fresh",
            "source_priority_backfill",
            "release_preference_profile",
            "release_acquisition_policy",
        } & set(applied):
            database.provider_priority = settings.provider_priority_order
            database.source_ranking = settings.source_ranking
        monitor_fields = {
            "monitor_enabled",
            "monitor_interval_seconds",
            "wanted_search_enabled",
            "wanted_search_interval_seconds",
        }
        if monitor_fields.intersection(applied):
            if monitor_was_enabled or monitor.task is not None:
                await monitor.stop()
            if settings.monitor_enabled:
                await monitor.start()
        provider_fields = {
            "suwayomi_enabled",
            "suwayomi_url",
            "suwayomi_username",
            "suwayomi_password",
            "suwayomi_language",
            "suwayomi_source_ids",
            "suwayomi_mode",
        }
        runtime_fields = {
            "suwayomi_managed_heap_mb",
            "suwayomi_auto_install_official",
            "suwayomi_extension_store",
            "auth_username",
            "auth_password",
        }
        if runtime_fields.intersection(applied) or "suwayomi_mode" in applied:
            changed = suwayomi_runtime.configure(
                heap_mb=settings.suwayomi_managed_heap_mb,
                credentials=settings.suwayomi_managed_credentials,
                extension_store=settings.suwayomi_extension_store or None,
            )
            if not settings.suwayomi_managed:
                if suwayomi_runtime.running:
                    await suwayomi_runtime.stop()
            elif suwayomi_runtime.installed() and (
                changed or not suwayomi_runtime.running
            ):
                if suwayomi_runtime.running:
                    await suwayomi_runtime.stop()
                asyncio.create_task(
                    start_managed_suwayomi(), name="tankarr-suwayomi-restart"
                )
        reader_fields = {
            "reader_kind",
            "reader_url",
            "reader_internal_url",
            "reader_api_key",
            "reader_username",
            "reader_password",
            "reader_library_path",
        }
        if reader_fields.intersection(applied):
            applied = [*applied, *await _sync_komga_from_reader()]
        if provider_fields.intersection(applied):
            async with provider_reload_lock:
                retired, discarded = replace_configurable_providers(providers, settings)
                if {"suwayomi_mode", "suwayomi_url"} & set(applied):
                    retire_stale_suwayomi_identities()
                # Retired adapters may still belong to an in-flight chapter
                # job. Keep them alive until shutdown; unused replacement core
                # adapters can be closed immediately.
                retired_providers.extend(retired)
                for item in discarded:
                    await item.aclose()
        komga_fields = {
            "komga_link_enabled",
            "komga_url",
            "komga_library_id",
            "komga_auth_method",
            "komga_api_key",
            "komga_username",
            "komga_password",
        }
        reader_sync = None
        if komga_fields.intersection(applied):
            if settings.komga_link_enabled:
                service.komga = komga_links
                metadata.set_komga(komga_links)

                async def synchronize_reader() -> None:
                    alignment = await service.reconcile_komga_library()
                    if alignment.get("ready"):
                        alignment["metadata_sync"] = await metadata.sync_all_to_komga(
                            force=True
                        )
                        service.last_komga_library_alignment = alignment

                task = asyncio.create_task(
                    synchronize_reader(), name="tankarr-komga-full-sync"
                )
                discovery_tasks.add(task)
                task.add_done_callback(discovery_tasks.discard)
                reader_sync = {"started": True, "force": True}
            else:
                service.komga = managed_library_reader()
                metadata.set_komga(managed_metadata_reader(service.komga))
                reader_sync = {"started": False, "disabled": True}
        elif reader_fields.intersection(applied) and not settings.komga_link_enabled:
            # Stump credentials or the reader kind changed: rebuild the
            # managed reader so the next scan uses them.
            service.komga = managed_library_reader()
            metadata.set_komga(managed_metadata_reader(service.komga))
        if (
            komga_fields.intersection(applied)
            or reader_fields.intersection(applied)
            or "komga_refresh_interval_minutes" in applied
        ):
            service.reset_komga_refresh_schedule()
        return {
            "applied": applied,
            "settings": settings_view(settings, database),
            "metadata_refresh": refresh,
            "komga_sync": reader_sync,
        }

    async def _sync_komga_from_reader() -> list[str]:
        """Komga alignment is implied by choosing Komga as the reader with an
        API key; nothing else to configure, nothing to keep in sync by hand."""

        wants = (
            settings.reader_kind == "komga"
            and bool(settings.reader_url)
            and bool(settings.reader_api_key)
        )
        changes: dict[str, str] = {}
        if wants:
            if settings.komga_url != settings.reader_url:
                changes["komga_url"] = settings.reader_url or ""
            internal = settings.reader_internal_url or ""
            if (settings.komga_internal_url or "") != internal:
                changes["komga_internal_url"] = internal
            if settings.komga_api_key != settings.reader_api_key:
                changes["komga_api_key"] = settings.reader_api_key or ""
            if settings.komga_auth_method != "api_key":
                changes["komga_auth_method"] = "api_key"
            if not settings.komga_link_enabled:
                changes["komga_link_enabled"] = "true"
        elif settings.komga_link_enabled:
            changes["komga_link_enabled"] = "false"
        if not changes:
            return []
        return list(update_settings(settings, database, changes))

    @app.post("/api/settings/test/reader")
    async def test_reader(changes: dict[str, object] | None = None):
        allowed = {
            "reader_kind",
            "reader_url",
            "reader_internal_url",
            "reader_api_key",
            "reader_username",
            "reader_password",
            "reader_library_path",
            "reader_series_url_template",
        }
        try:
            candidate = preview_settings(settings, changes or {}, allowed=allowed)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        kind = effective_reader_kind(candidate)
        api_base = (candidate.reader_internal_url or candidate.reader_url or "").rstrip(
            "/"
        )
        try:
            async with async_client(timeout=15.0) as client:
                if kind == "none":
                    return {"ok": True, "detail": "No reader shortcut configured"}
                if kind == "tankarr":
                    return {
                        "ok": True,
                        "detail": "Tankarr built-in reader is ready; no connection or scan is required",
                    }
                if kind == "url":
                    return {
                        "ok": bool(candidate.reader_series_url_template),
                        "detail": "URL template set"
                        if candidate.reader_series_url_template
                        else None,
                        "error": None
                        if candidate.reader_series_url_template
                        else "Set a series URL template",
                    }
                if not api_base:
                    return {"ok": False, "error": "Set the reader URL"}
                if kind == "stump":
                    auth = await client.post(
                        f"{api_base}/api/v2/auth/login",
                        params={"generate_token": "true", "create_session": "false"},
                        json={
                            "username": candidate.reader_username or "",
                            "password": candidate.reader_password or "",
                        },
                    )
                    if auth.status_code in {401, 403}:
                        return {
                            "ok": False,
                            "error": "Stump rejected the username or password",
                        }
                    auth.raise_for_status()
                    token = str((auth.json() or {}).get("accessToken") or "")
                    stats = await client.post(
                        f"{api_base}/api/graphql",
                        json={"query": "{ numberOfSeries mediaCount }"},
                        headers={"Authorization": f"Bearer {token}"},
                    )
                    stats.raise_for_status()
                    data = (stats.json() or {}).get("data") or {}
                    return {
                        "ok": True,
                        "detail": f"Stump reachable · {data.get('numberOfSeries')} series, {data.get('mediaCount')} books",
                    }
                if kind == "kavita":
                    auth = await client.post(
                        f"{api_base}/api/Plugin/authenticate",
                        params={
                            "apiKey": candidate.reader_api_key or "",
                            "pluginName": "Tankarr",
                        },
                    )
                    if auth.status_code in {401, 403}:
                        return {"ok": False, "error": "Kavita rejected the API key"}
                    auth.raise_for_status()
                    return {"ok": True, "detail": "Kavita reachable · API key accepted"}
                if kind == "komga":
                    response = await client.get(
                        f"{api_base}/api/v1/libraries",
                        headers={"X-API-Key": candidate.reader_api_key or ""},
                    )
                    if response.status_code in {401, 403}:
                        return {"ok": False, "error": "Komga rejected the API key"}
                    response.raise_for_status()
                    libraries = response.json() or []
                    return {
                        "ok": True,
                        "detail": f"Komga reachable · {len(libraries)} librar{'y' if len(libraries) == 1 else 'ies'}; Tankarr will keep it aligned automatically",
                    }
        except httpx.HTTPError as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300]}
        return {"ok": False, "error": f"Unknown reader {kind}"}

    @app.post("/api/settings/discover/reader")
    async def discover_reader():
        return await discover_local_reader(settings)

    def _suwayomi_runtime_status() -> dict[str, Any]:
        return {
            **suwayomi_runtime.status(),
            "mode": settings.suwayomi_mode,
            "enabled": settings.suwayomi_enabled,
            "languages": list(settings.search_language_codes),
            "public_url": settings.suwayomi_public_url,
            "external_url": settings.suwayomi_url,
        }

    def _require_managed_suwayomi() -> None:
        if not settings.suwayomi_managed:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Suwayomi is in external mode; manage the server and its "
                    "extensions in your own Suwayomi instance"
                ),
            )

    async def _adopt_managed_suwayomi() -> None:
        """After a successful install, Tankarr becomes the Suwayomi owner."""

        changes: dict[str, str] = {}
        if not settings.suwayomi_managed:
            changes["suwayomi_mode"] = "managed"
        if not settings.suwayomi_enabled:
            changes["suwayomi_enabled"] = "true"
        if not changes:
            return
        update_settings(settings, database, changes)
        async with provider_reload_lock:
            retired, discarded = replace_configurable_providers(providers, settings)
            retired_providers.extend(retired)
            for item in discarded:
                await item.aclose()
            retire_stale_suwayomi_identities()

    @app.get("/api/system/suwayomi")
    async def suwayomi_status():
        return _suwayomi_runtime_status()

    @app.post("/api/system/suwayomi/install")
    async def suwayomi_install():
        # Installing is how an external/legacy setup migrates: no mode switch
        # is required first, the successful install adopts managed mode.
        try:
            await suwayomi_runtime.install()
        except SuwayomiRuntimeBusy as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except SuwayomiRuntimeError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        ready = await suwayomi_runtime.wait_ready()
        defaults: list[str] = []
        if ready:
            await _adopt_managed_suwayomi()
            defaults = await install_language_catalogue()
        return {
            **_suwayomi_runtime_status(),
            "ready": ready,
            "default_extensions": defaults,
        }

    @app.post("/api/system/suwayomi/restart")
    async def suwayomi_restart():
        _require_managed_suwayomi()
        if not suwayomi_runtime.installed():
            raise HTTPException(status_code=409, detail="Suwayomi is not installed")
        try:
            await suwayomi_runtime.restart()
        except SuwayomiRuntimeError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        ready = await suwayomi_runtime.wait_ready()
        return {**_suwayomi_runtime_status(), "ready": ready}

    @app.post("/api/system/suwayomi/stop")
    async def suwayomi_stop():
        _require_managed_suwayomi()
        await suwayomi_runtime.stop()
        return _suwayomi_runtime_status()

    @app.get("/api/system/suwayomi/extensions")
    async def suwayomi_extensions(
        language: str | None = Query(default=None, max_length=16),
        refresh: bool = Query(default=False),
    ):
        _require_managed_suwayomi()
        if not suwayomi_runtime.running:
            raise HTTPException(status_code=409, detail="Suwayomi is not running")
        if language == "*":
            languages: tuple[str, ...] | None = None
        elif language:
            languages = (language,)
        else:
            languages = settings.search_language_codes
        try:
            extensions = await suwayomi_runtime.list_extensions(
                languages=languages, refresh=refresh
            )
        except Exception as exc:  # noqa: BLE001 - surface the engine's error
            raise HTTPException(status_code=502, detail=str(exc)[:300]) from exc
        return {"extensions": extensions, "count": len(extensions)}

    @app.get("/api/system/suwayomi/languages")
    async def suwayomi_languages():
        _require_managed_suwayomi()
        if not suwayomi_runtime.running:
            raise HTTPException(status_code=409, detail="Suwayomi is not running")
        try:
            return {"languages": await suwayomi_runtime.list_languages()}
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=str(exc)[:300]) from exc

    @app.post("/api/system/suwayomi/update")
    async def suwayomi_update_now():
        """Force what the daily maintenance does: server JAR + extensions."""

        _require_managed_suwayomi()
        result = await maintain_managed_suwayomi(force=True)
        return {**_suwayomi_runtime_status(), "maintenance": result}

    @app.get("/api/system/suwayomi/extensions/{pkg_name}/icon")
    async def suwayomi_extension_icon(pkg_name: str):
        try:
            items = await suwayomi_runtime.list_extensions()
        except Exception as exc:  # noqa: BLE001 - the store may be unreachable
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        icon_path = next(
            (
                str(item.get("icon_path") or "")
                for item in items
                if item.get("pkg_name") == pkg_name
            ),
            "",
        )
        if not icon_path:
            raise HTTPException(status_code=404, detail="No icon for this extension")
        try:
            content, media_type = await suwayomi_runtime.fetch_icon(icon_path)
        except Exception as exc:  # noqa: BLE001 - a missing icon is not a fault
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        # Only raster images are served from Tankarr's origin: an extension
        # store cannot make the browser render HTML or SVG script here.
        if not media_type.startswith("image/") or "svg" in media_type:
            media_type = "application/octet-stream"
        return Response(
            content=content,
            media_type=media_type,
            headers={"Cache-Control": "private, max-age=86400"},
        )

    @app.post("/api/system/suwayomi/extensions")
    async def suwayomi_set_extension(payload: dict[str, object]):
        _require_managed_suwayomi()
        pkg_name = str(payload.get("pkg_name") or "").strip()
        if not pkg_name.startswith("eu.kanade.tachiyomi.extension."):
            raise HTTPException(status_code=400, detail="Unknown extension package")
        if not suwayomi_runtime.running:
            raise HTTPException(status_code=409, detail="Suwayomi is not running")
        try:
            result = await suwayomi_runtime.set_extension(
                pkg_name, installed=bool(payload.get("installed", True))
            )
        except Exception as exc:  # noqa: BLE001 - surface the engine's error
            raise HTTPException(status_code=502, detail=str(exc)[:300]) from exc
        return result

    @app.post("/api/system/suwayomi/test")
    async def suwayomi_test_sources(payload: dict[str, object] | None = None):
        """Probe every enabled source, streaming verdicts as they land.

        NDJSON: a ``start`` line with the total, one ``result`` line per
        source the moment its probe finishes, and a closing ``done`` line.
        Every verdict also feeds the perennial standings, so running a test
        after adding providers draws up their first ranking.
        """

        _require_managed_suwayomi()
        if not suwayomi_runtime.running:
            raise HTTPException(status_code=409, detail="Suwayomi is not running")
        language = str((payload or {}).get("language") or settings.default_language)
        try:
            sources = await suwayomi_runtime.list_sources()
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=str(exc)[:300]) from exc
        selected_ids = {str(item) for item in settings.suwayomi_source_id_set}
        candidates = [
            source
            for source in sources
            if source["language"] in {language.casefold(), "all", "multi"}
            and (not selected_ids or source["id"] in selected_ids)
        ]

        async def stream():
            yield (
                json.dumps(
                    {"type": "start", "language": language, "count": len(candidates)}
                )
                + "\n"
            )
            async for item in suwayomi_runtime.test_sources_iter(
                candidates,
                language=language,
                timeout=float(settings.suwayomi_search_timeout_seconds),
            ):
                database.record_source_health(
                    f"suwayomi:{item['id']}",
                    ok=item["verdict"] != "unreachable",
                    reason=str(item.get("error") or ""),
                )
                yield json.dumps({"type": "result", **item}) + "\n"
            yield json.dumps({"type": "done"}) + "\n"

        return StreamingResponse(stream(), media_type="application/x-ndjson")

    @app.post("/api/settings/test/provider/{provider_name}")
    async def test_download_provider(
        provider_name: str, changes: dict[str, object] | None = None
    ):
        allowed_by_provider = {
            "suwayomi": {
                "search_languages",
                "suwayomi_enabled",
                "suwayomi_url",
                "suwayomi_username",
                "suwayomi_password",
                "suwayomi_language",
                "suwayomi_source_ids",
                "suwayomi_mode",
                "suwayomi_managed_heap_mb",
                "suwayomi_public_url",
            },
        }
        allowed = allowed_by_provider.get(provider_name)
        if allowed is None:
            raise HTTPException(status_code=404, detail="Unknown download provider")
        try:
            candidate = preview_settings(
                settings,
                changes or {},
                allowed=allowed,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        probe_registry = build_providers(candidate)
        selected = probe_registry.get(provider_name)
        if selected is None:
            for item in probe_registry.values():
                await item.aclose()
            return {
                "ok": False,
                "provider": provider_name,
                "error": f"{provider_name} is disabled",
            }
        timeout = 30.0
        try:
            details = await asyncio.wait_for(selected.healthcheck(), timeout=timeout)
            result = {
                "ok": True,
                "provider": selected.name,
                "label": selected.label,
            }
            if isinstance(details, dict):
                if provider_name == "suwayomi":
                    source_details = await selected.source_catalog(
                        candidate.search_language_codes
                    )
                    details["source_details"] = source_details
                    details["sources"] = sum(
                        1 for item in source_details if item["enabled"]
                    )
                for key in (
                    "language",
                    "sources",
                    "source_details",
                ):
                    if key in details:
                        result[key] = details[key]
            return result
        except Exception as exc:  # noqa: BLE001 - connection failure is the result
            return {
                "ok": False,
                "provider": provider_name,
                "error": f"{type(exc).__name__}: {exc}"[:300],
            }
        finally:
            for item in probe_registry.values():
                await item.aclose()

    @app.post("/api/settings/test/prowlarr")
    async def test_prowlarr(changes: dict[str, object] | None = None):
        try:
            candidate = preview_settings(
                settings,
                changes or {},
                allowed={
                    "prowlarr_enabled",
                    "prowlarr_url",
                    "prowlarr_api_key",
                    "prowlarr_indexer_ids",
                    "prowlarr_categories",
                },
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        probe_client = ProwlarrClient(candidate)
        try:
            return await probe_client.probe()
        except Exception as exc:  # noqa: BLE001 - connection failure is the result
            return {"ok": False, "error": redact_secrets(str(exc))[:300]}

    @app.post("/api/settings/test/internet-archive")
    async def test_internet_archive(changes: dict[str, object] | None = None):
        try:
            candidate = preview_settings(
                settings, changes or {}, allowed={"internet_archive_enabled"}
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        probe_client = InternetArchiveClient(candidate)
        try:
            return await probe_client.probe()
        finally:
            await probe_client.aclose()

    @app.post("/api/settings/test/komga")
    async def test_komga_shortcut(changes: dict[str, object] | None = None):
        try:
            candidate = preview_settings(
                settings,
                changes or {},
                allowed={
                    "komga_link_enabled",
                    "komga_url",
                    "komga_library_id",
                    "komga_auth_method",
                    "komga_api_key",
                    "komga_username",
                    "komga_password",
                },
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not candidate.komga_link_enabled:
            return {"ok": False, "error": "Komga shortcut is disabled"}
        probe_client = KomgaClient(candidate)
        try:
            return await probe_client.probe_catalogue()
        except Exception as exc:  # noqa: BLE001 - connection failure is the result
            return {"ok": False, "error": redact_secrets(str(exc))[:300]}

    @app.post("/api/settings/test/qbittorrent")
    async def test_qbittorrent(changes: dict[str, object] | None = None):
        try:
            candidate = preview_settings(
                settings,
                changes or {},
                allowed={
                    "qbittorrent_url",
                    "qbittorrent_username",
                    "qbittorrent_password",
                    "qbittorrent_category",
                    "sabnzbd_url",
                    "sabnzbd_api_key",
                    "sabnzbd_category",
                    "sabnzbd_complete_path",
                },
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        probe_client = QBitTorrentClient(candidate)
        if not probe_client.configured:
            return {"ok": False, "error": "qBittorrent is not configured"}
        try:
            return await probe_client.probe()
        except Exception as exc:  # noqa: BLE001 - the failure is the result
            return {"ok": False, "error": redact_secrets(str(exc))[:300]}

    @app.post("/api/settings/test/sabnzbd")
    async def test_sabnzbd(changes: dict[str, object] | None = None):
        try:
            candidate = preview_settings(
                settings,
                changes or {},
                allowed={
                    "sabnzbd_url",
                    "sabnzbd_api_key",
                    "sabnzbd_category",
                    "sabnzbd_complete_path",
                },
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        probe_client = SABnzbdClient(candidate)
        if not probe_client.configured:
            return {"ok": False, "error": "SABnzbd is not configured"}
        try:
            result = await probe_client.probe()
            await probe_client.ensure_category()
            return result
        except Exception as exc:  # noqa: BLE001 - the failure is the result
            return {"ok": False, "error": redact_secrets(str(exc))[:300]}

    @app.post("/api/settings/test/ntfy")
    async def test_ntfy(changes: dict[str, object] | None = None):
        try:
            candidate = preview_settings(
                settings,
                changes or {},
                allowed={"ntfy_url", "ntfy_topic"},
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return await Notifier(candidate).test_delivery()

    @app.post("/api/settings/test/notifications/{channel}")
    async def test_notification_channel(
        channel: str, changes: dict[str, object] | None = None
    ):
        allowed = CHANNEL_SETTINGS.get(channel)
        if allowed is None:
            raise HTTPException(status_code=404, detail="Unknown notification channel")
        try:
            candidate = preview_settings(settings, changes or {}, allowed=set(allowed))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return await Notifier(candidate).test_delivery(channel)

    @app.post("/api/settings/test/metadata/{source_name}")
    async def test_metadata_source(
        source_name: str, changes: dict[str, object] | None = None
    ):
        credential_by_source = dict(SPINE_SOURCE_FLAGS)
        credential = credential_by_source.get(source_name)
        if credential is None:
            raise HTTPException(status_code=404, detail="Unknown metadata source")
        try:
            candidate = preview_settings(
                settings,
                changes or {},
                allowed={credential},
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        sources = build_metadata_sources(candidate)
        selected = next(item for item in sources if item.name == source_name)
        if not selected.configured:
            for source in sources:
                await source.aclose()
            return {"ok": False, "error": selected.unavailable_reason}
        try:
            query = "one piece" if source_name in SPINE_SOURCE_FLAGS else "one"
            results = await selected.search_series(query, limit=1)
            if source_name in SPINE_SOURCE_FLAGS and not results:
                return {
                    "ok": False,
                    "error": f"{selected.label} answered but returned no results",
                }
            return {"ok": True, "source": selected.label, "results": len(results)}
        except httpx.HTTPStatusError as exc:
            # The status alone tells the operator whether the key is wrong
            # (401/403), the daily quota is spent (429) or the service is down.
            return {
                "ok": False,
                "error": (
                    f"{selected.label} answered HTTP {exc.response.status_code}"
                    f"{_metadata_api_message(exc)}"
                ),
            }
        except Exception as exc:  # noqa: BLE001 - the failure is the result
            return {
                "ok": False,
                "error": f"{type(exc).__name__}: metadata API request failed",
            }
        finally:
            for source in sources:
                await source.aclose()

    calendar_cache: OrderedDict[tuple[str, ...], tuple[bytes, float]] = OrderedDict()
    calendar_cache_lock = asyncio.Lock()
    calendar_cache_limit = 12

    def render_calendar(
        *,
        days: int,
        ahead: int,
        start_on: date | None,
        end_on: date | None,
        _with_expiry: bool = False,
    ) -> dict[str, Any]:
        now = datetime.now(UTC)
        if (start_on is None) != (end_on is None):
            raise HTTPException(
                status_code=422, detail="start and end must be supplied together"
            )
        if start_on is not None and end_on is not None:
            if end_on < start_on:
                raise HTTPException(
                    status_code=422, detail="end must not precede start"
                )
            if (end_on - start_on).days > 31:
                raise HTTPException(
                    status_code=422, detail="calendar windows cannot exceed 32 days"
                )
            # Release dates may be stored either as YYYY-MM-DD or as complete
            # timestamps. A date-only lower bound includes both representations.
            release_start = start_on.isoformat()
            release_end = f"{end_on.isoformat()}T23:59:59.999999+00:00"
            prediction_horizon = max(1, (end_on - now.date()).days)
        else:
            release_start = (now - timedelta(days=days)).isoformat()
            release_end = (now + timedelta(days=max(2, ahead))).isoformat()
            prediction_horizon = ahead

        def published_at(value: object) -> datetime | None:
            raw = str(value or "").strip()
            if not raw:
                return None
            try:
                parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                return None
            return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed

        release_window_start = published_at(release_start)
        release_window_end = published_at(release_end)
        assert release_window_start is not None and release_window_end is not None

        calendar_inputs = database.list_calendar_inputs()
        # A release can cross its announcement/window boundary without a DB
        # write. Expire exactly at the next such transition or UTC midnight.
        expires_at = datetime.combine(
            now.date() + timedelta(days=1), datetime.min.time(), tzinfo=UTC
        ).timestamp()
        for context in calendar_inputs:
            for release in context["chapters"]:
                published = published_at(release.get("publish_at"))
                if published is None:
                    continue
                transitions = [published.timestamp()]
                if start_on is None:
                    transitions.extend(
                        (
                            (published + timedelta(days=days)).timestamp() + 0.000001,
                            (published - timedelta(days=max(2, ahead))).timestamp(),
                        )
                    )
                expires_at = min(
                    [
                        expires_at,
                        *(moment for moment in transitions if moment > now.timestamp()),
                    ]
                )
        inputs_by_manga = {
            str(entry["manga"]["id"]): entry for entry in calendar_inputs
        }
        # Each series follows one unit (chapters or volumes). Work from the
        # complete snapshot so a secondary release cannot take the calendar
        # date merely because its official counterpart falls outside the
        # requested window.
        unit_filtered: list[dict[str, Any]] = []
        policy = _acquisition_policy()
        for context in calendar_inputs:
            # The snapshot already carries every input of the unit choice;
            # embedding it spares three queries per series.
            manga_row = {
                **context["manga"],
                "_unit_context": {
                    "preferred": settings.preferred_unit,
                    "acquisition_policy": policy,
                    **(context.get("unit_context") or {}),
                },
            }
            metadata_row = context["metadata"]
            calendar_candidates: list[dict[str, Any]] = []
            for release in context["chapters"]:
                candidate = dict(release)
                host = (
                    (urlsplit(str(candidate.get("source_url") or "")).hostname or "")
                    .casefold()
                    .removeprefix("www.")
                )
                if host:
                    source_key = str(
                        candidate.get("source_key")
                        or candidate.get("source_name")
                        or candidate.get("provider")
                        or ""
                    )
                    candidate["source_key"] = f"{source_key}@{host}"
                calendar_candidates.append(candidate)
            calendar_hosts = official_hosts(
                ((metadata_row or {}).get("data") or {}).get("official_links"),
                language=str(manga_row.get("preferred_language") or ""),
            )
            unit_info, kept = select_releases(
                manga_row,
                (metadata_row or {}).get("data") or {},
                calendar_candidates,
            )
            kept_ids = {str(release.get("id") or "") for release in kept}
            if unit_info["unit"] == "chapters":
                kept.extend(
                    release
                    for release in calendar_candidates
                    if str(release.get("id") or "") not in kept_ids
                    and str(release.get("numbering_status") or "mapped") == "mapped"
                    and canonical_number(release.get("chapter")) is not None
                    and is_not_yet_released(release)
                    and is_official_release(release, calendar_hosts)
                )
            unit_filtered.extend(
                release
                for release in kept
                if str(release.get("numbering_status") or "mapped") == "mapped"
            )
        # One row per canonical unit. Acquisition policy is intentionally not
        # consulted: it selects a file, never a chapter identity or date.
        grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
        for release in unit_filtered:
            label = canonical_number(release.get("chapter")) or ""
            volume = canonical_number(release.get("volume")) or "" if not label else ""
            if not label and not volume:
                continue
            grouped.setdefault((str(release["manga_id"]), label, volume), []).append(
                release
            )
        releases: list[dict[str, Any]] = []
        hosts_by_manga: dict[str, frozenset[str]] = {}
        for candidates in grouped.values():
            manga_key = str(candidates[0]["manga_id"])
            if manga_key not in hosts_by_manga:
                context = inputs_by_manga.get(manga_key)
                metadata_row = context["metadata"] if context is not None else None
                hosts_by_manga[manga_key] = official_hosts(
                    ((metadata_row or {}).get("data") or {}).get("official_links"),
                    language=str(candidates[0].get("language") or ""),
                )
            hosts = hosts_by_manga[manga_key]
            official = [
                item
                for item in candidates
                if is_official_release(item, hosts)
                and published_at(item.get("publish_at"))
            ]
            # With a mapped publisher platform, only its date is authoritative.
            # A secondary-only candidate is represented on the predicted
            # official date below as "early available".
            if hosts and not official:
                continue
            context = inputs_by_manga.get(manga_key)
            if not official and context is not None:
                publication = publication_summary(
                    {
                        **context["manga"],
                        "publication_signals": context["publication_signals"],
                    },
                    (context["metadata"] or {}).get("data") or {},
                )
                # A recent scan upload is not evidence that a paused or ended
                # publication released a new chapter. Keep dated official
                # history, but do not present secondary reuploads as news.
                if publication["status"] == "ended" or publication.get("paused"):
                    continue
            dated = official or [
                item for item in candidates if published_at(item.get("publish_at"))
            ]
            if not dated:
                continue
            best = min(
                dated,
                key=lambda item: (
                    published_at(item.get("publish_at"))
                    or datetime.max.replace(tzinfo=UTC),
                    str(item.get("id") or ""),
                ),
            )
            release_date = published_at(best.get("publish_at"))
            if (
                release_date is None
                or release_date < release_window_start
                or release_date > release_window_end
            ):
                continue
            entry = {
                k: v
                for k, v in best.items()
                if k not in {"source_key", "source_name", "provider"}
            }
            context = inputs_by_manga.get(manga_key)
            manga_row = context["manga"] if context is not None else {}
            metadata_row = context["metadata"] if context is not None else None
            metadata_data = (metadata_row or {}).get("data") or {}
            entry["manga_title"] = manga_row.get("title")
            if metadata_row is not None and metadata_row.get("artwork_path"):
                entry["manga_cover_url"] = series_artwork_url(
                    manga_key,
                    normalized_artwork_sha256(metadata_row.get("artwork_sha256")),
                )
            else:
                entry["manga_cover_url"] = metadata_data.get(
                    "cover_url"
                ) or manga_row.get("cover_url")
            entry["downloaded"] = any(
                bool(item.get("downloaded")) for item in candidates
            )
            if entry["downloaded"]:
                entry["availability_status"] = "downloaded"
            elif release_date > now:
                entry["availability_status"] = "expected"
            elif official:
                entry["availability_status"] = "official_available"
            else:
                entry["availability_status"] = "early_available"
            entry["release_count"] = len(candidates)
            releases.append(entry)
        releases.sort(key=lambda item: str(item.get("publish_at") or ""), reverse=True)
        expected: list[dict[str, Any]] = []
        for context in calendar_inputs:
            item = context["manga"]
            if item.get("monitor_mode") not in FUTURE_MONITOR_MODES:
                continue
            # Only the publisher's own release dates make a calendar entry:
            # works without a mapped official platform have no schedule.
            metadata_row = context["metadata"]
            metadata_data = (metadata_row or {}).get("data") or {}
            hosts = official_hosts(
                metadata_data.get("official_links"),
                language=str(item.get("preferred_language") or ""),
            )
            chapters = context["chapters"]
            official_history = [
                {
                    "chapter": row.get("chapter"),
                    "release_date": row.get("publish_at"),
                }
                for row in chapters
                if hosts
                and str(row.get("numbering_status") or "mapped") == "mapped"
                and row.get("chapter")
                and is_official_release(row, hosts)
                and (released := published_at(row.get("publish_at"))) is not None
                and released <= now
            ]
            if len(official_history) < 4:
                continue  # only the publisher's own dates make a calendar
            # A paused or finished work has no expected chapter, whatever its
            # past rhythm says (unORDINARY: "On hiatus" on Webtoons, and the
            # calendar kept promising the next Thursday).
            publication = publication_summary(
                {
                    **item,
                    "publication_signals": context["publication_signals"],
                },
                metadata_data,
            )
            if publication["status"] == "ended" or publication.get("paused"):
                continue
            history = official_history
            predictions = expected_releases(
                history, today=now.date(), horizon_days=prediction_horizon
            )
            future_official = {
                str(canonical_number(row.get("chapter"))): released
                for row in chapters
                if hosts
                and str(row.get("numbering_status") or "mapped") == "mapped"
                and row.get("chapter")
                and is_official_release(row, hosts)
                and (released := published_at(row.get("publish_at"))) is not None
                and released > now
            }
            if not predictions and not future_official:
                continue
            known: dict[str, dict[str, bool]] = {}
            for row in chapters:
                if str(row.get("numbering_status") or "mapped") != "mapped":
                    continue
                chapter_key = str(canonical_number(row.get("chapter")) or "")
                if not chapter_key:
                    continue
                state = known.setdefault(
                    chapter_key,
                    {
                        "downloaded": False,
                        "official_available": False,
                        "early_available": False,
                    },
                )
                state["downloaded"] = state["downloaded"] or bool(row.get("downloaded"))
                released = published_at(row.get("publish_at"))
                available_now = released is None or released <= now
                if available_now and is_official_release(row, hosts):
                    state["official_available"] = True
                elif available_now:
                    state["early_available"] = True
            if metadata_row is not None and metadata_row.get("artwork_path"):
                cover_url = series_artwork_url(
                    str(item["id"]),
                    normalized_artwork_sha256(metadata_row.get("artwork_sha256")),
                )
            else:
                cover_url = metadata_data.get("cover_url") or item.get("cover_url")
            for prediction in predictions:
                if prediction.chapter in future_official:
                    continue
                if (
                    start_on is not None
                    and end_on is not None
                    and not start_on <= prediction.expected_at <= end_on
                ):
                    continue
                state = known.get(
                    prediction.chapter,
                    {
                        "downloaded": False,
                        "official_available": False,
                        "early_available": False,
                    },
                )
                # Under an official acquisition policy a scanlator running
                # ahead is not an availability: those releases are exactly
                # what the download gate refuses, so advertising them turns
                # the calendar into an invitation to lower the policy. The
                # entry stays "expected" on the publisher's own date. Only
                # first_available treats an early release as acquirable news.
                early_counts = (
                    database.source_ranking.acquisition_policy == "first_available"
                )
                availability_status = (
                    "downloaded"
                    if state["downloaded"]
                    else "official_available"
                    if state["official_available"]
                    else "early_available"
                    if state["early_available"] and early_counts
                    else "expected"
                )
                expected.append(
                    {
                        "manga_id": item["id"],
                        "manga_title": item.get("title"),
                        "manga_cover_url": cover_url,
                        "chapter": prediction.chapter,
                        "expected_at": prediction.expected_at.isoformat(),
                        "cadence_days": prediction.cadence_days,
                        "cadence_label": prediction.cadence_label,
                        "last_chapter": prediction.last_chapter,
                        "last_release_at": prediction.last_release_at.isoformat(),
                        "availability_status": availability_status,
                        "available": availability_status
                        in {"early_available", "official_available", "downloaded"},
                        "downloaded": state["downloaded"],
                        # The date is a projection of the work's own rhythm,
                        # never a publisher announcement.
                        "estimated": True,
                        "overdue_days": prediction.overdue_days,
                    }
                )
        expected.sort(
            key=lambda entry: (entry["expected_at"], str(entry["manga_title"]))
        )
        result = {"releases": releases, "expected": expected}
        if _with_expiry:
            result["_expires_at"] = expires_at
        return result

    app.state.render_calendar = render_calendar

    async def cached_calendar_payload(
        *,
        days: int,
        ahead: int,
        start_on: date | None,
        end_on: date | None,
        revision: tuple[str, ...],
    ) -> bytes:
        key = (
            str(days),
            str(ahead),
            start_on.isoformat() if start_on is not None else "",
            end_on.isoformat() if end_on is not None else "",
            *revision,
        )
        cached = calendar_cache.get(key)
        if cached is not None and cached[1] > datetime.now(UTC).timestamp():
            calendar_cache.move_to_end(key)
            return cached[0]
        async with calendar_cache_lock:
            cached = calendar_cache.get(key)
            if cached is not None and cached[1] > datetime.now(UTC).timestamp():
                calendar_cache.move_to_end(key)
                return cached[0]

            def build_payload() -> tuple[bytes, float]:
                rendered = render_calendar(
                    days=days,
                    ahead=ahead,
                    start_on=start_on,
                    end_on=end_on,
                    _with_expiry=True,
                )
                expires_at = rendered.pop("_expires_at")
                payload = json.dumps(
                    rendered, ensure_ascii=False, separators=(",", ":")
                ).encode("utf-8")
                return payload, expires_at

            payload, expires_at = await run_api_blocking(build_payload)
            calendar_cache[key] = payload, expires_at
            calendar_cache.move_to_end(key)
            while len(calendar_cache) > calendar_cache_limit:
                calendar_cache.popitem(last=False)
            return payload

    @app.get("/api/calendar")
    async def calendar(
        request: Request,
        days: int = Query(default=14, ge=1, le=90),
        ahead: int = Query(default=14, ge=1, le=60),
        start: date | None = Query(default=None),
        end: date | None = Query(default=None),
    ):
        if (start is None) != (end is None):
            raise HTTPException(
                status_code=422, detail="start and end must be supplied together"
            )
        if start is not None and end is not None:
            if end < start:
                raise HTTPException(
                    status_code=422, detail="end must not precede start"
                )
            if (end - start).days > 31:
                raise HTTPException(
                    status_code=422, detail="calendar windows cannot exceed 32 days"
                )
        revision = (
            *await run_revision(database.calendar_revision),
            database.source_ranking.acquisition_policy,
            datetime.now(UTC).date().isoformat(),
        )
        payload = await cached_calendar_payload(
            days=days,
            ahead=ahead,
            start_on=start,
            end_on=end,
            revision=revision,
        )
        etag = snapshot_validators.etag("calendar", payload)
        headers = {"ETag": etag, "Cache-Control": "private, no-cache"}
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers=headers)
        return Response(
            content=payload,
            media_type="application/json",
            headers=headers,
        )

    @app.patch("/api/manga/{manga_id}/chapters/{chapter_id}/monitored")
    async def set_chapter_monitored(
        manga_id: str, chapter_id: str, request: MonitorChapterRequest
    ):
        try:
            chapter = database.get_chapter(chapter_id)
            if chapter["manga_id"] != manga_id:
                raise KeyError(chapter_id)
            updated = database.set_chapter_monitored(chapter_id, request.monitored)
            return {"chapter_id": chapter_id, "releases_updated": updated}
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Chapter not found") from exc

    @app.patch("/api/manga/{manga_id}/volumes/{volume}/monitoring")
    async def set_volume_monitoring(
        manga_id: str, volume: str, request: MonitorVolumeRequest
    ):
        try:
            service.assert_mutations_allowed(db_only=True)
            return database.set_volume_monitor_override(manga_id, volume, request.state)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Series not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RecoveryBlocked as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.get("/api/import/scan")
    async def import_scan():
        try:
            return await run_api_blocking(importer.scan)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/import/uploads", status_code=201)
    async def create_import_upload():
        return await run_api_blocking(importer.create_upload)

    @app.put("/api/import/uploads/{upload_id}/files", status_code=201)
    async def upload_import_file(
        upload_id: str,
        request: Request,
        path: str = Query(min_length=1, max_length=1000),
    ):
        content_length: int | None = None
        if raw_length := request.headers.get("content-length"):
            try:
                content_length = int(raw_length)
            except ValueError:
                content_length = None
        try:
            return await importer.store_upload_file(
                upload_id,
                path,
                request.stream(),
                content_length=content_length,
            )
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except FileExistsError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            message = str(exc)
            if "safety limit" in message:
                status = 413
            elif "free space" in message:
                status = 507
            else:
                status = 400
            raise HTTPException(status_code=status, detail=message) from exc
        except OSError as exc:
            raise HTTPException(
                status_code=507, detail=f"Import upload could not be stored: {exc}"
            ) from exc

    @app.get("/api/import/uploads/{upload_id}/scan")
    async def scan_import_upload(upload_id: str):
        try:
            return await run_api_blocking(importer.scan_upload, upload_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.delete("/api/import/uploads/{upload_id}", status_code=204)
    async def delete_import_upload(upload_id: str):
        try:
            await run_api_blocking(importer.delete_upload, upload_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return Response(status_code=204)

    @app.post("/api/import", status_code=202)
    async def start_import(request: ImportRequest):
        try:
            service.assert_mutations_allowed()
            return importer.start(
                [group.model_dump() for group in request.groups], request.language
            )
        except RecoveryBlocked as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(
                status_code=404, detail="Target series not found"
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/manga/{manga_id}/manual-import", status_code=201)
    async def manual_import_existing_series(
        manga_id: str, request: ManualImportRequest
    ):
        language = enabled_search_language(request.language)
        try:
            return await importer.import_existing_series_archive(
                manga_id,
                request.path,
                language,
                [part.model_dump() for part in request.parts],
                source_url=request.source_url,
                source_name=request.source_name,
                confirm_language=request.confirm_language,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Manga not found") from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except LanguageReviewRequired as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": str(exc),
                    "language_evidence": exc.evidence,
                },
            ) from exc
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/import/status")
    async def import_status():
        return importer.state

    @app.post("/api/monitor/run")
    async def run_monitor():
        try:
            service.assert_mutations_allowed()
            return await monitor.run_cycle()
        except RecoveryBlocked as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.get("/api/library/orphans")
    async def list_library_orphans():
        return service.library_orphans()

    @app.post("/api/library/orphans/delete")
    async def delete_library_orphans(request: LibraryOrphanDeleteRequest):
        try:
            return await service.delete_library_orphans(request.folders)
        except LibraryUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.post("/api/library/organize")
    async def organize_library(dry_run: bool = Query(default=False)):
        result = await service.organize_library(dry_run=dry_run)
        if result["organization_blocked"]:
            raise HTTPException(status_code=409, detail=result)
        return result

    def operation_probes():
        checks = {name: item.healthcheck for name, item in providers.items()}
        if settings.prowlarr_enabled:
            checks["Prowlarr"] = prowlarr.probe
        if settings.internet_archive_enabled:
            checks["Internet Archive"] = internet_archive.probe
        if settings.qbittorrent_url:
            checks["qBittorrent"] = qbittorrent.probe
        if settings.sabnzbd_url:
            checks["SABnzbd"] = sabnzbd.probe
        if effective_reader_kind(settings) not in {"none", "url"}:
            checks["Reader"] = test_reader
        return checks

    register_operations_routes(
        app,
        settings=settings,
        database=database,
        service=service,
        add_manga=add_manga,
        reader_link=get_manga_reader_link,
        probes=operation_probes,
        maintenance=maintenance,
    )

    register_chapter_map_routes(app, database, service)
    register_native_reader_routes(app, settings, database)
    assembler = register_assemble_routes(app, database, service)
    assembly_batch = BookAssemblyBatch(
        assembler, busy=lambda: bool(importer.state.get("running"))
    )
    register_assembly_batch_routes(app, assembly_batch)
    monitor.book_assembly = assembly_batch
    app.state.book_assembly = assembly_batch
    app.state.series_audit = register_audit_routes(app, database, service)
    from tankarr.translation_routes import register_translation_routes

    register_translation_routes(app, translations, monitor)

    # ------------------------------------------------------------ tasks
    tasks = TaskRegistry()

    def _release_monitor_status() -> dict[str, Any]:
        status = monitor.status()
        return {
            "enabled": bool(status["enabled"]),
            "last_run_at": status["last_cycle_at"],
            "last_error": status["last_cycle_error"],
        }

    def _wanted_search_status() -> dict[str, Any]:
        status = monitor.status()["wanted_search"]
        return {
            "enabled": bool(status["enabled"]),
            "running": bool(status["running"]),
            "last_run_at": status["last_search_at"],
            "next_run_at": status["next_search_at"],
            "last_error": status["last_error"],
            "last_result": status["last_result"],
        }

    def _metadata_status() -> dict[str, Any]:
        status = metadata.status()
        return {
            "enabled": bool(status["enabled"]),
            "running": bool(status["running"]),
            "last_run_at": status["last_cycle_at"],
            "last_error": status["last_cycle_error"],
        }

    def _download_clients_status() -> dict[str, Any]:
        status = torrents.status()
        return {
            "enabled": bool(status["configured"]),
            "last_run_at": status["last_poll_at"],
            "last_error": status["last_error"],
        }

    def _orphan_sweep_status() -> dict[str, Any]:
        sweep = torrents.last_orphan_sweep or {}
        return {
            "enabled": bool(qbittorrent.configured),
            "last_run_at": sweep.get("at"),
            "last_result": sweep or None,
        }

    def _maintenance_status() -> dict[str, Any]:
        jobs = maintenance.status()["jobs"]
        attempts = [
            job["last_attempt_at"]
            for job in jobs.values()
            if job.get("last_attempt_at")
        ]
        errors = [job["error"] for job in jobs.values() if job.get("error")]
        return {
            "enabled": not settings.restored_safe_mode,
            "running": any(job.get("status") == "running" for job in jobs.values()),
            "last_run_at": max(attempts) if attempts else None,
            "last_error": errors[0] if errors else None,
            "last_result": {name: job.get("status") for name, job in jobs.items()},
        }

    def _komga_status() -> dict[str, Any]:
        status = service.komga_refresh_status()
        last = status.get("last_result") or {}
        due_in = status.get("next_due_in_seconds")
        return {
            "enabled": bool(status["enabled"]),
            "last_run_at": last.get("attempted_at") or last.get("completed_at"),
            "next_run_at": (
                (datetime.now(UTC) + timedelta(seconds=float(due_in))).isoformat(
                    timespec="seconds"
                )
                if status["enabled"] and due_in is not None
                else None
            ),
            "last_error": last.get("error")
            if last and not last.get("ready", True)
            else None,
        }

    def _update_status() -> dict[str, Any]:
        status = updates.status()
        return {
            "enabled": bool(status["enabled"]),
            "last_run_at": status["checked_at"],
            "last_error": status["error"],
            "last_result": {
                "latest": status["latest"],
                "update_available": status["update_available"],
            },
        }

    def _suwayomi_status() -> dict[str, Any]:
        return {
            "enabled": bool(settings.suwayomi_managed and suwayomi_runtime.installed()),
            "last_run_at": suwayomi_runtime.last_update_check_at,
        }

    async def _metadata_refresh_now() -> dict[str, Any]:
        return metadata.start_bulk_refresh(force=True)

    async def _komga_refresh_now() -> dict[str, Any]:
        if not service.komga_refresh_status()["enabled"]:
            raise RuntimeError("Komga is not configured")
        return await service.refresh_komga_library(reason="manual")

    for task in (
        ScheduledTask(
            "release_monitor",
            "Release monitor",
            "Checks monitored series for new chapters and queues them.",
            status=_release_monitor_status,
            run=lambda: monitor.run_cycle(),
            interval_seconds=lambda: settings.monitor_interval_seconds,
        ),
        ScheduledTask(
            "wanted_search",
            "Wanted recovery",
            "Searches every channel for the releases still missing from the library.",
            status=_wanted_search_status,
            run=lambda: monitor.search_wanted(trigger="manual"),
            interval_seconds=lambda: settings.wanted_search_interval_seconds,
        ),
        ScheduledTask(
            "metadata_refresh",
            "Metadata refresh",
            "Asks the catalogues again about every series whose record has aged.",
            status=_metadata_status,
            run=_metadata_refresh_now,
            interval_seconds=lambda: settings.metadata_refresh_interval_hours * 3600,
        ),
        ScheduledTask(
            "download_clients",
            "Download clients",
            "Follows the downloads handed to qBittorrent, SABnzbd and archive.org and imports the finished ones.",
            status=_download_clients_status,
            run=lambda: torrents.poll_once(),
            interval_seconds=lambda: settings.torrent_poll_interval_seconds,
        ),
        ScheduledTask(
            "orphan_sweep",
            "Torrent orphan sweep",
            "Removes Tankarr's own torrents whose download was deleted; reports foreign ones.",
            status=_orphan_sweep_status,
            run=lambda: torrents.sweep_orphans(),
            interval_seconds=lambda: ORPHAN_SWEEP_INTERVAL_SECONDS,
        ),
        ScheduledTask(
            "nightly_maintenance",
            "Nightly maintenance",
            "Backup, recycle-bin purge and the library repair check, while nothing is downloading.",
            status=_maintenance_status,
            run=lambda: maintenance.run_once(force=True),
            schedule="Every night between 03:00 and 06:00",
        ),
        ScheduledTask(
            "komga_refresh",
            "Komga refresh",
            "Scans the linked Komga library and reapplies Tankarr's metadata and covers.",
            status=_komga_status,
            run=_komga_refresh_now,
            interval_seconds=lambda: settings.komga_refresh_interval_minutes * 60,
        ),
        ScheduledTask(
            "update_check",
            "Update check",
            "Asks GitHub whether a newer Tankarr release exists.",
            status=_update_status,
            run=lambda: updates.check(),
            interval_seconds=lambda: 86400,
        ),
        ScheduledTask(
            "suwayomi_maintenance",
            "Managed Suwayomi maintenance",
            "Updates the managed Suwayomi server and its extensions when a release is out.",
            status=_suwayomi_status,
            run=lambda: maintain_managed_suwayomi(force=True),
            interval_seconds=lambda: 86400,
        ),
    ):
        tasks.register(task)

    @app.get("/api/system/tasks")
    async def list_system_tasks():
        return {"tasks": tasks.snapshot()}

    @app.post("/api/system/tasks/{task_id}/run")
    async def run_system_task(task_id: str):
        try:
            return await tasks.run(task_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Unknown task") from exc
        except TaskBusy as exc:
            raise HTTPException(
                status_code=409, detail=f"{exc} is already running"
            ) from exc
        except TaskNotRunnable as exc:
            raise HTTPException(
                status_code=409, detail=f"{exc} cannot be started by hand"
            ) from exc

    frontend_dist = settings.frontend_dir or (
        Path(__file__).resolve().parent.parent / "frontend" / "dist"
    )
    if frontend_dist.exists():
        assets = frontend_dist / "assets"
        if assets.exists():
            app.mount("/assets", VersionedStaticFiles(directory=assets), name="assets")

        spa_headers = {
            "Cache-Control": "no-store, max-age=0",
            "Pragma": "no-cache",
        }

        @app.get("/{full_path:path}", include_in_schema=False)
        async def spa(full_path: str):
            requested = frontend_dist / full_path
            if (
                full_path
                and requested.is_file()
                and frontend_dist in requested.resolve().parents
            ):
                headers = spa_headers if requested.name == "index.html" else None
                return FileResponse(requested, headers=headers)
            return FileResponse(frontend_dist / "index.html", headers=spa_headers)

    return app
