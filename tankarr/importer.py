from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import time
import zipfile
from collections.abc import AsyncIterable
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO
from urllib.parse import urlsplit

from tankarr.archive import (
    IMAGE_SUFFIXES,
    package_cbz_validated,
    publish_without_overwrite,
    sha256,
)
from tankarr.chapter_map import canonical_label
from tankarr.chapter_mapping import effective_edition_book_count
from tankarr.config import Settings
from tankarr.database import Database, local_release_identity, local_series_identity
from tankarr.import_limits import (
    ImportLimitError,
    ImportLimits,
    check_page_geometry,
    run_decoder,
)
from tankarr.language_audit import audit_english_pages, automatic_english_decision
from tankarr.page_order import restore_cover_first
from tankarr.service import (
    LOCAL_CONTENT_FINGERPRINT_VERSION,
    ExternalImportConflict,
    TankarrService,
    local_page_content_sha256,
)
from tankarr.torrent_sources import (
    TORRENT_IMPORT_PROVIDERS,
    torrent_source_label,
)

ARCHIVE_SUFFIXES = {".cbz", ".zip", ".cbr", ".rar", ".pdf"}
logger = logging.getLogger(__name__)
RAR_SUFFIXES = {".cbr", ".rar"}
PDF_SUFFIXES = {".pdf"}
MIN_FOLDER_IMAGES = 3
MAX_UPLOAD_FILE_BYTES = 16 * 1024 * 1024 * 1024
UPLOAD_WRITE_BUFFER_BYTES = 4 * 1024 * 1024
UPLOAD_DISK_RESERVE_BYTES = 512 * 1024 * 1024
STALE_UPLOAD_SECONDS = 7 * 24 * 60 * 60
UPLOAD_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
IMPORT_NUMBER_PATTERN = re.compile(r"^\d+(?:\.\d+)?(?:-\d+(?:\.\d+)?)?$")

SERIES_NUMBER_PATTERN = re.compile(
    r"^(?P<base>.+?)[\s_-]+(?:v(?:ol)?\.?\s*|#\s*)?(?P<number>\d{1,4}(?:\.\d+)?)$"
)
CHAPTER_DIR_PATTERN = re.compile(
    r"^chapter\s*(?P<number>\d+(?:\.\d+)?)\s*[_\-–:]*\s*(?P<title>.*)$",
    re.IGNORECASE,
)
VOLUME_DIR_PATTERN = re.compile(
    r"^v(?:ol(?:ume)?)?\.?\s*(?P<number>\d+(?:\.\d+)?)$", re.IGNORECASE
)
AUTHOR_TITLE_PATTERN = re.compile(r"^(?P<author>.{2,60}?)\s+-\s+(?P<title>.{2,})$")
FLEXIBLE_VOLUME_PATTERN = re.compile(
    r"(?i)(?:^|[\s._\-[(])(?:v|vol(?:ume)?)\.?\s*0*(?P<start>\d+(?:\.\d+)?)"
    r"(?:\s*[-–]\s*(?:v|vol(?:ume)?)?\.?\s*0*(?P<end>\d+(?:\.\d+)?))?"
    r"(?![a-z0-9])"
)
FLEXIBLE_CHAPTER_PATTERN = re.compile(
    r"(?i)(?:^|[\s._\-[(])(?:ch(?:apter)?|c)\.?\s*0*(?P<start>\d+(?:\.\d+)?)"
    r"(?:\s*[-–]\s*(?:ch(?:apter)?|c)?\.?\s*0*(?P<end>\d+(?:\.\d+)?))?"
    r"(?![a-z])"
)


class TorrentImportAmbiguous(RuntimeError):
    def __init__(self, message: str, *, skip_decisions: list[dict] | None = None):
        self.skip_decisions = skip_decisions or []
        super().__init__(message)


class LanguageReviewRequired(RuntimeError):
    def __init__(self, evidence: dict[str, Any]):
        self.evidence = evidence
        super().__init__(
            "OCR could not confirm English for every book; manual review is required"
        )


def natural_key(text: str) -> tuple:
    return tuple(
        int(token) if token.isdigit() else token.lower()
        for token in re.split(r"(\d+)", text)
    )


def rar_tool() -> str | None:
    """Preferred RAR reader: bsdtar (libarchive), then unar, unrar, 7-Zip.

    Archives come from indexers and Usenet, so the decoder parses untrusted
    input. libarchive's RAR and RAR5 readers are maintained in Debian main and
    receive security updates; unrar (non-free) and unar do not, so they are
    fallbacks for hosts without libarchive. unar also mis-extracts solid RAR5
    books ("Attempted to read more data than was available" on a few pages of
    an archive libarchive and unrar read whole).
    """

    if shutil.which("bsdtar"):
        return "bsdtar"
    if shutil.which("lsar") and shutil.which("unar"):
        return "unar"
    if shutil.which("unrar"):
        return "unrar"
    if shutil.which("7zz"):
        return "7zz"
    if shutil.which("7z"):
        return "7z"
    return None


def _rar_list(path: Path) -> list[str] | None:
    tool = rar_tool()
    if tool is None:
        return None
    if tool == "bsdtar":
        # ``-t``: list one entry path per line, directories with a trailing slash.
        command = ["bsdtar", "-tf", str(path)]
    elif tool == "unar":
        command = ["lsar", str(path)]
    elif tool == "unrar":
        # ``lb``: bare listing, one entry path per line.
        command = ["unrar", "lb", str(path)]
    else:
        command = [tool, "l", "-ba", str(path)]
    try:
        with tempfile.TemporaryDirectory(prefix="tankarr-rar-list-") as temporary:
            result = run_decoder(
                command,
                Path(temporary),
                ImportLimits(),
                timeout=120,
                capture_limit=1024 * 1024,
            )
    except (OSError, ImportLimitError):
        return []
    if result.returncode != 0:
        return []
    lines = result.stdout.splitlines()
    if tool == "unar":
        # The first line repeats the archive path; entries follow.
        return [line.strip() for line in lines[1:] if line.strip()]
    if tool in {"bsdtar", "unrar"}:
        return [line.strip() for line in lines if line.strip()]
    return [line.split(maxsplit=5)[-1] for line in lines if line.strip()]


def _rar_extract(
    path: Path, target: Path, *, limits: ImportLimits | None = None
) -> None:
    tool = rar_tool()
    if tool is None:
        raise RuntimeError("RAR support requires bsdtar, unar, unrar or 7z")
    if tool == "bsdtar":
        # bsdtar strips leading slashes and refuses ``..`` members on its own;
        # the caller rejects symbolic links after extraction.
        command = ["bsdtar", "-xf", str(path), "-C", str(target)]
    elif tool == "unar":
        command = [
            "unar",
            "-quiet",
            "-force-overwrite",
            "-output-directory",
            str(target),
            str(path),
        ]
    elif tool == "unrar":
        # ``x`` keeps paths, ``-o+`` overwrites, ``-inul`` silences the
        # progress; the trailing slash marks the last argument as a directory.
        command = ["unrar", "x", "-o+", "-inul", "-y", str(path), f"{target}/"]
    else:
        command = [tool, "x", "-y", f"-o{target}", str(path)]
    result = run_decoder(command, target, limits or ImportLimits(), timeout=900)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()[:200]
        raise RuntimeError(f"RAR extraction failed: {detail}")


def is_rar_file(path: Path) -> bool:
    """Detect RAR by magic bytes: extensions lie surprisingly often."""

    try:
        with path.open("rb") as handle:
            return handle.read(4) == b"Rar!"
    except OSError:
        return False


def pdf_tools_available() -> bool:
    return bool(shutil.which("pdfinfo") and shutil.which("pdftoppm"))


def _pdf_page_count(path: Path, *, limits: ImportLimits | None = None) -> int:
    if not pdf_tools_available():
        return -1
    try:
        with tempfile.TemporaryDirectory(prefix="tankarr-pdfinfo-") as temporary:
            result = run_decoder(
                ["pdfinfo", str(path)],
                Path(temporary),
                limits or ImportLimits(),
                timeout=30,
            )
    except (OSError, ImportLimitError):
        return 0
    if result.returncode != 0:
        return 0
    match = re.search(r"(?mi)^Pages:\s*(\d+)\s*$", result.stdout)
    return int(match.group(1)) if match else 0


def _pdf_extract(
    path: Path, target: Path, *, limits: ImportLimits | None = None
) -> list[Path]:
    if not pdf_tools_available():
        raise RuntimeError("PDF import requires the pdfinfo and pdftoppm tools")
    render_root = target.parent / "pdf"
    render_root.mkdir(parents=True, exist_ok=True)
    limits = limits or ImportLimits()
    page_count = _pdf_page_count(path, limits=limits)
    if page_count <= 0:
        raise ValueError("PDF contains no renderable pages")
    limits.check(0, page_count, render_root)
    pages = []
    total = 0
    deadline = time.monotonic() + 1800
    # Render one page at a time. A long PDF must never rasterize every page
    # before its page-count, output-byte and free-space budgets are checked.
    for number in range(1, page_count + 1):
        limits.check(total, number, render_root)
        prefix = render_root / f"page-{number:06d}"
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ImportLimitError("PDF rendering exceeded 30 minutes")
        run_decoder(
            [
                "pdftoppm",
                "-f",
                str(number),
                "-l",
                str(number),
                "-singlefile",
                "-jpeg",
                "-r",
                "160",
                "-jpegopt",
                "quality=90",
                str(path),
                str(prefix),
            ],
            render_root,
            limits,
            timeout=min(120, remaining),
        )
        page = prefix.with_suffix(".jpg")
        total += page.stat().st_size
        limits.check(total, number, render_root)
        pages.append(page)
    return pages


def split_author_title(stem: str) -> tuple[list[str], str]:
    match = AUTHOR_TITLE_PATTERN.match(stem.strip())
    if match:
        return [match.group("author").strip()], match.group("title").strip()
    return [], stem.strip()


def _normalized_number(value: str) -> str:
    if "." in value:
        return value.lstrip("0").rstrip("0").rstrip(".") or "0"
    return value.lstrip("0") or "0"


def _normalized_volume_number(value: str) -> str:
    # Digital releases often append their publication year directly after the
    # volume token ("Vol.23.2021"). A four-digit suffix is not a fraction.
    if "." in value and len(value.split(".", 1)[1]) >= 3:
        value = value.split(".", 1)[0]
    return _normalized_number(value)


def infer_numbered_name(stem: str) -> tuple[str, str | None, str | None]:
    """Extract one explicit chapter/volume token without guessing from a year."""

    chapter_match = FLEXIBLE_CHAPTER_PATTERN.search(stem)
    volume_match = FLEXIBLE_VOLUME_PATTERN.search(stem)
    selected = chapter_match or volume_match
    if selected is None:
        legacy = SERIES_NUMBER_PATTERN.match(stem.strip())
        if legacy is None:
            return stem.strip(), None, None
        return (
            legacy.group("base").strip(),
            _normalized_number(legacy.group("number")),
            None,
        )
    normalize = (
        _normalized_number if selected is chapter_match else _normalized_volume_number
    )
    start = normalize(selected.group("start"))
    raw_end = selected.group("end")
    number = f"{start}-{normalize(raw_end)}" if raw_end else start
    base = f"{stem[: selected.start()]} {stem[selected.end() :]}"
    base = re.sub(r"\s+", " ", base).strip(" ._-") or stem.strip()
    if selected is chapter_match:
        return base, None, number
    return base, number, None


def _zip_image_count(path: Path) -> int:
    try:
        with zipfile.ZipFile(path) as archive:
            return sum(
                1
                for name in archive.namelist()
                if Path(name).suffix.lower() in IMAGE_SUFFIXES
                and not name.startswith("__MACOSX")
            )
    except (OSError, zipfile.BadZipFile):
        return 0


def _rar_image_count(path: Path) -> int:
    names = _rar_list(path)
    if names is None:
        return -1
    return sum(1 for name in names if Path(name).suffix.lower() in IMAGE_SUFFIXES)


class LibraryImporter:
    """Scan a drop directory and ingest local archives as `local` series."""

    def __init__(self, settings: Settings, database: Database, service: TankarrService):
        self.settings = settings
        self.database = database
        self.service = service
        self._task: asyncio.Task | None = None
        if not settings.restored_safe_mode:
            self._purge_stale_workspaces()
        self._operation = self._load_operation()
        if not settings.restored_safe_mode:
            self._purge_stale_uploads()
        self.state: dict[str, Any] = (
            dict(self._operation.get("state") or {})
            if self._operation is not None
            else {"running": False}
        )
        if self._operation is not None and not self.state.get("finished"):
            self.state.update(
                {
                    "running": False,
                    "resumable": True,
                    "current": "Waiting for safe startup",
                }
            )

    def _purge_stale_workspaces(self) -> None:
        """Remove private temp trees left by an interrupted container process."""

        root = self.settings.staging_dir.resolve()
        if not root.is_dir():
            return
        for candidate in root.iterdir():
            if (
                candidate.is_symlink()
                or not candidate.is_dir()
                or not candidate.name.startswith(
                    (
                        "import-",
                        "nyaa-import-",
                        "torrent-import-",
                        "job-",
                        "manual-import-",
                        "assemble-",
                    )
                )
            ):
                continue
            resolved = candidate.resolve()
            if resolved.parent != root:
                continue
            try:
                shutil.rmtree(resolved)
            except OSError:
                # A read-only or concurrently removed workspace is harmless;
                # the fresh import still uses a unique directory.
                continue

    def _load_operation(self) -> dict[str, Any] | None:
        path = self.settings.import_operation_path
        try:
            raw = path.read_text(encoding="utf-8")
            payload = json.loads(raw)
        except FileNotFoundError:
            return None
        except (OSError, ValueError, json.JSONDecodeError):
            return None
        if (
            not isinstance(payload, dict)
            or payload.get("version") != 1
            or not isinstance(payload.get("groups"), list)
            or not isinstance(payload.get("language"), str)
            or not isinstance(payload.get("state"), dict)
        ):
            return None
        return payload

    def _purge_stale_uploads(self) -> None:
        root = self.settings.data_dir / "import-uploads"
        if not root.is_dir():
            return
        operation = self._operation or {}
        operation_state = operation.get("state") or {}
        active_ids = (
            {
                str(group.get("upload_id"))
                for group in operation.get("groups", [])
                if group.get("upload_id")
            }
            if not operation_state.get("finished")
            else set()
        )
        cutoff = time.time() - STALE_UPLOAD_SECONDS
        for candidate in root.iterdir():
            if (
                candidate.name in active_ids
                or not UPLOAD_ID_PATTERN.fullmatch(candidate.name)
                or candidate.is_symlink()
                or not candidate.is_dir()
            ):
                continue
            try:
                if candidate.stat().st_mtime < cutoff:
                    shutil.rmtree(candidate)
            except OSError:
                continue

    def _persist_operation(self) -> None:
        if self._operation is None:
            return
        self._operation["state"] = self.state
        path = self.settings.import_operation_path
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".json.tmp")
        payload = json.dumps(self._operation, ensure_ascii=False, indent=2)
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(path)
            directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            temporary.unlink(missing_ok=True)

    async def resume_pending(self) -> bool:
        if (
            self._operation is None
            or self.state.get("finished")
            or (self._task is not None and not self._task.done())
        ):
            return False
        self.state.update(
            {"running": True, "resumable": False, "current": "Resuming import"}
        )
        self._persist_operation()
        self._task = asyncio.create_task(
            self._run(
                list(self._operation["groups"]),
                str(self._operation["language"]),
            ),
            name="tankarr-library-import",
        )
        return True

    async def stop(self) -> None:
        if self._task is None or self._task.done():
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    @property
    def root(self) -> Path | None:
        if self.settings.import_dir is None:
            return None
        root = Path(self.settings.import_dir)
        return root if root.is_dir() else None

    @property
    def uploads_root(self) -> Path:
        return self.settings.data_dir / "import-uploads"

    def create_upload(self) -> dict[str, str]:
        self.uploads_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        while True:
            upload_id = secrets.token_hex(16)
            root = self.uploads_root / upload_id
            try:
                root.mkdir(mode=0o700)
            except FileExistsError:
                continue
            return {"upload_id": upload_id}

    def _upload_root(self, upload_id: str) -> Path:
        if not UPLOAD_ID_PATTERN.fullmatch(upload_id):
            raise FileNotFoundError("Upload session not found")
        root = self.uploads_root / upload_id
        if root.is_symlink() or not root.is_dir():
            raise FileNotFoundError("Upload session not found")
        return root.resolve()

    def _upload_destination(self, upload_id: str, relative: str) -> tuple[Path, Path]:
        root = self._upload_root(upload_id)
        raw = relative.strip()
        parts = raw.split("/")
        if (
            not raw
            or raw.startswith("/")
            or "\\" in raw
            or any(part in {"", ".", ".."} for part in parts)
            or PurePosixPath(raw).is_absolute()
        ):
            raise ValueError("Upload path must be a safe relative path")
        destination = root.joinpath(*parts)
        if destination.suffix.casefold() not in ARCHIVE_SUFFIXES | set(IMAGE_SUFFIXES):
            raise ValueError(
                "Only comic archives, PDFs, and image files can be uploaded"
            )
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        resolved_parent = destination.parent.resolve()
        if root not in resolved_parent.parents and resolved_parent != root:
            raise ValueError("Upload path escapes its private session")
        return root, destination

    @staticmethod
    def _flush_uploaded_file(handle: BinaryIO) -> None:
        handle.flush()
        os.fsync(handle.fileno())

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    async def store_upload_file(
        self,
        upload_id: str,
        relative: str,
        chunks: AsyncIterable[bytes],
        *,
        content_length: int | None = None,
    ) -> dict[str, Any]:
        _root, destination = self._upload_destination(upload_id, relative)
        if content_length is not None and content_length > MAX_UPLOAD_FILE_BYTES:
            raise ValueError("Import file exceeds the 16 GiB safety limit")
        if content_length is not None and (
            content_length + UPLOAD_DISK_RESERVE_BYTES
            > shutil.disk_usage(destination.parent).free
        ):
            raise ValueError("Not enough free space for this import file")
        if destination.exists():
            raise FileExistsError(f"A file named {relative} is already selected")
        temporary = destination.with_name(
            f".{destination.name}.{secrets.token_hex(6)}.part"
        )
        written = 0
        buffer = bytearray()
        try:
            with temporary.open("xb") as handle:
                async for chunk in chunks:
                    if not chunk:
                        continue
                    written += len(chunk)
                    if written > MAX_UPLOAD_FILE_BYTES:
                        raise ValueError("Import file exceeds the 16 GiB safety limit")
                    buffer.extend(chunk)
                    if len(buffer) >= UPLOAD_WRITE_BUFFER_BYTES:
                        payload = bytes(buffer)
                        buffer.clear()
                        await asyncio.to_thread(handle.write, payload)
                if buffer:
                    await asyncio.to_thread(handle.write, bytes(buffer))
                await asyncio.to_thread(self._flush_uploaded_file, handle)
            await asyncio.to_thread(publish_without_overwrite, temporary, destination)
            await asyncio.to_thread(self._fsync_directory, destination.parent)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        return {"path": relative, "size": written}

    def scan_upload(self, upload_id: str) -> dict[str, Any]:
        root = self._upload_root(upload_id)
        result = self._scan_root(root, display_root="Selected files")
        result["upload_id"] = upload_id
        return result

    def delete_upload(self, upload_id: str) -> None:
        root = self._upload_root(upload_id)
        active_ids = {
            str(group.get("upload_id"))
            for group in (self._operation or {}).get("groups", [])
            if group.get("upload_id")
        }
        if upload_id in active_ids and not self.state.get("finished"):
            raise RuntimeError("The upload is referenced by a resumable import")
        shutil.rmtree(root)

    # ------------------------------- scanning -------------------------------

    def scan(self) -> dict[str, Any]:
        root = self.root
        if root is None:
            raise FileNotFoundError("No import directory is configured or mounted")
        return self._scan_root(root)

    def _scan_root(
        self, root: Path, *, display_root: str | None = None
    ) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        skipped: list[dict[str, str]] = []
        self._walk(root, root, items, skipped)
        groups = self._group_items(items)
        return {
            "root": display_root or str(root),
            "groups": groups,
            "skipped": skipped,
            "rar_supported": rar_tool() is not None,
            "pdf_supported": pdf_tools_available(),
        }

    def _walk(
        self,
        directory: Path,
        root: Path,
        items: list[dict[str, Any]],
        skipped: list[dict[str, str]],
    ) -> None:
        try:
            children = sorted(directory.iterdir(), key=lambda p: natural_key(p.name))
        except OSError:
            return
        for child in children:
            if child.name.startswith("."):
                continue
            if child.is_symlink():
                skipped.append(
                    {
                        "path": str(child.relative_to(root)),
                        "reason": "Symlinks are not imported",
                    }
                )
                continue
            relative = str(child.relative_to(root))
            if child.is_dir():
                direct_images = [
                    entry
                    for entry in child.iterdir()
                    if entry.is_file() and entry.suffix.lower() in IMAGE_SUFFIXES
                ]
                if len(direct_images) >= MIN_FOLDER_IMAGES:
                    items.append(self._folder_item(child, relative, len(direct_images)))
                else:
                    self._walk(child, root, items, skipped)
                continue
            suffix = child.suffix.lower()
            if suffix not in ARCHIVE_SUFFIXES:
                continue
            if suffix in PDF_SUFFIXES:
                images = _pdf_page_count(child)
                if images == -1:
                    skipped.append(
                        {
                            "path": relative,
                            "reason": "PDF import requires the pdfinfo and pdftoppm tools",
                        }
                    )
                    continue
                if images == 0:
                    skipped.append(
                        {"path": relative, "reason": "PDF contains no renderable pages"}
                    )
                    continue
            elif suffix in RAR_SUFFIXES or is_rar_file(child):
                images = _rar_image_count(child)
                if images == -1:
                    skipped.append(
                        {
                            "path": relative,
                            "reason": "RAR support requires the unar or 7z tool",
                        }
                    )
                    continue
            else:
                images = _zip_image_count(child)
            if images <= 0:
                skipped.append(
                    {"path": relative, "reason": "No images found in archive"}
                )
                continue
            items.append(self._archive_item(child, relative, images))

    @staticmethod
    def _folder_item(path: Path, relative: str, images: int) -> dict[str, Any]:
        chapter_match = CHAPTER_DIR_PATTERN.match(path.name)
        volume_match = (
            VOLUME_DIR_PATTERN.match(path.name) if chapter_match is None else None
        )
        inferred_base, inferred_volume, inferred_chapter = infer_numbered_name(
            path.name
        )
        relative_parent = Path(relative).parent
        series_hint = path.parent.name if path.parent.name else inferred_base
        if (
            relative_parent == Path(".")
            and chapter_match is None
            and volume_match is None
        ):
            series_hint = inferred_base
        if chapter_match:
            chapter_title = chapter_match.group("title").strip()
        elif volume_match:
            chapter_title = ""
        else:
            chapter_title = path.name
        return {
            "path": relative,
            "kind": "folder",
            "size": sum(f.stat().st_size for f in path.iterdir() if f.is_file()),
            "images": images,
            "series_hint": series_hint,
            "volume": (
                volume_match.group("number") if volume_match else inferred_volume
            ),
            "chapter": (
                chapter_match.group("number") if chapter_match else inferred_chapter
            ),
            "chapter_title": chapter_title,
        }

    @staticmethod
    def _archive_item(path: Path, relative: str, images: int) -> dict[str, Any]:
        stem = path.stem.strip()
        series_hint, volume, chapter = infer_numbered_name(stem)
        return {
            "path": relative,
            "kind": "archive",
            "size": path.stat().st_size,
            "images": images,
            "series_hint": series_hint,
            "volume": volume,
            "chapter": chapter,
            "chapter_title": "",
        }

    @staticmethod
    def _group_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for item in items:
            if item["kind"] == "folder":
                key = f"dir:{Path(item['path']).parent}"
            else:
                base = item["series_hint"]
                # Numbered archives with siblings that share the same base
                # belong to one series; unnumbered files stand alone.
                key = (
                    f"series:{Path(item['path']).parent}:{base.lower()}"
                    if item["volume"] is not None or item["chapter"] is not None
                    else f"single:{item['path']}"
                )
            grouped.setdefault(key, []).append(item)
        groups: list[dict[str, Any]] = []
        for key, members in grouped.items():
            members.sort(key=lambda item: natural_key(item["path"]))
            authors, title = split_author_title(members[0]["series_hint"])
            if (
                len(members) == 1
                and members[0]["volume"] is None
                and members[0]["chapter"] is None
            ):
                members[0]["volume"] = "1"  # A standalone file is one volume.
            groups.append(
                {
                    "key": key,
                    "title": title,
                    "authors": authors,
                    "items": members,
                    "total_size": sum(item["size"] for item in members),
                }
            )
        groups.sort(key=lambda group: group["title"].lower())
        return groups

    # ------------------------------- importing ------------------------------

    def start(self, groups: list[dict[str, Any]], language: str) -> dict[str, Any]:
        if self._task is not None and not self._task.done():
            raise RuntimeError("An import is already running")
        if any(not group.get("upload_id") for group in groups) and self.root is None:
            raise FileNotFoundError("No import directory is configured or mounted")
        for group in groups:
            if upload_id := group.get("upload_id"):
                self._upload_root(str(upload_id))
            if target_manga_id := group.get("target_manga_id"):
                self.database.get_manga(str(target_manga_id))
        self.state = {
            "running": True,
            "total": sum(len(group["items"]) for group in groups),
            "done": 0,
            "imported": 0,
            "reused": 0,
            "series": 0,
            "current": "",
            "errors": [],
            "finished": False,
            "resumable": False,
            "completed_paths": [],
            "completed_groups": [],
        }
        self._operation = {
            "version": 1,
            "language": language,
            "groups": groups,
            "state": self.state,
        }
        self._persist_operation()
        self._task = asyncio.create_task(
            self._run(groups, language), name="tankarr-library-import"
        )
        return self.state

    async def _run(self, groups: list[dict[str, Any]], language: str) -> None:
        try:
            completed_groups = set(self.state.get("completed_groups") or [])
            for group in groups:
                legacy_group_key = str(group.get("key") or group.get("title") or "")
                group_key = ":".join(
                    (
                        str(group.get("upload_id") or "drop"),
                        legacy_group_key,
                    )
                )
                if (
                    group_key in completed_groups
                    or legacy_group_key in completed_groups
                ):
                    continue
                try:
                    root = self._group_root(group)
                    await self._import_group(root, group, language)
                    self.state["series"] += 1
                except Exception as exc:  # noqa: BLE001 - one series must not stop the run
                    self.state["errors"].append(
                        {"group": group.get("title", "?"), "error": str(exc)}
                    )
                completed_groups.add(group_key)
                self.state["completed_groups"] = sorted(completed_groups)
                self._persist_operation()
            alignment = await self.service.reconcile_komga_library()
            if not alignment.get("ready"):
                self.state["errors"].append(
                    {
                        "group": "Komga alignment",
                        "error": alignment.get("error")
                        or "Komga did not import every tracked book",
                    }
                )
            self.state["finished"] = True
            self.state["resumable"] = False
        except asyncio.CancelledError:
            self.state["finished"] = False
            self.state["resumable"] = True
            raise
        except Exception as exc:  # noqa: BLE001 - persist a resumable operation
            self.state["errors"].append(
                {
                    "group": "Import finalization",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            self.state["finished"] = False
            self.state["resumable"] = True
        finally:
            self.state["running"] = False
            self.state["current"] = ""
            self._persist_operation()

    def _group_root(self, group: dict[str, Any]) -> Path:
        upload_id = str(group.get("upload_id") or "")
        if upload_id:
            return self._upload_root(upload_id)
        root = self.root
        if root is None:
            raise FileNotFoundError("The configured import directory is unavailable")
        return root

    def _confined_source(self, root: Path, relative: str) -> Path:
        path = (root / relative).resolve()
        if root.resolve() not in path.parents and path != root.resolve():
            raise ValueError(f"Import path escapes the import directory: {relative}")
        return path

    @staticmethod
    def _deterministic_manga_id(title: str, authors: list[str]) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40] or "series"
        identity = local_series_identity(title, authors)
        return f"local-{slug}-{identity[:16]}"

    @staticmethod
    def _items_for_unit(
        raw_items: list[dict[str, Any]], unit: str | None
    ) -> list[dict[str, Any]]:
        items = [dict(item) for item in raw_items]
        if unit is None:
            for item in items:
                item["release_unit"] = (
                    "volume"
                    if item.get("volume") and not item.get("chapter")
                    else "chapter"
                )
            return items
        if unit not in {"chapters", "volumes"}:
            raise ValueError("Import unit must be chapters or volumes")
        number_field = "chapter" if unit == "chapters" else "volume"
        fallback_field = "volume" if unit == "chapters" else "chapter"
        release_unit = "chapter" if unit == "chapters" else "volume"
        for item in items:
            raw_number = item.get(number_field) or item.get(fallback_field)
            number = str(raw_number or "").strip()
            if not IMPORT_NUMBER_PATTERN.fullmatch(number):
                raise ValueError(
                    f"{item.get('path', 'Import item')} needs a valid {release_unit} number"
                )
            item["volume"] = number if release_unit == "volume" else None
            item["chapter"] = number if release_unit == "chapter" else None
            item["release_unit"] = release_unit
        return items

    @staticmethod
    def _completed_path_key(group: dict[str, Any], relative_path: str) -> str:
        upload_id = str(group.get("upload_id") or "")
        return f"upload:{upload_id}:{relative_path}" if upload_id else relative_path

    async def _import_group(
        self, root: Path, group: dict[str, Any], language: str
    ) -> None:
        group_language = str(group.get("language") or language).strip()
        target_manga_id = str(group.get("target_manga_id") or "").strip()
        if target_manga_id:
            manga = self.database.get_manga(target_manga_id)
            manga_id = str(manga["id"])
            title = str(manga["title"])
            expected_language = str(manga.get("preferred_language") or "").strip()
            if expected_language and group_language != expected_language:
                raise ValueError(
                    "Import language differs from the selected series language profile"
                )
            group_language = expected_language or group_language
        else:
            title = str(group.get("title") or "").strip()
            if not title:
                raise ValueError("A series title is required")
            authors = [
                str(author).strip()
                for author in group.get("authors", [])
                if str(author).strip()
            ]
            if not authors:
                inferred_authors, inferred_title = split_author_title(title)
                if inferred_authors:
                    authors = inferred_authors
                    title = inferred_title
            manga_id = self._deterministic_manga_id(title, authors)
            manga = {
                "id": manga_id,
                "provider": "local",
                "title": title,
                "description": "",
                "cover_url": None,
                "authors": authors,
                "original_language": group.get("original_language"),
                "status": None,
                "year": None,
                "last_volume": None,
                "last_chapter": None,
                "available_languages": [group_language],
                "source_url": group.get("source_url"),
            }

        def sort_number(value: object) -> float:
            try:
                return float(str(value))
            except (TypeError, ValueError):
                return float("inf")

        items = sorted(
            self._items_for_unit(group["items"], group.get("unit")),
            key=lambda item: (
                sort_number(item.get("volume")),
                sort_number(item.get("chapter")),
                natural_key(str(item["path"])),
            ),
        )
        seen_paths: set[str] = set()
        seen_releases: dict[str, str] = {}
        for item in items:
            relative_path = str(item["path"])
            if relative_path in seen_paths:
                raise ValueError(f"Import group repeats the same path: {relative_path}")
            seen_paths.add(relative_path)
            release_identity = local_release_identity(
                group_language, item.get("volume"), item.get("chapter")
            )
            duplicate = seen_releases.get(release_identity)
            if duplicate is not None:
                raise ValueError(
                    "Import group contains two files for the same language/volume/"
                    f"chapter: {duplicate} and {relative_path}"
                )
            seen_releases[release_identity] = relative_path

        if not target_manga_id:
            manga = await self.service.register_local_import_series(
                manga, group_language
            )
            manga_id = str(manga["id"])
        cover_written = bool(manga.get("cover_url"))
        completed_paths = set(self.state.get("completed_paths") or [])
        unit_override_applied = False
        for item in items:
            relative_path = str(item["path"])
            completed_path = self._completed_path_key(group, relative_path)
            if completed_path in completed_paths:
                continue
            source = self._confined_source(root, str(item["path"]))
            self.state["current"] = f"{title}: {source.name}"
            chapter_title = str(item.get("chapter_title") or "")
            if not chapter_title and item.get("volume") and not item.get("chapter"):
                # Volume archives carry no chapter number; give readers a
                # meaningful book title instead of "Chapter Special".
                chapter_title = f"Volume {item['volume']}"
            release_identity = local_release_identity(
                group_language, item.get("volume"), item.get("chapter")
            )
            source_provider = (
                item.get("source_provider") or group.get("source_provider") or "local"
            )
            if target_manga_id:
                source_provider = (
                    item.get("source_provider")
                    or group.get("source_provider")
                    or "Manual import"
                )
                identity_payload = "\0".join(
                    (manga_id, group_language, release_identity)
                )
                chapter_id = (
                    "manual-"
                    + hashlib.sha256(identity_payload.encode()).hexdigest()[:32]
                )
            else:
                chapter_id = f"{manga_id}-release-{release_identity[:16]}"
            chapter = {
                "id": chapter_id,
                "volume": item.get("volume"),
                "chapter": item.get("chapter"),
                "title": chapter_title,
                "language": group_language,
                # Imported files remain local ledger entries. Provenance is
                # carried separately so local identity/reuse invariants remain
                # deterministic while ComicInfo and the source link stay useful.
                "provider": "manual" if target_manga_id else "local",
                "source_provider": source_provider,
                "groups": [] if source_provider == "local" else [source_provider],
                "publish_at": None,
                "source_url": item.get("source_url") or group.get("source_url") or "",
                "pages": None,
                "version": 1,
                "release_unit": item["release_unit"],
            }
            try:
                result = None
                if not target_manga_id:
                    reuse_probe = await self.service.managed_local_import_probe(
                        manga_id, chapter
                    )
                    if reuse_probe is not None:
                        managed_path = Path(reuse_probe["path"])
                        archive_match = await asyncio.to_thread(
                            self._matching_archive_page_signature, source, managed_path
                        )
                        if archive_match:
                            result = await self.service.reuse_managed_local_import(
                                manga_id, chapter, archive_pages_match=True
                            )
                        elif reuse_probe["content_sha256"] is not None:
                            source_fingerprint = await asyncio.to_thread(
                                self._fast_source_content_sha256,
                                source,
                                ImportLimits.from_settings(self.settings),
                            )
                            if source_fingerprint is not None:
                                result = await self.service.reuse_managed_local_import(
                                    manga_id, chapter, source_fingerprint
                                )
                if result is None:
                    result = await self._import_item(
                        manga,
                        chapter,
                        source,
                        include_cover=not cover_written and not target_manga_id,
                        target_manga_id=target_manga_id or None,
                    )
                cover_written = bool(result.get("cover_url"))
                self.state["imported"] += 1
                if result.get("reused"):
                    self.state["reused"] += 1
                if (
                    target_manga_id
                    and not unit_override_applied
                    and group.get("set_series_unit")
                    and group.get("unit") in {"chapters", "volumes"}
                ):
                    await asyncio.to_thread(
                        self.database.update_manga,
                        manga_id,
                        {"series_unit_override": group["unit"]},
                    )
                    unit_override_applied = True
            except Exception as exc:  # noqa: BLE001 - keep importing the rest
                self.state["errors"].append(
                    {"group": title, "path": str(item["path"]), "error": str(exc)}
                )
            self.state["done"] += 1
            completed_paths.add(completed_path)
            self.state["completed_paths"] = sorted(completed_paths)
            self._persist_operation()

    @staticmethod
    def _fast_source_content_sha256(
        source: Path, limits: ImportLimits | None = None
    ) -> str | None:
        """Fingerprint ZIP/folder pages without extracting or repackaging them."""

        limits = limits or ImportLimits()
        digest = hashlib.sha256(LOCAL_CONTENT_FINGERPRINT_VERSION)
        if source.is_dir():
            entries = sorted(
                (
                    entry
                    for entry in source.iterdir()
                    if entry.is_file() and entry.suffix.lower() in IMAGE_SUFFIXES
                ),
                key=lambda entry: natural_key(entry.name),
            )
            if not entries:
                return None
            if any(entry.is_symlink() for entry in entries):
                raise ValueError("Symlinked import pages are not supported")
            if (
                len(entries) > limits.pages
                or sum(entry.stat().st_size for entry in entries)
                > limits.expanded_bytes
            ):
                raise ImportLimitError("Import exceeds page or expanded-byte budget")
            for index, entry in enumerate(entries, start=1):
                size = entry.stat().st_size
                digest.update(f"{index}:{size}\0".encode())
                with entry.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(chunk)
            return digest.hexdigest()
        if source.suffix.casefold() not in {".cbz", ".zip"}:
            return None
        with zipfile.ZipFile(source) as archive:
            entries = sorted(
                (
                    entry
                    for entry in archive.infolist()
                    if not entry.is_dir()
                    and Path(entry.filename).suffix.lower() in IMAGE_SUFFIXES
                    and not entry.filename.startswith("__MACOSX")
                    and not Path(entry.filename).name.startswith(".")
                ),
                key=lambda entry: natural_key(entry.filename),
            )
            if not entries:
                return None
            if (
                len(entries) > limits.pages
                or sum(entry.file_size for entry in entries) > limits.expanded_bytes
            ):
                raise ImportLimitError("Import exceeds page or expanded-byte budget")
            for index, entry in enumerate(entries, start=1):
                digest.update(f"{index}:{entry.file_size}\0".encode())
                with archive.open(entry) as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(chunk)
        return digest.hexdigest()

    @classmethod
    def _matching_archive_page_signature(cls, source: Path, managed: Path) -> bool:
        if is_rar_file(source):
            source_signature = cls._rar_page_signature(source)
        elif source.suffix.casefold() in {".cbz", ".zip"}:
            source_signature = cls._zip_page_signature(source)
        else:
            return False
        return bool(source_signature) and source_signature == cls._zip_page_signature(
            managed
        )

    @staticmethod
    def _rar_page_signature(path: Path) -> tuple[tuple[int, int], ...]:
        """Read RAR page sizes and CRCs from lsar's structured index.

        lsar only lists; whichever tool extracts, its index is used when it is
        installed.
        """

        if not shutil.which("lsar"):
            return ()
        try:
            result = subprocess.run(
                ["lsar", "-json", str(path)],
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            if result.returncode != 0:
                return ()
            payload = json.loads(result.stdout)
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
            return ()
        entries: list[tuple[str, int, int]] = []
        for entry in payload.get("lsarContents", []):
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("XADFileName") or "")
            if (
                Path(name).suffix.casefold() not in IMAGE_SUFFIXES
                or name.startswith("__MACOSX")
                or Path(name).name.startswith(".")
            ):
                continue
            size = entry.get("XADFileSize")
            crc = entry.get("RARCRC32")
            if not isinstance(size, int) or not isinstance(crc, int):
                continue
            entries.append((name, size, crc))
        entries.sort(key=lambda item: natural_key(item[0]))
        return tuple((size, crc) for _name, size, crc in entries)

    @staticmethod
    def _zip_page_signature(path: Path) -> tuple[tuple[int, int], ...]:
        """Read only the ZIP directory: no page decompression is required."""

        with zipfile.ZipFile(path) as archive:
            entries = sorted(
                (
                    entry
                    for entry in archive.infolist()
                    if not entry.is_dir()
                    and Path(entry.filename).suffix.lower() in IMAGE_SUFFIXES
                    and not entry.filename.startswith("__MACOSX")
                    and not Path(entry.filename).name.startswith(".")
                ),
                key=lambda entry: natural_key(entry.filename),
            )
            return tuple((entry.file_size, entry.CRC) for entry in entries)

    async def _import_item(
        self,
        manga: dict[str, Any],
        chapter: dict[str, Any],
        source: Path,
        *,
        include_cover: bool,
        target_manga_id: str | None = None,
    ) -> dict[str, Any]:
        with tempfile.TemporaryDirectory(
            dir=self.settings.staging_dir, prefix="import-"
        ) as workspace:
            work_dir = Path(workspace)
            pages = await asyncio.to_thread(
                self._extract_pages, source, work_dir / "pages"
            )
            if not pages:
                raise ValueError("No importable pages found")
            chapter["pages"] = len(pages)
            cbz_path = work_dir / "import.cbz"
            content_sha256, archive_info = await asyncio.to_thread(
                self._package_local_import, cbz_path, pages, manga, chapter
            )
            if target_manga_id:
                return await self.service.publish_external_import(
                    target_manga_id,
                    chapter,
                    cbz_path,
                    archive_info["sha256"],
                    content_sha256,
                )
            return await self.service.publish_local_import(
                manga,
                chapter,
                cbz_path,
                archive_info["sha256"],
                content_sha256,
                pages[0] if include_cover else None,
            )

    def _single_book_work(self, manga: dict[str, Any]) -> bool:
        """Whether the catalogue (or the operator) says the edition is one book."""

        unit = str(manga.get("expected_count_unit_override") or "").casefold()
        override = manga.get("expected_count_override")
        if override is not None and unit in {"", "volume", "volumes", "book", "books"}:
            try:
                return int(override) == 1
            except (TypeError, ValueError):
                return False
        if effective_edition_book_count(manga) == 1:
            return True
        metadata_row = self.database.get_series_metadata(str(manga["id"]))
        data = (metadata_row or {}).get("data") or manga.get("metadata") or {}
        try:
            return int(data.get("volume_count") or 0) == 1
        except (TypeError, ValueError):
            return False

    def scan_torrent_content(self, content: Path) -> dict[str, Any]:
        """Inspect only one qBittorrent-owned content path."""

        if not content.exists() or content.is_symlink():
            raise FileNotFoundError("Completed torrent content is unavailable")
        items: list[dict[str, Any]] = []
        skipped: list[dict[str, str]] = []
        if content.is_dir():
            root = content
            self._walk(root, root, items, skipped)
        elif content.is_file():
            root = content.parent
            suffix = content.suffix.casefold()
            relative = content.name
            if suffix not in ARCHIVE_SUFFIXES:
                raise TorrentImportAmbiguous(
                    "The completed torrent contains no supported comic archive"
                )
            if suffix in PDF_SUFFIXES:
                images = _pdf_page_count(content)
            elif suffix in RAR_SUFFIXES or is_rar_file(content):
                images = _rar_image_count(content)
            else:
                images = _zip_image_count(content)
            if images <= 0:
                raise TorrentImportAmbiguous(
                    "The completed torrent archive contains no image pages"
                )
            items.append(self._archive_item(content, relative, images))
        else:
            raise TorrentImportAmbiguous("Torrent content is not a file or directory")
        if not items:
            detail = skipped[0]["reason"] if skipped else "No comic archives found"
            raise TorrentImportAmbiguous(detail)
        if len(items) > 500:
            raise TorrentImportAmbiguous("Torrent contains more than 500 comic books")
        return {"root": root, "items": items, "skipped": skipped}

    async def import_existing_series_archive(
        self,
        manga_id: str,
        relative_path: str,
        language: str,
        parts: list[dict[str, Any]],
        *,
        source_url: str,
        source_name: str,
        confirm_language: bool = False,
    ) -> dict[str, Any]:
        """Split and attach one manually mapped archive to a canonical series.

        Manual imports are deliberately explicit: every source page must belong to
        exactly one numbered part. This keeps Wanted counts, per-chapter deletion,
        naming, and reader metadata correct instead of hiding several chapters in
        one opaque omnibus file.
        """

        self.service.assert_mutations_allowed()
        root = self.root
        if root is None:
            raise FileNotFoundError("No import directory is configured or mounted")
        unresolved = root / relative_path
        if unresolved.is_symlink():
            raise ValueError("Symlinks are not accepted for manual imports")
        source = self._confined_source(root, relative_path)
        if not source.is_file():
            raise FileNotFoundError("The manual import archive is unavailable")
        if source.suffix.casefold() not in ARCHIVE_SUFFIXES:
            raise ValueError("The manual import source is not a supported archive")

        manga = self.database.get_manga(manga_id)
        if language != manga.get("preferred_language"):
            raise ValueError(
                "Manual import language differs from the series language profile"
            )
        if language != "en":
            raise ValueError(
                "Manual archive imports currently require an English series profile"
            )
        source_url = source_url.strip()
        parsed_source = urlsplit(source_url)
        if (
            parsed_source.scheme not in {"http", "https"}
            or not parsed_source.hostname
            or parsed_source.username is not None
            or parsed_source.password is not None
        ):
            raise ValueError("Manual import source URL must be a public HTTP(S) URL")
        normalized_source_name = " ".join(source_name.split()).strip()
        if not normalized_source_name:
            raise ValueError("Manual import source name is required")

        normalized_parts = sorted(parts, key=lambda item: int(item["page_start"]))
        identities: set[str] = set()
        for part in normalized_parts:
            start = int(part["page_start"])
            end = int(part["page_end"])
            if end < start:
                raise ValueError("Manual import page_end cannot precede page_start")
            identity = local_release_identity(
                language, part.get("volume"), part.get("chapter")
            )
            if identity in identities:
                raise ValueError(
                    "Manual import maps more than one part to the same volume/chapter"
                )
            identities.add(identity)

        prepared: list[dict[str, Any]] = []
        audits: list[dict[str, Any]] = []
        with tempfile.TemporaryDirectory(
            dir=self.settings.staging_dir, prefix="manual-import-"
        ) as workspace:
            workspace_root = Path(workspace)
            pages = await asyncio.to_thread(
                self._extract_pages, source, workspace_root / "source-pages"
            )
            if not pages:
                raise ValueError("The manual import archive contains no image pages")

            assigned: set[int] = set()
            for part in normalized_parts:
                start = int(part["page_start"])
                end = int(part["page_end"])
                if end > len(pages):
                    raise ValueError(
                        f"Manual import page range {start}-{end} exceeds the "
                        f"{len(pages)} source pages"
                    )
                page_numbers = set(range(start, end + 1))
                if assigned & page_numbers:
                    raise ValueError("Manual import page ranges overlap")
                assigned.update(page_numbers)
            expected_pages = set(range(1, len(pages) + 1))
            if assigned != expected_pages:
                missing = sorted(expected_pages - assigned)
                first = missing[0]
                last = missing[-1]
                gap = str(first) if first == last else f"{first}-{last}"
                raise ValueError(
                    "Manual import mapping must account for every source page; "
                    f"unmapped page range includes {gap}"
                )

            for index, part in enumerate(normalized_parts, start=1):
                start = int(part["page_start"])
                end = int(part["page_end"])
                selected_pages = pages[start - 1 : end]
                audit = await asyncio.to_thread(
                    audit_english_pages, selected_pages, detect_scripts=True
                )
                audit["confirmation_decision"] = automatic_english_decision(
                    audit,
                    declared_language=language,
                    expected_language=str(manga["preferred_language"]),
                    names=[normalized_source_name, relative_path],
                )
                audits.append(
                    {
                        "volume": part.get("volume"),
                        "chapter": part.get("chapter"),
                        "page_start": start,
                        "page_end": end,
                        **audit,
                    }
                )
                chapter_number = part.get("chapter")
                volume_number = part.get("volume")
                chapter_title = str(part.get("title") or "").strip()
                if not chapter_title:
                    chapter_title = (
                        f"Chapter {chapter_number}"
                        if chapter_number not in (None, "")
                        else f"Volume {volume_number}"
                    )
                identity_payload = "\0".join(
                    (
                        manga_id,
                        language,
                        source_url,
                        str(volume_number or ""),
                        str(chapter_number or ""),
                        str(start),
                        str(end),
                    )
                )
                chapter = {
                    "id": (
                        "manual-"
                        + hashlib.sha256(identity_payload.encode()).hexdigest()[:32]
                    ),
                    "volume": volume_number,
                    "chapter": chapter_number,
                    "title": chapter_title,
                    "language": language,
                    "provider": "manual",
                    "source_provider": normalized_source_name,
                    "groups": [normalized_source_name],
                    "publish_at": None,
                    "source_url": source_url,
                    "pages": len(selected_pages),
                    "version": 1,
                }
                cbz_path = workspace_root / f"book-{index:04d}.cbz"
                content_sha256, archive_info = await asyncio.to_thread(
                    self._package_local_import,
                    cbz_path,
                    selected_pages,
                    manga,
                    chapter,
                )
                prepared.append(
                    {
                        "chapter": chapter,
                        "cbz_path": cbz_path,
                        "content_sha256": content_sha256,
                        "archive_sha256": archive_info["sha256"],
                    }
                )

            evidence = {
                "provider": {
                    "name": "manual",
                    "label": normalized_source_name,
                    "declared_language": language,
                    "verdict": "confirmed",
                },
                "ocr": audits,
                "manual_confirmation": bool(confirm_language),
            }
            hard_refusal = any(
                audit["confirmation_decision"].get("verdict") == "refused"
                for audit in audits
            )
            if hard_refusal or (
                not confirm_language
                and any(audit.get("verdict") != "confirmed" for audit in audits)
            ):
                evidence["verdict"] = "review"
                raise LanguageReviewRequired(evidence)
            evidence["verdict"] = "confirmed"

            paths: list[str] = []
            reused = 0
            for item in prepared:
                result = await self.service.publish_external_import(
                    manga_id,
                    item["chapter"],
                    item["cbz_path"],
                    item["archive_sha256"],
                    item["content_sha256"],
                )
                paths.append(str(result["path"]))
                reused += int(bool(result.get("reused")))

        return {
            "manga_id": manga_id,
            "source_path": relative_path,
            "books": len(paths),
            "paths": paths,
            "reused": reused,
            "language_evidence": evidence,
            "reader_independent": True,
        }

    async def import_torrent_download(
        self,
        job: dict[str, Any],
        content: Path,
        *,
        confirm_language: bool = False,
        skip_unnumbered: bool = False,
        selected_paths: set[str] | None = None,
        assigned: dict[str, dict[str, Any]] | None = None,
        automatic_decision: bool = False,
    ) -> dict[str, Any]:
        """Normalize, OCR-audit, and attach one completed torrent release.

        ``selected_paths`` is the operator's explicit choice of which files in
        the release to take. A pack can hold two editions of the same volume,
        and picking between them is a judgement Tankarr must not make on its
        own; when the choice is given, it is obeyed exactly. Automatic decisions
        skip ambiguous slots while retaining useful books elsewhere in a pack.
        """

        scan = self.scan_torrent_content(content)
        root = Path(scan["root"])
        items = list(scan["items"])
        skip_decisions = list(scan["skipped"])
        manual_language = bool(confirm_language and not automatic_decision)
        if selected_paths is not None:
            items = [item for item in items if str(item["path"]) in selected_paths]
            if not items:
                raise TorrentImportAmbiguous("No file in this release was selected")
        for item in items:
            # A book the release does not number is unplaceable by inspection;
            # what the operator says it is, it is.
            named = (assigned or {}).get(str(item["path"]))
            if not named:
                continue
            volume = str(named.get("volume") or "").strip()
            chapter = str(named.get("chapter") or "").strip()
            if not volume and not chapter:
                continue
            item["volume"] = volume or None
            item["chapter"] = chapter or None
        manga = self.database.get_manga(str(job["manga_id"]))
        language = str(job["language"])
        provider_source = str(job.get("source") or "").casefold()
        if provider_source not in TORRENT_IMPORT_PROVIDERS:
            raise TorrentImportAmbiguous("Unsupported torrent import provider")
        if language != "en" or manga.get("preferred_language") != language:
            raise TorrentImportAmbiguous(
                "Torrent imports currently require an English series language profile"
            )
        missing = [
            item for item in items if not item.get("volume") and not item.get("chapter")
        ]
        if missing and len(items) == 1:
            hint = str(job.get("volume_hint") or "")
            if "-" in hint or "," in hint:
                # Six tankobon in one archive are not volume "1": a pack has
                # to be split, or taken as an omnibus on purpose by the
                # operator (Pineapple Army, 2026-09-03: 1 456 pages as v1).
                raise TorrentImportAmbiguous(
                    f"One file carries volumes {hint}: split it or assign it by hand"
                )
            missing[0]["volume"] = job.get("volume_hint")
            missing[0]["chapter"] = job.get("chapter_hint")
            if not missing[0]["volume"] and not missing[0]["chapter"]:
                # A single unnumbered book of a one-book work has only one
                # place to go (Ding Dong Circus, Cinderalla: the file is the
                # whole edition). Guessing a number for anything else stays
                # a decision for the operator.
                if self._single_book_work(manga):
                    missing[0]["volume"] = "1"
            missing = [
                item
                for item in items
                if not item.get("volume") and not item.get("chapter")
            ]
        if missing and skip_unnumbered and len(missing) < len(items):
            # A pack can bundle other works with no number of their own; the
            # operator asked to take only the books this release identifies.
            skipped = {id(item) for item in missing}
            skip_decisions.extend(
                {"path": str(item["path"]), "reason": "unnumbered"} for item in missing
            )
            items = [item for item in items if id(item) not in skipped]
            missing = []
        if missing:
            names = ", ".join(str(item["path"]) for item in missing[:3])
            raise TorrentImportAmbiguous(
                f"Cannot derive a volume or chapter number for: {names}",
                skip_decisions=skip_decisions
                + [
                    {"path": str(item["path"]), "reason": "unnumbered"}
                    for item in missing
                ],
            )

        # A persisted picker choice can outlive the state it was based on, and
        # older clients could explicitly select a slot that the library already
        # owns. Replacing an existing book is not part of this import flow: drop
        # occupied slots before duplicate checks, extraction, OCR and packaging
        # so the remaining books in a pack can still be imported.
        def identity_for(item, *, owned=False):
            volume, chapter = item.get("volume"), item.get("chapter")
            if automatic_decision:
                if owned and item.get("release_unit") == "volume":
                    chapter = None
                elif chapter is not None or (
                    owned and item.get("release_unit") == "chapter"
                ):
                    volume = None
            if volume is None and chapter is None:
                return None
            return local_release_identity(
                item.get("language", language), volume, chapter
            )

        if automatic_decision:
            numbered = []
            for item in items:
                number = (
                    item.get("chapter")
                    if item.get("chapter") is not None
                    else item.get("volume")
                )
                if canonical_label(number) is None:
                    skip_decisions.append(
                        {"path": str(item["path"]), "reason": "ambiguous_numbering"}
                    )
                else:
                    numbered.append(item)
            items = numbered

        owned_identities = {
            identity_for(chapter, owned=True)
            for chapter in self.database.list_all_chapters(str(job["manga_id"]))
            if chapter.get("downloaded")
            and chapter.get("provider") != "assembled"
            and (chapter.get("volume") or chapter.get("chapter"))
        }
        already_owned_items: list[dict[str, Any]] = []
        importable_items: list[dict[str, Any]] = []
        for item in items:
            identity = identity_for(item)
            if identity in owned_identities:
                already_owned_items.append(item)
            else:
                importable_items.append(item)
        items = importable_items
        already_owned_paths = [str(item["path"]) for item in already_owned_items]
        skip_decisions.extend(
            {"path": path, "reason": "already_owned"} for path in already_owned_paths
        )

        if automatic_decision:
            groups: dict[str, list[dict]] = {}
            for item in items:
                groups.setdefault(identity_for(item), []).append(item)
            items = []
            for candidates in groups.values():
                candidates.sort(key=lambda item: natural_key(str(item["path"])))
                if len(candidates) == 1:
                    items.extend(candidates)
                    continue
                same_bytes = False
                if (
                    all(item["kind"] == "archive" for item in candidates)
                    and len({item["size"] for item in candidates}) == 1
                ):
                    digests = [
                        await asyncio.to_thread(
                            sha256, self._confined_source(root, str(item["path"]))
                        )
                        for item in candidates
                    ]
                    same_bytes = len(set(digests)) == 1
                if same_bytes:
                    items.append(candidates[0])
                skip_decisions.extend(
                    {
                        "path": str(item["path"]),
                        "reason": "identical_copy"
                        if same_bytes
                        else "ambiguous_editions",
                    }
                    for item in (candidates[1:] if same_bytes else candidates)
                )

        language_evidence = {
            "provider": {
                "name": provider_source,
                "label": torrent_source_label(provider_source, job.get("indexer")),
                "indexer": job.get("indexer"),
                "category": job["category"],
                "declared_language": "en",
                "verdict": "confirmed",
            },
            "ocr": [],
            "manual_confirmation": manual_language,
            "automatic_decision": automatic_decision,
            "skip_decisions": skip_decisions,
            "verdict": "confirmed",
        }
        if not items:
            if automatic_decision:
                raise TorrentImportAmbiguous(
                    "No useful numbered books remain after the automatic pack decision",
                    skip_decisions=skip_decisions,
                )
            self._report_torrent_progress(
                job,
                progress=1,
                message=(
                    f"Skipped {len(already_owned_items)} book(s) already in the library"
                ),
            )
            return {
                "books": 0,
                "paths": [],
                "reused": 0,
                "already_owned": len(already_owned_items),
                "already_owned_paths": already_owned_paths,
                "skipped": scan["skipped"],
                "skipped_paths": [item["path"] for item in skip_decisions],
                "skip_decisions": skip_decisions,
                "language_evidence": language_evidence,
                "komga": None,
            }

        identities: dict[str, str] = {}
        for item in items:
            identity = identity_for(item)
            previous = identities.get(identity)
            if previous is not None:
                raise TorrentImportAmbiguous(
                    "Torrent maps two files to the same volume/chapter: "
                    f"{previous} and {item['path']}"
                )
            identities[identity] = str(item["path"])

        prepared: list[dict[str, Any]] = []
        audits: list[dict[str, Any]] = []
        with tempfile.TemporaryDirectory(
            dir=self.settings.staging_dir, prefix="torrent-import-"
        ) as workspace:
            workspace_root = Path(workspace)
            for index, item in enumerate(items, start=1):
                # Normalising a 250-page book takes tens of seconds, so a pack
                # runs for minutes: say which book is in hand rather than
                # leaving one opaque bar for the whole release.
                self._report_torrent_progress(
                    job,
                    progress=(index - 1) / (2 * len(items)),
                    message=f"Preparing book {index} of {len(items)}",
                )
                archive_source = self._confined_source(root, str(item["path"]))
                pages = await asyncio.to_thread(
                    self._extract_pages,
                    archive_source,
                    workspace_root / f"pages-{index:04d}",
                )
                if not pages:
                    raise TorrentImportAmbiguous(
                        f"No importable pages found in {item['path']}"
                    )
                audit = await asyncio.to_thread(
                    audit_english_pages,
                    pages,
                    **(
                        {"detect_scripts": True}
                        if automatic_decision or manual_language
                        else {}
                    ),
                )
                if automatic_decision or manual_language:
                    audit = {
                        **audit,
                        "automatic_decision": automatic_english_decision(
                            audit,
                            declared_language=language,
                            expected_language=str(manga["preferred_language"]),
                            names=[
                                str(job.get("title") or ""),
                                str(item["path"]),
                                str(job.get("category") or ""),
                                str(job.get("torrent_url") or ""),
                            ],
                        ),
                    }
                audits.append({"path": str(item["path"]), **audit})
                chapter_title = (
                    f"Chapter {item['chapter']}"
                    if item.get("chapter")
                    else f"Volume {item['volume']}"
                )
                chapter = {
                    "id": self._torrent_chapter_id(job, item, language),
                    "volume": item.get("volume"),
                    "chapter": item.get("chapter"),
                    "title": chapter_title,
                    "language": language,
                    "provider": provider_source,
                    "groups": [str(job.get("indexer") or "Torrent")],
                    "publish_at": job.get("publish_at"),
                    "source_url": job["source_url"],
                    "pages": len(pages),
                    "version": 1,
                }
                cbz_path = workspace_root / f"book-{index:04d}.cbz"
                content_sha256, archive_info = await asyncio.to_thread(
                    self._package_local_import, cbz_path, pages, manga, chapter
                )
                prepared.append(
                    {
                        "source_path": str(item["path"]),
                        "chapter": chapter,
                        "cbz_path": cbz_path,
                        "content_sha256": content_sha256,
                        "archive_sha256": archive_info["sha256"],
                    }
                )

            language_evidence["ocr"] = audits
            hard_refusal = manual_language and any(
                audit.get("automatic_decision", {}).get("verdict") == "refused"
                for audit in audits
            )
            if hard_refusal or (
                not manual_language
                and any(
                    (
                        audit.get("automatic_decision", {})
                        if automatic_decision
                        else audit
                    ).get("verdict")
                    != "confirmed"
                    for audit in audits
                )
            ):
                language_evidence["verdict"] = "review"
                raise LanguageReviewRequired(language_evidence)
            language_evidence["verdict"] = "confirmed"

            paths: list[str] = []
            reused = 0
            for index, item in enumerate(prepared, start=1):
                try:
                    result = await self.service.publish_external_import(
                        str(job["manga_id"]),
                        item["chapter"],
                        item["cbz_path"],
                        item["archive_sha256"],
                        item["content_sha256"],
                    )
                except ExternalImportConflict as exc:
                    if not automatic_decision:
                        raise
                    original_path = item["source_path"]
                    skip_decisions.append(
                        {
                            "path": original_path,
                            "reason": "owned_conflict",
                            "detail": str(exc),
                        }
                    )
                    already_owned_paths.append(original_path)
                    already_owned_items.append({"path": original_path})
                    continue
                paths.append(str(result["path"]))
                reused += int(bool(result.get("reused")))
                # Persist each book as it lands: the operator can see which of
                # a ten-volume pack are already in the library.
                self._report_torrent_progress(
                    job,
                    progress=0.5 + index / (2 * len(prepared)),
                    message=f"Imported {len(paths)} of {len(prepared)} books",
                    imported_paths=list(paths),
                )

        if automatic_decision and not paths:
            raise TorrentImportAmbiguous(
                "No useful books remain after checking current library ownership",
                skip_decisions=skip_decisions,
            )
        alignment = await self.service.reconcile_komga_library()
        return {
            "books": len(paths),
            "paths": paths,
            "reused": reused,
            "already_owned": len(already_owned_items),
            "already_owned_paths": already_owned_paths,
            "skipped": scan["skipped"],
            "skipped_paths": [item["path"] for item in skip_decisions],
            "skip_decisions": skip_decisions,
            "language_evidence": language_evidence,
            "komga": alignment,
        }

    def _report_torrent_progress(
        self,
        job: dict[str, Any],
        *,
        progress: float,
        message: str,
        imported_paths: list[str] | None = None,
    ) -> None:
        """Publish how far a multi-book release has got, best effort."""

        identifier = job.get("id")
        if identifier is None:
            return
        try:
            self.database.update_torrent_download(
                int(identifier),
                progress=max(0.0, min(1.0, progress)),
                message=message,
                **({"imported_paths": imported_paths} if imported_paths else {}),
            )
        except Exception:  # noqa: BLE001 - progress must never fail an import
            return

    @staticmethod
    def _torrent_chapter_id(
        job: dict[str, Any], item: dict[str, Any], language: str
    ) -> str:
        payload = "\0".join(
            (
                str(job.get("source") or "torrent").casefold(),
                str(job["info_hash"]).casefold(),
                str(item["path"]),
                language,
                str(item.get("volume") or ""),
                str(item.get("chapter") or ""),
            )
        )
        return f"torrent-{hashlib.sha256(payload.encode()).hexdigest()[:32]}"

    def _package_local_import(
        self,
        cbz_path: Path,
        pages: list[Path],
        manga: dict[str, Any],
        chapter: dict[str, Any],
    ) -> tuple[str, dict[str, Any]]:
        if chapter.get("volume") and not chapter.get("chapter") and manga.get("id"):
            root = self.settings.data_dir.resolve()
            for record in self.database.list_volume_metadata(str(manga["id"])):
                if str(record.get("volume_key")) != str(chapter["volume"]):
                    continue
                relative = str(record.get("artwork_path") or "")
                if not relative:
                    break
                cover = (root / relative).resolve()
                if cover.is_relative_to(root) and restore_cover_first(pages, cover):
                    logger.info(
                        "Restored cover-first page order for volume %s of %s",
                        chapter["volume"],
                        manga["id"],
                    )
                break
        content_sha256 = local_page_content_sha256(pages)
        _path, archive_info = package_cbz_validated(cbz_path, pages, manga, chapter)
        return content_sha256, archive_info

    def _extract_pages(self, source: Path, target: Path) -> list[Path]:
        target.mkdir(parents=True, exist_ok=True)
        limits = ImportLimits.from_settings(self.settings)
        if source.is_dir():
            entries = sorted(
                (
                    entry
                    for entry in source.iterdir()
                    if entry.is_file() and entry.suffix.lower() in IMAGE_SUFFIXES
                ),
                key=lambda entry: natural_key(entry.name),
            )
            if any(entry.is_symlink() for entry in entries):
                raise ImportLimitError("Symlinked image pages are not imported")
            limits.check(
                sum(entry.stat().st_size for entry in entries), len(entries), target
            )
            return self._number_pages(entries, target, copy=True)
        suffix = source.suffix.lower()
        if suffix in PDF_SUFFIXES:
            entries = _pdf_extract(source, target, limits=limits)
            # Preserve the extractor tree until workspace cleanup. Moving
            # nested RAR members while traversing that same tree can leave a
            # directory in a transient non-empty state on some archives.
            return self._number_pages(entries, target, copy=True)
        if suffix in RAR_SUFFIXES or is_rar_file(source):
            extracted = target.parent / "rar"
            extracted.mkdir(parents=True, exist_ok=True)
            _rar_extract(source, extracted, limits=limits)
            if any(entry.is_symlink() for entry in extracted.rglob("*")):
                raise ImportLimitError("Symlinked archive members are not imported")
            entries = sorted(
                (
                    entry
                    for entry in extracted.rglob("*")
                    if entry.is_file() and entry.suffix.lower() in IMAGE_SUFFIXES
                ),
                key=lambda entry: natural_key(str(entry.relative_to(extracted))),
            )
            return self._number_pages(entries, target, copy=False)
        with zipfile.ZipFile(source) as archive:
            entries = sorted(
                (
                    item
                    for item in archive.infolist()
                    if not item.is_dir()
                    and Path(item.filename).suffix.lower() in IMAGE_SUFFIXES
                    and not item.filename.startswith("__MACOSX")
                    and not Path(item.filename).name.startswith(".")
                ),
                key=lambda item: natural_key(item.filename),
            )
            limits.check(sum(item.file_size for item in entries), len(entries), target)
            pages: list[Path] = []
            total = 0
            for index, item in enumerate(entries, start=1):
                page = target / f"{index:04d}{Path(item.filename).suffix.lower()}"
                with archive.open(item) as member, page.open("wb") as handle:
                    while chunk := member.read(1024 * 1024):
                        total += len(chunk)
                        limits.check(total, index, target, reserve_output=False)
                        handle.write(chunk)
                check_page_geometry(page)
                pages.append(page)
            return pages

    @staticmethod
    def _number_pages(entries: list[Path], target: Path, *, copy: bool) -> list[Path]:
        pages: list[Path] = []
        for index, entry in enumerate(entries, start=1):
            check_page_geometry(entry)
            page = target / f"{index:04d}{entry.suffix.lower()}"
            if copy:
                shutil.copyfile(entry, page)
            else:
                shutil.move(entry, page)
            pages.append(page)
        return pages
