"""Lazy file audit, bounded cached edge previews, and signed retirement reviews."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import struct
import tempfile
import time
import zipfile
from decimal import Decimal
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote

from PIL import Image

from tankarr import page_quality
from tankarr.archive import IMAGE_SUFFIXES
from tankarr.artwork_thumbnails import ARTWORK_THUMBNAIL_VERSION, ArtworkThumbnailCache
from tankarr.chapter_map import canonical_label
from tankarr.database import Database

MAX_ENTRIES = 10_000
MAX_DIRECTORY_BYTES = 8 * 1024**2
MAX_PAGE_BYTES = 32 * 1024**2
MAX_PAGE_PIXELS = 40_000_000
THUMBNAIL_WIDTH = 192
MAX_SELECTION = 1000
_FINGERPRINT = re.compile(r"^[a-f0-9]{64}$")


class StaleAudit(ValueError):
    pass


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _natural(value: str):
    return [
        (0, int(part)) if part.isdecimal() else (1, part.casefold())
        for part in re.split(r"(\d+)", value)
    ]


def _number_sort(value: str | None):
    label = canonical_label(value)
    return (0, Decimal(label)) if label is not None else (1, value or "")


def _source(release: dict, sources: list[dict]) -> dict | None:
    provider = str(release.get("provider") or "")
    candidates = [
        source
        for source in sources
        if source["provider"] == provider
        and source["language"] == release.get("language")
    ]
    explicit = release.get("provider_manga_id")
    if explicit:
        candidates = [
            source for source in candidates if source["provider_manga_id"] == explicit
        ]
    elif release.get("source_name"):
        candidates = [
            source
            for source in candidates
            if source.get("source_name") == release["source_name"]
        ]
    else:
        return None
    return candidates[0] if len(candidates) == 1 else None


def _safe_directory(root: Path) -> None:
    # The parent was supplied by the application, but newly introduced cache
    # components may never redirect writes through a symlink.
    absolute = root.absolute()
    current = Path(absolute.anchor)
    for component in absolute.parts[1:]:
        current /= component
        if current.is_symlink():
            raise ValueError("Refusing a symlinked audit cache directory")
        current.mkdir(mode=0o700, exist_ok=True)
        if current.is_symlink() or not current.is_dir():
            raise ValueError("Invalid audit cache directory")


class SeriesAudit:
    def __init__(
        self, database: Database, service: Any, *, cache_root: Path | None = None
    ):
        self.database = database
        self.service = service
        self.cache_root = (
            cache_root or service.settings.data_dir.resolve() / "cache" / "series-audit"
        )
        self.thumbnails = ArtworkThumbnailCache(
            self.cache_root / "images", render_concurrency=1
        )
        self._render_lock = asyncio.Lock()
        self._key = secrets.token_bytes(32)

    def _file(self, release: dict, root: Path) -> tuple[Path, dict, str]:
        raw = release.get("library_path")
        if not release.get("downloaded") or not raw:
            raise ValueError("File is not available in the library")
        path = self.service._recorded_library_path(str(raw), root)
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("Library file is not a regular file")
        fingerprint = {
            "id": release["id"],
            "path": str(path),
            "library_sha256": release.get("library_sha256"),
            "updated_at": release.get("updated_at"),
            "device": info.st_dev,
            "inode": info.st_ino,
            "size": info.st_size,
            "mtime_ns": info.st_mtime_ns,
            "ctime_ns": info.st_ctime_ns,
        }
        return path, fingerprint, _digest(fingerprint)

    def _cached(self, key: str) -> dict | None:
        if not _FINGERPRINT.fullmatch(key) or not self.cache_root.exists():
            return None
        _safe_directory(self.cache_root)
        path = self.cache_root / f"{key}.json"
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor, "rb") as handle:
                if os.fstat(handle.fileno()).st_size > 64 * 1024:
                    return None
                result = json.load(handle)
            if not isinstance(result, dict) or result.get("version") != 1:
                return None
            if (
                result.get("error")
                and time.time() - float(result.get("checked_at") or 0) >= 300
            ):
                return None
            return result
        except (OSError, ValueError, TypeError):
            return None

    def _save_cache(self, key: str, result: dict) -> None:
        _safe_directory(self.cache_root)
        descriptor, name = tempfile.mkstemp(prefix=".audit-", dir=self.cache_root)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "w") as handle:
                json.dump({"version": 1, **result}, handle)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(self.cache_root / f"{key}.json")
        finally:
            temporary.unlink(missing_ok=True)

    def _snapshot(self, manga_id: str) -> dict:
        with self.database.read_snapshot():
            manga = self.database.get_manga(manga_id, include_logical_counts=False)
            releases = [
                release
                for release in self.database.list_all_chapters(manga_id)
                if release.get("downloaded")
            ]
            qualities = self.database.page_quality(manga_id)
            sources = self.database.list_release_sources(manga_id, enabled_only=False)
        root = self.service._library_root()
        fingerprints = {}
        for release in releases:
            try:
                _path, fingerprint, key = self._file(release, root)
                fingerprints[str(release["id"])] = {
                    "fingerprint": fingerprint,
                    "key": key,
                }
            except (OSError, ValueError):
                fingerprints[str(release["id"])] = {"unavailable": True}
        revision = _digest(
            {
                "manga": manga,
                "releases": releases,
                "sources": sources,
                "fingerprints": fingerprints,
            }
        )
        return {
            "manga": manga,
            "releases": releases,
            "qualities": qualities,
            "sources": sources,
            "fingerprints": fingerprints,
            "revision": revision,
        }

    def _report(self, snapshot: dict) -> dict:
        manga = snapshot["manga"]
        releases = snapshot["releases"]
        numbers = {
            str(release["id"]): release.get("chapter")
            for release in releases
            if release.get("release_unit") != "volume"
        }
        baseline = page_quality.baseline_from(
            record
            for identifier, record in snapshot["qualities"].items()
            if page_quality.is_measurable(numbers.get(identifier))
            and record.get("verdict") != page_quality.REFUSED
        )
        identities = {}
        for release in releases:
            unit = "volume" if release.get("release_unit") == "volume" else "chapter"
            number = canonical_label(
                release.get("volume") if unit == "volume" else release.get("chapter")
            )
            if number is not None:
                identities.setdefault((unit, number), set()).add(
                    (release.get("provider"), release.get("source_name"))
                )
        files = []
        for release in releases:
            identifier = str(release["id"])
            state = snapshot["fingerprints"][identifier]
            key = state.get("key")
            cached = self._cached(key) if key else None
            available = key is not None
            mapping = _source(release, snapshot["sources"])
            unit = "volume" if release.get("release_unit") == "volume" else "chapter"
            number = canonical_label(
                release.get("volume") if unit == "volume" else release.get("chapter")
            )
            quality = snapshot["qualities"].get(identifier)
            pages = (
                (cached or {}).get("pages")
                if cached and not cached.get("error")
                else release.get("pages")
            )
            last_black = (cached or {}).get("last_page_black")
            anomalies = []
            if pages is not None and 0 <= int(pages) < 8:
                anomalies.append({"code": "few_pages", "label": "Fewer than 8 pages"})
            if quality and baseline:
                aspect = float(quality.get("median_aspect") or 0)
                reference = float(baseline.get("median_aspect") or 0)
                if 0 < reference < 1.8 and aspect >= max(2.2, reference * 1.4):
                    anomalies.append(
                        {
                            "code": "webtoon_shape",
                            "label": "Strip-shaped pages in a series read as pages",
                        }
                    )
            if last_black:
                anomalies.append(
                    {"code": "last_page_black", "label": "Last page is a black card"}
                )
            if number is not None and len(identities[(unit, number)]) > 1:
                anomalies.append(
                    {
                        "code": "multiple_sources",
                        "label": "This number has files from different sources",
                    }
                )
            if not available or (cached or {}).get("error"):
                anomalies.append(
                    {
                        "code": "file_unavailable",
                        "label": "File or preview is unavailable",
                    }
                )
            prefix = f"/api/manga/{quote(str(manga['id']), safe='')}/audit/files/{quote(identifier, safe='')}"
            files.append(
                {
                    "id": identifier,
                    "unit": unit,
                    "number": number,
                    "title": str(release.get("title") or ""),
                    "provider": str(release.get("provider") or ""),
                    "language": str(release.get("language") or ""),
                    "provider_manga_id": mapping["provider_manga_id"]
                    if mapping
                    else None,
                    "source_name": release.get("source_name"),
                    "pages": pages,
                    "verdict": {
                        field: quality.get(field)
                        for field in (
                            "verdict",
                            "reason",
                            "pages",
                            "measured_pages",
                            "median_aspect",
                            "measured_at",
                            "whole_chapter",
                        )
                    }
                    if quality
                    else None,
                    "size_bytes": state.get("fingerprint", {}).get("size"),
                    "first_thumbnail_url": f"{prefix}/first?revision={key}"
                    if available
                    else None,
                    "last_thumbnail_url": f"{prefix}/last?revision={key}"
                    if available
                    else None,
                    "thumbnail_status": "unavailable"
                    if not available or (cached or {}).get("error")
                    else "ready"
                    if cached
                    else "pending",
                    "last_page_black": last_black,
                    "anomalies": anomalies,
                    "can_retire": available,
                    "can_reject_source": mapping is not None and available,
                }
            )
        files.sort(
            key=lambda item: (
                item["unit"] != "volume",
                _number_sort(item["number"]),
                item["id"],
            )
        )
        sources = []
        for source in snapshot["sources"]:
            matched = [
                item
                for item in files
                if item["provider"] == source["provider"]
                and item["provider_manga_id"] == source["provider_manga_id"]
            ]
            ambiguous = any(
                release.get("provider") == source["provider"]
                and release.get("language") == source["language"]
                and (
                    not release.get("source_name")
                    or release.get("source_name") == source.get("source_name")
                )
                and _source(release, snapshot["sources"]) is None
                for release in releases
            )
            sources.append(
                {
                    "provider": source["provider"],
                    "provider_manga_id": source["provider_manga_id"],
                    "source_name": source.get("source_name"),
                    "language": source["language"],
                    "file_ids": [item["id"] for item in matched],
                    "can_reject": not ambiguous
                    and all(item["can_retire"] for item in matched),
                }
            )
        source_status = {
            (item["provider"], item["provider_manga_id"]): item["can_reject"]
            for item in sources
        }
        for item in files:
            item["can_reject_source"] = bool(
                item["can_reject_source"]
                and source_status.get((item["provider"], item["provider_manga_id"]))
            )
        return {
            "manga_id": manga["id"],
            "revision": snapshot["revision"],
            "files": files,
            "sources": sources,
        }

    def read(self, manga_id: str) -> dict:
        return self._report(self._snapshot(manga_id))

    def preview(
        self,
        manga_id: str,
        chapter_ids: list[str] | None = None,
        source: dict | None = None,
        *,
        revision: str | None = None,
    ) -> dict:
        report = self.read(manga_id)
        if revision is not None and revision != report["revision"]:
            raise StaleAudit(
                "Library files changed; reload the audit before reviewing this action"
            )
        if source is not None:
            mapping = next(
                (
                    item
                    for item in report["sources"]
                    if item["provider"] == source.get("provider")
                    and item["provider_manga_id"] == source.get("provider_manga_id")
                ),
                None,
            )
            if mapping is None or not mapping["can_reject"]:
                raise ValueError("The exact release source is not available")
            if chapter_ids is not None:
                raise ValueError("Choose a file selection or a source, not both")
            source = {
                field: mapping[field] for field in ("provider", "provider_manga_id")
            }
            selected = sorted(mapping["file_ids"])
        else:
            if (
                not chapter_ids
                or len(chapter_ids) > MAX_SELECTION
                or any(not isinstance(value, str) or not value for value in chapter_ids)
            ):
                raise ValueError("Select between 1 and 1000 library files")
            selected = sorted(set(chapter_ids))
        files = {item["id"]: item for item in report["files"]}
        if any(
            identifier not in files or not files[identifier]["can_retire"]
            for identifier in selected
        ):
            raise StaleAudit("Selected files are no longer available for retirement")
        signed = {
            "revision": report["revision"],
            "manga_id": manga_id,
            "chapter_ids": selected,
            "source": source,
        }
        return {
            **signed,
            "action": "reject_source" if source else "retire",
            "files": [files[identifier] for identifier in selected],
            "confirmation_snapshot": hmac.new(
                self._key, json.dumps(signed, sort_keys=True).encode(), hashlib.sha256
            ).hexdigest(),
        }

    def validate(
        self,
        manga_id: str,
        chapter_ids: list[str] | None = None,
        source: dict | None = None,
        *,
        confirmation_snapshot: str | None,
    ) -> dict:
        """Caller holds the service mutation lock through this and its action."""
        current = self.preview(manga_id, chapter_ids, source)
        if (
            not isinstance(confirmation_snapshot, str)
            or not _FINGERPRINT.fullmatch(confirmation_snapshot)
            or not hmac.compare_digest(
                confirmation_snapshot, current["confirmation_snapshot"]
            )
        ):
            raise StaleAudit(
                "Library files or source mapping changed; preview and confirm again"
            )
        return current

    @staticmethod
    def _extract(
        path: Path, fingerprint: dict, work: Path
    ) -> tuple[int, dict[str, Path]]:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as handle:
            info = os.fstat(handle.fileno())
            if any(
                getattr(info, attr) != fingerprint[key]
                for attr, key in (
                    ("st_dev", "device"),
                    ("st_ino", "inode"),
                    ("st_size", "size"),
                    ("st_mtime_ns", "mtime_ns"),
                    ("st_ctime_ns", "ctime_ns"),
                )
            ):
                raise StaleAudit("Library file changed before its preview was read")
            # Check classic EOCD before ZipFile allocates its central directory.
            handle.seek(max(0, info.st_size - 65577))
            tail = handle.read(65577)
            offset = tail.rfind(b"PK\x05\x06")
            if offset < 0 or len(tail) - offset < 22:
                raise ValueError("Archive has no valid bounded ZIP directory")
            # ZipFile follows this locator and replaces the classic directory
            # limits with ZIP64 values, even when the classic values fit.
            if offset >= 20 and tail[offset - 20 : offset - 16] == b"PK\x06\x07":
                raise ValueError("ZIP64 archive previews exceed audit limits")
            (
                _sig,
                disk,
                directory_disk,
                disk_entries,
                entries,
                directory_size,
                _start,
                _comment,
            ) = struct.unpack_from("<4s4H2LH", tail, offset)
            if (
                disk
                or directory_disk
                or disk_entries != entries
                or entries > MAX_ENTRIES
                or directory_size > MAX_DIRECTORY_BYTES
            ):
                raise ValueError("Archive directory exceeds audit limits")
            handle.seek(0)
            with zipfile.ZipFile(handle) as archive:
                infos = archive.infolist()
                if len(infos) > MAX_ENTRIES:
                    raise ValueError("Archive has too many entries")
                images = []
                names = set()
                for item in infos:
                    name = PurePosixPath(item.filename)
                    if (
                        name.is_absolute()
                        or ".." in name.parts
                        or "\\" in item.filename
                        or stat.S_ISLNK(item.external_attr >> 16)
                        or item.filename in names
                    ):
                        raise ValueError("Archive contains unsafe paths or links")
                    names.add(item.filename)
                    if not item.is_dir() and name.suffix.lower() in IMAGE_SUFFIXES:
                        images.append(item)
                images.sort(key=lambda item: _natural(item.filename))
                if not images:
                    raise ValueError("Archive has no supported pages")
                output = {}
                for edge, item in (("first", images[0]), ("last", images[-1])):
                    if item.file_size > MAX_PAGE_BYTES or item.flag_bits & 1:
                        raise ValueError("Archive edge page is oversized or encrypted")
                    target = (
                        work / f"{edge}{PurePosixPath(item.filename).suffix.lower()}"
                    )
                    remaining = MAX_PAGE_BYTES + 1
                    with archive.open(item) as source, target.open("xb") as destination:
                        while remaining:
                            chunk = source.read(min(1024 * 1024, remaining))
                            if not chunk:
                                break
                            destination.write(chunk)
                            remaining -= len(chunk)
                    if remaining == 0:
                        raise ValueError(
                            "Archive edge page exceeds the expanded size limit"
                        )
                    geometry = page_quality._page_geometry(target)
                    if geometry is None or geometry[0] * geometry[1] > MAX_PAGE_PIXELS:
                        raise ValueError(
                            "Archive edge page has invalid or oversized dimensions"
                        )
                    output[edge] = target
                return len(images), output

    def _cached_thumbnail(self, cached: dict, edge: str) -> tuple[Path, str] | None:
        digest = cached.get(edge)
        if not isinstance(digest, str) or not _FINGERPRINT.fullmatch(digest):
            return None
        _safe_directory(self.thumbnails.root)
        path = (
            self.thumbnails.root
            / f"{digest}-w{THUMBNAIL_WIDTH}-v{ARTWORK_THUMBNAIL_VERSION}.webp"
        )
        return (path, digest) if self.thumbnails._usable(path) else None

    async def thumbnail(
        self, manga_id: str, release_id: str, edge: str, revision: str
    ) -> tuple[Path, str]:
        if edge not in {"first", "last"} or not _FINGERPRINT.fullmatch(revision):
            raise ValueError("Invalid audit thumbnail request")
        release = await asyncio.to_thread(self.database.get_chapter, release_id)
        if release["manga_id"] != manga_id:
            raise KeyError(release_id)
        root = await asyncio.to_thread(self.service._library_root)
        path, fingerprint, key = await asyncio.to_thread(self._file, release, root)
        if key != revision:
            raise StaleAudit("Library file changed; reload the audit")
        async with self._render_lock:
            cached = await asyncio.to_thread(self._cached, key)
            if cached and cached.get("error"):
                raise ValueError("This archive preview is unavailable; retry later")
            if cached:
                ready = await asyncio.to_thread(self._cached_thumbnail, cached, edge)
                if ready:
                    return ready
            await asyncio.to_thread(_safe_directory, self.cache_root)
            await asyncio.to_thread(_safe_directory, self.thumbnails.root)
            try:
                with tempfile.TemporaryDirectory(
                    prefix=".pages-", dir=self.cache_root
                ) as temporary:
                    count, pages = await asyncio.to_thread(
                        self._extract, path, fingerprint, Path(temporary)
                    )
                    metrics = {"pages": count}
                    last_thumbnail = None
                    for side, source in pages.items():
                        target, digest = await self.thumbnails.get(
                            source, None, THUMBNAIL_WIDTH
                        )
                        metrics[side] = digest
                        if side == "last":
                            last_thumbnail = target
                    darkness = await asyncio.to_thread(
                        page_quality._page_darkness, last_thumbnail.read_bytes()
                    )
                    metrics["last_page_black"] = bool(
                        darkness
                        and darkness[0] < page_quality.DARK_PAGE_LUMINANCE
                        and darkness[1] < page_quality.DARK_PAGE_SPREAD
                    )
                    (
                        _current_path,
                        _current_fingerprint,
                        current_key,
                    ) = await asyncio.to_thread(self._file, release, root)
                    if current_key != key:
                        raise StaleAudit(
                            "Library file changed while its preview was rendered"
                        )
                    await asyncio.to_thread(self._save_cache, key, metrics)
            except StaleAudit:
                raise
            except (
                OSError,
                ValueError,
                zipfile.BadZipFile,
                NotImplementedError,
                Image.DecompressionBombError,
            ) as exc:
                await asyncio.to_thread(
                    self._save_cache,
                    key,
                    {"error": type(exc).__name__, "checked_at": time.time()},
                )
                raise ValueError("Archive preview is unavailable") from exc
            ready = await asyncio.to_thread(self._cached_thumbnail, metrics, edge)
            if ready is None:
                raise ValueError("Audit thumbnail was not published")
            return ready
