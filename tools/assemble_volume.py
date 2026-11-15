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
import os
import re
import shutil
import stat
import tempfile
import warnings
import zipfile
from pathlib import Path

from PIL import Image, UnidentifiedImageError

IMAGES = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif"}
GIB = 1024**3


def natural_key(value):
    return [int(x) if x.isdigit() else x.casefold() for x in re.split(r"(\d+)", value)]


def checksum(path):
    value = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            value.update(chunk)
    return value.hexdigest()


def package(manifest, source_dir, destination, *, reserve_bytes=25 * GIB):
    if manifest.get("version") != 1:
        raise ValueError("Unsupported manifest")
    pages = manifest.get("pages")
    expanded = manifest.get("expanded_bytes")
    if type(pages) is not int or not 1 <= pages <= 20_000:
        raise ValueError("Invalid page budget")
    if type(expanded) is not int or not 0 < expanded <= 16 * GIB:
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
    destination.parent.mkdir(parents=True, exist_ok=True)
    needed = reserve_bytes + expanded + len(xml) + pages * 1024
    if shutil.disk_usage(destination.parent).free < needed:
        raise ValueError("Worker scratch reserve would be violated")
    selected = []
    for index, item in enumerate(sources, 1):
        if item.get("file") != f"{index:06d}.cbz":
            raise ValueError("Source names must be consecutive private files")
        path = source_dir / item["file"]
        if path.is_symlink() or not path.is_file():
            raise ValueError("Source is not a regular private file")
        if path.stat().st_size != item["size"] or checksum(path) != item["sha256"]:
            raise ValueError("Source differs from the reviewed snapshot")
        selected.append((item, path))
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
            for item, path in selected:
                before = count
                with zipfile.ZipFile(path) as source:
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
                    for member in members:
                        if (
                            stat.S_ISLNK(member.external_attr >> 16)
                            or member.flag_bits & 1
                        ):
                            raise ValueError("Unsafe source page")
                        total += member.file_size
                        count += 1
                        if total > expanded or count > pages:
                            raise ValueError("Sources exceeded the reviewed budget")
                        with source.open(member) as reader:
                            try:
                                with warnings.catch_warnings():
                                    warnings.simplefilter(
                                        "error", Image.DecompressionBombWarning
                                    )
                                    with Image.open(reader) as image:
                                        if image.width * image.height > 40_000_000:
                                            raise ValueError(
                                                "Image exceeds the pixel limit"
                                            )
                            except UnidentifiedImageError:
                                pass  # Same opaque-codec rule as the native importer.
                        name = f"{count:06d}{Path(member.filename).suffix.casefold()}"
                        with (
                            source.open(member) as reader,
                            output.open(name, "w", force_zip64=True) as writer,
                        ):
                            shutil.copyfileobj(reader, writer, 1024 * 1024)
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
    parser.add_argument("manifest", type=Path)
    parser.add_argument("sources", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--reserve-gib", type=int, default=25)
    args = parser.parse_args()
    if args.reserve_gib < 25:
        parser.error("the worker must retain at least 25 GiB")
    result = package(
        json.loads(args.manifest.read_text()),
        args.sources,
        args.output,
        reserve_bytes=args.reserve_gib * GIB,
    )
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
