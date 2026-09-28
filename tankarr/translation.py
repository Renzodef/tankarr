"""Optional translation fallback, with local OCR or an external processor."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import zipfile
from contextlib import suppress
from pathlib import Path

import httpx

from tankarr.archive import package_cbz_validated, sha256, validate_cbz
from tankarr.http import async_client
from tankarr.importer import LibraryImporter
from tankarr.local_translation import LocalTranslationError, translate_archive
from tankarr.providers.base import ProviderRequestError
from tankarr.service import local_page_content_sha256
from tankarr.translation_policy import (
    eligible_source,
    fallback_languages,
    matches_slot,
    slot_key,
    validate_translation_url,
)
from tankarr.translation_store import TranslationStore

logger = logging.getLogger(__name__)


async def file_chunks(path: Path):
    with path.open("rb") as handle:
        while chunk := await asyncio.to_thread(handle.read, 1024 * 1024):
            yield chunk


class TranslationManager:
    def __init__(self, settings, database, service, release_sources):
        self.settings = settings
        self.database = database
        self.service = service
        self.release_sources = release_sources
        self.store = TranslationStore(database)
        self.importer = LibraryImporter(settings, database, service)
        self.task = None
        self.acquisition = None
        self.lock = asyncio.Lock()

    def directory(self, job: dict) -> Path:
        return self.settings.staging_dir / "translations" / job["id"]

    def configured(self) -> bool:
        return bool(
            self.settings.translation_enabled
            and all(
                (
                    self.settings.translation_ai_url,
                    self.settings.translation_ai_model,
                    self.settings.translation_ai_api_key,
                )
            )
        )

    def enabled(self, manga: dict) -> bool:
        return self.configured() and bool(manga.get("translation_enabled"))

    async def queue_missing(self, manga_id: str) -> int:
        manga = self.database.get_manga(manga_id)
        if not self.enabled(manga):
            return 0
        self.service.assert_mutations_allowed()
        languages = fallback_languages(
            manga, self.settings.translation_source_languages
        )
        if not languages or not self._missing(manga_id):
            return 0
        # Discovery uses exactly the normal identity checks, without changing the
        # preferred language or monitoring foreign releases as native downloads.
        for language in languages:
            await self.release_sources.discover_and_refresh(
                manga_id, monitor_new=False, language=language
            )
        async with self.service._mutation_lock:
            manga = self.database.get_manga(manga_id)
            if not self.enabled(manga):
                return 0
            blocked = self.database.blocked_releases(manga_id)
            candidates = [
                source
                for source in self.database.list_all_chapters(manga_id)
                if eligible_source(source, languages) and source["id"] not in blocked
            ]
            candidates.sort(
                key=lambda source: (
                    languages.index(source["language"]),
                    str(source["id"]),
                )
            )
            existing = self.store.slots(manga_id, manga["preferred_language"])
            queued = 0
            for missing in self._missing(manga_id):
                key = slot_key(missing)
                if key in existing:
                    continue
                source = next(
                    (item for item in candidates if slot_key(item) == key), None
                )
                if source is None:
                    continue
                self.store.create(manga_id, source, manga["preferred_language"])
                existing.add(key)
                queued += 1
                if queued >= 10:
                    break
        if self.acquisition is not None:
            queued += await self.acquisition.hunt(manga_id, languages)
        return queued

    def _missing(self, manga_id: str) -> list[dict]:
        # A native release still queued or obtainable always wins, even if the
        # foreign edition was discovered first. Wanted also respects slot overrides.
        entry = next(
            (
                item
                for item in self.service.list_wanted()
                if item["manga"]["id"] == manga_id
            ),
            None,
        )
        if not entry:
            return []
        if any(
            job["status"] not in {"failed", "imported", "rejected", "cancelled"}
            and not (job.get("language_evidence") or {}).get("translation_request")
            for job in self.database.list_torrent_downloads(
                manga_id=manga_id, limit=500
            )
        ):
            return []
        return [
            item
            for item in entry["chapters"]
            if (item.get("expected") or item.get("blocked"))
            and not item.get("queue_job_id")
        ]

    async def start(self):
        if self.task is None:
            self.task = asyncio.create_task(self._run(), name="tankarr-translation")

    async def stop(self):
        if self.task is not None:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
            self.task = None

    async def _run(self):
        while True:
            try:
                await self.tick()
            except Exception:  # noqa: BLE001 - an optional integration must not stop monitoring
                logger.warning("Translation queue temporarily unavailable")
            await asyncio.sleep(15)

    async def tick(self):
        if self.settings.restored_safe_mode:
            return
        await self.flush_cancellations()
        if not self.configured():
            return
        async with self.lock:
            self.service.assert_mutations_allowed()
            active = [
                job
                for job in self.store.list(active=True, enabled_only=True)
                if self.enabled(self.database.get_manga(job["manga_id"]))
            ]
            # Two outstanding processor jobs allow small installations to use
            # two workers. Downloads and archive work on the Tankarr host stay
            # serial.
            active.sort(
                key=lambda job: (
                    2
                    if job["status"] == "waiting_native"
                    else int(job["status"] == "queued"),
                    job["created_at"],
                )
            )
            for job in active[:2]:
                endpoint = (
                    "Translation processor"
                    if self.settings.translation_processor_url
                    else "AI provider"
                )
                try:
                    await self.process(job)
                except LocalTranslationError as exc:
                    self.store.update(job["id"], status="failed", message=str(exc))
                except httpx.HTTPStatusError as exc:
                    if (
                        400 <= exc.response.status_code < 500
                        and exc.response.status_code != 429
                    ):
                        self.store.update(
                            job["id"],
                            status="failed",
                            message=f"{endpoint} rejected the request (HTTP {exc.response.status_code}); check its settings before retrying",
                        )
                    else:
                        self.store.update(
                            job["id"],
                            message=f"Waiting for {endpoint.lower()}; retrying automatically",
                        )
                except (httpx.HTTPError, OSError):
                    self.store.update(
                        job["id"],
                        message=f"Waiting for {endpoint.lower()}; retrying automatically",
                    )
                except (ValueError, KeyError, zipfile.BadZipFile, ProviderRequestError):
                    self.store.update(
                        job["id"],
                        status="failed",
                        message="Translation or archive validation failed; retry after checking the processor",
                    )
            if self.acquisition is not None:
                await self.acquisition.finish_sources()

    def cancel_job(self, job, message=None):
        if message is None:
            message = (
                "Cancelled; remote cancellation requested"
                if self.settings.translation_processor_url
                else "Cancelled; stopping local processing"
            )
        result = self.store.update(job["id"], status="cancelled", message=message)
        if self.settings.translation_processor_url:
            base = validate_translation_url(self.settings.translation_processor_url)
            remote_id = f"{job['id']}-{job['attempts']}"
            root = self.settings.staging_dir / "translations" / "cancel-requests"
            root.mkdir(parents=True, exist_ok=True)
            path = root / (remote_id + ".json")
            partial = path.with_suffix(".partial")
            partial.write_text(json.dumps({"remote_id": remote_id, "base": base}))
            partial.chmod(0o600)
            partial.replace(path)
        return result

    async def flush_cancellations(self):
        root = self.settings.staging_dir / "translations" / "cancel-requests"
        if not root.exists() or not self.settings.translation_processor_url:
            return
        base = validate_translation_url(self.settings.translation_processor_url)
        headers = (
            {"Authorization": f"Bearer {self.settings.translation_processor_token}"}
            if self.settings.translation_processor_token
            else {}
        )
        async with async_client(
            headers=headers, timeout=10, follow_redirects=False
        ) as client:
            for path in list(root.glob("*.json"))[:20]:
                request = json.loads(path.read_text())
                if request["base"] != base:
                    # Never send a new processor's credential to a previous endpoint.
                    continue
                try:
                    response = await client.delete(
                        f"{base}/jobs/{request['remote_id']}"
                    )
                    if response.status_code != 404:
                        response.raise_for_status()
                except httpx.HTTPError:
                    continue
                path.unlink(missing_ok=True)

    def _current(self, job: dict) -> dict | None:
        manga = self.database.get_manga(job["manga_id"])
        if not self.enabled(manga) or self.store.get(job["id"])["status"] in {
            "cancelled",
            "failed",
        }:
            return None
        if manga["preferred_language"] != job["target_language"]:
            self.cancel_job(job, "Series language changed")
            return None
        if "target_edition_book_count" in job["source"] and job["source"][
            "target_edition_book_count"
        ] != manga.get("edition_book_count"):
            self.cancel_job(
                job,
                "Managed edition changed; assign the source to the correct book again",
            )
            return None
        releases = self.database.list_chapters(manga["id"], job["target_language"])
        for owned in releases:
            if owned.get("downloaded") and matches_slot(owned, job["slot_key"]):
                own_result = owned["id"] == f"translated:{job['id']}"
                self.store.update(
                    job["id"],
                    status="completed" if own_result else "cancelled",
                    result_chapter_id=owned["id"] if own_result else None,
                    message="Imported"
                    if own_result
                    else "A release is already in the library",
                )
                if not own_result:
                    self.cancel_job(job, "A release is already in the library")
                return None
        blocked = self.database.blocked_releases(manga["id"])
        if any(
            release["id"] not in blocked
            and release.get("provider") in self.service.providers
            and release.get("provider") not in {"local", "catalogue"}
            and release.get("numbering_status", "mapped") == "mapped"
            and matches_slot(release, job["slot_key"])
            for release in releases
        ):
            self.store.update(
                job["id"],
                status="waiting_native",
                message="Waiting for the available release in the requested language",
            )
            return None
        return manga

    async def _prepare(self, job: dict, manga: dict) -> Path:
        directory = self.directory(job)
        source_file = directory / "source.cbz"
        if source_file.exists():
            return source_file
        source = self.database.get_chapter(job["source"]["id"])
        if source["id"] in self.database.blocked_releases(job["manga_id"]):
            raise ValueError("Source release is blocked")
        if (
            source["manga_id"] != job["manga_id"]
            or source["language"] != job["source"]["language"]
            or slot_key(source) != job["slot_key"]
        ):
            raise ValueError("Source identity changed")
        self.store.update(
            job["id"],
            status="preparing",
            message="Downloading the source edition for translation",
        )
        pages_dir = directory / "input-pages"
        pages_dir.mkdir(parents=True, exist_ok=True)

        async def progress(done, total):
            self.store.update(
                job["id"], message=f"Downloading source: {done}/{total} pages"
            )

        if source.get("downloaded") and source.get("library_path"):
            path = self.service._recorded_library_path(
                source["library_path"], self.service._library_root()
            )
            pages = await asyncio.to_thread(
                self.importer._extract_pages, path, pages_dir
            )
        else:
            pages = await self.service.provider_for(source["provider"]).download_pages(
                source["id"],
                pages_dir,
                progress,
                concurrency=self.settings.download_concurrency,
            )
        # Reuse import limits and image geometry checks for downloaded pages too.
        checked = await asyncio.to_thread(
            self.importer._extract_pages, pages_dir, directory / "checked-input"
        )
        if len(checked) != len(pages):
            raise ValueError("Source page count changed")
        await asyncio.to_thread(
            package_cbz_validated, source_file, checked, manga, source
        )
        return source_file

    async def process(self, job: dict):
        manga = self._current(job)
        if manga is None:
            return
        source = await self._prepare(job, manga)
        if self._current(job) is None:
            return
        if not self.settings.translation_processor_url:
            await self._process_local(job, source)
            return
        base = validate_translation_url(self.settings.translation_processor_url)
        headers = (
            {"Authorization": f"Bearer {self.settings.translation_processor_token}"}
            if self.settings.translation_processor_token
            else {}
        )
        remote_id = f"{job['id']}-{job['attempts']}"
        url = f"{base}/jobs/{remote_id}"
        async with async_client(
            headers=headers, timeout=60, follow_redirects=False
        ) as client:
            response = await client.get(url)
            if response.status_code == 404:
                response = await client.put(
                    url + "/source",
                    content=file_chunks(source),
                    headers={
                        "Content-Type": "application/zip",
                        "Content-Length": str(source.stat().st_size),
                    },
                )
                response.raise_for_status()
                response = await client.post(
                    url,
                    json={
                        "source_sha256": await asyncio.to_thread(sha256, source),
                        "source_language": job["source"]["language"],
                        "target_language": job["target_language"],
                        "ai": {
                            "base_url": validate_translation_url(
                                self.settings.translation_ai_url
                            ),
                            "model": self.settings.translation_ai_model,
                            "api_key": self.settings.translation_ai_api_key,
                        },
                    },
                )
                response.raise_for_status()
            else:
                response.raise_for_status()
            result = response.json()
            if result.get("status") in {"failed", "cancelled", "cancelling"}:
                raise ValueError("Processor failed")
            if result.get("status") not in {"queued", "running", "completed"}:
                raise ValueError("Invalid processor status")
            if self._current(job) is None:
                return
            self.store.update(
                job["id"],
                status="processing",
                message="OCR and translation in progress"
                if result["status"] == "running"
                else "Waiting for translation processor",
            )
            if result.get("status") != "completed":
                return
            output = self.directory(job) / "result.cbz"
            partial = output.with_suffix(".partial")
            try:
                async with client.stream("GET", url + "/result") as stream:
                    stream.raise_for_status()
                    total = 0
                    with partial.open("wb") as handle:
                        async for chunk in stream.aiter_bytes(1024 * 1024):
                            total += len(chunk)
                            if total > self.settings.import_max_expanded_bytes:
                                raise ValueError(
                                    "Translation result exceeds import limit"
                                )
                            await asyncio.to_thread(handle.write, chunk)
                partial.replace(output)
            finally:
                partial.unlink(missing_ok=True)
        await self._publish(job, output, source)

    async def _process_local(self, job: dict, source: Path):
        output = self.directory(job) / "result.cbz"
        request = {
            "source_sha256": await asyncio.to_thread(sha256, source),
            "source_language": job["source"]["language"],
            "target_language": job["target_language"],
            "ai": {
                "base_url": validate_translation_url(self.settings.translation_ai_url),
                "model": self.settings.translation_ai_model,
                "api_key": self.settings.translation_ai_api_key,
            },
        }

        def progress(done, total):
            if self._current(job) is None:
                return
            self.store.update(
                job["id"],
                status="processing",
                message=f"Local OCR and translation: {done}/{total} pages",
            )

        task = asyncio.create_task(translate_archive(source, output, request, progress))
        try:
            while not task.done():
                # Settings, cancellation and native arrivals take effect even
                # during a long provider request or Tesseract invocation.
                if (
                    self.settings.restored_safe_mode
                    or self._current(job) is None
                    or self.settings.translation_processor_url
                    or request["ai"]
                    != {
                        "base_url": self.settings.translation_ai_url,
                        "model": self.settings.translation_ai_model,
                        "api_key": self.settings.translation_ai_api_key,
                    }
                ):
                    return
                await asyncio.wait({task}, timeout=0.5)
            await task
            await self._publish(job, output, source)
        finally:
            if not task.done():
                task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    async def _publish(self, job: dict, output: Path, source: Path):
        manga = self._current(job)
        if manga is None:
            return
        pages = await asyncio.to_thread(
            self.importer._extract_pages, output, self.directory(job) / "output-pages"
        )
        source_info = await asyncio.to_thread(validate_cbz, source)
        if len(pages) != source_info["page_count"]:
            raise ValueError("Translated archive lost or added pages")
        with zipfile.ZipFile(output) as archive:
            info = archive.getinfo("translation.json")
            if info.file_size > 64 * 1024:
                raise ValueError("Translation receipt too large")
            receipt = json.loads(archive.read(info))
        if (
            receipt.get("source_sha256") != source_info["sha256"]
            or receipt.get("source_language") != job["source"]["language"]
            or receipt.get("target_language") != job["target_language"]
            or receipt.get("page_count") != len(pages)
            or receipt.get("translated_regions", 0) < 1
            or receipt.get("untranslated_regions") != 0
            or receipt.get("detected_source_language")
            != job["source"]["language"].split("-")[0]
            or receipt.get("detected_target_language")
            != job["target_language"].split("-")[0]
        ):
            raise ValueError("Translation receipt does not match this job")
        chapter = {
            **job["source"],
            "id": f"translated:{job['id']}",
            "provider": "translated",
            "language": job["target_language"],
            "pages": len(pages),
            "groups": ["Machine translation"],
            "notes": f"Machine translation {job['source']['language']} → {job['target_language']}; model {str(receipt.get('model') or 'unknown')[:200]}; source SHA-256 {source_info['sha256']}; job {job['id']}",
        }
        published = self.directory(job) / "published.cbz"
        _, archive_info = await asyncio.to_thread(
            package_cbz_validated, published, pages, manga, chapter
        )
        content_hash = await asyncio.to_thread(local_page_content_sha256, pages)
        async with self.service._mutation_lock:
            self.service.assert_mutations_allowed()
            if self._current(job) is None:
                return
            self.store.update(
                job["id"],
                status="importing",
                message="Importing the translated archive",
            )
            imported = await self.service._finish_import_thread(
                self.service._publish_external_import_locked,
                manga["id"],
                chapter,
                published,
                archive_info["sha256"],
                content_hash,
            )
            self.store.update(
                job["id"],
                status="completed",
                message="Imported machine translation",
                result_chapter_id=imported["chapter"]["id"],
            )
        await self.service._request_komga_reconciliation(True)
        await asyncio.to_thread(shutil.rmtree, self.directory(job), True)
