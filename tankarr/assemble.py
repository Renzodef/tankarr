"""Assemble an exact book from verified chapter files, then recycle its sources."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import secrets
import stat
import tempfile
import zipfile
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query

from tankarr.archive import IMAGE_SUFFIXES, package_cbz_validated
from tankarr.chapter_map import canonical_label, chapters_by_volume
from tankarr.chapter_mapping import effective_edition_book_count
from tankarr.database import Database
from tankarr.import_limits import ImportLimitError, ImportLimits, check_page_geometry
from tankarr.importer import natural_key
from tankarr.naming import final_library_path
from tankarr.series_unit import is_volume_release
from tankarr.service import (
    LibraryUnavailable,
    RecoveryBlocked,
    TankarrService,
    UnsafeLibraryPath,
    local_page_content_sha256,
)


class AssemblyConflict(ValueError):
    def __init__(self, message: str, *, missing_chapters: list[str] | None = None):
        super().__init__(message)
        self.detail = {"message": message, "missing_chapters": missing_chapters or []}


def _source_label(release: dict[str, Any]) -> str:
    label = str(
        release.get("source_name") or release.get("provider") or "Unknown source"
    )
    language = str(release.get("language") or "").upper()
    suffix = f"({language})"
    return f"{label} {suffix}" if language and not label.endswith(suffix) else label


@contextmanager
def _open_library_file(path: Path, root: Path):
    """Open beneath the verified root without following changed symlink parents."""
    relative = path.relative_to(root)
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in relative.parts[:-1]:
            child = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = child
        file_descriptor = os.open(
            relative.name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=descriptor,
        )
        with os.fdopen(file_descriptor, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise AssemblyConflict("A source is not a regular chapter file")
            yield handle
    finally:
        os.close(descriptor)


def _image_members(archive: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    entries = sorted(
        (
            item
            for item in archive.infolist()
            if not item.is_dir()
            and Path(item.filename).suffix.casefold() in IMAGE_SUFFIXES
            and not item.filename.startswith("__MACOSX")
            and not Path(item.filename).name.startswith(".")
        ),
        key=lambda item: natural_key(item.filename),
    )
    if not entries:
        raise AssemblyConflict("A chapter archive contains no image pages")
    if any(stat.S_ISLNK(item.external_attr >> 16) for item in entries):
        raise AssemblyConflict("Symlinked archive pages cannot be assembled")
    if any(item.flag_bits & 1 for item in entries):
        raise AssemblyConflict("Encrypted chapter pages cannot be assembled")
    return entries


def _file_snapshot(path: Path, root: Path) -> dict[str, Any]:
    with _open_library_file(path, root) as handle:
        before = os.fstat(handle.fileno())
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
        handle.seek(0)
        with zipfile.ZipFile(handle) as archive:
            images = _image_members(archive)
            pages = len(images)
            expanded = sum(item.file_size for item in images)
        after = os.fstat(handle.fileno())

    def identity(info):
        return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)

    if identity(before) != identity(after):
        raise AssemblyConflict("A chapter file changed while it was inspected")
    return {
        "path": str(path),
        "sha256": digest,
        "size": after.st_size,
        "identity": identity(after),
        "pages": pages,
        "expanded_bytes": expanded,
    }


class BookAssembler:
    def __init__(self, database: Database, service: TankarrService):
        self.database = database
        self.service = service
        self._confirmation_key = secrets.token_bytes(32)

    def _confirmation(self, plan: dict[str, Any]) -> str:
        reviewed = {
            **plan,
            "manga": {
                key: plan["manga"].get(key)
                for key in (
                    "id",
                    "title",
                    "authors",
                    "description",
                    "preferred_language",
                    "metadata",
                )
            },
        }
        payload = json.dumps(
            reviewed, sort_keys=True, separators=(",", ":"), default=str
        )
        return hmac.new(
            self._confirmation_key, payload.encode(), hashlib.sha256
        ).hexdigest()

    def _plan(self, manga_id: str, volume: str) -> dict[str, Any]:
        label = canonical_label(volume)
        if label is None or Decimal(label) < 1:
            raise AssemblyConflict("Volume must be a positive number")
        manga = self.database.get_manga(manga_id, include_logical_counts=False)
        releases = self.database.list_chapters(manga_id, manga["preferred_language"])
        entries = self.database.chapter_map(manga_id)
        exact = chapters_by_volume(entries)
        required = sorted(exact.get(label, ()), key=Decimal)
        if not required:
            raise AssemblyConflict("An exact chapter-to-book map is required")
        if any(
            set(required) & chapters
            for other_volume, chapters in exact.items()
            if other_volume != label
        ):
            raise AssemblyConflict(
                "The exact map assigns a chapter to more than one book"
            )
        if any(
            release.get("downloaded")
            and is_volume_release(release)
            and canonical_label(release.get("volume")) == label
            for release in releases
            # An unresolved provider chapter with a volume hint is not a book.
            if str(release.get("release_unit") or "chapter") == "volume"
            or release.get("source_chapter") is None
        ):
            raise AssemblyConflict("This volume is already owned")
        root = self.service._library_root()
        selected = []
        missing = []
        problems = []
        canonical = {number for numbers in exact.values() for number in numbers}
        # A whole chapter the map lists beside its own parts has no file of
        # its own: Adekan's volume 18 maps 65, 65.1 and 65.2, the two parts
        # are on disk and no source has ever carried a "65". The parts are
        # already required for this book, so they cover it; demanding the
        # whole as well only blocks the assembly for good. What counts is a
        # usable file, not a row: a provider may list the whole and never
        # deliver it. A book that maps the whole alone keeps the ordinary
        # path, where the parts stand in for it.
        required_parts: dict[str, list[str]] = defaultdict(list)
        for other in required:
            base = self._part_base(other)
            if base is not None and base in required:
                required_parts[base].append(other)
        # Only a complete contiguous set from one base is that chapter split
        # in two, exactly as the split candidates demand. A lone "55.5" is a
        # side story the sources list below the numbering, not half of 55.
        parts_in_book = {
            base
            for base, parts in required_parts.items()
            if len(parts) >= 2
            and sorted(parts, key=Decimal)
            == [
                canonical_label(format(Decimal(base) + Decimal(index) / 10, "f"))
                for index in range(1, len(parts) + 1)
            ]
        }
        for number in required:
            alternatives = [
                [
                    [
                        release
                        for release in releases
                        if canonical_label(release.get("chapter")) == number
                    ]
                ],
                *self._split_candidates(number, releases, canonical),
            ]
            chosen = None
            errors = []
            for groups in alternatives:
                files = []
                for candidates in groups:
                    item, failures = self._select_file(candidates, root)
                    errors.extend(failures)
                    if item is None:
                        break
                    files.append({**item, "chapter": number})
                else:
                    chosen = files
                    break
            if chosen is None:
                # Nothing at all to select for the whole, and its own parts
                # are the book: the whole was never published. A file that
                # existed and was refused is a failure, never a silent skip,
                # or an unsafe path would quietly drop a chapter.
                if number in parts_in_book and not errors:
                    continue
                missing.append(number)
                if errors:
                    problems.append(f"Chapter {number}: {errors[0]}")
            else:
                selected.extend(chosen)
        if missing:
            message = "Every mapped chapter must have a usable file on disk"
            if problems:
                message += ". " + "; ".join(problems)
            raise AssemblyConflict(message, missing_chapters=missing)
        paths = [Path(item["file"]["path"]) for item in selected]
        if len(set(paths)) != len(paths):
            raise AssemblyConflict("Several mapped chapters refer to the same file")
        self.service._assert_library_paths_not_shared(
            manga_id, {str(item["release"]["id"]) for item in selected}, paths
        )
        limits = ImportLimits.from_settings(self.service.settings)
        expanded = sum(item["file"]["expanded_bytes"] for item in selected)
        pages = sum(item["file"]["pages"] for item in selected)
        limits.check(expanded, pages, self.service.settings.data_dir)
        metadata = (self.database.get_series_metadata(manga_id) or {}).get("data") or {}
        volume_metadata = next(
            (
                item["data"]
                for item in self.database.list_volume_metadata(manga_id)
                if canonical_label(item["volume_key"]) == label
            ),
            {},
        )
        book_count = effective_edition_book_count(manga)
        if book_count is not None:
            metadata = {**metadata, "book_count": book_count}
        manga = {**manga, "metadata": metadata}
        chapter_ids = [str(item["release"]["id"]) for item in selected]
        identity = json.dumps([manga_id, manga["preferred_language"], label])
        sources = sorted({_source_label(item["release"]) for item in selected})
        notes = f"Assembled from chapters {', '.join(required)}; sources: {', '.join(sources)}"
        provenance = {
            "version": 1,
            "chapter_ids": chapter_ids,
            "chapters": required,
            "volume": label,
            "sources": sources,
            "notes": notes,
        }
        book = {
            "id": "assembled-" + hashlib.sha256(identity.encode()).hexdigest()[:32],
            "provider": "assembled",
            "release_unit": "volume",
            "volume": label,
            "chapter": None,
            "language": manga["preferred_language"],
            "title": volume_metadata.get("title") or f"Volume {label}",
            "metadata": volume_metadata,
            "pages": pages,
            "version": 1,
            "groups": [],
            "publish_at": None,
            "source_url": "",
            "assembled_from": provenance,
            "notes": notes,
        }
        destination = self.service._confined_library_path(
            final_library_path(root, manga, book), root
        )
        return {
            "manga": manga,
            "volume": label,
            "map": [asdict(entry) for entry in entries],
            "releases": [
                release
                for release in releases
                if canonical_label(release.get("chapter")) in required
                or canonical_label(release.get("volume")) == label
                or self._part_base(release.get("chapter")) in required
            ],
            "selected": selected,
            "required": required,
            "book": book,
            "root": str(root),
            "destination": str(destination),
            "destination_file": _file_snapshot(destination, root)
            if destination.exists()
            else None,
        }

    @staticmethod
    def _part_base(label: object) -> str | None:
        canonical = canonical_label(label)
        if canonical is None:
            return None
        number = Decimal(canonical)
        if number != number.to_integral_value():
            return str(int(number))
        return None

    @classmethod
    def _split_candidates(cls, number, releases, canonical):
        base = Decimal(number)
        if base <= 0 or base != base.to_integral_value():
            return []
        sources = defaultdict(lambda: defaultdict(list))
        for release in releases:
            part = canonical_label(
                release.get("chapter")
                if release.get("chapter") is not None
                else release.get("source_chapter")
            )
            if part in canonical or cls._part_base(part) != number:
                continue
            source = str(
                release.get("source_key")
                or release.get("source_name")
                or release.get("provider")
            )
            sources[source][part].append(release)
        alternatives = []
        for source in sorted(sources):
            parts = sorted(sources[source], key=Decimal)
            # Only a complete contiguous set from one source represents the
            # whole chapter. A missing known final part cannot be truncated.
            expected = [
                canonical_label(format(base + Decimal(index) / 10, "f"))
                for index in range(1, len(parts) + 1)
            ]
            if len(parts) >= 2 and parts == expected:
                alternatives.append([sources[source][part] for part in parts])
        return alternatives

    def _select_file(self, candidates, root):
        errors = []
        for release in sorted(
            candidates,
            key=lambda release: (
                int(release.get("version") or 0),
                int(release.get("pages") or 0),
                str(release.get("publish_at") or ""),
                str(release.get("id") or ""),
            ),
            reverse=True,
        ):
            if (
                not release.get("downloaded")
                or is_volume_release(release)
                or str(release.get("numbering_status") or "mapped") != "mapped"
            ):
                continue
            try:
                recorded = release.get("library_path")
                if not recorded:
                    raise AssemblyConflict("No recorded library file")
                path = self.service._recorded_library_path(str(recorded), root)
                snapshot = _file_snapshot(path, root)
                expected = self.service._expected_library_digest(release)
                if expected and not hmac.compare_digest(expected, snapshot["sha256"]):
                    raise AssemblyConflict(
                        "The chapter file differs from its hash ledger"
                    )
                return {"release": release, "file": snapshot}, errors
            except (OSError, ValueError, zipfile.BadZipFile) as exc:
                errors.append(str(exc))
        return None, errors

    def _response(self, plan: dict[str, Any]) -> dict[str, Any]:
        return {
            "manga_id": plan["manga"]["id"],
            "volume": plan["volume"],
            "filename": Path(plan["destination"]).name,
            "pages": plan["book"]["pages"],
            "chapters": [
                {
                    "id": item["release"]["id"],
                    "chapter": item["chapter"],
                    "source_chapter": item["release"]["chapter"],
                    "pages": item["file"]["pages"],
                    "source": item["release"].get("source_name")
                    or item["release"]["provider"],
                }
                for item in plan["selected"]
            ],
            "confirmation_snapshot": self._confirmation(plan),
        }

    def preview(self, manga_id: str, volume: str) -> dict[str, Any]:
        with self.database.read_snapshot():
            return self._response(self._plan(manga_id, volume))

    def _package(self, plan: dict[str, Any], workspace: Path):
        limits = ImportLimits.from_settings(self.service.settings)
        pages = []
        total = 0
        for item in plan["selected"]:
            # A verified private copy prevents later library edits from mixing
            # old and new bytes in the assembled volume.
            copied = workspace / "source.cbz"
            digest = hashlib.sha256()
            copied_bytes = 0
            with (
                _open_library_file(
                    Path(item["file"]["path"]), Path(plan["root"])
                ) as source,
                copied.open("xb") as target,
            ):
                while chunk := source.read(1024 * 1024):
                    copied_bytes += len(chunk)
                    if copied_bytes > item["file"]["size"]:
                        raise AssemblyConflict("A chapter file grew after preview")
                    limits.check(total + copied_bytes, len(pages), workspace)
                    digest.update(chunk)
                    target.write(chunk)
            if not hmac.compare_digest(digest.hexdigest(), item["file"]["sha256"]):
                raise AssemblyConflict("A chapter file changed after preview")
            with zipfile.ZipFile(copied) as archive:
                for member in _image_members(archive):
                    number = len(pages) + 1
                    page = (
                        workspace
                        / f"{number:06d}{Path(member.filename).suffix.casefold()}"
                    )
                    with archive.open(member) as reader, page.open("xb") as writer:
                        while chunk := reader.read(1024 * 1024):
                            total += len(chunk)
                            limits.check(total, number, workspace)
                            writer.write(chunk)
                    check_page_geometry(page)
                    pages.append(page)
            copied.unlink()
        if len(pages) != plan["book"]["pages"]:
            raise AssemblyConflict("Chapter page counts changed after preview")
        content_sha256 = local_page_content_sha256(pages)
        path, archive_info = package_cbz_validated(
            workspace / "assembled.cbz", pages, plan["manga"], plan["book"]
        )
        return path, archive_info, content_sha256

    def _publish_and_retire(self, plan, packaged, confirmation):
        path, archive_info, content_sha256 = packaged
        # The file and its provenance are published through the ordinary import
        # path. This transaction commits before any source can enter the bin.
        with self.database.write_snapshot():
            current = self._plan(plan["manga"]["id"], plan["volume"])
            if not hmac.compare_digest(self._confirmation(current), confirmation):
                raise AssemblyConflict(
                    "Series or files changed. Preview and confirm again"
                )
            result = self.service._publish_external_import_locked(
                plan["manga"]["id"],
                plan["book"],
                path,
                archive_info["sha256"],
                content_sha256,
            )
        book = self.database.get_chapter(result["chapter"]["id"])
        if (
            not book.get("downloaded")
            or book.get("provider") != "assembled"
            or book.get("assembled_from") != plan["book"]["assembled_from"]
            or book.get("local_import_sha256") != content_sha256
        ):
            raise AssemblyConflict(
                "The imported book has no committed assembly proof; chapters were kept"
            )
        root = self.service._library_root()
        installed = self.service._recorded_library_path(str(book["library_path"]), root)
        if _file_snapshot(installed, root)["sha256"] != book["library_sha256"]:
            raise AssemblyConflict("The imported book changed; chapter files were kept")
        exact = chapters_by_volume(self.database.chapter_map(plan["manga"]["id"]))
        if sorted(exact.get(plan["volume"], ()), key=Decimal) != plan["required"]:
            raise AssemblyConflict("Book boundaries changed; chapter files were kept")
        for item in plan["selected"]:
            release = self.database.get_chapter(item["release"]["id"])
            if (
                any(
                    release.get(key) != item["release"].get(key)
                    for key in ("chapter", "language", "library_path", "downloaded")
                )
                or _file_snapshot(Path(item["file"]["path"]), root) != item["file"]
            ):
                raise AssemblyConflict("A chapter changed; source files were kept")
        retirement = self.service._delete_duplicate_chapter_files_locked(
            plan["manga"]["id"],
            only=set(plan["book"]["assembled_from"]["chapter_ids"]),
            recycle=True,
            volume=plan["volume"],
            expected_file_sha256={
                item["file"]["path"]: item["file"]["sha256"]
                for item in plan["selected"]
            },
        )
        return result, retirement

    async def assemble(
        self, manga_id: str, volume: str, confirmation_snapshot: str | None
    ) -> dict[str, Any]:
        async with self.service._mutation_lock:
            self.service.assert_mutations_allowed()

            def prepare():
                with self.database.read_snapshot():
                    plan = self._plan(manga_id, volume)
                if confirmation_snapshot is None or not hmac.compare_digest(
                    self._confirmation(plan), confirmation_snapshot
                ):
                    raise AssemblyConflict(
                        "Series or files changed. Preview and confirm again"
                    )
                return plan

            plan = await asyncio.to_thread(prepare)
            self.service.settings.staging_dir.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(
                dir=self.service.settings.staging_dir, prefix="assemble-"
            ) as temporary:
                packaged = await self._finish_thread(
                    self._package, plan, Path(temporary)
                )
                result, retirement = await self._finish_thread(
                    self._publish_and_retire, plan, packaged, confirmation_snapshot
                )
        result.pop("_already_imported", None)
        has_paths = retirement.pop("_has_paths", False)
        retirement["komga_scan"] = await self.service._request_komga_reconciliation(
            has_paths
            and not retirement.get("cleanup_errors")
            and not retirement.get("quarantine_files_remaining")
        )
        reader = await self.service._sync_imported_path_with_komga(Path(result["path"]))
        await self.service.notifier.chapter_imported(plan["manga"], result["chapter"])
        return {
            **self._response(plan),
            **result,
            "retirement": retirement,
            "reader": reader,
        }

    @staticmethod
    async def _finish_thread(function, *args):
        task = asyncio.create_task(asyncio.to_thread(function, *args))
        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                # Repeated cancellation must not release the mutation lock or
                # remove staging while the filesystem thread is still active.
                cancelled = True
        result = task.result()
        if cancelled:
            raise asyncio.CancelledError
        return result


def register_assemble_routes(
    app: FastAPI, database: Database, service: TankarrService
) -> BookAssembler:
    assembler = BookAssembler(database, service)

    @app.post("/api/manga/{manga_id}/volumes/{volume}/assemble")
    async def assemble_book(
        manga_id: str,
        volume: str,
        dry_run: bool = False,
        confirmation_snapshot: str | None = Query(
            default=None, min_length=64, max_length=64, pattern="^[0-9a-f]{64}$"
        ),
    ):
        try:
            if dry_run:
                return await asyncio.to_thread(assembler.preview, manga_id, volume)
            return await assembler.assemble(manga_id, volume, confirmation_snapshot)
        except KeyError as exc:
            raise HTTPException(404, "Series not found") from exc
        except AssemblyConflict as exc:
            raise HTTPException(409, exc.detail) from exc
        except (RecoveryBlocked, LibraryUnavailable) as exc:
            raise HTTPException(503, str(exc)) from exc
        except (
            UnsafeLibraryPath,
            ImportLimitError,
            OSError,
            zipfile.BadZipFile,
        ) as exc:
            raise HTTPException(
                409, {"message": str(exc), "missing_chapters": []}
            ) from exc

    return assembler
