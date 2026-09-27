"""Small CBZ reader backed by Tankarr's existing library and database."""

from __future__ import annotations

import asyncio
import mimetypes
import re
import zipfile
from functools import lru_cache
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response
from PIL import Image
from starlette.concurrency import run_in_threadpool

from tankarr.config import Settings
from tankarr.database import Database
from tankarr.series_form import _is_webtoon

IMAGE_SUFFIXES = {".avif", ".bmp", ".gif", ".jpeg", ".jpg", ".png", ".webp"}
MAX_PAGE_BYTES = 64 * 1024 * 1024


class NativeReaderLibrary:
    """No-op library adapter: Tankarr already owns the reader catalogue."""

    standalone = True
    configured = True
    gates_readiness = False

    @staticmethod
    def result(*, requested: bool = False) -> dict[str, Any]:
        return {
            "configured": True,
            "requested": requested,
            "triggered": False,
            "reader_independent": True,
            "ready": True,
            "reason": "Tankarr reads its managed files directly",
        }

    async def scan(self, relative_paths=None) -> dict[str, Any]:
        return self.result(requested=bool(tuple(relative_paths or ())))

    async def ensure_present(self, relative_paths) -> dict[str, Any]:
        paths = tuple(relative_paths)
        return {
            **self.result(),
            "expected_books": len(paths),
            "matched_expected_books": len(paths),
            "missing_books": 0,
        }

    async def prepare_for_moves(self) -> dict[str, Any]:
        return self.result()

    async def reconcile_deleted(
        self, relative_paths, *, safety_check=None
    ) -> dict[str, Any]:
        if safety_check is not None:
            safety_check()
        paths = tuple(relative_paths)
        return {
            **self.result(requested=bool(paths)),
            "purged": False,
            "paths": len(paths),
        }


def _natural_key(value: str) -> tuple[object, ...]:
    return tuple(
        int(part) if part.isdigit() else part.casefold()
        for part in re.split(r"(\d+)", value)
    )


@lru_cache(maxsize=256)
def _cached_page_names(path: str, mtime_ns: int, size: int) -> tuple[str, ...]:
    del mtime_ns, size
    with zipfile.ZipFile(path) as archive:
        pages = [
            item.filename
            for item in archive.infolist()
            if not item.is_dir()
            and not item.filename.startswith(("__MACOSX/", "."))
            and Path(item.filename).suffix.casefold() in IMAGE_SUFFIXES
        ]
    return tuple(sorted(pages, key=_natural_key))


def _book_path(settings: Settings, release: dict[str, Any]) -> Path:
    raw = release.get("library_path")
    if not release.get("downloaded") or not raw:
        raise HTTPException(status_code=404, detail="This book is not in the library")
    source = Path(str(raw))
    root = settings.library_dir.resolve()
    path = source.resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail="Unsafe library path") from exc
    if source.is_symlink() or not path.is_file():
        raise HTTPException(status_code=404, detail="The library file is unavailable")
    if path.suffix.casefold() != ".cbz":
        raise HTTPException(
            status_code=415, detail="The built-in reader supports CBZ books"
        )
    return path


def _page_names(path: Path) -> tuple[str, ...]:
    stat = path.stat()
    try:
        pages = _cached_page_names(str(path), stat.st_mtime_ns, stat.st_size)
    except (OSError, zipfile.BadZipFile) as exc:
        raise HTTPException(
            status_code=422, detail="The CBZ archive is invalid"
        ) from exc
    if not pages:
        raise HTTPException(
            status_code=422, detail="The CBZ archive has no readable pages"
        )
    return pages


def _number_key(value: object) -> tuple[int, float, str]:
    text = str(value or "").strip()
    try:
        return (0, float(text), text)
    except ValueError:
        return (1, 0.0, text.casefold())


def _release_key(release: dict[str, Any]) -> tuple[object, ...]:
    unit = str(release.get("release_unit") or "chapter")
    return (
        _number_key(release.get("volume")),
        0 if unit == "volume" else 1,
        _number_key(release.get("chapter")),
        str(release.get("publish_at") or ""),
        str(release.get("id") or ""),
    )


def _book_label(release: dict[str, Any]) -> str:
    if str(release.get("release_unit") or "chapter") == "volume" and release.get(
        "volume"
    ):
        return f"Volume {release['volume']}"
    if release.get("chapter"):
        return f"Chapter {release['chapter']}"
    if release.get("volume"):
        return f"Volume {release['volume']}"
    return str(release.get("title") or "Book")


def _ordered_releases(releases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if all(str(item.get("release_unit") or "chapter") != "volume" for item in releases):
        # Partial book maps and webtoon seasons must never reorder chapters.
        return sorted(
            releases,
            key=lambda item: (
                _number_key(item.get("canonical_chapter") or item.get("chapter")),
                str(item.get("id") or ""),
            ),
        )
    return sorted(releases, key=_release_key)


def _looks_like_vertical_strip(path: Path, pages: tuple[str, ...]) -> bool:
    """Use image headers only; metadata remains the stronger signal."""

    samples = pages[: min(3, len(pages))]
    tall = 0
    try:
        with zipfile.ZipFile(path) as archive:
            for name in samples:
                with archive.open(name) as source, Image.open(source) as image:
                    width, height = image.size
                if width > 0 and height / width >= 2.2:
                    tall += 1
    except (OSError, ValueError, zipfile.BadZipFile):
        return False
    return bool(samples) and tall * 2 >= len(samples)


def _reader_mode(
    settings: Settings,
    manga: dict[str, Any],
    path: Path,
    pages: tuple[str, ...],
) -> tuple[str, str]:
    override = str(manga.get("reader_mode_override") or "").casefold()
    if override in {"manga", "webtoon"}:
        return override, "series"
    configured = str(settings.reader_display_mode or "auto").casefold()
    if configured in {"manga", "webtoon"}:
        return configured, "global"
    metadata = manga.get("metadata") or {}
    # Manhua/Manhwa describe the work's origin, not a vertical page layout.
    # Use the same explicit webtoon evidence as the series shelf; otherwise
    # inspect the pages before choosing a scrolling reader.
    if _is_webtoon(metadata):
        return "webtoon", "metadata"
    if str(metadata.get("content_kind") or "").casefold() == "manga":
        return "manga", "metadata"
    if _looks_like_vertical_strip(path, pages):
        return "webtoon", "page_shape"
    return "manga", "automatic"


def _reader_direction(manga: dict[str, Any], display_mode: str) -> str:
    if display_mode == "webtoon":
        return "ltr"
    override = str(manga.get("reader_direction_override") or "").casefold()
    if override in {"ltr", "rtl"}:
        return override
    metadata = manga.get("metadata") or {}
    direction = str(metadata.get("reading_direction") or "").strip().casefold()
    if direction in {"left_to_right", "left-to-right", "ltr"}:
        return "ltr"
    if direction in {"right_to_left", "right-to-left", "rtl"}:
        return "rtl"
    content_kind = str(metadata.get("content_kind") or "").strip().casefold()
    if content_kind in {"manhua", "manhwa", "comic", "book"}:
        return "ltr"
    if content_kind == "manga":
        return "rtl"
    language = (
        str(metadata.get("original_language") or manga.get("original_language") or "")
        .strip()
        .casefold()
    )
    if language == "zh" or language.startswith("zh-") or language == "ko":
        return "ltr"
    if language == "ja" or language.startswith("ja-"):
        return "rtl"
    return "rtl"


def native_reader_link(database: Database, manga_id: str) -> dict[str, Any]:
    manga = database.get_manga(manga_id, include_logical_counts=False)
    releases = [
        item
        for item in database.list_downloaded_chapters([manga_id])
        if item.get("library_path")
    ]
    releases = _ordered_releases(releases)
    books = [
        {
            "chapter_id": str(item["id"]),
            "book_id": str(item["id"]),
            "url": f"#/reader/{item['id']}",
        }
        for item in releases
    ]
    bookmark = database.reader_bookmark(manga_id)
    resume = None
    if bookmark:
        resume = {
            "chapter_id": str(bookmark["chapter_id"]),
            "page_index": int(bookmark["page_index"]),
            "label": _book_label(bookmark),
            "url": f"#/reader/{bookmark['chapter_id']}?page={int(bookmark['page_index']) + 1}",
        }
    first_url = books[0]["url"] if books else None
    return {
        "reader": "tankarr",
        "label": "Tankarr",
        "configured": True,
        "available": bool(books),
        "url": resume["url"] if resume else first_url,
        "title": manga.get("title"),
        "matched_books": len(books),
        "managed_books": len(books),
        "books": books,
        "bookmark": resume,
        **({} if books else {"reason": "No downloaded CBZ books are available"}),
    }


def register_native_reader_routes(
    app: FastAPI, settings: Settings, database: Database
) -> None:
    # Bound decompression memory while allowing nearby pages to load together.
    page_reads = asyncio.Semaphore(2)

    @app.get("/api/reader/bookmarks")
    def list_bookmarks():
        return [
            {
                "manga_id": item["manga_id"],
                "manga_title": item["manga_title"],
                "manga_cover_url": item["manga_cover_url"],
                "chapter_id": item["chapter_id"],
                "label": _book_label(item),
                "page_index": item["page_index"],
                "updated_at": item["updated_at"],
                "url": f"#/reader/{item['chapter_id']}?page={item['page_index'] + 1}",
            }
            for item in database.list_reader_bookmarks()
        ]

    def release_and_pages(
        release_id: str,
    ) -> tuple[dict[str, Any], Path, tuple[str, ...]]:
        try:
            release = database.get_chapter(release_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Book not found") from exc
        path = _book_path(settings, release)
        return release, path, _page_names(path)

    def reader_book(release_id: str):
        release, path, pages = release_and_pages(release_id)
        manga_id = str(release["manga_id"])
        manga = database.get_manga(manga_id, include_logical_counts=False)
        display_mode, display_mode_source = _reader_mode(settings, manga, path, pages)
        reading_direction = _reader_direction(manga, display_mode)
        ordered = [
            item
            for item in database.list_downloaded_chapters([manga_id])
            if item.get("library_path")
        ]
        ordered = _ordered_releases(ordered)
        ids = [str(item["id"]) for item in ordered]
        position = ids.index(release_id)
        bookmark = database.reader_bookmark(manga_id)
        page_index = (
            min(max(int(bookmark.get("page_index") or 0), 0), len(pages) - 1)
            if bookmark and bookmark.get("chapter_id") == release_id
            else 0
        )
        return {
            "release_id": release_id,
            "page_version": str(
                release.get("library_sha256") or f"{path.stat().st_mtime_ns:x}"
            )[:16],
            "manga_id": manga_id,
            "series_title": manga.get("title"),
            "book_title": _book_label(release),
            "display_mode": display_mode,
            "display_mode_source": display_mode_source,
            "reading_direction": reading_direction,
            "page_count": len(pages),
            "page_index": page_index,
            "bookmarked": bool(bookmark and bookmark.get("chapter_id") == release_id),
            "bookmark_page_index": int(bookmark["page_index"])
            if bookmark and bookmark.get("chapter_id") == release_id
            else None,
            "previous_release_id": ids[position - 1] if position > 0 else None,
            "next_release_id": ids[position + 1] if position + 1 < len(ids) else None,
        }

    def open_reader_page(release_id: str, page_index: int):
        _release, path, pages = release_and_pages(release_id)
        if page_index < 0 or page_index >= len(pages):
            raise HTTPException(status_code=404, detail="Page not found")
        name = pages[page_index]
        archive = None
        opened = False
        try:
            archive = zipfile.ZipFile(path)
            info = archive.getinfo(name)
            if info.file_size > MAX_PAGE_BYTES:
                raise HTTPException(status_code=413, detail="The page is too large")
            etag = f'"{path.stat().st_mtime_ns:x}-{info.CRC:x}-{info.file_size:x}"'
            media_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
            opened = True
            return archive, info, etag, media_type
        except (OSError, zipfile.BadZipFile) as exc:
            raise HTTPException(
                status_code=422, detail="The CBZ archive is invalid"
            ) from exc
        finally:
            if archive is not None and not opened:
                archive.close()

    def update_reader_bookmark(manga_id: str, payload: dict[str, object]):
        release_id = str(payload.get("release_id") or "")
        raw_page = payload.get("page_index")
        if isinstance(raw_page, bool) or not isinstance(raw_page, int) or raw_page < 0:
            raise HTTPException(
                status_code=400, detail="page_index must be a non-negative integer"
            )
        try:
            release, _path, pages = release_and_pages(release_id)
            if str(release["manga_id"]) != manga_id or raw_page >= len(pages):
                raise KeyError(release_id)
            return database.save_reader_bookmark(
                manga_id, release_id, page_index=raw_page
            )
        except KeyError as exc:
            raise HTTPException(
                status_code=404, detail="Book not found in this series"
            ) from exc

    @app.get("/api/reader/books/{release_id}")
    async def read_book(release_id: str):
        async with app.state.service._mutation_lock:
            return await run_in_threadpool(reader_book, release_id)

    @app.get("/api/reader/books/{release_id}/pages/{page_index}")
    async def read_page(release_id: str, page_index: int, request: Request):
        async with page_reads:
            # Pin the archive's open file descriptor while paths are stable.
            # Decompression can then run concurrently without blocking imports.
            async with app.state.service._mutation_lock:
                archive, info, etag, media_type = await run_in_threadpool(
                    open_reader_page, release_id, page_index
                )
            headers = {"Cache-Control": "private, max-age=86400", "ETag": etag}
            try:
                if request.headers.get("if-none-match") == etag:
                    return Response(status_code=304, headers=headers)
                try:
                    content = await run_in_threadpool(archive.read, info)
                except (OSError, zipfile.BadZipFile) as exc:
                    raise HTTPException(
                        status_code=422, detail="The CBZ archive is invalid"
                    ) from exc
                return Response(content, media_type=media_type, headers=headers)
            finally:
                archive.close()

    @app.put("/api/reader/series/{manga_id}/bookmark")
    async def save_bookmark(manga_id: str, payload: dict[str, object]):
        async with app.state.service._mutation_lock:
            return await run_in_threadpool(update_reader_bookmark, manga_id, payload)

    @app.delete("/api/reader/series/{manga_id}/bookmark")
    def delete_reader_bookmark(manga_id: str):
        try:
            database.get_manga(manga_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Manga not found") from exc
        database.clear_reader_bookmark(manga_id)
        return Response(status_code=204)
