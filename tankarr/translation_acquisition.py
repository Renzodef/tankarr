"""Conservative indexer fallback for individually numbered foreign books."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import shutil
from pathlib import Path

from tankarr.archive import package_cbz_validated
from tankarr.chapter_mapping import canonical_number
from tankarr.importer import ARCHIVE_SUFFIXES
from tankarr.languages import SUPPORTED_LANGUAGES
from tankarr.release_kind import classify_release, title_matches_release
from tankarr.translation_policy import matches_slot, slot_key, source_rank

logger = logging.getLogger(__name__)


def release_language(title: str, language: str) -> bool:
    """A search language is not evidence. Require an explicit name marker too.

    The processor must subsequently verify the language of the actual OCR text.
    This only avoids downloading obviously unrelated language editions.
    """
    labels = dict(SUPPORTED_LANGUAGES)
    names = [language, labels.get(language, "").split(" (")[0]]
    if language == "ja":
        names += ["jpn", "jap", "raw"]
    return any(
        name
        and re.search(
            r"(?<![a-z])" + re.escape(name.casefold()) + r"(?![a-z])", title.casefold()
        )
        for name in names
    )


class TranslationAcquisition:
    def __init__(self, manager, torrents):
        self.manager = manager
        self.torrents = torrents
        self.database = manager.database

    def can_finish_source(self, job: dict) -> bool:
        evidence = job.get("language_evidence") or {}
        if "translation_jobs" not in evidence:
            return False
        try:
            return all(
                self.manager.store.get(identifier)["status"] == "completed"
                for identifier in evidence["translation_jobs"]
            )
        except KeyError:
            return False

    async def finish_sources(self):
        if self.torrents.completed_action != "remove_after_import":
            return
        for job in self.database.iter_torrent_cleanup():
            if (
                (job.get("language_evidence") or {}).get("translation_request")
                and job.get("qbit_state") != "removed"
                and self.can_finish_source(job)
            ):
                try:
                    await self.torrents._remove_from_client(
                        job,
                        "translation imported; source removed according to download policy",
                    )
                except Exception:  # noqa: BLE001 - retry client cleanup independently of completed translation
                    self.database.update_torrent_download(
                        job["id"],
                        message="Translation imported; waiting to apply download retention policy",
                    )

    async def hunt(
        self,
        manga_id: str,
        languages: list[str],
        *,
        search_languages: list[str] | None = None,
    ) -> int:
        manga = self.database.get_manga(manga_id)
        if (
            not self.manager.enabled(manga)
            or manga.get("edition_book_count") is not None
        ):
            # A differently sized edition requires explicit book/chapter assignment.
            return 0
        known = self.manager.store.slots(manga_id, manga["preferred_language"])
        pending = self.database.list_torrent_downloads(manga_id=manga_id, limit=500)
        taken = {
            key
            for job in pending
            if job["status"] != "failed"
            for key in (job.get("language_evidence") or {})
            .get("translation_request", {})
            .get("slots", [])
        }
        missing = {
            slot_key(item)
            for item in self.manager._missing(manga_id)
            if item.get("chapter") in {None, ""} and slot_key(item) not in known | taken
        }
        if not missing:
            return 0
        target = self.manager.release_sources._target(manga_id)
        titles = list(
            dict.fromkeys([manga["title"], *target.get("alternate_titles", [])])
        )[:3]
        candidates = {}
        archive = getattr(self.torrents, "internet_archive", None)
        if archive is not None and archive.enabled:
            try:
                # One unrestricted Archive search, not one search per language.
                releases = await archive.search_translation(manga["title"], limit=50)
            except Exception as error:  # noqa: BLE001 - Archive must not hide indexer candidates
                logger.warning(
                    "Translation Archive search unavailable (%s)", type(error).__name__
                )
            else:
                for release in releases:
                    if release.get("language") in languages and title_matches_release(
                        manga["title"], release["title"]
                    ):
                        candidates[(release["provider"], str(release["id"]))] = dict(
                            release
                        )
        if self.manager.settings.prowlarr_enabled:
            for language in (
                search_languages if search_languages is not None else languages[:3]
            ):
                if language not in languages:
                    continue
                for title in titles:
                    try:
                        releases = await self.torrents.prowlarr.search(
                            title, language=language, limit=50
                        )
                    except Exception as error:  # noqa: BLE001 - retain working direct-download candidates
                        logger.warning(
                            "Translation indexer search unavailable (%s)",
                            type(error).__name__,
                        )
                        continue
                    for release in releases:
                        if release_language(
                            release.get("title", ""), language
                        ) and title_matches_release(title, release["title"]):
                            candidates[
                                (
                                    str(release.get("provider") or "prowlarr"),
                                    str(release["id"]),
                                )
                            ] = {**release, "language": language}
        grabbed = 0
        for release in sorted(
            candidates.values(), key=lambda item: source_rank(item, manga)
        ):
            language = release["language"]
            current = self.database.get_manga(manga_id)
            if (
                not self.manager.enabled(current)
                or current["preferred_language"] != manga["preferred_language"]
                or current.get("edition_book_count") is not None
            ):
                return grabbed
            self.manager.service.assert_mutations_allowed()
            kind = classify_release(title=release["title"], language=language)
            if kind.kind not in {"volume", "pack"}:
                continue
            volumes = kind.volumes if kind.kind == "pack" else (kind.volume,)
            selected = {
                f"volume:{canonical_number(volume)}" for volume in volumes
            } & missing
            selected &= {
                slot_key(item) for item in self.manager._missing(manga_id)
            } - self.manager.store.slots(manga_id, manga["preferred_language"])
            if not selected:
                continue
            if (
                release.get("protocol", "torrent") == "torrent"
                and int(release.get("seeders") or 0) <= 0
            ):
                continue
            request = {
                "source_language": language,
                "target_language": manga["preferred_language"],
                "slots": sorted(selected),
            }
            provider = str(release.get("provider") or "prowlarr")
            self.torrents._release_cache[(manga_id, provider, str(release["id"]))] = (
                asyncio.get_running_loop().time(),
                dict(release),
            )
            await self.torrents.grab(
                manga_id, provider, str(release["id"]), translation=request
            )
            missing -= selected
            grabbed += 1
            if not missing or grabbed >= 2:
                return grabbed
        return grabbed

    async def stage_torrent(self, job: dict, content: Path) -> dict:
        request = job["language_evidence"]["translation_request"]
        manga = self.database.get_manga(job["manga_id"])
        if not self.manager.enabled(manga):
            return self.database.update_torrent_download(
                job["id"],
                status="completed",
                message="Source downloaded; waiting for translation fallback to be enabled",
            )
        if manga["preferred_language"] != request["target_language"]:
            return self.database.update_torrent_download(
                job["id"],
                status="failed",
                message="Translation target changed; source archive preserved",
            )
        if manga.get("edition_book_count") is not None:
            return self.database.update_torrent_download(
                job["id"],
                status="failed",
                message="Managed edition changed; explicit source assignment is required",
            )
        self.manager.service.assert_mutations_allowed()
        scan = await asyncio.to_thread(
            self.manager.importer.scan_torrent_content, content
        )
        owned = self.database.list_chapters(manga["id"], request["target_language"])
        planned = {
            key
            for key in request["slots"]
            if not any(
                item.get("downloaded") and matches_slot(item, key) for item in owned
            )
        }
        staged = []
        for item in scan["items"]:
            path = (scan["root"] / item["path"]).resolve()
            if (
                not path.is_relative_to(scan["root"].resolve())
                or path.is_symlink()
                or path.suffix.casefold() not in ARCHIVE_SUFFIXES
            ):
                continue
            volume = canonical_number(item.get("volume"))
            if volume is None and len(scan["items"]) == 1:
                volume = canonical_number(job.get("volume_hint"))
            if item.get("chapter") or f"volume:{volume}" not in planned:
                continue
            source = {
                "id": f"torrent:{job['id']}:{hashlib.sha256(item['path'].encode()).hexdigest()[:16]}",
                "provider": "upload",
                "language": request["source_language"],
                "chapter": None,
                "volume": volume,
                "release_unit": "volume",
                "title": job["title"],
                "source_url": job.get("source_url", ""),
                "source_name": job.get("indexer"),
            }
            async with self.manager.service._mutation_lock:
                self.manager.service.assert_mutations_allowed()
                current = self.database.get_manga(manga["id"])
                if (
                    not self.manager.enabled(current)
                    or current["preferred_language"] != request["target_language"]
                ):
                    return self.database.update_torrent_download(
                        job["id"],
                        status="completed",
                        message="Translation settings changed; source archive preserved",
                    )
                translated = self.manager.store.create(
                    manga["id"], source, request["target_language"]
                )
                if translated["source"]["id"] != source["id"]:
                    continue
                directory = self.manager.directory(translated)
                directory.mkdir(parents=True, exist_ok=True)
                if not (directory / "source.cbz").exists():
                    pages = await self.manager.service._finish_import_thread(
                        self.manager.importer._extract_pages,
                        path,
                        directory / "source-pages",
                    )
                    await self.manager.service._finish_import_thread(
                        package_cbz_validated,
                        directory / "source.cbz",
                        pages,
                        manga,
                        source,
                    )
                    await asyncio.to_thread(
                        shutil.rmtree, directory / "source-pages", True
                    )
                staged.append(translated["id"])
        if planned and not staged:
            return self.database.update_torrent_download(
                job["id"],
                status="failed",
                message="No unambiguous source CBZ matched the missing books; source preserved",
            )
        return self.database.update_torrent_download(
            job["id"],
            status="imported",
            progress=1,
            content_path=content,
            language_evidence={**job["language_evidence"], "translation_jobs": staged},
            message=f"Source staged for {len(staged)} translation job(s); no foreign file imported into the library",
        )
