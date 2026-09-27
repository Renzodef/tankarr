from __future__ import annotations

import asyncio
import logging
import os
import shutil
import uuid
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from tankarr.database import Database, local_release_identity
from tankarr.importer import (
    LanguageReviewRequired,
    LibraryImporter,
    TorrentImportAmbiguous,
)
from tankarr.internet_archive import PROTOCOL as DIRECT_PROTOCOL
from tankarr.internet_archive import PROVIDER_NAME as DIRECT_PROVIDER
from tankarr.metadata.service import MetadataService
from tankarr.prowlarr import ProwlarrClient
from tankarr.qbittorrent import QBitTorrentClient, QBitTorrentError
from tankarr.release_kind import classify_release
from tankarr.sabnzbd import SABnzbdError
from tankarr.series_unit import is_volume_release, normalize_series_unit
from tankarr.service import ExternalImportConflict, TankarrService
from tankarr.torrent_sources import TORRENT_IMPORT_PROVIDERS

POLLABLE_STATUSES = (
    "adding",
    "queued",
    "downloading",
    "checking",
    "completed",
    "importing",
)
COMPLETE_QBIT_STATES = frozenset(
    {
        "uploading",
        "stalledup",
        "queuedup",
        "pausedup",
        "stoppedup",
        "forcedup",
        "checkingup",
    }
)
CHECKING_QBIT_STATES = frozenset({"checkingdl", "checkingup", "checkingresumedata"})
# qBittorrent pauses/stops a finished torrent once its ratio or seeding-time
# limits are reached: that is "seeding done" for the remove_when_seeded action.
SEEDED_QBIT_STATES = frozenset({"pausedup", "stoppedup"})
ORPHAN_SWEEP_INTERVAL_SECONDS = 3600.0
REMOVED_EXTERNALLY = "Removed from qBittorrent outside Tankarr; pick another release or Retry to add it again"
FAILED_QBIT_STATES = frozenset({"error", "missingfiles", "unknown"})
# Still fetching the torrent's metadata from the swarm: nothing has been
# downloaded and nothing can be until a peer answers.
METADATA_QBIT_STATES = frozenset({"metadl", "forcedmetadl"})
DISCOVERY_SERIES_ID = "__add_new__"


logger = logging.getLogger(__name__)


class TorrentManager:
    """Coordinate indexer releases through qBittorrent and library import."""

    def __init__(
        self,
        database: Database,
        service: TankarrService,
        importer: LibraryImporter,
        metadata: MetadataService,
        prowlarr: ProwlarrClient,
        qbittorrent: QBitTorrentClient,
        sabnzbd: Any = None,
        internet_archive: Any = None,
    ):
        self.database = database
        self.service = service
        self.importer = importer
        self.metadata = metadata
        self.prowlarr = prowlarr
        self.qbittorrent = qbittorrent
        self.sabnzbd = sabnzbd
        self.internet_archive = internet_archive
        # Direct downloads Tankarr performs itself, by job id.
        self._direct_tasks: dict[int, asyncio.Task[None]] = {}
        self.task: asyncio.Task[None] | None = None
        self.last_poll_at: str | None = None
        self.last_error: str | None = None
        self.last_orphan_sweep: dict[str, Any] | None = None
        self._last_orphan_sweep_at: float | None = None
        self._operation_lock = asyncio.Lock()
        self._release_cache: dict[
            tuple[str, str, str], tuple[float, dict[str, Any]]
        ] = {}

    @property
    def settings(self):
        return self.service.settings

    @property
    def direct_available(self) -> bool:
        return bool(
            self.internet_archive is not None
            and getattr(self.internet_archive, "enabled", False)
        )

    @property
    def direct_download_dir(self) -> Path:
        root = getattr(self.settings, "data_dir", None)
        if root is None:
            raise RuntimeError("Direct downloads need a data directory")
        return Path(root) / "direct-downloads"

    @staticmethod
    def _is_direct(job: dict[str, Any]) -> bool:
        return str(job.get("protocol") or "torrent") == DIRECT_PROTOCOL

    async def start(self) -> None:
        if self.task is not None and not self.task.done():
            return
        self.task = asyncio.create_task(self._run(), name="tankarr-torrent-monitor")

    async def stop(self) -> None:
        if self.task is None:
            return
        self.task.cancel()
        with suppress(asyncio.CancelledError):
            await self.task
        self.task = None

    async def _run(self) -> None:
        while True:
            try:
                await self.poll_once()
                self.last_error = None
            except Exception as exc:  # noqa: BLE001 - durable jobs remain retryable
                self.last_error = f"{type(exc).__name__}: {exc}"[:500]
            loop_now = asyncio.get_running_loop().time()
            if (
                self._last_orphan_sweep_at is None
                or loop_now - self._last_orphan_sweep_at
                >= ORPHAN_SWEEP_INTERVAL_SECONDS
            ):
                self._last_orphan_sweep_at = loop_now
                try:
                    await self.sweep_orphans()
                except Exception as exc:  # noqa: BLE001 - next hour retries
                    self.last_orphan_sweep = {
                        "at": datetime.now(UTC).isoformat(),
                        "error": f"{type(exc).__name__}: {exc}"[:300],
                    }
            await asyncio.sleep(self.settings.torrent_poll_interval_seconds)

    @property
    def completed_action(self) -> str:
        return str(getattr(self.settings, "torrent_completed_action", "seed") or "seed")

    async def _remove_from_client(
        self, job: dict[str, Any], note: str
    ) -> dict[str, Any]:
        if str(job.get("protocol") or "torrent") == "usenet":
            if self.sabnzbd is not None and job.get("client_id"):
                await self.sabnzbd.delete(str(job["client_id"]), delete_files=True)
        else:
            await self.qbittorrent.delete_torrent(job["info_hash"], delete_files=True)
        return self.database.update_torrent_download(
            job["id"],
            qbit_state="removed",
            message=f"{job.get('message') or 'Imported'} · {note}",
        )

    async def _finish_seeded(self) -> int:
        """remove_when_seeded: drop imported torrents qBittorrent has paused
        at its ratio/time limits. Nothing happens under the other actions."""

        if self.completed_action != "remove_when_seeded":
            return 0
        removed = 0
        for job in self.database.list_torrent_downloads(
            statuses=("imported",), limit=200
        ):
            if str(job.get("qbit_state") or "").casefold() == "removed":
                continue
            if (job.get("language_evidence") or {}).get("translation_request"):
                translations = getattr(self, "translations", None)
                if translations is None or not translations.can_finish_source(job):
                    continue
            if str(job.get("protocol") or "torrent") in {"usenet", DIRECT_PROTOCOL}:
                continue
            current = await self.qbittorrent.torrent_info(job["info_hash"])
            if current is None:
                self.database.update_torrent_download(job["id"], qbit_state="removed")
                continue
            if str(current.get("state") or "").casefold() in SEEDED_QBIT_STATES:
                await self._remove_from_client(
                    job, "seeding finished, removed from qBittorrent"
                )
                removed += 1
        return removed

    async def _recycle_rejected_payload(self, job: dict[str, Any]) -> dict[str, Any]:
        """Detach a refused grab without deleting bytes, then retain its payload."""
        from tankarr.download_recycle import recycle_payload

        self.service.assert_mutations_allowed()
        try:
            if not job.get("content_path"):
                raise ValueError("The rejected download has no recorded payload path")
            content = Path(str(job["content_path"]))
            if self._is_direct(job):
                root = self.direct_download_dir
                content = root / str(job["id"])
            else:
                key = (
                    "usenet_download_dir"
                    if job.get("protocol") == "usenet"
                    else "torrent_download_dir"
                )
                configured = getattr(self.settings, key, None)
                if configured is None:
                    raise ValueError("The download mount is not configured")
                root = Path(configured)
            resolved = content.resolve()
            resolved_root = root.resolve()
            if resolved == resolved_root or not resolved.is_relative_to(resolved_root):
                raise ValueError("The payload is outside its configured download mount")
            library = getattr(self.settings, "library_dir", None)
            if library is not None:
                library = Path(library).resolve()
                if resolved.is_relative_to(library) or library.is_relative_to(resolved):
                    raise ValueError("A download payload cannot contain library files")
            with self.database.connect() as connection:
                references = connection.execute(
                    "SELECT id, qbit_state, content_path FROM torrent_download "
                    "WHERE content_path IS NOT NULL AND content_path <> ''"
                ).fetchall()
            for reference in references:
                other = dict(reference)
                if other["id"] == job["id"] or other.get("qbit_state") == "removed":
                    continue
                if other.get("content_path"):
                    other_path = Path(str(other["content_path"])).resolve()
                    if resolved.is_relative_to(other_path) or other_path.is_relative_to(
                        resolved
                    ):
                        raise ValueError(
                            "Another download still references this payload"
                        )
            if not os.access(resolved_root, os.W_OK):
                return await self._retire_on_read_only_mount(job)
            if not self._is_direct(job):
                if job.get("protocol") == "usenet":
                    if self.sabnzbd is None or not job.get("client_id"):
                        raise ValueError("The Usenet client is unavailable")
                    await self.sabnzbd.delete(str(job["client_id"]), delete_files=False)
                else:
                    await self.qbittorrent.delete_torrent(
                        job["info_hash"], delete_files=False
                    )
            result = await asyncio.to_thread(
                recycle_payload,
                root,
                content,
                int(job["id"]),
                attempt_key=(job.get("language_evidence") or {}).get(
                    "payload_retirement_key"
                ),
            )
            if result.get("source_recreated"):
                raise ValueError(
                    "A different payload appeared after this retirement; it was preserved"
                )
            evidence = dict(job.get("language_evidence") or {})
            evidence["retired_payload"] = result["path"]
            return self.database.update_torrent_download(
                job["id"],
                qbit_state="removed",
                language_evidence=evidence,
                message=f"{job.get('message') or 'Import refused'} · payload moved to the recycle bin",
            )
        except Exception as exc:  # noqa: BLE001 - client and storage failures retry durably
            logger.warning(
                "Rejected payload retirement pending for job %s: %s", job["id"], exc
            )
            return self.database.update_torrent_download(
                job["id"], qbit_state="recycle_pending"
            )

    async def _retire_on_read_only_mount(self, job: dict[str, Any]) -> dict[str, Any]:
        """Keep refused bytes for review when Tankarr cannot move them.

        An OCR refusal can be wrong, and a client may own other files under
        the same download directory. Deleting the client's data here can
        destroy usable books before an operator can inspect them.
        """

        note = "payload retained on the read-only download mount for review"
        evidence = dict(job.get("language_evidence") or {})
        evidence["retired_payload"] = str(job["content_path"])
        return self.database.update_torrent_download(
            job["id"],
            qbit_state="review_required",
            language_evidence=evidence,
            message=f"{job.get('message') or 'Import refused'} · {note}",
        )

    def _metadata_timed_out(self, job: dict[str, Any], current: dict[str, Any]) -> bool:
        if str(current.get("state") or "").casefold() not in METADATA_QBIT_STATES:
            return False
        raw = str(job.get("created_at") or "")
        try:
            started = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return False
        if started.tzinfo is None:
            started = started.replace(tzinfo=UTC)
        minutes = int(
            getattr(self.settings, "torrent_metadata_timeout_minutes", 30) or 30
        )
        return datetime.now(UTC) - started >= timedelta(minutes=minutes)

    async def _abandon_metadata_stall(self, job: dict[str, Any]) -> dict[str, Any]:
        """Give up on a magnet no peer will describe, so the slot moves on.

        The failed job keeps the hash: the hunt skips a release that already
        has a job, so the next cycle picks another candidate instead of
        re-adding the same dead swarm.
        """

        minutes = int(
            getattr(self.settings, "torrent_metadata_timeout_minutes", 30) or 30
        )
        try:
            await self.qbittorrent.delete_torrent(job["info_hash"], delete_files=True)
        except Exception as exc:  # noqa: BLE001 - the job fails either way
            logger.warning(
                "Metadata-stalled torrent %s not removed: %s", job["info_hash"], exc
            )
        return self.database.update_torrent_download(
            job["id"],
            status="failed",
            qbit_state="metadata_timeout",
            message=(
                f"No peer delivered the torrent metadata in {minutes} minutes; "
                "the release was abandoned and the next candidate will be tried"
            ),
        )

    async def _refuse_import(self, job, content, message, *, evidence=None):
        evidence = dict(
            evidence if evidence is not None else job.get("language_evidence") or {}
        )
        evidence["payload_retirement_key"] = uuid.uuid4().hex
        failed = self.database.update_torrent_download(
            job["id"],
            status="failed",
            progress=1,
            content_path=content,
            message=message,
            qbit_state="recycle_pending",
            language_evidence=evidence,
        )
        return await self._recycle_rejected_payload(failed)

    async def sweep_orphans(self) -> dict[str, Any]:
        """Remove torrents in Tankarr's category that no job references.

        A job Tankarr dropped (series deleted, chapter unmonitored, unit
        changed) can leave its torrent behind; after the grace period the
        torrent and its files go. Torrents outside the category are never
        touched, and anything a job still references is kept.
        """

        if not self.qbittorrent.configured:
            return {"seen": 0, "removed": 0}
        grace_hours = int(
            getattr(self.settings, "torrent_orphan_grace_hours", 24) or 24
        )
        known = {
            str(job["info_hash"]).casefold()
            for job in self.database.list_torrent_downloads(limit=5000)
        }
        now = datetime.now(UTC).timestamp()
        removed: list[str] = []
        torrents = await self.qbittorrent.list_category_torrents()
        for item in torrents:
            info_hash = str(item.get("hash") or "").casefold()
            if not info_hash or info_hash in known:
                continue
            added_on = float(item.get("added_on") or 0)
            if added_on and now - added_on < grace_hours * 3600:
                continue
            await self.qbittorrent.delete_torrent(info_hash, delete_files=True)
            removed.append(str(item.get("name") or info_hash))
        self.last_orphan_sweep = {
            "at": datetime.now(UTC).isoformat(),
            "seen": len(torrents),
            "removed": len(removed),
            "names": removed[:20],
        }
        return {"seen": len(torrents), "removed": len(removed)}

    def status(self) -> dict[str, Any]:
        source_status = {
            "prowlarr": bool(self.prowlarr.enabled and self.prowlarr.configured),
            DIRECT_PROVIDER: self.direct_available,
        }
        configured = bool(
            (source_status["prowlarr"] and self.qbittorrent.configured)
            or source_status[DIRECT_PROVIDER]
        )
        active = self.database.list_torrent_downloads(
            statuses=POLLABLE_STATUSES, limit=500
        )
        review = self.database.list_torrent_downloads(statuses=("review",), limit=500)
        return {
            "enabled": any(source_status.values()),
            "configured": configured,
            "sources": source_status,
            "running": bool(self.task is not None and not self.task.done()),
            "import_mount": self.qbittorrent.import_ready,
            "category": self.settings.qbittorrent_category,
            "active": len(active),
            "review": len(review),
            "completed_action": self.completed_action,
            "sabnzbd_configured": bool(
                self.sabnzbd is not None and self.sabnzbd.configured
            ),
            "orphan_sweep": self.last_orphan_sweep,
            "last_poll_at": self.last_poll_at,
            "last_error": self.last_error,
        }

    async def search(
        self,
        manga_id: str,
        query: str | None = None,
        *,
        limit: int = 50,
        _include_download_ref: bool = False,
        sources: tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        """Search the release sources for one series.

        ``_include_download_ref`` keeps the resolved download reference on
        each result instead of stripping it for the browser. Only a trusted
        internal caller (the volume-pack hunter, which persists results
        into a review a human may accept later) may set it; the public
        ``/torrent-search`` route never does.

        ``sources`` narrows the pass: the chapter rung of the Wanted recovery
        asks the indexers only, because archive.org holds whole books and is
        asked at most once per second - never for a chapter at a time.
        """
        manga = self.database.get_manga(manga_id)
        if manga["preferred_language"] != "en":
            raise ValueError(
                "Torrent import currently requires a series configured in English"
            )
        search_query = " ".join((query or manga["title"]).split()).strip()
        wanted_sources = set(sources or ("prowlarr", DIRECT_PROVIDER))
        prowlarr_available = bool(
            "prowlarr" in wanted_sources
            and self.prowlarr.enabled
            and self.prowlarr.configured
        )
        direct_available = bool(
            DIRECT_PROVIDER in wanted_sources and self.direct_available
        )
        if not prowlarr_available and not direct_available:
            raise ValueError(
                "Enable and configure Prowlarr, or Internet Archive, in Settings"
            )
        errors: list[dict[str, str]] = []
        candidates: list[dict[str, Any]] = []
        source_counts: dict[str, int] = {}

        async def search_source(source: str) -> list[dict[str, Any]]:
            try:
                if source == DIRECT_PROVIDER:
                    # The archive is asked by identity, not by the indexer
                    # phrasing: the work's own title, once per pass.
                    batch = await self.internet_archive.search(
                        str(manga["title"]), "en", limit=limit
                    )
                else:
                    batch = await self.prowlarr.search(search_query, "en", limit=limit)
            except Exception as exc:  # noqa: BLE001 - fall back to the next source
                errors.append(
                    {
                        "provider": source,
                        "error": f"{type(exc).__name__}: {exc}"[:500],
                    }
                )
                source_counts[source] = 0
                return []
            normalized: list[dict[str, Any]] = []
            for item in batch:
                normalized.append(dict(item))
            source_counts[source] = len(normalized)
            return normalized

        if prowlarr_available:
            candidates.extend(await search_source("prowlarr"))
        if direct_available:
            candidates.extend(await search_source(DIRECT_PROVIDER))
        if errors and not candidates and len(errors) == len(source_counts):
            raise RuntimeError("; ".join(item["error"] for item in errors))

        by_hash: dict[str, dict[str, Any]] = {}
        for release in candidates:
            info_hash = str(release["info_hash"])
            current = by_hash.get(info_hash)
            if current is None or (
                release["provider"] == "prowlarr" and current["provider"] != "prowlarr"
            ):
                by_hash[info_hash] = release
        releases = list(by_hash.values())
        now = asyncio.get_running_loop().time()
        existing = {
            job["info_hash"]: job
            for job in self.database.list_torrent_downloads(
                manga_id=manga_id, limit=500
            )
        }
        for release in releases:
            self._release_cache[(manga_id, release["provider"], release["id"])] = (
                now,
                dict(release),
            )
            job = existing.get(release["info_hash"])
            release["download"] = (
                {
                    "id": job["id"],
                    "status": job["status"],
                    "progress": job["progress"],
                    "message": job["message"],
                }
                if job
                else None
            )
        self._expire_release_cache(now)
        releases.sort(
            key=lambda item: (
                int(item["match_score"]),
                int(item["seeders"]),
                int(item["downloads"]),
            ),
            reverse=True,
        )
        releases = releases[: max(1, min(limit, 75))]
        return {
            "provider": "aggregate",
            "providers": source_counts,
            "errors": errors,
            "query": search_query,
            "results": (
                list(releases)
                if _include_download_ref
                else [
                    {
                        key: value
                        for key, value in release.items()
                        if key != "download_ref"
                    }
                    for release in releases
                ]
            ),
        }

    async def discover(
        self, query: str, language: str, *, limit: int = 50
    ) -> dict[str, Any]:
        """Search Prowlarr before a series exists and retain opaque grab data.

        The public response never contains Prowlarr's authenticated download
        reference. A user must subsequently associate one cached release with a
        canonical Tankarr series before it can be handed to qBittorrent.
        """

        normalized_language = str(language or "").strip().casefold()
        if normalized_language != "en":
            raise ValueError(
                "Prowlarr discovery currently requires English so the imported "
                "release can be verified with OCR"
            )
        normalized_query = " ".join(str(query or "").split()).strip()
        if len(normalized_query) < 2 or len(normalized_query) > 200:
            raise ValueError("Prowlarr search query must contain 2 to 200 characters")
        if not self.prowlarr.enabled or not self.prowlarr.configured:
            raise ValueError("Enable and configure Prowlarr in Settings")

        releases = await self.prowlarr.search(
            normalized_query, normalized_language, limit=limit
        )
        now = asyncio.get_running_loop().time()
        for release in releases:
            self._release_cache[
                (DISCOVERY_SERIES_ID, str(release["provider"]), str(release["id"]))
            ] = (now, dict(release))
        self._expire_release_cache(now)
        return {
            "provider": "aggregate",
            "providers": {"prowlarr": len(releases)},
            "errors": [],
            "query": normalized_query,
            "results": [
                {key: value for key, value in release.items() if key != "download_ref"}
                for release in releases
            ],
        }

    async def grab_discovered(
        self, manga_id: str, provider: str, release_id: str
    ) -> dict[str, Any]:
        """Bind one release-first search result to a canonical series and grab it."""

        normalized_provider = str(provider or "").strip().casefold()
        if normalized_provider != "prowlarr":
            raise ValueError("Add New discovery currently supports Prowlarr releases")
        key = (DISCOVERY_SERIES_ID, normalized_provider, release_id)
        cached = self._release_cache.get(key)
        now = asyncio.get_running_loop().time()
        if cached is None or now - cached[0] > 900:
            raise ValueError("Search Add New again before selecting this release")
        # get_manga validates the canonical target before the opaque release is
        # made available to the normal grab path.
        self.database.get_manga(manga_id)
        self._release_cache[(manga_id, normalized_provider, release_id)] = cached
        return await self.grab(manga_id, normalized_provider, release_id)

    def _expire_release_cache(self, now: float) -> None:
        expired = [
            key for key, (seen, _) in self._release_cache.items() if now - seen > 900
        ]
        for key in expired:
            self._release_cache.pop(key, None)

    async def grab_known_release(
        self, manga_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """Grab a release Tankarr already identified, refreshing its reference.

        An indexer's download reference is short-lived, so a release recorded
        minutes or months ago can no longer be grabbed by reference. The
        release itself has not changed: search again and use the live entry
        with the same info hash.
        """

        provider = str(payload.get("provider") or "prowlarr").casefold()
        release_id = str(payload.get("id") or "")
        info_hash = str(payload.get("info_hash") or "").casefold()
        title = str(payload.get("title") or "").strip()
        key = (manga_id, provider, release_id)
        loop = asyncio.get_running_loop()
        if payload.get("download") or payload.get("magnet_url"):
            self._release_cache[key] = (loop.time(), dict(payload))
            try:
                return await self.grab(*key)
            except ValueError:
                logger.info("Stored reference for %s expired; searching again", title)
        found = await self.search(manga_id, title or None, limit=50)
        releases = found.get("results") or []
        match = next(
            (
                item
                for item in releases
                if str(item.get("info_hash") or "").casefold() == info_hash
            ),
            None,
        ) or next(
            (item for item in releases if str(item.get("title") or "") == title),
            None,
        )
        if match is None:
            raise ValueError(
                "That release is no longer offered by any configured indexer"
            )
        return await self.grab(
            manga_id,
            str(match.get("provider") or provider),
            str(match["id"]),
        )

    async def grab(
        self,
        manga_id: str,
        provider: str,
        release_id: str,
        *,
        translation: dict | None = None,
    ) -> dict[str, Any]:
        self.service.assert_mutations_allowed()
        manga = self.database.get_manga(manga_id)
        if translation is None and manga["preferred_language"] != "en":
            raise ValueError("The series translation language must be English")
        if translation is not None:
            from tankarr.translation_policy import fallback_languages

            if (
                not self.settings.translation_enabled
                or not manga.get("translation_enabled")
                or translation.get("target_language") != manga["preferred_language"]
                or translation.get("source_language")
                not in fallback_languages(
                    manga, self.settings.translation_source_languages
                )
            ):
                raise ValueError(
                    "Translation fallback is disabled or its language profile changed"
                )
        normalized_provider = str(provider or "").casefold()
        if normalized_provider not in TORRENT_IMPORT_PROVIDERS:
            raise ValueError("Unsupported torrent release provider")
        cached = self._release_cache.get((manga_id, normalized_provider, release_id))
        now = asyncio.get_running_loop().time()
        if cached is None or now - cached[0] > 900:
            raise ValueError("Search releases again before selecting this result")
        release = dict(cached[1])
        existing = self.database.get_torrent_download_by_hash(release["info_hash"])
        if existing is not None:
            if existing["manga_id"] != manga_id:
                raise ValueError("This torrent is already assigned to another series")
            existing_translation = (existing.get("language_evidence") or {}).get(
                "translation_request"
            )
            if existing_translation != translation:
                raise ValueError("This download already has a different import purpose")
            # A grab that failed is not an answer to a new one: the operator is
            # asking for this release again, with a reference that works now.
            # Anything still live is returned as it stands.
            if existing["status"] != "failed":
                return existing

        job = self.database.create_torrent_download(manga_id, release)
        if translation is not None:
            job = self.database.update_torrent_download(
                job["id"], language_evidence={"translation_request": translation}
            )
        if str(release.get("protocol") or "torrent") == DIRECT_PROTOCOL:
            self.database.set_torrent_download_protocol(job["id"], DIRECT_PROTOCOL)
            job = self.database.update_torrent_download(
                job["id"],
                status="queued",
                progress=0,
                message="Queued for direct download",
            )
            self._start_direct_download(job)
            return self.database.get_torrent_download(job["id"])
        if str(release.get("protocol") or "torrent") == "usenet":
            self.database.set_torrent_download_protocol(job["id"], "usenet")
            try:
                if self.sabnzbd is None or not self.sabnzbd.configured:
                    raise SABnzbdError(
                        "SABnzbd is not configured: set it under Settings → Indexers & downloads"
                    )
                nzo_id = await self.sabnzbd.add_nzb_url(
                    str(release.get("download_ref") or ""),
                    name=str(release.get("title") or "nzb"),
                )
                return self.database.update_torrent_download(
                    job["id"],
                    status="queued",
                    client_id=nzo_id,
                    message="Queued in SABnzbd",
                )
            except Exception as exc:
                self.database.update_torrent_download(
                    job["id"], status="failed", message=f"{type(exc).__name__}: {exc}"
                )
                raise
        try:
            current = await self.qbittorrent.torrent_info(release["info_hash"])
            if (
                current is not None
                and str(current.get("category") or "")
                != self.settings.qbittorrent_category
            ):
                raise QBitTorrentError(
                    "The same torrent already exists outside Tankarr's qBittorrent category"
                )
            if current is None:
                await self._add_release(release)
                current = await self.qbittorrent.wait_for_torrent(release["info_hash"])
            return self._record_qbit_state(job, current)
        except Exception as exc:
            self.database.update_torrent_download(
                job["id"], status="failed", message=f"{type(exc).__name__}: {exc}"
            )
            raise

    async def poll_once(self) -> dict[str, int]:
        async with self._operation_lock:
            checked = 0
            imported = 0
            # Apply the current per-file rules to unfinished legacy decisions.
            if not getattr(self.settings, "restored_safe_mode", False):
                for failed in self.database.list_torrent_downloads(
                    statuses=("failed",), limit=500
                ):
                    if failed.get("qbit_state") == "recycle_pending":
                        await self._recycle_rejected_payload(failed)
                for previous in self.database.list_torrent_downloads(
                    statuses=("review",), limit=500
                ):
                    self.database.update_torrent_download(
                        previous["id"],
                        status="completed",
                        message="Applying automatic per-file import checks",
                    )
            for job in self.database.list_torrent_downloads(
                statuses=POLLABLE_STATUSES, limit=500
            ):
                checked += 1
                if self._is_direct(job):
                    updated = await self._poll_direct(job)
                    if (
                        updated is not None
                        and updated["status"] == "completed"
                        and self.settings.torrent_auto_import
                    ):
                        result = await self._import(updated, {}, confirm_language=False)
                        imported += int(result["status"] == "imported")
                    continue
                if str(job.get("protocol") or "torrent") == "usenet":
                    updated = await self._poll_usenet(job)
                    if (
                        updated is not None
                        and updated["status"] == "completed"
                        and self.settings.torrent_auto_import
                        and self.sabnzbd is not None
                        and self.sabnzbd.import_ready
                    ):
                        result = await self._import(
                            updated, updated.get("_state") or {}, confirm_language=False
                        )
                        imported += int(result["status"] == "imported")
                    continue
                current = await self.qbittorrent.torrent_info(job["info_hash"])
                if current is None:
                    # Deleted by hand in qBittorrent: the job fails and is not
                    # re-grabbed automatically; Retry re-adds the same release.
                    self.database.update_torrent_download(
                        job["id"],
                        status="failed",
                        qbit_state="removed",
                        message=REMOVED_EXTERNALLY,
                    )
                    continue
                updated = self._record_qbit_state(job, current)
                if updated["status"] == "queued" and self._metadata_timed_out(
                    job, current
                ):
                    await self._abandon_metadata_stall(job)
                    continue
                if (
                    updated["status"] == "completed"
                    and self.settings.torrent_auto_import
                    and self.qbittorrent.import_ready
                ):
                    result = await self._import(
                        updated, current, confirm_language=False
                    )
                    imported += int(result["status"] == "imported")
            seeded = await self._finish_seeded()
            self.last_poll_at = datetime.now(UTC).isoformat()
            result = {"checked": checked, "imported": imported}
            if seeded:
                result["removed_after_seeding"] = seeded
            return result

    # -- direct downloads (archive.org) ----------------------------------

    def _start_direct_download(self, job: dict[str, Any]) -> None:
        job_id = int(job["id"])
        current = self._direct_tasks.get(job_id)
        if current is not None and not current.done():
            return
        self._direct_tasks[job_id] = asyncio.create_task(
            self._direct_download(job), name=f"tankarr-direct-{job_id}"
        )

    def _direct_destination(self, job: dict[str, Any]) -> Path:
        url = str(job.get("torrent_url") or "")
        name = unquote(urlsplit(url).path.rsplit("/", 1)[-1])
        # Decode before checking the basename: an encoded slash must never
        # turn an archive.org filename into an absolute/local filesystem path.
        if any(character in name for character in ("/", "\\", "\0")):
            raise ValueError("Direct download has an unsafe filename")
        job_id = int(job["id"])
        if job_id <= 0:
            raise ValueError("Direct download has an invalid job id")
        if not name or name.startswith("."):
            name = f"{job_id}.cbz"
        root = self.direct_download_dir.resolve()
        directory = root / str(job_id)
        if directory.is_symlink() or (directory / name).is_symlink():
            raise ValueError("Direct download destination must not be a symlink")
        return directory / name

    async def _direct_download(self, job: dict[str, Any]) -> None:
        job_id = int(job["id"])
        client = self.internet_archive
        try:
            if client is None:
                raise RuntimeError("Internet Archive is not configured")
            destination = self._direct_destination(job)
            self.database.update_torrent_download(
                job_id, status="downloading", message="Downloading from archive.org"
            )

            async def report(written: int, total: int) -> None:
                progress = (written / total) if total else 0.0
                self.database.update_torrent_download(
                    job_id,
                    status="downloading",
                    progress=max(0.0, min(progress, 0.999)),
                    message=f"Downloading from archive.org · {written // (1024 * 1024)} MiB",
                )

            path = await client.download(
                str(job["torrent_url"]),
                destination,
                expected_size=int(job.get("size_bytes") or 0),
                progress=report,
            )
            self.database.update_torrent_download(
                job_id,
                status="completed",
                progress=1,
                content_path=path,
                message="Downloaded from archive.org",
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the job records its failure
            self.database.update_torrent_download(
                job_id, status="failed", message=f"{type(exc).__name__}: {exc}"[:500]
            )

    async def _poll_direct(self, job: dict[str, Any]) -> dict[str, Any] | None:
        """Keep a direct job moving: restart what a restart interrupted."""

        job_id = int(job["id"])
        status = str(job.get("status") or "")
        task = self._direct_tasks.get(job_id)
        if status in {"adding", "queued", "downloading", "checking"}:
            if task is None or task.done():
                if not self.direct_available:
                    return self.database.update_torrent_download(
                        job_id,
                        status="failed",
                        message="Internet Archive is disabled in Settings",
                    )
                self._start_direct_download(self.database.get_torrent_download(job_id))
            return None
        if status == "completed":
            content = job.get("content_path")
            if not content or not Path(str(content)).exists():
                return self.database.update_torrent_download(
                    job_id,
                    status="failed",
                    message="The downloaded file is gone; Retry fetches it again",
                )
            return job
        return None

    def _discard_direct_files(self, job: dict[str, Any]) -> None:
        task = self._direct_tasks.pop(int(job["id"]), None)
        if task is not None and not task.done():
            task.cancel()
        try:
            folder = (self.direct_download_dir / str(job["id"])).resolve()
            root = self.direct_download_dir.resolve()
            if folder != root and folder.is_relative_to(root) and folder.exists():
                shutil.rmtree(folder, ignore_errors=True)
        except (RuntimeError, OSError):
            pass

    async def _poll_usenet(self, job: dict[str, Any]) -> dict[str, Any] | None:
        if self.sabnzbd is None or not self.sabnzbd.configured:
            return self.database.update_torrent_download(
                job["id"], status="failed", message="SABnzbd is not configured"
            )
        nzo_id = str(job.get("client_id") or "")
        state = await self.sabnzbd.job_state(nzo_id) if nzo_id else None
        if state is None:
            return self.database.update_torrent_download(
                job["id"],
                status="failed",
                qbit_state="removed",
                message=REMOVED_EXTERNALLY.replace("qBittorrent", "SABnzbd"),
            )
        return self._record_sab_state(job, state)

    def _record_sab_state(
        self, job: dict[str, Any], state: dict[str, Any]
    ) -> dict[str, Any]:
        # Queue slots say "cat", history slots say "category".
        if (
            str(state.get("cat") or state.get("category") or "")
            != self.settings.sabnzbd_category
        ):
            raise SABnzbdError("Download moved outside Tankarr's SABnzbd category")
        raw_status = str(state.get("status") or "").strip()
        normalized = raw_status.casefold()
        if state.get("where") == "queue":
            try:
                progress = max(
                    0.0, min(1.0, float(str(state.get("percentage") or "0")) / 100.0)
                )
            except ValueError:
                progress = 0.0
            if normalized in {"downloading", "fetching"} or progress > 0:
                status, message = (
                    "downloading",
                    f"Downloading from Usenet ({progress * 100:.1f}%)",
                )
            elif normalized in {"paused"}:
                status, message = "queued", "Paused in SABnzbd"
            else:
                status, message = "queued", "Queued in SABnzbd"
        elif normalized == "completed":
            status, progress, message = (
                "completed",
                1.0,
                "Download complete; ready to import",
            )
        elif normalized == "failed":
            status, progress, message = (
                "failed",
                1.0,
                f"SABnzbd: {state.get('fail_message') or 'download failed'}"[:300],
            )
        else:
            status, progress, message = (
                "checking",
                1.0,
                f"SABnzbd is post-processing ({raw_status or 'working'})",
            )
        updated = self.database.update_torrent_download(
            job["id"],
            status=status,
            progress=progress,
            qbit_state=raw_status[:100] or None,
            message=message,
        )
        updated["_state"] = state
        return updated

    def _record_qbit_state(
        self, job: dict[str, Any], current: dict[str, Any]
    ) -> dict[str, Any]:
        if str(current.get("category") or "") != self.settings.qbittorrent_category:
            raise QBitTorrentError(
                "Torrent moved outside Tankarr's qBittorrent category"
            )
        qbit_state = str(current.get("state") or "unknown")
        normalized = qbit_state.casefold()
        progress = max(0.0, min(1.0, float(current.get("progress") or 0)))
        if normalized in FAILED_QBIT_STATES:
            status = "failed"
            message = f"qBittorrent state: {qbit_state}"
        elif progress >= 1 or normalized in COMPLETE_QBIT_STATES:
            status = "completed"
            progress = 1.0
            message = "Download complete; ready to import"
        elif normalized in CHECKING_QBIT_STATES:
            status = "checking"
            message = "qBittorrent is checking the download"
        elif progress > 0:
            status = "downloading"
            source = str(job.get("indexer") or job.get("source") or "torrent")
            message = f"Downloading from {source} peers ({progress * 100:.1f}%)"
        else:
            status = "queued"
            message = "Queued in qBittorrent"
        return self.database.update_torrent_download(
            job["id"],
            status=status,
            progress=progress,
            qbit_state=qbit_state,
            message=message,
        )

    async def contents(self, download_id: int) -> dict[str, Any]:
        """What the completed release actually holds, so a human can choose.

        Each file carries the volume/chapter Tankarr derived from its name and
        whether that slot is contested (two editions of one volume) or already
        in the library, which is exactly the evidence the choice needs.
        """

        job = self.database.get_torrent_download(download_id)
        content = job.get("content_path")
        if not content:
            raise FileNotFoundError("This release has no completed content yet")
        scan = await asyncio.to_thread(
            self.importer.scan_torrent_content, Path(content)
        )
        language = str(job["language"])
        owned = {
            local_release_identity(
                language, chapter.get("volume"), chapter.get("chapter")
            )
            for chapter in self.database.list_all_chapters(str(job["manga_id"]))
            if chapter.get("downloaded")
        }
        slots: dict[str, int] = {}
        for item in scan["items"]:
            if item.get("volume") or item.get("chapter"):
                identity = local_release_identity(
                    language, item.get("volume"), item.get("chapter")
                )
                slots[identity] = slots.get(identity, 0) + 1
        files = []
        for item in scan["items"]:
            numbered = bool(item.get("volume") or item.get("chapter"))
            identity = (
                local_release_identity(
                    language, item.get("volume"), item.get("chapter")
                )
                if numbered
                else None
            )
            files.append(
                {
                    "path": str(item["path"]),
                    "size": int(item.get("size") or 0),
                    "pages": int(item.get("images") or 0),
                    "volume": item.get("volume"),
                    "chapter": item.get("chapter"),
                    "numbered": numbered,
                    "contested": bool(identity and slots.get(identity, 0) > 1),
                    "already_owned": bool(identity and identity in owned),
                }
            )
        files.sort(key=lambda entry: entry["path"])
        return {
            "id": job["id"],
            "manga_id": job["manga_id"],
            "title": job["title"],
            "files": files,
            "skipped": [dict(entry) for entry in scan.get("skipped") or []],
        }

    async def import_now(
        self,
        download_id: int,
        *,
        confirm_language: bool = False,
        skip_unnumbered: bool = False,
        selected_paths: set[str] | None = None,
        assigned: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        self.service.assert_mutations_allowed()
        if selected_paths is not None:
            # Remember it: a restart mid-import, or the automatic retry that
            # follows, must take the same books rather than the whole pack.
            self.database.update_torrent_download(
                download_id, selected_paths=sorted(selected_paths)
            )
        async with self._operation_lock:
            job = self.database.get_torrent_download(download_id)
            if job["status"] not in {"completed", "review", "failed"}:
                raise ValueError("Only completed or review downloads can be imported")
            if self._is_direct(job):
                content = Path(str(job.get("content_path") or ""))
                if not str(job.get("content_path") or "") or not content.exists():
                    raise FileNotFoundError(
                        "The downloaded file is gone; Retry fetches it again"
                    )
                return await self._import(
                    job,
                    {},
                    confirm_language=confirm_language,
                    skip_unnumbered=skip_unnumbered,
                    selected_paths=selected_paths,
                    assigned=assigned,
                )
            if str(job.get("protocol") or "torrent") == "usenet":
                current_job = await self._poll_usenet(job)
                if current_job is None or current_job["status"] != "completed":
                    raise ValueError("SABnzbd has not completed this download")
                return await self._import(
                    current_job,
                    current_job.get("_state") or {},
                    confirm_language=confirm_language,
                    skip_unnumbered=skip_unnumbered,
                    selected_paths=selected_paths,
                    assigned=assigned,
                )
            current = await self.qbittorrent.torrent_info(job["info_hash"])
            if current is None:
                raise FileNotFoundError("Torrent is missing from qBittorrent")
            current_job = self._record_qbit_state(job, current)
            if current_job["status"] != "completed":
                raise ValueError("qBittorrent has not completed this download")
            return await self._import(
                current_job,
                current,
                confirm_language=confirm_language,
                skip_unnumbered=skip_unnumbered,
                selected_paths=selected_paths,
                assigned=assigned,
            )

    def _edition_conflict(self, job: dict[str, Any]) -> str | None:
        """Refuse loose chapters when an explicitly book-based collection owns books.

        Whole books are always assessed per slot by the importer, including when
        the existing coverage consists only of chapter files.
        """

        try:
            manga = self.database.get_manga(str(job.get("manga_id") or ""))
        except KeyError:
            return None
        language = str(job.get("language") or manga.get("preferred_language") or "en")
        owned = [
            chapter
            for chapter in self.database.list_chapters(str(manga["id"]), language)
            if chapter.get("downloaded")
        ]
        if not owned:
            return None
        books = sum(1 for chapter in owned if is_volume_release(chapter))
        parts = len(owned) - books
        unit = normalize_series_unit(manga.get("series_unit_override"))
        if unit is None:
            if books and not parts:
                unit = "volumes"
            elif parts and not books:
                unit = "chapters"
            else:
                return None
        if str(job.get("volume_hint") or "").strip():
            kind = "volume"
        else:
            kind = classify_release(
                title=str(job.get("title") or ""),
                size_bytes=int(job.get("size_bytes") or 0) or None,
                language=language,
            ).kind
        title = str(manga.get("title") or job.get("manga_id"))
        # Whole books can fill their slots even when chapters already cover them.
        if unit == "volumes" and kind == "chapter" and books:
            return (
                f"Edition mismatch: {title} is kept as books ({books} on disk) "
                "and this release is a chapter, so importing it would add a "
                "second edition of the same pages. The automatic import was refused."
            )
        return None

    async def _import(
        self,
        job: dict[str, Any],
        current: dict[str, Any],
        *,
        confirm_language: bool,
        skip_unnumbered: bool = False,
        selected_paths: set[str] | None = None,
        assigned: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        if self._is_direct(job):
            content = Path(str(job.get("content_path") or ""))
            if not str(job.get("content_path") or "") or not content.exists():
                return self.database.update_torrent_download(
                    job["id"],
                    status="failed",
                    message="The downloaded file is gone; Retry fetches it again",
                )
        elif str(job.get("protocol") or "torrent") == "usenet":
            content = self.sabnzbd.local_content_path(current)
        else:
            content = self.qbittorrent.local_content_path(current)
        if (job.get("language_evidence") or {}).get("translation_request"):
            translations = getattr(self, "translations", None)
            if translations is None:
                return self.database.update_torrent_download(
                    job["id"],
                    status="completed",
                    message="Waiting for translation fallback",
                )
            return await translations.stage_torrent(job, content)
        self.database.update_torrent_download(
            job["id"],
            status="importing",
            content_path=content,
            message="Normalizing archive and auditing English with OCR",
        )
        # An automatic retry (a restart mid-import, the poller picking the job
        # back up) carries no selection of its own: fall back to the one the
        # operator already made rather than taking the whole release.
        if selected_paths is None and job.get("selected_paths"):
            selected_paths = set(job["selected_paths"])
        if selected_paths is None and assigned is None:
            conflict = self._edition_conflict(job)
            if conflict:
                return await self._refuse_import(job, content, conflict)
        try:
            result = await self.importer.import_torrent_download(
                job,
                content,
                confirm_language=confirm_language,
                skip_unnumbered=skip_unnumbered or not confirm_language,
                selected_paths=selected_paths,
                assigned=assigned,
                automatic_decision=not confirm_language,
            )
        except LanguageReviewRequired as exc:
            return await self._refuse_import(
                job,
                content,
                "Import refused: the pages or release language conflict with the series language.",
                evidence=exc.evidence,
            )
        except TorrentImportAmbiguous as exc:
            return await self._refuse_import(
                job,
                content,
                "Import refused: no unambiguous numbered books remain after automatic selection.",
                evidence={
                    "import_decisions": {
                        "imported_paths": [],
                        "already_owned_paths": [],
                        "skipped": getattr(exc, "skip_decisions", []),
                    }
                },
            )
        except ExternalImportConflict as exc:
            return await self._refuse_import(
                job, content, f"No useful books could be imported: {exc}"
            )
        except Exception as exc:
            self.database.update_torrent_download(
                job["id"], status="failed", message=f"{type(exc).__name__}: {exc}"
            )
            raise
        imported_books = int(result["books"])
        already_owned = int(result.get("already_owned") or 0)
        if imported_books:
            import_message = (
                f"Imported {imported_books} book(s) into the Comics library"
            )
            if already_owned:
                import_message += f"; skipped {already_owned} already in the library"
        else:
            import_message = (
                f"No new books imported; {already_owned} already in the library"
            )
        owned_paths = set(result.get("already_owned_paths") or [])
        extra_decisions = [
            decision
            for decision in result.get("skip_decisions", [])
            if decision.get("path") not in owned_paths
        ]
        if extra_decisions:
            import_message += f"; skipped {len(extra_decisions)} other file(s)"
        evidence = dict(result["language_evidence"])
        evidence["import_decisions"] = {
            "imported_paths": result["paths"],
            "already_owned_paths": result.get("already_owned_paths", []),
            "skipped": extra_decisions,
        }
        if not imported_books:
            return await self._refuse_import(
                job, content, import_message, evidence=evidence
            )
        updated = self.database.update_torrent_download(
            job["id"],
            status="imported",
            progress=1,
            content_path=content,
            imported_paths=result["paths"],
            language_evidence=evidence,
            message=import_message,
        )
        usenet = str(job.get("protocol") or "torrent") == "usenet"
        if self._is_direct(job):
            # The staging copy has done its job; the library holds the book.
            self._discard_direct_files(job)
            updated = self.database.update_torrent_download(
                job["id"], qbit_state="removed"
            )
        elif self.completed_action == "remove_after_import" or (
            usenet and self.completed_action != "seed"
        ):
            try:
                updated = await self._remove_from_client(
                    updated,
                    "removed from SABnzbd" if usenet else "removed from qBittorrent",
                )
            except Exception as exc:  # noqa: BLE001 - the library import already succeeded
                self.database.update_torrent_download(
                    job["id"],
                    message=f"{updated.get('message')} · could not remove from qBittorrent: {exc}"[
                        :300
                    ],
                )
        try:
            await self.service.rerank_queued_jobs(str(job["manga_id"]))
        except Exception as exc:  # noqa: BLE001 - the books are already imported
            logger.warning(
                "Could not reconcile queued jobs after importing %s: %s",
                job["manga_id"],
                exc,
            )
        if self.settings.metadata_enabled:
            try:
                await self.metadata.enrich_series(job["manga_id"], force=False)
            except Exception:  # noqa: BLE001 - the metadata monitor retries later
                pass
        if not self.database.list_torrent_downloads(
            statuses=POLLABLE_STATUSES, limit=1
        ):
            refresh = getattr(self.service, "refresh_komga_library", None)
            if refresh is not None:
                try:
                    await refresh(reason="torrent_queue_drained")
                except Exception:  # noqa: BLE001 - maintenance retries it
                    pass
        return updated

    async def retry(self, download_id: int) -> dict[str, Any]:
        self.service.assert_mutations_allowed()
        async with self._operation_lock:
            job = self.database.get_torrent_download(download_id)
            if job["status"] not in {"failed", "review"}:
                raise ValueError("Only failed or review downloads can be retried")
            if self._is_direct(job):
                if not self.direct_available:
                    raise ValueError("Internet Archive is disabled in Settings")
                self.database.retry_torrent_download(download_id)
                self._start_direct_download(
                    self.database.get_torrent_download(download_id)
                )
                return self.database.get_torrent_download(download_id)
            if str(job.get("protocol") or "torrent") == "usenet":
                if self.sabnzbd is None or not self.sabnzbd.configured:
                    raise SABnzbdError("SABnzbd is not configured")
                state = await self.sabnzbd.job_state(str(job.get("client_id") or ""))
                if state is None:
                    nzo_id = await self.sabnzbd.add_nzb_url(
                        str(job["torrent_url"]), name=str(job["title"])
                    )
                    self.database.update_torrent_download(job["id"], client_id=nzo_id)
                    job = self.database.get_torrent_download(download_id)
                    state = await self.sabnzbd.job_state(nzo_id)
                self.database.retry_torrent_download(download_id)
                return self._record_sab_state(
                    job, state or {"where": "queue", "status": "Queued"}
                )
            current = await self.qbittorrent.torrent_info(job["info_hash"])
            if current is None:
                await self._add_release(
                    {
                        "provider": job["source"],
                        "id": job["source_id"],
                        "info_hash": job["info_hash"],
                        "download_ref": job["torrent_url"],
                    }
                )
                current = await self.qbittorrent.wait_for_torrent(job["info_hash"])
            self.database.retry_torrent_download(download_id)
            return self._record_qbit_state(job, current)

    async def discard(
        self, download_id: int, *, delete_files: bool = True
    ) -> dict[str, Any]:
        async with self._operation_lock:
            job = self.database.get_torrent_download(download_id)
            if self._is_direct(job):
                if delete_files:
                    self._discard_direct_files(job)
                else:
                    task = self._direct_tasks.pop(int(job["id"]), None)
                    if task is not None and not task.done():
                        task.cancel()
            elif str(job.get("protocol") or "torrent") == "usenet":
                if self.sabnzbd is not None and job.get("client_id"):
                    await self.sabnzbd.delete(
                        str(job["client_id"]), delete_files=delete_files
                    )
            else:
                await self.qbittorrent.delete_torrent(
                    job["info_hash"], delete_files=delete_files
                )
            self.database.delete_torrent_download(download_id)
            return {
                "id": download_id,
                "discarded": True,
                "torrent_files_deleted": delete_files,
            }

    async def _add_release(self, release: dict[str, Any]) -> None:
        provider = str(release.get("provider") or "").casefold()
        release_id = str(release["id"])
        info_hash = str(release["info_hash"])
        if provider == "prowlarr":
            kind, payload = await self.prowlarr.resolve_download(
                str(release.get("download_ref") or ""), info_hash
            )
            if kind == "magnet":
                await self.qbittorrent.add_magnet(
                    str(payload),
                    release_id=release_id,
                    expected_hash=info_hash,
                )
            else:
                if not isinstance(payload, bytes):
                    raise ValueError("Prowlarr returned an invalid torrent payload")
                await self.qbittorrent.add_torrent(
                    payload,
                    release_id=release_id,
                    expected_hash=info_hash,
                )
            return
        raise ValueError("Unsupported torrent release provider")
