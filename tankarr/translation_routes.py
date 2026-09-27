from __future__ import annotations

import asyncio
import shutil
import uuid

from fastapi import FastAPI, HTTPException, Query, Request

from tankarr.archive import package_cbz_validated
from tankarr.languages import normalize_language_code
from tankarr.translation_policy import slot_key


def register_translation_routes(app: FastAPI, manager, monitor):
    @app.get("/api/translations")
    async def translations(manga_id: str | None = None):
        return {"enabled": manager.configured(), "jobs": manager.store.list(manga_id)}

    @app.post("/api/manga/{manga_id}/translations/search")
    async def search(manga_id: str):
        try:
            manga = manager.database.get_manga(manga_id)
            if not manager.enabled(manga):
                raise ValueError(
                    "Enable translation fallback in Settings and for this series first"
                )
            return await monitor.search_wanted_series(manga_id, trigger="translation")
        except KeyError as exc:
            raise HTTPException(404, "Series not found") from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/translations/{job_id}/retry")
    async def retry(job_id: str):
        async with manager.service._mutation_lock:
            manager.service.assert_mutations_allowed()
            try:
                job = manager.store.get(job_id)
            except KeyError as exc:
                raise HTTPException(404, "Translation job not found") from exc
            if job["status"] not in {"failed", "cancelled"}:
                raise HTTPException(409, "Only failed or cancelled jobs can be retried")
            return manager.store.update(
                job_id,
                status="queued",
                message="Retry requested",
                attempts=job["attempts"] + 1,
            )

    @app.post("/api/translations/{job_id}/cancel")
    async def cancel(job_id: str):
        async with manager.service._mutation_lock:
            manager.service.assert_mutations_allowed()
            try:
                job = manager.store.get(job_id)
            except KeyError as exc:
                raise HTTPException(404, "Translation job not found") from exc
            if job["status"] == "completed":
                raise HTTPException(409, "The translation has already been imported")
            return manager.cancel_job(job)

    @app.post("/api/manga/{manga_id}/translations/upload")
    async def upload(
        request: Request,
        manga_id: str,
        source_language: str = Query(min_length=2, max_length=16),
        volume: str | None = Query(default=None, pattern=r"^\d+(?:\.\d+)?$"),
        chapter: str | None = Query(default=None, pattern=r"^\d+(?:\.\d+)?$"),
    ):
        """An explicitly assigned source CBZ is staged, never imported untranslated."""
        directory = (
            manager.settings.staging_dir
            / "translations"
            / ("upload-" + uuid.uuid4().hex)
        )
        try:
            manga = manager.database.get_manga(manga_id)
            language = normalize_language_code(source_language)
            if not manager.enabled(manga):
                raise ValueError(
                    "Enable translation fallback in Settings and for this series first"
                )
            if language == manga["preferred_language"]:
                raise ValueError(
                    "The source already uses the requested language; use Library Import"
                )
            source = {
                "id": "upload:" + uuid.uuid4().hex,
                "provider": "upload",
                "language": language,
                "volume": volume,
                "chapter": chapter,
                "source_url": "",
                "title": "Uploaded source archive",
                "release_unit": "chapter" if chapter is not None else "volume",
            }
            key = slot_key(source)
            manager.service.assert_mutations_allowed()
            directory.mkdir(parents=True)
            uploaded = directory / "upload.cbz"
            total = 0
            with uploaded.open("wb") as handle:
                async for chunk in request.stream():
                    total += len(chunk)
                    if total > manager.settings.import_max_expanded_bytes:
                        raise HTTPException(
                            413, "Source archive exceeds the import limit"
                        )
                    await asyncio.to_thread(handle.write, chunk)
            pages = await asyncio.to_thread(
                manager.importer._extract_pages, uploaded, directory / "pages"
            )
            source_file = directory / "source.cbz"
            await asyncio.to_thread(
                package_cbz_validated, source_file, pages, manga, source
            )
            async with manager.service._mutation_lock:
                manager.service.assert_mutations_allowed()
                current = manager.database.get_manga(manga_id)
                if (
                    not manager.enabled(current)
                    or current["preferred_language"] != manga["preferred_language"]
                ):
                    raise HTTPException(
                        409, "Series translation settings changed during upload"
                    )
                if key in manager.store.slots(manga_id, manga["preferred_language"]):
                    raise HTTPException(409, "This slot already has a translation job")
                job = manager.store.create(
                    manga_id, source, manga["preferred_language"]
                )
                destination = manager.directory(job)
                destination.mkdir(parents=True, exist_ok=True)
                source_file.replace(destination / "source.cbz")
                return job
        except KeyError as exc:
            raise HTTPException(404, "Series not found") from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        finally:
            await asyncio.to_thread(shutil.rmtree, directory, True)
