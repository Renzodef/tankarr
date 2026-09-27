from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from tankarr.catalogue import NO_REMOTE_PROVIDERS
from tankarr.config import Settings
from tankarr.database import Database
from tankarr.monitoring import (
    BACKLOG_MONITOR_MODES,
    FUTURE_MONITOR_MODES,
    normalize_monitor_mode,
)
from tankarr.series_summary import automatic_updates_paused
from tankarr.service import (
    PRIMARY_SOURCE_FAILURE_NOTE,
    RecoveryBlocked,
    TankarrService,
)
from tankarr.worker import DownloadWorker

if TYPE_CHECKING:
    from tankarr.release_sources import ReleaseSourceManager

logger = logging.getLogger(__name__)

WANTED_SEARCH_LAST_AT_SETTING = "__runtime_wanted_search_last_at"
WANTED_SEARCH_LAST_RESULT_SETTING = "__runtime_wanted_search_last_result"
# What the operator has already been told needs a decision.
DECISIONS_NOTIFIED_SETTING = "__runtime_decisions_notified"


class ReleaseMonitor:
    """Periodically refresh monitored titles and safely queue only new releases."""

    # Indexer queries cost time and goodwill, so one pass probes at most this
    # many series for book offers; a longer Wanted list rotates across cycles.
    BOOK_PROBE_PER_CYCLE = 10

    def __init__(
        self,
        settings: Settings,
        database: Database,
        service: TankarrService,
        worker: DownloadWorker,
        release_sources: ReleaseSourceManager | None = None,
    ):
        self.settings = settings
        # What the series-level book hunt answered this pass, per series.
        self._last_book_hunt: dict[str, dict[str, Any]] = {}
        self._last_book_probe_id: str | None = None
        self.database = database
        self.service = service
        self.worker = worker
        self.release_sources = release_sources
        self.translations = None
        self.task: asyncio.Task | None = None
        self._wanted_scheduler_task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._wanted_search_lock = asyncio.Lock()
        self._wanted_search_task: asyncio.Task[dict[str, Any]] | None = None
        self.last_cycle_at: str | None = None
        self.last_cycle_error: str | None = None
        self.last_wanted_search_at: str | None = None
        self.last_wanted_search_result: dict[str, Any] | None = None
        self.last_wanted_search_error: str | None = None
        self.wanted_started_at: str | None = None
        self.wanted_current_manga_id: str | None = None
        self.wanted_progress = {"processed": 0, "total": 0}
        self._restore_wanted_search_state()

    def _restore_wanted_search_state(self) -> None:
        """Keep a deploy from immediately repeating the last expensive pass."""
        try:
            stored = self.database.get_setting_overrides()
            last_at = str(stored.get(WANTED_SEARCH_LAST_AT_SETTING) or "")
            if last_at:
                parsed = datetime.fromisoformat(last_at)
                if parsed.tzinfo is not None:
                    self.last_wanted_search_at = last_at
            raw_result = stored.get(WANTED_SEARCH_LAST_RESULT_SETTING)
            result = json.loads(raw_result) if raw_result else None
            if isinstance(result, dict):
                self.last_wanted_search_result = result
        except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
            # Runtime telemetry must never make Tankarr fail to start.
            return

    def _persist_wanted_search_state(self) -> None:
        try:
            if self.last_wanted_search_at:
                self.database.save_setting(
                    WANTED_SEARCH_LAST_AT_SETTING,
                    self.last_wanted_search_at,
                )
            if self.last_wanted_search_result is not None:
                self.database.save_setting(
                    WANTED_SEARCH_LAST_RESULT_SETTING,
                    json.dumps(
                        self.last_wanted_search_result,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                )
        except (AttributeError, OSError, TypeError, ValueError):
            # A later cycle can retry persistence; recovery itself succeeded.
            return

    async def start(self) -> None:
        if not self.settings.monitor_enabled or self.task is not None:
            return
        self._stop.clear()
        self.task = asyncio.create_task(self._run(), name="tankarr-release-monitor")
        self._wanted_scheduler_task = asyncio.create_task(
            self._run_wanted(), name="tankarr-wanted-scheduler"
        )

    async def stop(self) -> None:
        self._stop.set()
        tasks = {
            task
            for task in (
                self.task,
                self._wanted_scheduler_task,
                self._wanted_search_task,
            )
            if task is not None
        }
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.task = None
        self._wanted_scheduler_task = None
        self._wanted_search_task = None
        self.wanted_current_manga_id = None

    async def _run(self) -> None:
        while True:
            await self.run_cycle(include_wanted=False)
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self.settings.monitor_interval_seconds
                )
                return
            except TimeoutError:
                continue

    async def _run_wanted(self) -> None:
        """Keep backlog deadlines independent of a slow source-refresh cycle."""

        while not self._stop.is_set():
            failed = False
            if self._wanted_search_due():
                try:
                    await self.search_wanted(trigger="scheduled")
                except Exception as exc:  # noqa: BLE001 - one outage is not a dead scheduler
                    failed = True
                    self.last_wanted_search_error = f"{type(exc).__name__}: {exc}"[:300]
                    logger.warning(
                        "Wanted scheduler: %s", self.last_wanted_search_error
                    )
            next_at = self._next_wanted_search_at()
            delay = (
                max(
                    1.0,
                    (
                        datetime.fromisoformat(next_at) - datetime.now(UTC)
                    ).total_seconds(),
                )
                if next_at is not None and not failed
                else 60.0
            )
            # Settings are hot-reloaded; observe a shorter interval or newly
            # enabled recovery without waiting for the previous long deadline.
            delay = min(delay, 60.0)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except TimeoutError:
                continue

    async def run_cycle(self, *, include_wanted: bool = True) -> dict[str, Any]:
        from tankarr.database import utc_now

        self.service.assert_mutations_allowed()
        checked = 0
        queued = 0
        errors: list[dict[str, str]] = []
        for manga in self.database.list_manga():
            if automatic_updates_paused(manga):
                continue
            if manga.get("provider") == "local":
                continue  # Local series have no remote source to poll.
            try:
                if manga["monitor_mode"] in FUTURE_MONITOR_MODES:
                    result = await self.refresh_one(manga["id"])
                else:
                    await self.service.refresh_metadata(manga["id"])
                    result = {"queued": 0}
                checked += 1
                queued += result["queued"]
            except Exception as exc:  # noqa: BLE001 - one title must not stop the cycle
                checked += 1
                message = f"{type(exc).__name__}: {exc}"
                self.database.record_monitor_result(
                    manga["id"],
                    error=message,
                    initialized=manga["monitor_initialized"],
                )
                errors.append({"manga_id": manga["id"], "error": message})
                logger.warning("Unable to refresh manga %s: %s", manga["id"], message)
        wanted_search = None
        if include_wanted and self._wanted_search_due():
            wanted_search = await self.search_wanted(trigger="scheduled")
            if wanted_search["errors"]:
                errors.extend(wanted_search["errors"])
        self.last_cycle_at = utc_now()
        self.last_cycle_error = errors[0]["error"] if errors else None
        return {
            "checked": checked,
            "queued": queued + int((wanted_search or {}).get("queued", 0)),
            "errors": errors,
            "wanted_search": wanted_search,
        }

    def _wanted_search_due(self) -> bool:
        if not self.settings.wanted_search_enabled:
            return False
        if self.last_wanted_search_at is None:
            return True
        try:
            last = datetime.fromisoformat(self.last_wanted_search_at)
        except ValueError:
            return True
        return datetime.now(UTC) >= last + timedelta(
            seconds=self.settings.wanted_search_interval_seconds
        )

    def _next_wanted_search_at(self) -> str | None:
        if not self.settings.wanted_search_enabled:
            return None
        if self.last_wanted_search_at is None:
            return datetime.now(UTC).isoformat()
        try:
            last = datetime.fromisoformat(self.last_wanted_search_at)
        except ValueError:
            return datetime.now(UTC).isoformat()
        return (
            last + timedelta(seconds=self.settings.wanted_search_interval_seconds)
        ).isoformat()

    async def search_wanted(self, *, trigger: str = "manual") -> dict[str, Any]:
        """Join an in-flight Wanted pass instead of repeating every source query."""

        current = self._wanted_search_task
        if current is not None and not current.done():
            return dict(await asyncio.shield(current))

        task = asyncio.create_task(
            self._search_wanted_once(trigger=trigger),
            name=f"tankarr-wanted-search-{trigger}",
        )
        self._wanted_search_task = task
        self.wanted_started_at = datetime.now(UTC).isoformat()
        self.wanted_progress = {"processed": 0, "total": 0}
        self.last_wanted_search_error = None

        def clear(completed: asyncio.Task[dict[str, Any]]) -> None:
            if self._wanted_search_task is completed:
                self._wanted_search_task = None
                self.wanted_current_manga_id = None
            if (
                not completed.cancelled()
                and (error := completed.exception()) is not None
            ):
                self.last_wanted_search_error = f"{type(error).__name__}: {error}"[:300]

        task.add_done_callback(clear)
        return dict(await asyncio.shield(task))

    async def search_wanted_series(
        self, manga_id: str, *, trigger: str = "add"
    ) -> dict[str, Any]:
        """Climb the recovery ladder for one series right away.

        Sonarr searches a series' missing episodes the moment it is added;
        here the same happens once the add-time discovery has mapped the
        sources and queued what they list. Whatever no source lists is then
        asked of the indexers by chapter and by book, and of the direct
        archives, so the operator sees within minutes what is obtainable and
        what is not - instead of waiting for the six-hourly Wanted pass.
        """

        async with self._wanted_search_lock:
            self._last_book_hunt.pop(manga_id, None)
            entries = await asyncio.to_thread(self.service.list_wanted)
            if trigger == "scheduled":
                entries = [
                    entry
                    for entry in entries
                    if not automatic_updates_paused(entry["manga"])
                ]
            entry = next(
                (item for item in entries if str(item["manga"]["id"]) == str(manga_id)),
                None,
            )
            if entry is None:
                return {"trigger": trigger, "manga_id": manga_id, "missing": 0}
            queued = await self.queue_missing(manga_id)
            hunted = await self._hunt_missing_volumes(entry)
            self._last_book_hunt[manga_id] = hunted
            queued += len(hunted.get("grabbed") or [])
            recovered = await self.recover_wanted_slots(entry, hunted)
            queued += int(recovered.get("grabbed") or 0)
            translated = (
                await self.translations.queue_missing(manga_id)
                if self.translations
                else 0
            )
            result = {
                "trigger": trigger,
                "manga_id": manga_id,
                "missing": len(entry["chapters"])
                + int(entry.get("unmapped_expected_count") or 0),
                "queued": queued,
                "searched": int(recovered.get("searched") or 0),
            }
            if translated or trigger == "translation":
                result["translations_queued"] = translated
            logger.info(
                "Searched %s on %s: %s missing, %s queued, %s slots asked",
                entry["manga"].get("title") or manga_id,
                trigger,
                result["missing"],
                result["queued"],
                result["searched"],
            )
            return result

    async def _search_wanted_once(self, *, trigger: str) -> dict[str, Any]:
        """Queue every monitored missing release, independently of RSS refresh."""

        from datetime import UTC, datetime

        from tankarr.database import utc_now
        from tankarr.search_cadence import after_pass, plan_pass

        async with self._wanted_search_lock:
            self._last_book_hunt.clear()
            entries = await asyncio.to_thread(self.service.list_wanted)
            if trigger == "scheduled":
                entries = [
                    entry
                    for entry in entries
                    if not automatic_updates_paused(entry["manga"])
                ]
            missing = sum(
                len(entry["chapters"]) + int(entry.get("unmapped_expected_count") or 0)
                for entry in entries
            )
            # A finished work whose sources have not changed is not looked at
            # every pass: the ladder in search_cadence decides, a person's
            # request overrides it, and a budget bounds every pass.
            now = datetime.now(UTC)
            try:
                states = await asyncio.to_thread(self.database.wanted_search_states)
            except Exception:  # noqa: BLE001 - cadence is an optimisation
                states = {}
            planned, skipped_not_due = plan_pass(
                entries,
                states,
                now,
                budget=int(getattr(self.settings, "wanted_search_budget", 0) or 0),
                force=trigger != "scheduled",
            )
            self.wanted_progress = {"processed": 0, "total": len(planned)}
            queued = 0
            discovered = 0
            searched = 0
            errors: list[dict[str, str]] = []
            for entry in planned:
                manga_id = str(entry["manga"]["id"])
                self.wanted_current_manga_id = manga_id
                queued_before, discovered_before = queued, discovered
                try:
                    if self.release_sources is not None:
                        if entry["manga"].get("provider") not in NO_REMOTE_PROVIDERS:
                            await self._refresh_primary_source(
                                manga_id, entry["manga"]["preferred_language"]
                            )
                        if self.before_discovery is not None:
                            try:
                                await self.before_discovery(manga_id)
                            except Exception:  # noqa: BLE001 - discovery still runs
                                logger.debug(
                                    "before_discovery hook failed for %s",
                                    manga_id,
                                    exc_info=True,
                                )
                        discovery = await self.release_sources.discover_and_refresh(
                            manga_id, monitor_new=True
                        )
                        discovered += int(discovery.get("new") or 0)
                    queued += await self.queue_missing(manga_id)
                    hunted = await self._hunt_missing_volumes(entry)
                    self._last_book_hunt[manga_id] = hunted
                    queued += len(hunted.get("grabbed") or [])
                    recovered = await self.recover_wanted_slots(entry, hunted)
                    queued += int(recovered.get("grabbed") or 0)
                    searched += int(recovered.get("searched") or 0)
                    if self.translations is not None:
                        await self.translations.queue_missing(manga_id)
                except RecoveryBlocked:
                    raise
                except Exception as exc:  # noqa: BLE001 - continue other series
                    message = f"{type(exc).__name__}: {exc}"
                    errors.append({"manga_id": manga_id, "error": message})
                    logger.warning(
                        "Unable to recover Wanted releases for %s: %s",
                        manga_id,
                        message,
                    )
                finally:
                    self.wanted_progress["processed"] += 1
                    try:
                        await asyncio.to_thread(
                            self.database.save_wanted_search_state,
                            after_pass(
                                states.get(manga_id),
                                manga_id,
                                datetime.now(UTC),
                                found=queued > queued_before,
                                discovered=discovered > discovered_before,
                            ),
                        )
                    except Exception:  # noqa: BLE001 - cadence is an optimisation
                        logger.debug(
                            "Could not record the search cadence", exc_info=True
                        )
            self.wanted_current_manga_id = None
            try:
                await self.probe_book_offers(planned)
            except Exception as exc:  # noqa: BLE001
                errors.append(
                    {
                        "manga_id": "*",
                        "error": f"book offers: {type(exc).__name__}: {exc}",
                    }
                )
            try:
                await self.align_chapter_contents()
            except Exception as exc:  # noqa: BLE001
                errors.append(
                    {
                        "manga_id": "*",
                        "error": f"content alignment: {type(exc).__name__}: {exc}",
                    }
                )
            try:
                await self.retire_redundant_chapters()
            except Exception as exc:  # noqa: BLE001
                errors.append(
                    {
                        "manga_id": "*",
                        "error": f"chapter retirement: {type(exc).__name__}: {exc}",
                    }
                )
            try:
                await self.retire_duplicate_chapter_files()
            except Exception as exc:  # noqa: BLE001
                errors.append(
                    {
                        "manga_id": "*",
                        "error": f"duplicate cleanup: {type(exc).__name__}: {exc}",
                    }
                )
            if (assembly := getattr(self, "book_assembly", None)) is not None:
                try:
                    assembled = await assembly.run_automatic()
                    errors.extend(
                        {
                            "manga_id": item["manga_id"],
                            "error": f"book assembly: {item['message']}",
                        }
                        for item in assembled["errors"]
                    )
                except Exception as exc:  # noqa: BLE001 - retry on the next cycle
                    errors.append(
                        {
                            "manga_id": "*",
                            "error": f"book assembly: {type(exc).__name__}: {exc}",
                        }
                    )
            try:
                await self.track_publication_pauses()
            except Exception as exc:  # noqa: BLE001
                errors.append(
                    {
                        "manga_id": "*",
                        "error": f"hiatus tracking: {type(exc).__name__}: {exc}",
                    }
                )
            upgraded = 0
            try:
                upgraded = await self.upgrade_to_official()
            except Exception as exc:  # noqa: BLE001 - next cycle retries
                errors.append(
                    {
                        "manga_id": "*",
                        "error": f"official upgrade: {type(exc).__name__}: {exc}",
                    }
                )
            aligned = {"deleted": 0, "series": 0}
            try:
                aligned = await self.align_official_editions()
            except Exception as exc:  # noqa: BLE001 - next cycle retries
                errors.append(
                    {
                        "manga_id": "*",
                        "error": f"official alignment: {type(exc).__name__}: {exc}",
                    }
                )
            quality = {"measured": 0, "replaced": 0}
            try:
                quality = await self.audit_page_quality()
            except Exception as exc:  # noqa: BLE001 - next cycle retries
                errors.append(
                    {
                        "manga_id": "*",
                        "error": f"page quality: {type(exc).__name__}: {exc}",
                    }
                )
            queued += int(quality.get("replaced") or 0)
            # A failure the failover already repaired is history, not a
            # problem: it leaves the count so the alert shows what still
            # needs someone.
            forgotten = self.database.prune_recovered_failures()
            if forgotten:
                logger.info("Forgot %s failed downloads recovered elsewhere", forgotten)
            result = {
                "trigger": trigger,
                "series": len(planned),
                "skipped_not_due": skipped_not_due,
                "missing": missing,
                "discovered": discovered,
                "searched": searched,
                "queued": queued,
                "upgraded": upgraded,
                "aligned": aligned,
                "quality": quality,
                "errors": errors,
            }
            self.last_wanted_search_at = utc_now()
            self.last_wanted_search_result = result
            self._persist_wanted_search_state()
            try:
                await self._notify_decisions()
            except Exception:  # noqa: BLE001 - a notification never fails the pass
                logger.debug("Decision notification failed", exc_info=True)
            return result

    async def align_official_editions(self) -> dict[str, Any]:
        """Keep every series on the numbering of its official edition.

        The series page, Wanted and the download gate all already stop at the
        official frontier. Files acquired before that gate existed do not
        disappear on their own, and while they sit there the library reads as
        ahead of a publication it is supposed to mirror.
        """

        if not getattr(self.settings, "official_edition_alignment_enabled", False):
            return {"deleted": 0, "series": 0, "enabled": False}
        deleted = 0
        touched = 0
        for manga in self.database.list_manga():
            if automatic_updates_paused(manga):
                continue
            if not manga.get("monitored"):
                continue
            try:
                outcome = await self.service.align_with_official_edition(
                    str(manga["id"])
                )
            except Exception as exc:  # noqa: BLE001 - one series must not stop it
                logger.info(
                    "Could not align %s with its official edition: %s",
                    manga.get("title"),
                    exc,
                )
                continue
            if outcome["deleted"]:
                deleted += int(outcome["deleted"])
                touched += 1
        return {"deleted": deleted, "series": touched, "enabled": True}

    async def audit_page_quality(self) -> dict[str, Any]:
        """Measure imported chapters, then replace the unreadable ones."""

        if not getattr(self.settings, "page_quality_recovery_enabled", False):
            return {"measured": 0, "replaced": 0, "kept": 0, "enabled": False}
        audit = await self.service.audit_library_page_quality()
        replaced = 0
        kept = 0
        for manga_id in {str(item["manga_id"]) for item in audit["degraded"]}:
            try:
                outcome = await self.service.recover_degraded_chapters(manga_id)
            except Exception as exc:  # noqa: BLE001 - one series must not stop it
                logger.info("Could not replace degraded chapters: %s", exc)
                continue
            replaced += len(outcome["requeued"])
            kept += len(outcome["kept"])
            for item in outcome["requeued"]:
                await self.worker.enqueue(int(item["job_id"]))
        return {
            "measured": int(audit["measured"]),
            "degraded": len(audit["degraded"]),
            "replaced": replaced,
            "kept": kept,
            "enabled": True,
        }

    torrents: Any = None  # set by the app once the download manager exists
    before_discovery: Any = None  # async hook: install official extensions first

    def _publishers_for(self, manga_id: str) -> tuple[list[str], bool]:
        metadata_row = self.database.get_series_metadata(manga_id)
        data = (metadata_row or {}).get("data") or {}
        publishers: list[str] = []
        for item in data.get("publishers") or []:
            name = (
                str((item or {}).get("name") or "").strip()
                if isinstance(item, dict)
                else str(item or "").strip()
            )
            kind = (
                str((item or {}).get("type") or "").casefold()
                if isinstance(item, dict)
                else ""
            )
            if name and kind in {"english", "en"} and name not in publishers:
                publishers.append(name)
        original = str(data.get("publisher") or "").strip()
        if original and original not in publishers:
            publishers.append(original)
        try:
            single = int(data.get("volume_count") or 0) == 1
        except (TypeError, ValueError):
            single = False
        return publishers, single

    def _creators_for(self, manga_id: str) -> list[str]:
        """The work's authors, as the catalogue names them: a release that
        carries the name ("Black Magic by Masamune Shirow") is corroborated
        by it, and the name may follow the title in the release."""

        metadata_row = self.database.get_series_metadata(manga_id)
        data = (metadata_row or {}).get("data") or {}
        names = []
        for item in data.get("authors") or []:
            name = str(
                item.get("name") if isinstance(item, dict) else item or ""
            ).strip()
            if name and name not in names:
                names.append(name)
        return names

    def _hunt_titles(self, manga: dict[str, Any]) -> list[str]:
        """The catalogue title first, then up to two Latin-script alternates
        ("Dominion" is offered as "Dominion Tank Police"; "The Ghost in the
        Shell" as "Ghost in the Shell"), so the hunt asks by every name the
        indexers actually use."""

        titles = [str(manga.get("title") or "").strip()]
        metadata_row = self.database.get_series_metadata(str(manga["id"]))
        data = (metadata_row or {}).get("data") or {}
        for item in data.get("alternate_titles") or []:
            alias = str(
                item.get("title") if isinstance(item, dict) else item or ""
            ).strip()
            if not alias or alias in titles:
                continue
            if not re.fullmatch(r"[A-Za-z0-9 .,:'!&\-]+", alias):
                continue
            if alias.casefold() == titles[0].casefold():
                continue
            titles.append(alias)
            if len(titles) >= 3:
                break
        return titles

    def _volume_count_for(self, manga_id: str) -> int | None:
        """The edition the shelf can complete: the managed English edition
        when the catalogue states one, else the work's own volume count.
        Master Keaton is 18 books in Japan and 12 at VIZ: a raw pack of 18
        must read as "beyond the work", not as eight more books to want."""

        from tankarr.catalogue_consensus import managed_volume_count

        metadata_row = self.database.get_series_metadata(manga_id)
        data = (metadata_row or {}).get("data") or {}
        managed = managed_volume_count(data)
        if managed:
            return managed
        try:
            return int(data.get("volume_count") or 0) or None
        except (TypeError, ValueError):
            return None

    async def probe_book_offers(self, entries: list[dict[str, Any]]) -> int:
        """Ask the indexers which books exist for works followed as chapters.

        A series only reaches Wanted because its current unit cannot complete
        it, so the alternative unit is always worth knowing about: the choice
        follows what the sources can actually deliver, not the status of the
        work. Chapters are discovered from the mapped sources on every pass;
        books live on the indexers and are invisible until asked for, so this
        is the half that has to be probed explicitly.

        Running works are probed too. A work still publishing can already have
        every volume out for the part that is missing here, and refusing to
        look guarantees the unit never changes. Each pass probes a bounded
        number of series - an indexer query is not free - so a long Wanted
        list rotates over successive cycles.
        """

        from tankarr.volume_hunt import probe_offers

        torrents = self.torrents
        if torrents is None or not (
            (torrents.prowlarr.enabled and torrents.prowlarr.configured)
            or getattr(torrents, "direct_available", False)
        ):
            return 0
        probed = 0
        # Resume after the last attempted series, not the first ten on every
        # pass. Use identity rather than an offset so insertion/removal does
        # not shift the rotation while a stable backlog waits its turn.
        start = next(
            (
                index + 1
                for index, entry in enumerate(entries)
                if str(entry["manga"]["id"]) == self._last_book_probe_id
            ),
            0,
        )
        for entry in entries[start:] + entries[:start]:
            manga = self.database.get_manga(str(entry["manga"]["id"]))
            if str(manga.get("preferred_language") or "") != "en":
                continue
            if any(
                row.get("volume") and not row.get("chapter")
                for row in entry.get("chapters") or []
            ):
                continue  # a volumes series: the hunt itself records offers
            if (self._last_book_hunt.get(str(manga["id"])) or {}).get(
                "searched_volumes"
            ):
                continue  # the chapter-to-book hunt records those offers too
            publishers, single = self._publishers_for(str(manga["id"]))
            self._last_book_probe_id = str(manga["id"])
            await probe_offers(
                torrents,
                manga=manga,
                publisher=publishers,
                single_volume=single,
                volume_count=self._volume_count_for(str(manga["id"])),
                creators=self._creators_for(str(manga["id"])),
            )
            probed += 1
            if probed >= self.BOOK_PROBE_PER_CYCLE:
                break
        return probed

    async def retire_redundant_chapters(self) -> int:
        """Once every book of a series is on disk, its chapter files are
        redundant by definition and are removed (the only safe chapter→book
        replacement without a chapter↔volume map)."""

        removed = 0
        for manga in self.database.list_manga():
            try:
                outcome = await self.service.retire_redundant_chapter_files(
                    str(manga["id"])
                )
            except Exception as exc:  # noqa: BLE001 - one series must not stop it
                logger.info(
                    "Could not retire chapters of %s: %s", manga.get("title"), exc
                )
                continue
            removed += int(outcome.get("deleted") or 0)
        return removed

    async def track_publication_pauses(self) -> dict[str, Any]:
        """Notice when a work enters or leaves a hiatus and say so.

        The signals themselves are refreshed elsewhere on their own rhythm:
        MangaBaka, MyAnimeList and MangaUpdates on the weekly metadata
        refresh, the official platform's flag on every source refresh. This
        step only compares the resulting state with the last one it saw, so
        a work that resumes goes back on the calendar the same day and the
        operator hears about it once.
        """

        from tankarr.series_summary import publication_summary

        entered: list[str] = []
        left: list[str] = []
        for manga in self.database.list_manga():
            if automatic_updates_paused(manga):
                continue
            manga_id = str(manga["id"])
            manga["publication_signals"] = self.database.publication_signals(manga_id)
            metadata_row = self.database.get_series_metadata(manga_id)
            metadata = (metadata_row or {}).get("data") or manga.get("metadata")
            summary = publication_summary(manga, metadata)
            paused = bool(summary.get("paused"))
            previous = self.database.record_publication_pause(
                manga_id,
                paused=paused,
                sources=list(summary.get("pause_sources") or []),
            )
            if previous is None or previous["paused"] is None:
                continue  # unchanged, or first sighting: nothing to announce
            title = str(manga.get("title") or manga_id)
            sources = ", ".join(summary.get("pause_sources") or []) or "catalogue"
            if paused:
                entered.append(title)
                logger.info("%s entered a hiatus according to %s", title, sources)
                detail = f"{title} is on hiatus ({sources}). No chapters are expected until it resumes."
            else:
                left.append(title)
                logger.info(
                    "%s is back from hiatus; expected chapters return to the calendar",
                    title,
                )
                detail = f"{title} is publishing again. Expected chapters are back on the calendar."
            notifier = getattr(self.service, "notifier", None)
            if notifier is not None and getattr(notifier, "configured", False):
                try:
                    await notifier.send(
                        "Hiatus" if paused else "Back from hiatus",
                        detail,
                        tags="pause_button" if paused else "arrow_forward",
                    )
                except Exception as exc:  # noqa: BLE001 - best effort
                    logger.info("Could not announce a hiatus change: %s", exc)
        return {"entered": entered, "left": left}

    async def align_chapter_contents(self, *, max_series: int = 25) -> dict[str, Any]:
        """Read the shelves of mixed series so every chapter file carries a
        verdict about the book it sits in. Cached by content hash, so a
        series costs a read once; bounded per cycle so a big library does
        not hold the cycle."""

        checked = 0
        series = 0
        for manga_id in self.database.manga_ids_with_books_and_chapters():
            if series >= max_series:
                break
            try:
                outcome = await asyncio.to_thread(
                    self.service.align_chapter_contents, manga_id
                )
            except Exception as exc:  # noqa: BLE001 - one series must not stop it
                logger.info("Could not align chapters of %s: %s", manga_id, exc)
                continue
            series += 1
            checked += int(outcome.get("checked") or 0)
        return {"checked": checked, "series": series}

    async def retire_duplicate_chapter_files(
        self, *, max_series: int = 10
    ) -> dict[str, Any]:
        """Delete chapter files whose content the volume↔chapter map proves is
        inside a book already on disk: the book is the better copy (whole,
        one source, the edition the operator chose), so the loose chapters
        are duplicates. Same quarantine path as the "Delete duplicate files"
        button; bounded per cycle so a bad map cannot empty a library."""

        if not getattr(self.settings, "duplicate_cleanup_enabled", False):
            return {"deleted": 0, "series": 0, "enabled": False}
        deleted = 0
        touched = 0
        for manga_id in self.database.manga_ids_with_books_and_chapters():
            if touched >= max_series:
                break
            try:
                duplicates = self.service.duplicate_chapter_files(manga_id)
                suspects = self.service.suspect_covering_volumes(manga_id)
                safe = [
                    item
                    for item in duplicates
                    if str(item.get("duplicate_of_volume") or "") not in suspects
                ]
                if len(safe) < len(duplicates):
                    logger.info(
                        "Kept %d chapter files of %s: their covering book is suspect (%s)",
                        len(duplicates) - len(safe),
                        manga_id,
                        "; ".join(f"v{v}: {why}" for v, why in suspects.items()),
                    )
                # The map says the book holds the chapter; the pages decide.
                await asyncio.to_thread(self.service.align_chapter_contents, manga_id)
                vetoed = self.service.content_blocks_retirement(
                    manga_id, [str(item["id"]) for item in safe]
                )
                if vetoed:
                    logger.info(
                        "Kept %d chapter files of %s: their pages are not inside the book",
                        len(vetoed),
                        manga_id,
                    )
                    safe = [item for item in safe if str(item["id"]) not in vetoed]
                if not safe:
                    continue
                outcome = await self.service.delete_duplicate_chapter_files(
                    manga_id, None, only={str(item["id"]) for item in safe}
                )
            except Exception as exc:  # noqa: BLE001 - one series must not stop it
                logger.info(
                    "Could not remove duplicate chapters of %s: %s", manga_id, exc
                )
                continue
            touched += 1
            deleted += int(outcome.get("files_deleted") or 0)
            logger.info(
                "Removed %d chapter files of %s duplicated by volume(s) %s",
                outcome.get("files_deleted") or 0,
                manga_id,
                ", ".join(outcome.get("volumes") or []),
            )
        return {"deleted": deleted, "series": touched, "enabled": True}

    async def upgrade_to_official(self) -> int:
        """Queue explicitly enabled replacements with official releases."""

        from tankarr.official_upgrade import (
            preferred_source_upgrades,
            upgrade_candidates,
        )
        from tankarr.source_circuit import source_gate_key
        from tankarr.source_ranking import (
            beyond_frontier,
            official_frontier,
            official_hosts,
            release_source_keys,
        )

        def enabled():
            return settings is not None and (
                settings.official_upgrade_enabled or settings.source_upgrade_enabled
            )

        settings = getattr(self.service, "settings", None)
        if not enabled():
            return 0
        queued = 0
        for manga in self.database.list_manga():
            if automatic_updates_paused(manga):
                continue
            if not enabled():
                break
            if not manga.get("monitored"):
                continue
            metadata_row = self.database.get_series_metadata(str(manga["id"]))
            hosts = official_hosts(
                ((metadata_row or {}).get("data") or {}).get("official_links"),
                language=str(manga.get("preferred_language") or ""),
            )
            if not hosts and not settings.source_upgrade_enabled:
                continue
            chapters = self.database.list_chapters(
                str(manga["id"]), str(manga["preferred_language"])
            )
            # An official release that already failed here, or that comes from
            # a source Tankarr no longer trusts, is not an upgrade: replacing
            # a file that works with one that cannot be fetched only costs a
            # failed download every cycle.
            blocked = self.database.blocked_releases(str(manga["id"]))
            demoted = self.database.demoted_sources(str(manga["id"]))
            chapters = [
                {
                    **release,
                    "blocked": str(release["id"]) in blocked
                    or bool(set(release_source_keys(release)) & demoted),
                }
                for release in chapters
            ]
            candidates = (
                preferred_source_upgrades(chapters, settings.source_ranking, hosts)
                if settings.source_upgrade_enabled
                else upgrade_candidates(chapters, hosts)
            )
            frontier = (
                official_frontier(
                    chapters,
                    hosts,
                    status=str(manga.get("status") or ""),
                    metadata=(metadata_row or {}).get("data") or {},
                )
                if settings.source_ranking.prefer_official
                else None
            )
            for release in candidates:
                if not enabled():
                    return queued
                if beyond_frontier(release, frontier):
                    continue
                if (
                    self.database.source_retry_at(
                        source_gate_key(
                            release.get("provider"),
                            release.get("source_url"),
                            release.get("source_name"),
                        )
                    )
                    > datetime.now(UTC).timestamp()
                ):
                    continue
                if str(release["id"]) in blocked:
                    continue
                if demoted and set(release_source_keys(release)) & demoted:
                    continue
                try:
                    job = await self.service.create_manual_download_job(
                        str(release["id"]), replace=True
                    )
                    if job["status"] == "queued":
                        await self.worker.enqueue(int(job["id"]))
                        queued += 1
                except Exception as exc:  # noqa: BLE001 - keep going
                    logger.info(
                        "Official upgrade skipped for %s: %s", release.get("id"), exc
                    )
        return queued

    # Indexer queries cost time and goodwill: one pass recovers at most this
    # many chapter slots per series, newest first, and a long backlog walks
    # forward over successive cycles instead of hammering every indexer.
    # Raised from 10: at 10/series/pass a handful of large backlogs (a few
    # hundred missing chapters each) took weeks to reach a verdict on every
    # slot, so Wanted stayed full of items nobody could tell were obtainable
    # or not. 40/series keeps the per-pass indexer load in the low hundreds
    # of queries even with several such series at once, which self-hosted
    # indexers tolerate at the six-hourly cadence this runs on.
    CHAPTER_RECOVERY_PER_SERIES = 40

    async def recover_wanted_slots(
        self, entry: dict[str, Any], hunted: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Walk every wanted slot of one series down the recovery ladder.

        Rung one is free: discovery has just refreshed the mapped sources and
        the Suwayomi catalogue, so a slot that now has a release only needs a
        job, which ``queue_missing`` has already created. What this adds is the
        two rungs nobody was climbing - asking the indexers for the chapter,
        then for the book that contains it - and, for every slot, a record of
        what each rung answered. A Wanted list that cannot say which of its
        entries is obtainable is not a list of work, it is a list of noise.
        """

        from tankarr.wanted_recovery import (
            CHANNEL_INDEXER_BOOK,
            CHANNEL_INDEXER_CHAPTER,
            CHANNEL_SOURCES,
            NOT_OFFERED,
            PENDING,
            QUEUED,
            UNAVAILABLE,
            slot_key_for,
        )

        manga_id = str(entry["manga"]["id"])
        if entry.get("no_sources"):
            return await self._recover_series_without_sources(manga_id)
        rows = list(entry.get("chapters") or [])
        grabbed = 0
        searched = 0
        chapter_slots: list[dict[str, Any]] = []
        book_slots: list[dict[str, Any]] = []
        for row in rows:
            slot_key = slot_key_for(row)
            if row.get("queue_status"):
                self.database.record_wanted_attempt(
                    manga_id,
                    slot_key,
                    channel=CHANNEL_SOURCES,
                    outcome=PENDING,
                    detail=f"already {row['queue_status']}",
                )
                continue
            if row.get("blocked"):
                # Every source's copy was refused; the indexers and the
                # archive may still hold the book. The slot goes on to those
                # rungs, so it can reach "not obtainable" instead of sitting
                # at "blocked" forever (measured live on four chapters).
                self.database.record_wanted_attempt(
                    manga_id,
                    slot_key,
                    channel=CHANNEL_SOURCES,
                    outcome="blocked",
                    detail=str(row.get("block_reason") or "release blocked"),
                )
                if row.get("chapter"):
                    chapter_slots.append(row)
                elif row.get("volume"):
                    book_slots.append(row)
                continue
            if not row.get("expected") and row.get("provider") != "expected":
                # Indexer offer records can remain after a rejected or
                # deleted import. They are not direct download sources, so
                # they cannot settle Wanted as a queued provider release.
                if not self.service.can_download_release(row):
                    self.database.record_wanted_attempt(
                        manga_id,
                        slot_key,
                        channel=CHANNEL_SOURCES,
                        outcome=NOT_OFFERED,
                        detail="no configured download provider serves this release",
                    )
                    if row.get("chapter"):
                        chapter_slots.append(row)
                    elif row.get("volume"):
                        book_slots.append(row)
                    continue
                # A real release the sources carry: ``queue_missing`` has just
                # created its job, so the source rung answered by acting.
                self.database.record_wanted_attempt(
                    manga_id,
                    slot_key,
                    channel=CHANNEL_SOURCES,
                    outcome=QUEUED,
                    detail=str(row.get("source_name") or row.get("provider") or ""),
                )
                continue
            self.database.record_wanted_attempt(
                manga_id,
                slot_key,
                channel=CHANNEL_SOURCES,
                outcome=NOT_OFFERED,
                detail="no mapped source and no installed extension lists it",
            )
            if row.get("chapter"):
                chapter_slots.append(row)
            elif row.get("volume"):
                book_slots.append(row)

        torrents = self.torrents
        indexers_ready = torrents is not None and (
            torrents.prowlarr.enabled and torrents.prowlarr.configured
        )
        manga = self.database.get_manga(manga_id)
        english = str(manga.get("preferred_language") or "") == "en"
        # A series followed by volume has already been through the book hunt,
        # which *is* its indexer rung: recording what it found is what lets a
        # book slot reach a verdict instead of sitting on one channel forever.
        self._record_book_hunt(
            manga_id,
            book_slots,
            hunted,
            available=(indexers_ready or getattr(torrents, "direct_available", False))
            and english,
        )
        if not chapter_slots:
            return {"grabbed": 0, "searched": 0}
        if not indexers_ready or not english:
            reason = (
                "Prowlarr is not configured"
                if not indexers_ready
                else "the indexers Tankarr can read are English-only"
            )
            for row in chapter_slots:
                slot_key = slot_key_for(row)
                for channel in (CHANNEL_INDEXER_CHAPTER, CHANNEL_INDEXER_BOOK):
                    self.database.record_wanted_attempt(
                        manga_id,
                        slot_key,
                        channel=channel,
                        outcome=UNAVAILABLE,
                        detail=reason,
                    )
            return {"grabbed": 0, "searched": 0}

        # Slots nobody has asked the indexers about come first, so a backlog
        # of twenty-three chapters walks forward a few at a time instead of
        # re-searching the same five every pass and never reaching the rest.
        # Within each group the newest chapter wins: a reader waiting on a
        # series wants the one that just came out.
        from tankarr.wanted_recovery import recently_answered

        ledger = self.database.wanted_attempts(manga_id)
        # A slot the indexers answered this week is not asked again yet: the
        # searches go to slots still waiting for their first answer.
        chapter_slots = [
            row
            for row in chapter_slots
            if not recently_answered(
                ledger.get(slot_key_for(row), []), CHANNEL_INDEXER_CHAPTER
            )
        ]

        def sort_key(row: dict[str, Any]) -> tuple[int, float]:
            attempts = ledger.get(slot_key_for(row), [])
            asked = any(
                str(item.get("channel")) == CHANNEL_INDEXER_CHAPTER
                and str(item.get("outcome")) != UNAVAILABLE
                for item in attempts
            )
            try:
                recency = -float(str(row.get("chapter")))
            except (TypeError, ValueError):
                recency = 0.0
            return (1 if asked else 0, recency)

        for row in sorted(chapter_slots, key=sort_key)[
            : self.CHAPTER_RECOVERY_PER_SERIES
        ]:
            outcome = await self._recover_one_chapter(manga, row)
            searched += 1
            grabbed += int(outcome.get("grabbed") or 0)
        return {"grabbed": grabbed, "searched": searched}

    async def _recover_series_without_sources(self, manga_id: str) -> dict[str, Any]:
        """Ask the indexers for a work no installed source lists at all.

        There is no slot to fill, so nothing is ever grabbed from here: a
        book the indexers offer for an unknown-sized work is a question for
        the operator (Interactive Search), and the verdict is what tells
        Wanted whether that question exists or the work is simply not
        obtainable anywhere Tankarr can reach.
        """

        from tankarr.volume_hunt import _title_leads
        from tankarr.wanted_recovery import (
            AMBIGUOUS,
            CHANNEL_INDEXER_BOOK,
            CHANNEL_SOURCES,
            ERROR,
            NOT_OFFERED,
            SERIES_SLOT,
            UNAVAILABLE,
            recently_answered,
            series_queries,
        )

        self.database.record_wanted_attempt(
            manga_id,
            SERIES_SLOT,
            channel=CHANNEL_SOURCES,
            outcome=NOT_OFFERED,
            detail="no installed extension lists this work",
        )
        torrents = self.torrents
        manga = self.database.get_manga(manga_id)
        indexers_ready = torrents is not None and (
            torrents.prowlarr.enabled and torrents.prowlarr.configured
        )
        english = str(manga.get("preferred_language") or "") == "en"
        if not indexers_ready or not english:
            self.database.record_wanted_attempt(
                manga_id,
                SERIES_SLOT,
                channel=CHANNEL_INDEXER_BOOK,
                outcome=UNAVAILABLE,
                detail=(
                    "Prowlarr is not configured"
                    if not indexers_ready
                    else "the indexers Tankarr can read are English-only"
                ),
            )
            return {"grabbed": 0, "searched": 0}
        ledger = self.database.wanted_attempts(manga_id)
        if recently_answered(ledger.get(SERIES_SLOT, []), CHANNEL_INDEXER_BOOK):
            return {"grabbed": 0, "searched": 0}
        publishers, _single = self._publishers_for(manga_id)
        pooled: dict[str, dict[str, Any]] = {}
        errors: list[str] = []
        for query in series_queries(str(manga["title"]), publishers)[:2]:
            try:
                result = await torrents.search(
                    manga_id, query, limit=75, _include_download_ref=True
                )
            except Exception as exc:  # noqa: BLE001 - next query / next cycle
                errors.append(f"{type(exc).__name__}: {exc}"[:120])
                continue
            errors.extend(str(error)[:200] for error in result.get("errors") or [])
            for release in result.get("results") or []:
                # "Happy!" also matches an Ikigai PDF and a weekend
                # magazine: only a release named after the work is an offer.
                if not _title_leads(
                    str(manga["title"]), str(release.get("title") or "")
                ):
                    continue
                pooled.setdefault(str(release.get("id")), release)
        if not pooled and errors:
            outcome, detail = ERROR, "; ".join(errors[:2])
        elif pooled:
            outcome = AMBIGUOUS
            detail = (
                f"{len(pooled)} indexer release(s) carry this title; with no "
                "catalogue size nothing can be taken alone - use Interactive Search"
            )
        else:
            outcome, detail = NOT_OFFERED, "no indexer offers this work"
        self.database.record_wanted_attempt(
            manga_id,
            SERIES_SLOT,
            channel=CHANNEL_INDEXER_BOOK,
            outcome=outcome,
            detail=detail,
        )
        return {"grabbed": 0, "searched": 1}

    async def _notify_decisions(self) -> None:
        """Tell the operator, once, about each new thing only they can settle."""

        notifier = getattr(self.service, "notifier", None)
        if notifier is None or not self.settings.ntfy_on_decision_needed:
            return
        try:
            stored = self.database.get_setting_overrides()
            raw = stored.get(DECISIONS_NOTIFIED_SETTING) or ""
            known = json.loads(raw) if raw else {}
        except Exception:  # noqa: BLE001 - a corrupt memo is an empty memo
            known = {}
        known_exhausted = set(known.get("exhausted") or [])
        known_reviews = set(known.get("reviews") or [])

        exhausted: dict[str, str] = {}
        for entry in await asyncio.to_thread(self.service.list_wanted):
            title = str(entry["manga"]["title"])
            manga_id = str(entry["manga"]["id"])
            if (entry.get("recovery") or {}).get("verdict") == "exhausted":
                exhausted[f"{manga_id}:series"] = f"{title} (no source lists it)"
            for chapter in entry.get("chapters") or []:
                if (chapter.get("recovery") or {}).get("verdict") != "exhausted":
                    continue
                slot = str(chapter.get("slot_key") or chapter.get("id"))
                label = (
                    f"c{chapter['chapter']}"
                    if chapter.get("chapter")
                    else f"v{chapter.get('volume')}"
                )
                exhausted[f"{manga_id}:{slot}"] = f"{title} {label}"
        reviews = {
            str(review["id"]): f"{review.get('manga_title') or ''}: "
            f"{review.get('release_title') or review.get('title') or ''}"
            for review in self.database.list_match_reviews(open_only=True)
        }
        new_exhausted = [
            exhausted[key] for key in exhausted if key not in known_exhausted
        ]
        new_reviews = [reviews[key] for key in reviews if key not in known_reviews]
        if new_exhausted or new_reviews:
            lines: list[str] = []
            if new_exhausted:
                lines.append(
                    f"Not obtainable ({len(new_exhausted)}): "
                    + ", ".join(new_exhausted[:8])
                    + (" …" if len(new_exhausted) > 8 else "")
                )
            if new_reviews:
                lines.append(
                    f"Match reviews ({len(new_reviews)}): "
                    + "; ".join(item[:80] for item in new_reviews[:5])
                    + (" …" if len(new_reviews) > 5 else "")
                )
            summary_parts = []
            if new_exhausted:
                summary_parts.append(f"{len(new_exhausted)} not obtainable")
            if new_reviews:
                summary_parts.append(f"{len(new_reviews)} to review")
            await notifier.decision_needed(", ".join(summary_parts), "\n".join(lines))
        try:
            self.database.save_setting(
                DECISIONS_NOTIFIED_SETTING,
                json.dumps(
                    {"exhausted": sorted(exhausted), "reviews": sorted(reviews)},
                    separators=(",", ":"),
                ),
            )
        except Exception:  # noqa: BLE001 - the memo is advisory
            logger.debug("Could not persist the decision memo", exc_info=True)

    def _record_book_hunt(
        self,
        manga_id: str,
        book_slots: list[dict[str, Any]],
        hunted: dict[str, Any] | None,
        *,
        available: bool,
    ) -> None:
        """Write what the book hunt answered for each wanted volume slot."""

        from tankarr.wanted_recovery import (
            CHANNEL_INDEXER_BOOK,
            UNAVAILABLE,
            book_hunt_outcome,
            slot_key_for,
        )

        if not book_slots:
            return
        for row in book_slots:
            volume = str(row.get("volume") or "")
            if not available:
                outcome, detail = UNAVAILABLE, "Prowlarr is not configured"
            else:
                outcome, detail = book_hunt_outcome(hunted, volumes=[volume])
            self.database.record_wanted_attempt(
                manga_id,
                slot_key_for(row),
                channel=CHANNEL_INDEXER_BOOK,
                outcome=outcome,
                detail=detail,
            )

    async def _recover_one_chapter(
        self, manga: dict[str, Any], row: dict[str, Any]
    ) -> dict[str, Any]:
        """Ask the indexers for one chapter, then for the book that holds it."""

        from tankarr.series_summary import publication_summary
        from tankarr.volume_hunt import hunt_volumes
        from tankarr.wanted_recovery import (
            AMBIGUOUS,
            CHANNEL_INDEXER_BOOK,
            CHANNEL_INDEXER_CHAPTER,
            ERROR,
            GRABBED,
            NOT_OFFERED,
            book_hunt_outcome,
            chapter_queries,
            containing_volumes,
            merge_book_hunts,
            pick_chapter_release,
            slot_key_for,
        )

        manga_id = str(manga["id"])
        metadata = (self.database.get_series_metadata(manga_id) or {}).get("data")
        publication = publication_summary(manga, metadata)
        continuing = publication["status"] == "continuing" or publication["paused"]
        slot_key = slot_key_for(row)
        chapter = str(row.get("chapter"))
        publishers, single = self._publishers_for(manga_id)
        torrents = self.torrents
        pooled: dict[str, dict[str, Any]] = {}
        errors: list[str] = []
        for query in chapter_queries(str(manga["title"]), chapter, publishers):
            try:
                # Indexers only: archive.org holds whole books and is asked
                # once per pass by the book rung, never chapter by chapter.
                result = await torrents.search(
                    manga_id,
                    query,
                    limit=75,
                    _include_download_ref=True,
                    sources=("prowlarr",),
                )
            except Exception as exc:  # noqa: BLE001 - next query / next cycle
                errors.append(f"{type(exc).__name__}: {exc}"[:120])
                continue
            errors.extend(str(error)[:200] for error in result.get("errors") or [])
            for release in result.get("results") or []:
                pooled.setdefault(str(release.get("id")), release)
            chosen, _reason = pick_chapter_release(
                list(pooled.values()),
                title=str(manga["title"]),
                chapter=chapter,
                publisher=publishers,
            )
            if chosen is not None:
                break
        chosen, reason = pick_chapter_release(
            list(pooled.values()),
            title=str(manga["title"]),
            chapter=chapter,
            publisher=publishers,
        )
        self._queue_indexer_reviews(manga_id, pooled.values())
        if chosen is not None:
            try:
                job = await torrents.grab(
                    manga_id, str(chosen["provider"]), str(chosen["id"])
                )
                self.database.update_torrent_download(job["id"], chapter_hint=chapter)
                self.database.record_wanted_attempt(
                    manga_id,
                    slot_key,
                    channel=CHANNEL_INDEXER_CHAPTER,
                    outcome=GRABBED,
                    detail=str(chosen.get("title") or "")[:200],
                )
                return {"grabbed": 1}
            except Exception as exc:  # noqa: BLE001 - the book rung still runs
                self.database.record_wanted_attempt(
                    manga_id,
                    slot_key,
                    channel=CHANNEL_INDEXER_CHAPTER,
                    outcome=ERROR,
                    detail=f"{type(exc).__name__}: {exc}"[:200],
                )
        else:
            reviewed = any(release.get("_review") for release in pooled.values())
            outcome = AMBIGUOUS if reviewed else ERROR if errors else NOT_OFFERED
            self.database.record_wanted_attempt(
                manga_id,
                slot_key,
                channel=CHANNEL_INDEXER_CHAPTER,
                outcome=outcome,
                detail="; ".join(errors[:2]) if outcome == ERROR else reason,
            )

        # A chapter nobody scanned separately is often inside a book somebody
        # released. Only the catalogue's chapter↔volume map may say which one:
        # a volume number written in a chapter title is a hint, not a fact.
        volumes = containing_volumes(chapter, self.database.chapter_map(manga_id))
        if not volumes:
            # For continuing works the book hunt waits until chapter search
            # has failed. Without a map it may ask for any missing book,
            # once per pass; estimates never claim to identify its contents.
            hunted = self._last_book_hunt.get(manga_id) or {}
            newly_grabbed = 0
            if continuing and not (
                hunted.get("searched_volumes") or hunted.get("pending_volumes")
            ):
                additional = await self._hunt_missing_volumes(
                    {"manga": manga, "chapters": [row]}, chapter_fallback=True
                )
                newly_grabbed = len(additional.get("grabbed") or [])
                hunted = merge_book_hunts(hunted, additional)
                self._last_book_hunt[manga_id] = hunted
            outcome, detail = book_hunt_outcome(hunted)
            self.database.record_wanted_attempt(
                manga_id,
                slot_key,
                channel=CHANNEL_INDEXER_BOOK,
                outcome=outcome,
                detail=detail,
            )
            return {"grabbed": newly_grabbed}
        owned = {
            int(float(str(release["volume"])))
            for release in self.database.list_chapters(
                manga_id, str(manga["preferred_language"])
            )
            if release.get("downloaded")
            and str(release.get("release_unit") or "chapter") == "volume"
            and str(release.get("volume") or "").replace(".", "", 1).isdecimal()
        }
        wanted_books = [volume for volume in volumes if volume not in owned]
        if not wanted_books:
            self.database.record_wanted_attempt(
                manga_id,
                slot_key,
                channel=CHANNEL_INDEXER_BOOK,
                outcome=NOT_OFFERED,
                detail=f"book {volumes[0]} is already in the library",
            )
            return {"grabbed": 0}
        try:
            hunted = self._last_book_hunt.get(manga_id) or {}
            already_searched = set(hunted.get("searched_volumes") or []) | set(
                hunted.get("pending_volumes") or []
            )
            unsearched_books = sorted(set(wanted_books) - already_searched)
            newly_grabbed = 0
            if unsearched_books:
                if continuing:
                    additional = await self._hunt_missing_volumes(
                        {"manga": manga, "chapters": [row]}, chapter_fallback=True
                    )
                else:
                    additional = await hunt_volumes(
                        torrents,
                        manga=manga,
                        missing_volumes=unsearched_books,
                        publisher=publishers,
                        single_volume=single,
                        volume_count=self._volume_count_for(str(manga["id"])),
                        creators=self._creators_for(str(manga["id"])),
                    )
                merged = merge_book_hunts(hunted, additional)
                newly_grabbed = len(merged["grabbed"]) - len(
                    hunted.get("grabbed") or []
                )
                hunted = merged
                self._last_book_hunt[manga_id] = hunted
        except Exception as exc:  # noqa: BLE001 - next cycle retries
            self.database.record_wanted_attempt(
                manga_id,
                slot_key,
                channel=CHANNEL_INDEXER_BOOK,
                outcome=ERROR,
                detail=f"{type(exc).__name__}: {exc}"[:200],
            )
            return {"grabbed": 0}
        outcome, detail = book_hunt_outcome(hunted, volumes=wanted_books)
        self.database.record_wanted_attempt(
            manga_id,
            slot_key,
            channel=CHANNEL_INDEXER_BOOK,
            outcome=outcome,
            detail=detail,
        )
        return {"grabbed": newly_grabbed}

    def _queue_indexer_reviews(
        self, manga_id: str, releases: Iterable[dict[str, Any]]
    ) -> None:
        """Keep the near-misses the picker refused as questions, not as noise."""

        for release in releases:
            note = release.get("_review")
            if not note:
                continue
            try:
                self.database.add_match_review(
                    manga_id,
                    kind="release",
                    provider=str(release.get("provider") or "prowlarr"),
                    candidate_id=str(release.get("id")),
                    title=str(release.get("title") or ""),
                    source_name=release.get("indexer"),
                    source_url=release.get("source_url"),
                    confidence=0.6,
                    reason=str(note),
                    payload={
                        key: value
                        for key, value in release.items()
                        if key not in {"_review", "_review_resolution"}
                    },
                    resolution=release.get("_review_resolution"),
                )
            except Exception:  # noqa: BLE001 - reviews are best effort
                logger.debug("Could not queue a release review", exc_info=True)

    async def _hunt_missing_volumes(
        self, entry: dict[str, Any], *, chapter_fallback: bool = False
    ) -> dict[str, Any]:
        """Ask the indexers and the archive for the books that would fill the gaps.

        Every series is asked by book, whatever unit it follows: a chapter no
        source offers may sit inside a volume an indexer or archive.org
        holds, and the release that completes the work is the one kept. The
        catalogue's chapter↔volume map says which book holds a missing
        chapter; without a map, every book not on disk is a candidate.
        """

        from tankarr.volume_hunt import hunt_volumes

        torrents = self.torrents
        if torrents is None or not (
            (torrents.prowlarr.enabled and torrents.prowlarr.configured)
            or getattr(torrents, "direct_available", False)
        ):
            return {"grabbed": [], "errors": [], "searched_volumes": []}
        manga = self.database.get_manga(str(entry["manga"]["id"]))
        if str(manga.get("preferred_language") or "") != "en":
            return {"grabbed": [], "errors": [], "searched_volumes": []}
        from tankarr.series_summary import publication_summary

        metadata_row = self.database.get_series_metadata(str(manga["id"]))
        data = (metadata_row or {}).get("data") or {}
        publication = publication_summary(manga, data)
        continuing = publication["status"] == "continuing" or publication["paused"]
        current = (
            self.database.preferred_missing_releases(str(manga["id"]))
            if continuing
            else []
        )
        offered_chapters = {
            str(release["chapter"])
            for release in current
            if release.get("chapter") is not None and not release.get("blocked")
        }
        missing: list[int] = []
        chapter_gaps: list[str] = []
        for row in entry.get("chapters") or []:
            if row.get("queue_status"):
                continue
            if row.get("chapter"):
                if continuing and str(row["chapter"]) in offered_chapters:
                    continue
                if (
                    continuing
                    and not chapter_fallback
                    and torrents.prowlarr.enabled
                    and torrents.prowlarr.configured
                ):
                    # _recover_one_chapter tries the numbered release first.
                    continue
                # A blocked chapter is a gap too: every copy was refused.
                chapter_gaps.append(str(row["chapter"]))
                continue
            if row.get("blocked") or not row.get("volume"):
                continue
            try:
                missing.append(int(float(str(row["volume"]))))
            except ValueError:
                continue
        if chapter_gaps:
            missing.extend(self._books_holding(manga, chapter_gaps))
        if int(entry.get("unmapped_expected_count") or 0) > 0:
            # The catalogue counts chapters no source numbers: nothing to
            # name, so every book not on disk is asked for.
            missing.extend(self._books_holding(manga, []))
        missing = sorted(set(missing))
        if not missing:
            return {"grabbed": [], "errors": [], "searched_volumes": []}
        # Only a live download occupies a missing slot. An imported book
        # whose library file was deleted must be searched again; pack hints
        # occupy every volume they cover while the download is pending.
        taken = {
            str(volume)
            for volume in self.database.pending_book_volumes([str(manga["id"])]).get(
                str(manga["id"]), set()
            )
        }
        taken.update(
            str(release["volume"])
            for release in current
            if release.get("volume")
            and not release.get("chapter")
            and release.get("queue_status")
        )
        pending = [volume for volume in missing if str(volume) in taken]
        missing = [volume for volume in missing if str(volume) not in taken]
        if not missing:
            return {
                "grabbed": [],
                "errors": [],
                "searched_volumes": [],
                "pending_volumes": pending,
            }
        # English edition publishers first (that is how the releases are
        # named), then the original one.
        publishers: list[str] = []
        for item in data.get("publishers") or []:
            name = (
                str((item or {}).get("name") or "").strip()
                if isinstance(item, dict)
                else str(item or "").strip()
            )
            kind = (
                str((item or {}).get("type") or "").casefold()
                if isinstance(item, dict)
                else ""
            )
            if name and kind in {"english", "en"} and name not in publishers:
                publishers.append(name)
        original = str(data.get("publisher") or "").strip()
        if original and original not in publishers:
            publishers.append(original)
        volume_count = self._volume_count_for(str(manga["id"]))
        creators = self._creators_for(str(manga["id"]))
        hunted: dict[str, Any] = {"grabbed": [], "errors": [], "searched_volumes": []}
        # By every name the indexers use: the catalogue title, then the
        # Latin alternates, until the books are grabbed.
        for title in self._hunt_titles(manga):
            attempt = await hunt_volumes(
                torrents,
                manga={**manga, "title": title},
                missing_volumes=[
                    v
                    for v in missing
                    if v not in {b.get("volume") for b in hunted["grabbed"]}
                    and str(v) not in {str(b.get("volume")) for b in hunted["grabbed"]}
                ],
                publisher=publishers,
                single_volume=volume_count == 1,
                volume_count=volume_count,
                creators=creators,
            )
            hunted = {
                "grabbed": [*hunted["grabbed"], *(attempt.get("grabbed") or [])],
                "errors": [*hunted["errors"], *(attempt.get("errors") or [])],
                "searched_volumes": sorted(
                    {
                        *hunted["searched_volumes"],
                        *(attempt.get("searched_volumes") or []),
                    }
                ),
                **{
                    k: v
                    for k, v in attempt.items()
                    if k not in ("grabbed", "errors", "searched_volumes")
                },
            }
            grabbed_now = {str(b.get("volume")) for b in hunted["grabbed"]}
            if all(str(v) in grabbed_now for v in missing):
                break
        hunted["pending_volumes"] = pending
        return hunted

    def _books_holding(self, manga: dict[str, Any], chapters: list[str]) -> list[int]:
        """The volumes a book hunt should ask for, given these missing chapters."""

        from tankarr.chapter_map import chapters_by_volume
        from tankarr.chapter_mapping import canonical_number

        manga_id = str(manga["id"])
        wanted = {canonical_number(chapter) or str(chapter) for chapter in chapters}
        by_volume = (
            chapters_by_volume(self.database.chapter_map(manga_id)) if wanted else {}
        )
        volumes: set[int] = set()
        mapped_any = False
        for volume, held in by_volume.items():
            try:
                number = int(float(str(volume)))
            except ValueError:
                continue
            if held & wanted:
                mapped_any = True
                volumes.add(number)
        if mapped_any:
            return sorted(volumes)
        # No map places these chapters: every book the catalogue counts that
        # is not on disk is a candidate, and the import sorts out overlap.
        metadata_row = self.database.get_series_metadata(manga_id)
        data = (metadata_row or {}).get("data") or {}
        try:
            volume_count = int(data.get("volume_count") or 0)
        except (TypeError, ValueError):
            volume_count = 0
        if volume_count <= 0:
            return []
        owned: set[int] = set()
        for release in self.database.list_chapters(
            manga_id, str(manga["preferred_language"])
        ):
            if not release.get("downloaded"):
                continue
            if str(release.get("release_unit") or "chapter") != "volume":
                continue
            try:
                owned.add(int(float(str(release.get("volume")))))
            except (TypeError, ValueError):
                continue
        return [number for number in range(1, volume_count + 1) if number not in owned]

    async def _refresh_primary_source(
        self, manga_id: str, language: str
    ) -> dict[str, Any]:
        manga = self.database.get_manga(manga_id)
        try:
            return await self.service.refresh_manga(manga_id, language)
        except RecoveryBlocked:
            raise
        except Exception as exc:
            if PRIMARY_SOURCE_FAILURE_NOTE not in getattr(exc, "__notes__", ()):
                raise
            # The service records this request failure in source health.
            # Keep retrying it on later passes while other sources can fill
            # the series now; local mutation errors must still fail the pass.
            logger.info("Primary source unavailable for %s: %s", manga_id, exc)
            return {
                "manga_id": manga_id,
                "language": language,
                "monitor_mode_before": manga["monitor_mode"],
                "seen": 0,
                "new_chapter_ids": [],
            }

    async def refresh_one(self, manga_id: str) -> dict[str, Any]:
        manga = self.database.get_manga(manga_id)
        was_initialized = manga["monitor_initialized"]
        result = await self._refresh_primary_source(
            manga_id, manga["preferred_language"]
        )
        if self.release_sources is not None:
            mapped = await self.release_sources.refresh_mappings(
                manga_id,
                monitor_new=result["monitor_mode_before"] in FUTURE_MONITOR_MODES,
            )
            result["seen"] = int(result.get("seen") or 0) + int(mapped["seen"])
            result["new_chapter_ids"] = list(
                dict.fromkeys(
                    [
                        *(result.get("new_chapter_ids") or []),
                        *(mapped.get("new_chapter_ids") or []),
                    ]
                )
            )
            result["release_sources"] = mapped["sources"]
        result[
            "official_evidence"
        ] = await self.service.refresh_official_edition_evidence(manga_id)
        if "mapped-ranges" in (result.get("reclassified_providers") or []):
            # Files just recognised as the books: the loose chapters they
            # replace go now, not at the next six-hourly pass.
            try:
                await self.service.retire_redundant_chapter_files(manga_id)
            except Exception as exc:  # noqa: BLE001 - the pass will retry
                logger.info("Could not retire chapters after a range repair: %s", exc)
        queued = 0
        if was_initialized and result["monitor_mode_before"] in FUTURE_MONITOR_MODES:
            queued = await self.queue_missing(manga_id, result["new_chapter_ids"])
        pages_counted = await self.service.backfill_page_counts(manga_id)
        self.database.record_monitor_result(manga_id)
        return {
            "manga_id": manga_id,
            "language": manga["preferred_language"],
            "seen": result["seen"],
            "new": len(result["new_chapter_ids"]),
            "queued": queued,
            "pages_counted": pages_counted,
            "baseline": not was_initialized,
        }

    async def configure_monitor_mode(self, manga_id: str, mode: str) -> dict[str, Any]:
        selected = normalize_monitor_mode(mode)
        # Monitoring a finished work, or one with no source yet, costs one
        # discovery pass that finds nothing: the stored reason explains why
        # nothing new is expected, but the operator's choice is honoured.
        self.database.configure_monitor_mode(manga_id, selected)
        queued = 0
        if selected in BACKLOG_MONITOR_MODES:
            queued = await self.queue_missing(manga_id)
        updated = self.database.get_manga(manga_id)
        updated["queued"] = queued
        return updated

    async def queue_missing(
        self, manga_id: str, chapter_ids: list[str] | None = None
    ) -> int:
        queued = 0
        jobs = await self.service.create_missing_download_jobs(manga_id, chapter_ids)
        for job in jobs:
            if job["status"] == "queued":
                await self.worker.enqueue(job["id"])
                queued += 1
        return queued

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.settings.monitor_enabled,
            "interval_seconds": self.settings.monitor_interval_seconds,
            "running": self.task is not None and not self.task.done(),
            "last_cycle_at": self.last_cycle_at,
            "last_cycle_error": self.last_cycle_error,
            "wanted_search": {
                "enabled": self.settings.wanted_search_enabled,
                "interval_seconds": self.settings.wanted_search_interval_seconds,
                "last_search_at": self.last_wanted_search_at,
                "next_search_at": self._next_wanted_search_at(),
                "last_result": self.last_wanted_search_result,
                "running": self._wanted_search_task is not None
                and not self._wanted_search_task.done(),
                "started_at": self.wanted_started_at,
                "current_manga_id": self.wanted_current_manga_id,
                "progress": dict(self.wanted_progress),
                "last_error": self.last_wanted_search_error,
            },
        }
