#!/usr/bin/env python3
"""Package a Tankarr assembly manifest on a worker, without API credentials.

Copy the exact sources named in the manifest into a private directory first.
This tool never imports a book or removes source files. Tankarr independently
attests the returned archive before publishing it and recycling its chapters.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import stat
import sys
import tempfile
import warnings
import zipfile
from pathlib import Path

from PIL import Image, UnidentifiedImageError
from PIL import __version__ as pillow_version

IMAGES = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif"}
GIB = 1024**3
BUFFER_BYTES = 1024**2
DEFAULT_RESERVE_BYTES = GIB // 2
DEFAULT_MAX_PAGES = 20_000
DEFAULT_MAX_EXPANDED_BYTES = 16 * GIB


class _HeaderNeedsSpool(Exception):
    """A codec needs more than the bounded, rewindable header prefix."""


class _HeaderReader:
    def __init__(self, reader, page_size):
        self.reader = reader
        self.page_size = page_size
        self.prefix = bytearray()
        self.position = 0

    def tell(self):
        return self.position

    def seek(self, offset, whence=0):
        if whence == 1:
            offset += self.position
        elif whence != 0:
            raise _HeaderNeedsSpool
        if offset < 0:
            raise ValueError("Negative header offset")
        if offset > BUFFER_BYTES:
            raise _HeaderNeedsSpool
        if offset > len(self.prefix):
            self.read(offset - self.position)
        self.position = offset
        return offset

    def read(self, size=-1):
        if size < 0 and self.page_size is not None:
            size = max(0, self.page_size - self.position)
        if size < 0 or self.position + size > BUFFER_BYTES:
            raise _HeaderNeedsSpool
        end = self.position + size
        if end > len(self.prefix):
            self.prefix.extend(self.reader.read(end - len(self.prefix)))
        result = bytes(self.prefix[self.position : end])
        self.position += len(result)
        return result

    def close(self):
        # Some plugins close the supplied handle. The page stream remains ours.
        pass


def _check_geometry(reader):
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(reader) as image:
                if image.width * image.height > 40_000_000:
                    raise ValueError("Image exceeds the pixel limit")
    except UnidentifiedImageError:
        pass  # Same opaque-codec rule as the native importer.


def _copy_page(reader, writer, scratch_dir, page_size=None):
    """Decompress once, retaining only a bounded prefix for ordinary headers.

    Seeking codecs spill to scratch rather than retaining a whole page in our
    buffers. Pillow's own codec allocations are unchanged by this optimization.
    """
    header = _HeaderReader(reader, page_size)
    try:
        _check_geometry(header)
    except _HeaderNeedsSpool:
        with tempfile.TemporaryFile(dir=scratch_dir) as page:
            page.write(header.prefix)
            shutil.copyfileobj(reader, page, BUFFER_BYTES)
            page.seek(0)
            _check_geometry(page)
            page.seek(0)
            shutil.copyfileobj(page, writer, BUFFER_BYTES)
    else:
        writer.write(header.prefix)
        shutil.copyfileobj(reader, writer, BUFFER_BYTES)


def _open_regular(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise ValueError("Source is not a regular private file")
    return os.fdopen(descriptor, "rb")


def _identity(handle):
    info = os.fstat(handle.fileno())
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _members(source):
    members = sorted(
        (
            x
            for x in source.infolist()
            if not x.is_dir()
            and Path(x.filename).suffix.casefold() in IMAGES
            and not x.filename.startswith("__MACOSX")
            and not Path(x.filename).name.startswith(".")
        ),
        key=lambda x: natural_key(x.filename),
    )
    if len({x.filename for x in members}) != len(members):
        raise ValueError("Duplicate source pages")
    if any(stat.S_ISLNK(x.external_attr >> 16) or x.flag_bits & 1 for x in members):
        raise ValueError("Unsafe source page")
    return members


def natural_key(value):
    return [int(x) if x.isdigit() else x.casefold() for x in re.split(r"(\d+)", value)]


def checksum(path):
    value = hashlib.sha256()
    with _open_regular(path) as handle:
        while chunk := handle.read(BUFFER_BYTES):
            value.update(chunk)
    return value.hexdigest()


def _policy(reserve_bytes, max_pages, max_expanded_bytes):
    if type(reserve_bytes) is not int or not 0 <= reserve_bytes <= 1024 * GIB:
        raise ValueError("Invalid scratch reserve")
    if type(max_pages) is not int or not 1 <= max_pages <= 100_000:
        raise ValueError("Invalid maximum page policy")
    if (
        type(max_expanded_bytes) is not int
        or not BUFFER_BYTES <= max_expanded_bytes <= 1024 * GIB
    ):
        raise ValueError("Invalid maximum expanded-size policy")
    return {
        "reserve_bytes": reserve_bytes,
        "max_pages": max_pages,
        "max_expanded_bytes": max_expanded_bytes,
    }


def _existing_directory(path):
    while not path.exists() and path.parent != path:
        path = path.parent
    if not path.is_dir():
        raise ValueError("Scratch parent is not a directory")
    return path


def preflight(
    manifest,
    source_dir,
    destination,
    *,
    reserve_bytes=DEFAULT_RESERVE_BYTES,
    max_pages=DEFAULT_MAX_PAGES,
    max_expanded_bytes=DEFAULT_MAX_EXPANDED_BYTES,
):
    policy = _policy(reserve_bytes, max_pages, max_expanded_bytes)
    if manifest.get("version") != 1:
        raise ValueError("Unsupported manifest")
    pages = manifest.get("pages")
    expanded = manifest.get("expanded_bytes")
    if type(pages) is not int or not 1 <= pages <= max_pages:
        raise ValueError("Invalid page budget")
    if type(expanded) is not int or not 0 < expanded <= max_expanded_bytes:
        raise ValueError("Invalid expanded-size budget")
    xml = manifest["comic_info"].encode("utf-8")
    if not xml or len(xml) > 2 * 1024**2:
        raise ValueError("Invalid metadata size")
    sources = manifest["sources"]
    if not isinstance(sources, list) or not 1 <= len(sources) <= pages:
        raise ValueError("Invalid sources")
    source_dir = Path(source_dir)
    destination = Path(destination)
    if destination.exists() or destination.is_symlink():
        raise FileExistsError("Output already exists")
    needed = reserve_bytes + expanded + len(xml) + pages * 1024
    free = shutil.disk_usage(_existing_directory(destination.parent)).free
    if free < needed:
        raise ValueError("Worker scratch reserve would be violated")
    largest_page = source_bytes = source_pages = 0
    for index, item in enumerate(sources, 1):
        if item.get("file") != f"{index:06d}.cbz":
            raise ValueError("Source names must be consecutive private files")
        path = source_dir / item["file"]
        if path.is_symlink() or not path.is_file():
            raise ValueError("Source is not a regular private file")
        if (
            type(item.get("pages")) is not int
            or not 1 <= item["pages"] <= pages
            or type(item.get("size")) is not int
            or item["size"] <= 0
            or not isinstance(item.get("id"), str)
            or not item["id"]
            or not isinstance(item.get("sha256"), str)
            or not re.fullmatch("[0-9a-f]{64}", item["sha256"])
        ):
            raise ValueError("Invalid source declaration")
        if path.stat().st_size != item["size"] or checksum(path) != item["sha256"]:
            raise ValueError("Source differs from the reviewed snapshot")
        with _open_regular(path) as handle, zipfile.ZipFile(handle) as source:
            members = _members(source)
            if len(members) != item["pages"]:
                raise ValueError("Source page count differs from the manifest")
            source_pages += len(members)
            source_bytes += sum(x.file_size for x in members)
            largest_page = max(largest_page, max(x.file_size for x in members))
    if sum(item["pages"] for item in sources) != pages:
        raise ValueError("Source page budgets differ from the manifest")
    if source_pages != pages or source_bytes != expanded:
        raise ValueError("Source sizes differ from the manifest")
    # A seeking codec may spill one page beside the output, never every page.
    needed += largest_page
    if shutil.disk_usage(_existing_directory(destination.parent)).free < needed:
        raise ValueError("Worker scratch reserve would be violated")
    return {
        "ready": True,
        "manifest_version": 1,
        "source_count": len(sources),
        "pages": pages,
        "expanded_bytes": expanded,
        "required_free_bytes": needed,
        "free_bytes": free,
        "policy": policy,
    }


def package(
    manifest,
    source_dir,
    destination,
    *,
    reserve_bytes=DEFAULT_RESERVE_BYTES,
    max_pages=DEFAULT_MAX_PAGES,
    max_expanded_bytes=DEFAULT_MAX_EXPANDED_BYTES,
):
    preflight(
        manifest,
        source_dir,
        destination,
        reserve_bytes=reserve_bytes,
        max_pages=max_pages,
        max_expanded_bytes=max_expanded_bytes,
    )
    pages = manifest["pages"]
    expanded = manifest["expanded_bytes"]
    xml = manifest["comic_info"].encode("utf-8")
    source_dir, destination = Path(source_dir), Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=".assemble-", dir=destination.parent
    )
    os.close(descriptor)
    temporary = Path(temporary)
    count = total = 0
    chapter_pages = []
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_STORED) as output:
            output.writestr("ComicInfo.xml", xml)
            for item in manifest["sources"]:
                path = source_dir / item["file"]
                before = count
                with _open_regular(path) as handle, zipfile.ZipFile(handle) as source:
                    identity = _identity(handle)
                    members = _members(source)
                    for member in members:
                        total += member.file_size
                        count += 1
                        if total > expanded or count > pages:
                            raise ValueError("Sources exceeded the reviewed budget")
                        name = f"{count:06d}{Path(member.filename).suffix.casefold()}"
                        with (
                            source.open(member) as reader,
                            output.open(name, "w", force_zip64=True) as writer,
                        ):
                            _copy_page(
                                reader, writer, destination.parent, member.file_size
                            )
                    if _identity(handle) != identity:
                        raise ValueError("Source changed while packaging")
                if count - before != item["pages"] or checksum(path) != item["sha256"]:
                    raise ValueError("Source changed while packaging")
                chapter_pages.append(
                    {"id": item["id"], "start": before + 1, "end": count}
                )
        if count != pages or total != expanded:
            raise ValueError("Incomplete page coverage")
        if shutil.disk_usage(destination.parent).free < reserve_bytes:
            raise ValueError("Worker scratch reserve lost before commit")
        with zipfile.ZipFile(temporary) as check:
            if check.testzip() is not None:
                raise ValueError("Output failed its integrity check")
        digest = checksum(temporary)
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        # Never replace a previous successful result, even after a race.
        os.link(temporary, destination)
        directory = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return {
            "sha256": digest,
            "size": temporary.stat().st_size,
            "pages": count,
            "expanded_bytes": total,
            "chapters": chapter_pages,
        }
    finally:
        temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path, nargs="?")
    parser.add_argument("sources", type=Path, nargs="?")
    parser.add_argument("output", type=Path, nargs="?")
    parser.add_argument("--reserve-gib", type=float, default=0.5)
    parser.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES)
    parser.add_argument("--max-expanded-gib", type=float, default=16)
    parser.add_argument("--scratch-dir", type=Path, default=Path.cwd())
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--capabilities", action="store_true")
    mode.add_argument("--preflight", action="store_true")
    args = parser.parse_args()
    try:
        if not all(math.isfinite(x) for x in (args.reserve_gib, args.max_expanded_gib)):
            raise ValueError("GiB limits must be finite")
        if args.reserve_gib < 0:
            raise ValueError("Scratch reserve must be nonnegative")
        policy = _policy(
            int(args.reserve_gib * GIB),
            args.max_pages,
            int(args.max_expanded_gib * GIB),
        )
        if args.capabilities:
            if any((args.manifest, args.sources, args.output)):
                raise ValueError("Capabilities does not take packaging paths")
            result = {
                "schema_version": 1,
                "manifest_versions": [1],
                "python": list(sys.version_info[:3]),
                "pillow": pillow_version,
                "cpu_count": len(os.sched_getaffinity(0))
                if hasattr(os, "sched_getaffinity")
                else os.cpu_count() or 1,
                "free_bytes": shutil.disk_usage(
                    _existing_directory(args.scratch_dir)
                ).free,
                "policy": policy,
                "features": ["preflight", "bounded-header-streaming", "durable-output"],
            }
        else:
            if not all((args.manifest, args.sources, args.output)):
                raise ValueError("manifest, sources and output are required")
            operation = preflight if args.preflight else package
            result = operation(
                json.loads(args.manifest.read_text()),
                args.sources,
                args.output,
                **policy,
            )
    except (ValueError, OSError, zipfile.BadZipFile) as exc:
        parser.exit(1, f"{exc}\n")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
