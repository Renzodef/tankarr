from __future__ import annotations

import ctypes
import errno
import hashlib
import os
import shutil
import sys
import uuid
import zipfile
from functools import cache
from pathlib import Path
from typing import Any

from tankarr.comicinfo import build_comic_info

IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif")
MAX_COMIC_INFO_BYTES = 2 * 1024**2


class ImportDurabilityError(RuntimeError):
    pass


def package_cbz(
    output_path: Path,
    pages: list[Path],
    manga: dict,
    chapter: dict,
) -> Path:
    packaged, _info = package_cbz_validated(output_path, pages, manga, chapter)
    return packaged


def package_cbz_validated(
    output_path: Path,
    pages: list[Path],
    manga: dict,
    chapter: dict,
) -> tuple[Path, dict[str, Any]]:
    """Create and validate a CBZ once, returning its immutable ledger."""

    if not pages:
        raise ValueError("Cannot package an empty chapter")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    partial = output_path.parent / f".tankarr-package-{uuid.uuid4().hex}.partial"
    try:
        with zipfile.ZipFile(
            partial, "x", compression=zipfile.ZIP_STORED, allowZip64=True
        ) as archive:
            archive.writestr(
                "ComicInfo.xml", build_comic_info(manga, chapter, len(pages))
            )
            for page in pages:
                archive.write(page, arcname=page.name)
        info = validate_cbz(partial)
        partial.replace(output_path)
        return output_path, info
    finally:
        partial.unlink(missing_ok=True)


def validate_cbz(path: Path) -> dict[str, Any]:
    with zipfile.ZipFile(path) as archive:
        if any(
            item.filename == "ComicInfo.xml" and item.file_size > MAX_COMIC_INFO_BYTES
            for item in archive.infolist()
        ):
            raise ValueError("ComicInfo.xml exceeds the 2 MiB metadata limit")
        bad = archive.testzip()
        if bad:
            raise ValueError(f"Corrupt CBZ member: {bad}")
        names = archive.namelist()
        images = [name for name in names if Path(name).suffix.lower() in IMAGE_SUFFIXES]
        if not images:
            raise ValueError("CBZ contains no supported image pages")
        if "ComicInfo.xml" not in names:
            raise ValueError("CBZ has no ComicInfo.xml")
    return {
        "page_count": len(images),
        "size": path.stat().st_size,
        "sha256": sha256(path),
    }


def replace_comic_info(
    path: Path, comic_info: bytes, *, allow_missing: bool = False
) -> dict[str, Any]:
    """Atomically replace ComicInfo.xml while preserving every page payload."""

    if len(comic_info) > MAX_COMIC_INFO_BYTES:
        raise ValueError("ComicInfo.xml exceeds the 2 MiB metadata limit")
    try:
        snapshot = read_comic_info(path)
    except ValueError as exc:
        if not allow_missing or str(exc) != "CBZ has no ComicInfo.xml":
            raise
        # A caller that has independently verified the page-content identity may
        # adopt a hand-imported ZIP/CBR by supplying its first ComicInfo record.
        with zipfile.ZipFile(path) as archive:
            images = [
                name
                for name in archive.namelist()
                if Path(name).suffix.lower() in IMAGE_SUFFIXES
            ]
            if not images:
                raise ValueError("CBZ contains no supported image pages")
            snapshot = {"comic_info": None, "page_count": len(images)}
    if snapshot["comic_info"] == comic_info:
        return {
            "page_count": snapshot["page_count"],
            "size": path.stat().st_size,
            "sha256": None,
            "changed": False,
        }

    partial = path.with_name(f".{path.name}.comicinfo-{uuid.uuid4().hex}.partial")
    try:
        with (
            zipfile.ZipFile(path) as source,
            zipfile.ZipFile(partial, "w", allowZip64=True) as target,
        ):
            target.comment = source.comment
            target.writestr("ComicInfo.xml", comic_info)
            for item in source.infolist():
                if item.filename == "ComicInfo.xml":
                    continue
                with (
                    source.open(item) as reader,
                    target.open(item, "w", force_zip64=True) as writer,
                ):
                    shutil.copyfileobj(reader, writer, length=1024 * 1024)
        after = validate_cbz(partial)
        with partial.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(partial, path)
        fsync_directory(path.parent)
        return {**after, "changed": True}
    finally:
        partial.unlink(missing_ok=True)


def read_comic_info(path: Path) -> dict[str, Any]:
    """Read portable metadata and page count without inflating every page."""

    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if "ComicInfo.xml" not in names:
            raise ValueError("CBZ has no ComicInfo.xml")
        images = [name for name in names if Path(name).suffix.lower() in IMAGE_SUFFIXES]
        if not images:
            raise ValueError("CBZ contains no supported image pages")
        member = archive.getinfo("ComicInfo.xml")
        if member.file_size > MAX_COMIC_INFO_BYTES:
            raise ValueError("ComicInfo.xml exceeds the 2 MiB metadata limit")
        with archive.open(member) as reader:
            metadata = reader.read(MAX_COMIC_INFO_BYTES + 1)
        if len(metadata) > MAX_COMIC_INFO_BYTES:
            raise ValueError("ComicInfo.xml exceeds the 2 MiB metadata limit")
        return {
            "comic_info": metadata,
            "page_count": len(images),
        }


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def install_atomically(
    source: Path,
    destination: Path,
    *,
    expected_sha256: str | None = None,
) -> Path:
    if expected_sha256 is not None and (
        len(expected_sha256) != 64
        or any(character not in "0123456789abcdef" for character in expected_sha256)
    ):
        raise ValueError("Expected archive SHA-256 is invalid")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_symlink():
        raise FileExistsError(f"Refusing to replace a library symlink: {destination}")
    if destination.exists():
        source_sha256 = sha256(source)
        if expected_sha256 is not None and source_sha256 != expected_sha256:
            raise ValueError("Staging file changed before publication")
        if source_sha256 == sha256(destination):
            _ensure_import_directory_durable(destination)
            return destination
        raise FileExistsError(
            f"Refusing to overwrite a different library file: {destination}"
        )
    partial = destination.parent / f".tankarr-import-{uuid.uuid4().hex}.partial"
    try:
        source_digest = hashlib.sha256()
        with source.open("rb") as source_handle, partial.open("xb") as target_handle:
            for chunk in iter(lambda: source_handle.read(1024 * 1024), b""):
                source_digest.update(chunk)
                target_handle.write(chunk)
            target_handle.flush()
            os.fsync(target_handle.fileno())
        source_sha256 = source_digest.hexdigest()
        if expected_sha256 is not None and source_sha256 != expected_sha256:
            raise ValueError("Staging file changed before publication")
        if source_sha256 != sha256(partial):
            raise ValueError("Imported CBZ hash does not match staging file")
        try:
            publish_without_overwrite(partial, destination)
        except FileExistsError as exc:
            # Another importer or reader may create the destination while the
            # staging file is copied. Reuse identical bytes, never clobber it.
            if destination.is_symlink() or source_sha256 != sha256(destination):
                raise FileExistsError(
                    f"Refusing to overwrite a different library file: {destination}"
                ) from exc
        _ensure_import_directory_durable(destination)
        return destination
    finally:
        partial.unlink(missing_ok=True)


@cache
def _linux_renameat2() -> Any:
    if not sys.platform.startswith("linux"):
        return None
    function = getattr(ctypes.CDLL(None, use_errno=True), "renameat2", None)
    if function is not None:
        function.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        function.restype = ctypes.c_int
    return function


def publish_without_overwrite(source: Path, destination: Path) -> None:
    """Publish a completed same-filesystem file, atomically and without clobbering.

    Linux RENAME_NOREPLACE supports CIFS without requiring hard links. Other
    platforms/filesystems can use link/unlink. Never fall back to exists+replace:
    the destination can appear between those calls. The caller fsyncs the file
    before publication and its directory afterwards.
    """

    renameat2 = _linux_renameat2()
    if renameat2 is not None:
        if renameat2(-100, os.fsencode(source), -100, os.fsencode(destination), 1) == 0:
            return
        error = ctypes.get_errno()
        if error not in {errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP}:
            raise OSError(error, os.strerror(error), str(destination))
    try:
        os.link(source, destination, follow_symlinks=False)
    except OSError as exc:
        if exc.errno in {errno.ENOSYS, errno.EOPNOTSUPP, errno.EPERM}:
            raise OSError(
                exc.errno,
                "Filesystem does not support safe no-overwrite publication; "
                "atomic rename-without-replace or hard links are required",
                str(destination),
            ) from exc
        raise
    source.unlink()


def fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _ensure_import_directory_durable(destination: Path) -> None:
    try:
        fsync_directory(destination.parent)
    except OSError as exc:
        raise ImportDurabilityError(
            "Library file was published but its directory durability could not "
            f"be confirmed: {destination}"
        ) from exc


def count_archive_pages(path: Path) -> int:
    """Number of image pages in one CBZ, without reading page contents."""

    with zipfile.ZipFile(path) as archive:
        return sum(
            1
            for name in archive.namelist()
            if not name.endswith("/") and Path(name).suffix.lower() in IMAGE_SUFFIXES
        )
