"""Recoverable, expiring retirement of original download payloads.

The caller first detaches the download client without deleting files and
checks that no other live job owns this payload. All filesystem operations
below stay on the configured filesystem and use directory descriptors.
"""

from __future__ import annotations

import ctypes
import errno
import fcntl
import hashlib
import json
import os
import re
import stat
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from tankarr.archive import _linux_renameat2
from tankarr.config import Settings

BIN_NAME = ".tankarr-download-recycle"
MARKER_NAME = "owner.json"
FORMAT = "tankarr-download-recycle-v1"
MAX_JOURNAL_BYTES = 64 * 1024
DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


class DownloadRecycleConflict(ValueError):
    """The payload or its receipt cannot be proven safe for this operation."""


def _absolute(path: Path) -> Path:
    if ".." in path.parts:
        raise DownloadRecycleConflict("Parent traversal is not a download path")
    return Path(os.path.abspath(path))


@contextmanager
def _directory(path: Path):
    """Open a directory without following any path-component symlink."""
    descriptor = os.open(path.anchor, DIRECTORY_FLAGS)
    try:
        for component in path.parts[1:]:
            following = os.open(component, DIRECTORY_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = following
        yield descriptor
    finally:
        os.close(descriptor)


def _read_json(directory: int, name: str) -> dict[str, Any]:
    descriptor = os.open(
        name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
    )
    with os.fdopen(descriptor, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_JOURNAL_BYTES:
            raise DownloadRecycleConflict("Invalid download recycle journal file")
        value = json.loads(handle.read(MAX_JOURNAL_BYTES + 1))
    if not isinstance(value, dict):
        raise DownloadRecycleConflict("Invalid download recycle journal object")
    return value


def _write_json(directory: int, name: str, value: dict[str, Any]) -> None:
    temporary = f".journal-{uuid.uuid4().hex}.tmp"
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
        dir_fd=directory,
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(json.dumps(value, sort_keys=True).encode())
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    finally:
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass


@contextmanager
def _recycle_directory(root: Path, *, create: bool):
    with _directory(root) as root_fd:
        if create:
            try:
                os.mkdir(BIN_NAME, mode=0o700, dir_fd=root_fd)
                os.fsync(root_fd)
            except FileExistsError:
                pass
        recycle_fd = os.open(BIN_NAME, DIRECTORY_FLAGS, dir_fd=root_fd)
        try:
            # A directory lock serializes threads and independent processes,
            # including journal publication and interrupted purge recovery.
            fcntl.flock(recycle_fd, fcntl.LOCK_EX)
            root_info = os.fstat(root_fd)
            if os.fstat(recycle_fd).st_dev != root_info.st_dev:
                raise DownloadRecycleConflict(
                    "Download recycle bin is on another filesystem"
                )
            marker = {
                "format": FORMAT,
                "root_device": root_info.st_dev,
                "root_inode": root_info.st_ino,
            }
            try:
                recorded = _read_json(recycle_fd, MARKER_NAME)
            except FileNotFoundError:
                if not create or os.listdir(recycle_fd):
                    raise DownloadRecycleConflict(
                        "Download recycle bin has no ownership marker"
                    ) from None
                _write_json(recycle_fd, MARKER_NAME, marker)
            else:
                if recorded != marker:
                    raise DownloadRecycleConflict(
                        "Download recycle ownership marker differs from its root"
                    )
            yield root_fd, recycle_fd
        finally:
            os.close(recycle_fd)


def _relative(value: object) -> Path:
    if not isinstance(value, str) or not value:
        raise DownloadRecycleConflict("Download receipt has no original path")
    relative = Path(value)
    if (
        relative.is_absolute()
        or not relative.parts
        or ".." in relative.parts
        or BIN_NAME in relative.parts
    ):
        raise DownloadRecycleConflict(
            "Download payload must be a child of its configured root"
        )
    return relative


@contextmanager
def _source_parent(root_fd: int, relative: Path):
    descriptor = os.dup(root_fd)
    try:
        for component in relative.parts[:-1]:
            try:
                following = os.open(component, DIRECTORY_FLAGS, dir_fd=descriptor)
            except FileNotFoundError:
                yield None
                return
            os.close(descriptor)
            descriptor = following
        yield descriptor
    finally:
        os.close(descriptor)


def _info(directory: int, name: str) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _fingerprint(info: os.stat_result) -> dict[str, int]:
    kind = stat.S_IFMT(info.st_mode)
    if kind not in (stat.S_IFREG, stat.S_IFDIR):
        raise DownloadRecycleConflict(
            "Download payload cannot be a symlink or special file"
        )
    return {
        "device": info.st_dev,
        "inode": info.st_ino,
        "kind": kind,
        "size": info.st_size,
        "modified_ns": info.st_mtime_ns,
    }


def _matches(info: os.stat_result, expected: dict[str, int], *, purging=False) -> bool:
    current = _fingerprint(info)
    keys = ("device", "inode", "kind") if purging else expected.keys()
    return all(current[key] == expected[key] for key in keys)


def _count_files(directory: int, device: int) -> int:
    count = 0
    for name in os.listdir(directory):
        info = os.stat(name, dir_fd=directory, follow_symlinks=False)
        if info.st_dev != device:
            raise DownloadRecycleConflict(
                "Download payload contains another filesystem"
            )
        if stat.S_ISDIR(info.st_mode):
            child = os.open(name, DIRECTORY_FLAGS, dir_fd=directory)
            try:
                count += _count_files(child, device)
            finally:
                os.close(child)
        elif stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
            count += 1
        else:
            raise DownloadRecycleConflict("Download payload contains a special file")
    return count


def _operation_id(job_id: int, relative: Path, attempt_key: str | None = None) -> str:
    if attempt_key is not None and (
        not isinstance(attempt_key, str)
        or re.fullmatch(r"[a-f0-9]{32}", attempt_key) is None
    ):
        raise DownloadRecycleConflict("Invalid download retirement attempt key")
    identity = f"{job_id}\0{relative.as_posix()}"
    if attempt_key is not None:
        identity += f"\0{attempt_key}"
    return hashlib.sha256(identity.encode()).hexdigest()


def _rename_payload(parent_fd: int, source: str, recycle_fd: int, target: str) -> None:
    rename = _linux_renameat2()
    if rename is None:
        raise OSError(
            errno.ENOTSUP, "Download recycling requires atomic no-overwrite rename"
        )
    if rename(parent_fd, os.fsencode(source), recycle_fd, os.fsencode(target), 1) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), target)


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise DownloadRecycleConflict("Download receipt has no retirement timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise DownloadRecycleConflict("Invalid download retirement timestamp") from exc
    if parsed.utcoffset() is None:
        raise DownloadRecycleConflict("Download retirement timestamp has no timezone")
    return parsed.astimezone(UTC)


def _validate_receipt(value: dict[str, Any], identifier: str) -> dict[str, Any]:
    relative = _relative(value.get("original"))
    job_id = value.get("job_id")
    fingerprint = value.get("fingerprint")
    if (
        value.get("format") != FORMAT
        or value.get("operation_id") != identifier
        or type(job_id) is not int
        or job_id < 1
        or _operation_id(job_id, relative, value.get("attempt_key")) != identifier
        or not isinstance(value.get("state"), str)
        or value.get("state") not in {"prepared", "committed", "purging", "purged"}
        or not isinstance(fingerprint, dict)
        or set(fingerprint) != {"device", "inode", "kind", "size", "modified_ns"}
        or any(type(item) is not int or item < 0 for item in fingerprint.values())
        or fingerprint.get("kind") not in {stat.S_IFREG, stat.S_IFDIR}
        or type(value.get("files")) is not int
        or value["files"] < 0
    ):
        raise DownloadRecycleConflict("Invalid download recycle receipt")
    _timestamp(value.get("prepared_at"))
    if value["state"] != "prepared":
        _timestamp(value.get("retired_at"))
    return value


def _commit_receipt(directory: int, receipt: dict[str, Any], now: datetime) -> None:
    receipt.update(state="committed", retired_at=now.isoformat())
    _write_json(directory, f"{receipt['operation_id']}.json", receipt)


def recycle_payload(
    root: Path, content: Path, job_id: int, *, attempt_key: str | None = None
) -> dict[str, Any]:
    """Retire one detached, exclusively owned download, or resume its receipt.

    A retry never captures a newly created payload at the original pathname.
    Purged receipts remain as small tombstones so this remains true after expiry.
    A new, explicitly authorized attempt gets a fresh key; retries reuse it.
    """
    if type(job_id) is not int or job_id < 1:
        raise ValueError("Download recycling requires a positive job ID")
    root = _absolute(Path(root))
    content = _absolute(Path(content))
    try:
        relative = _relative(content.relative_to(root).as_posix())
    except ValueError as exc:
        raise DownloadRecycleConflict(
            "Download payload is outside its configured root"
        ) from exc
    identifier = _operation_id(job_id, relative, attempt_key)
    journal = f"{identifier}.json"
    payload = f"{identifier}.payload"
    with _recycle_directory(root, create=True) as (root_fd, recycle_fd):
        try:
            receipt = _validate_receipt(_read_json(recycle_fd, journal), identifier)
        except FileNotFoundError:
            receipt = None
        with _source_parent(root_fd, relative) as parent_fd:
            source = _info(parent_fd, relative.name) if parent_fd is not None else None
            if source is not None:
                current = _fingerprint(source)
            else:
                current = None
            retired = _info(recycle_fd, payload)
            already_retired = receipt is not None and (
                retired is not None or receipt["state"] != "prepared"
            )
            if receipt is None:
                if source is None:
                    raise FileNotFoundError(content)
                if retired is not None:
                    raise DownloadRecycleConflict(
                        "Download recycle payload has no receipt"
                    )
                if source.st_dev != os.fstat(root_fd).st_dev:
                    raise DownloadRecycleConflict(
                        "Download payload is on another filesystem"
                    )
                if stat.S_ISREG(source.st_mode) and source.st_nlink != 1:
                    raise DownloadRecycleConflict(
                        "Download payload is shared through hard links"
                    )
                files = 1
                if stat.S_ISDIR(source.st_mode):
                    descriptor = os.open(
                        relative.name, DIRECTORY_FLAGS, dir_fd=parent_fd
                    )
                    try:
                        files = _count_files(descriptor, source.st_dev)
                    finally:
                        os.close(descriptor)
                receipt = {
                    "format": FORMAT,
                    "operation_id": identifier,
                    "job_id": job_id,
                    "attempt_key": attempt_key,
                    "original": relative.as_posix(),
                    "fingerprint": current,
                    "files": files,
                    "state": "prepared",
                    "prepared_at": datetime.now(UTC).isoformat(),
                    "retired_at": None,
                }
                _write_json(recycle_fd, journal, receipt)
            if retired is not None:
                if receipt["state"] == "purged" or not _matches(
                    retired,
                    receipt["fingerprint"],
                    purging=receipt["state"] == "purging",
                ):
                    raise DownloadRecycleConflict(
                        "Retired download differs from its receipt"
                    )
            elif receipt["state"] == "prepared":
                if source is None or not _matches(source, receipt["fingerprint"]):
                    raise DownloadRecycleConflict(
                        "Original download changed before retirement"
                    )
                # Recheck immediately before the atomic rename; a failed copy
                # across filesystems is never replaced with recursive deletion.
                latest = _info(parent_fd, relative.name)
                if latest is None or not _matches(latest, receipt["fingerprint"]):
                    raise DownloadRecycleConflict(
                        "Original download changed before retirement"
                    )
                _rename_payload(parent_fd, relative.name, recycle_fd, payload)
                retired = _info(recycle_fd, payload)
                if retired is None or not _matches(retired, receipt["fingerprint"]):
                    raise DownloadRecycleConflict(
                        "Moved download differs from its prepared receipt"
                    )
                current = None
            if receipt["state"] == "prepared":
                # Repeat both durability barriers on recovery: the previous
                # process may have stopped immediately after the rename.
                if parent_fd is not None:
                    os.fsync(parent_fd)
                os.fsync(recycle_fd)
                _commit_receipt(recycle_fd, receipt, datetime.now(UTC))
            return {
                "path": str(root / BIN_NAME / payload) if retired is not None else None,
                "operation_id": identifier,
                "files_retired": receipt["files"],
                "already_retired": already_retired,
                "source_recreated": current is not None,
                "purged": receipt["state"] == "purged",
            }


def _remove_tree(directory: int, device: int, result: dict[str, Any]) -> None:
    """Unlink payload entries without traversing symlinks or mounted trees."""
    for name in os.listdir(directory):
        info = os.stat(name, dir_fd=directory, follow_symlinks=False)
        if info.st_dev != device:
            raise DownloadRecycleConflict(
                "Retired download contains another filesystem"
            )
        if stat.S_ISDIR(info.st_mode):
            child = os.open(name, DIRECTORY_FLAGS, dir_fd=directory)
            try:
                _remove_tree(child, device, result)
            finally:
                os.close(child)
            os.rmdir(name, dir_fd=directory)
        elif stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
            os.unlink(name, dir_fd=directory)
            result["files_deleted"] += 1
        else:
            raise DownloadRecycleConflict("Retired download contains a special file")
    os.fsync(directory)


def _purge_root(root: Path, days: int, now: datetime, result: dict[str, Any]) -> None:
    with _recycle_directory(root, create=False) as (root_fd, recycle_fd):
        for journal in sorted(os.listdir(recycle_fd)):
            if not re.fullmatch(r"[a-f0-9]{64}\.json", journal):
                continue
            identifier = journal[:-5]
            try:
                receipt = _validate_receipt(_read_json(recycle_fd, journal), identifier)
                payload = f"{identifier}.payload"
                info = _info(recycle_fd, payload)
                if receipt["state"] == "purged":
                    if info is not None:
                        raise DownloadRecycleConflict(
                            "A purged download payload reappeared"
                        )
                    continue
                if info is not None and not _matches(
                    info, receipt["fingerprint"], purging=receipt["state"] == "purging"
                ):
                    raise DownloadRecycleConflict(
                        "Retired download differs from its receipt"
                    )
                if receipt["state"] == "prepared":
                    if info is None:
                        # Recovery never moves a still-live download source.
                        continue
                    with _source_parent(
                        root_fd, _relative(receipt["original"])
                    ) as parent_fd:
                        if parent_fd is not None:
                            os.fsync(parent_fd)
                    os.fsync(root_fd)
                    os.fsync(recycle_fd)
                    _commit_receipt(recycle_fd, receipt, now)
                retired_at = _timestamp(receipt["retired_at"])
                if now - retired_at < timedelta(days=days):
                    continue
                receipt["state"] = "purging"
                _write_json(recycle_fd, journal, receipt)
                if info is not None:
                    if stat.S_ISDIR(info.st_mode):
                        descriptor = os.open(
                            payload, DIRECTORY_FLAGS, dir_fd=recycle_fd
                        )
                        try:
                            if not _matches(
                                os.fstat(descriptor),
                                receipt["fingerprint"],
                                purging=True,
                            ):
                                raise DownloadRecycleConflict(
                                    "Retired directory changed before purge"
                                )
                            _remove_tree(descriptor, info.st_dev, result)
                        finally:
                            os.close(descriptor)
                        os.rmdir(payload, dir_fd=recycle_fd)
                    else:
                        os.unlink(payload, dir_fd=recycle_fd)
                        result["files_deleted"] += 1
                    os.fsync(recycle_fd)
                receipt.update(state="purged", purged_at=now.isoformat())
                _write_json(recycle_fd, journal, receipt)
                result["purged"] += 1
            except (OSError, ValueError) as exc:
                result["errors"].append(
                    f"{root / BIN_NAME / journal}: {type(exc).__name__}: {exc}"
                )


def purge_download_recycle(
    settings: Settings, *, now: datetime | None = None
) -> dict[str, Any]:
    """Recover interrupted renames and expire committed download payloads."""
    instant = now or datetime.now(UTC)
    if instant.utcoffset() is None:
        raise ValueError("Download recycle purge time must have a timezone")
    result: dict[str, Any] = {"errors": [], "purged": 0, "files_deleted": 0}
    roots = {
        Path(value)
        for value in (
            settings.torrent_download_dir,
            settings.usenet_download_dir,
            settings.data_dir / "direct-downloads",
        )
        if value is not None
    }
    for configured in sorted(roots):
        try:
            root = _absolute(configured)
            _purge_root(
                root,
                settings.recycle_bin_retention_days,
                instant.astimezone(UTC),
                result,
            )
        except FileNotFoundError:
            continue
        except (OSError, ValueError) as exc:
            result["errors"].append(f"{configured}: {type(exc).__name__}: {exc}")
    return result
