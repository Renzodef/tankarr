"""Private, validated SQLite snapshots published only when complete."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import tempfile
import uuid
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from time import monotonic
from typing import Any

from tankarr.archive import fsync_directory, publish_without_overwrite, sha256
from tankarr.config import Settings
from tankarr.settings_store import SETTINGS_WRITE_LOCK

BUNDLE_FORMAT = "tankarr-application-v1"
BUNDLE_FILES = frozenset(
    {
        "tankarr.sqlite3",
        "effective-settings.json",
        "metadata.env",
        ".tankarr-library-id",
        "import-operation.json",
    }
)
MAX_BUNDLE_BYTES = 4 * 1024**3
RESTORE_DISABLED_SETTINGS = (
    "monitor_enabled",
    "metadata_enabled",
    "torrent_auto_import",
    "wanted_search_enabled",
    "duplicate_cleanup_enabled",
)


class DatabaseBackups:
    def __init__(self, source: Path, directory: Path, *, retain: int = 14):
        self.source = source
        self.directory = directory
        self.retain = max(1, retain)
        self._lock = Lock()

    def _entries(self) -> list[tuple[Path, os.stat_result]]:
        if not self.directory.is_dir():
            return []
        entries = []
        for path in self.directory.glob("tankarr-*.sqlite3"):
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            # Never follow or prune a link masquerading as a managed backup.
            if stat.S_ISREG(info.st_mode) and info.st_size:
                entries.append((path, info))
        return sorted(entries, key=lambda item: (item[1].st_mtime_ns, item[0].name))

    def list(self, limit: int = 10) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        return [
            {
                "name": path.name,
                "size": info.st_size,
                "created_at": datetime.fromtimestamp(info.st_mtime, tz=UTC).isoformat(),
            }
            for path, info in reversed(self._entries()[-limit:])
        ]

    def _copy(self, target: Path) -> None:
        deadline = monotonic() + 180

        def progress(_status: int, _remaining: int, _total: int) -> None:
            if monotonic() > deadline:
                raise TimeoutError("Database stayed busy for too long during backup")

        # Opening read-only also prevents a missing source being silently
        # replaced with an empty database and reported as a successful backup.
        source = sqlite3.connect(f"{self.source.resolve().as_uri()}?mode=ro", uri=True)
        try:
            destination = sqlite3.connect(target)
            try:
                source.backup(destination, pages=1024, progress=progress, sleep=0.05)
                if destination.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                    raise sqlite3.DatabaseError("Backup integrity check failed")
            finally:
                destination.close()
        finally:
            source.close()

    def create(self) -> dict[str, Any]:
        # Serializing the whole copy/publish/retention sequence prevents two
        # requests from exhausting disk together or pruning each other's work.
        with self._lock:
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(
                dir=self.directory,
                prefix=f".tankarr-{datetime.now(UTC):%Y%m%d-%H%M%S-%f}-",
                suffix=".partial",
            )
            os.close(descriptor)
            temporary = Path(temporary_name)
            target = self.directory / f"{temporary.stem.removeprefix('.')}.sqlite3"
            try:
                self._copy(temporary)
                # mkstemp's 0600 mode survives SQLite writes and publication.
                with temporary.open("rb") as handle:
                    os.fsync(handle.fileno())
                temporary.replace(target)
                if os.name == "posix":
                    directory_fd = os.open(self.directory, os.O_RDONLY)
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
                # Never prune a previous backup until the new one is valid
                # and durably published. Hidden/incomplete files are ignored.
                previous = [entry for entry in self._entries() if entry[0] != target]
                # The new snapshot must survive even if the system clock went
                # backwards or filesystem timestamps have coarse resolution.
                obsolete_count = max(0, len(previous) - (self.retain - 1))
                for stale, _info in previous[:obsolete_count]:
                    stale.unlink(missing_ok=True)
                return {
                    "name": target.name,
                    "size": target.stat().st_size,
                    "verified": True,
                    "backups": self.list(),
                }
            finally:
                temporary.unlink(missing_ok=True)


class ApplicationBackups:
    """Versioned control-plane backup; media and the source engine are separate."""

    def __init__(
        self,
        settings: Settings,
        directory: Path | None = None,
        *,
        retain: int | None = None,
    ):
        self.settings = settings
        self._directory_override = directory
        self._retain_override = retain
        self._lock = Lock()

    @property
    def retain(self) -> int:
        return max(
            1,
            self._retain_override
            if self._retain_override is not None
            else self.settings.backup_retention_count,
        )

    @property
    def directory(self) -> Path:
        return (
            self._directory_override
            or self.settings.backup_directory
            or self.settings.data_dir / "backups"
        )

    def _entries(self):
        if not self.directory.is_dir():
            return []
        return sorted(
            (
                path
                for path in self.directory.glob("tankarr-*.zip")
                if not path.is_symlink() and path.is_file()
            ),
            key=lambda path: (path.stat().st_mtime_ns, path.name),
        )

    def list(self, limit: int = 10) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        return [
            {
                "name": path.name,
                "size": path.stat().st_size,
                "created_at": datetime.fromtimestamp(
                    path.stat().st_mtime, tz=UTC
                ).isoformat(),
                "format": BUNDLE_FORMAT,
            }
            for path in reversed(self._entries()[-limit:])
        ]

    def export_path(self, name: str) -> Path:
        if (
            Path(name).name != name
            or not name.startswith("tankarr-")
            or not name.endswith(".zip")
        ):
            raise ValueError("Invalid backup name")
        path = self.directory / name
        if path.is_symlink() or not path.is_file():
            raise FileNotFoundError("Backup not found")
        return path

    def verify(self, name: str) -> dict[str, Any]:
        return verify_bundle(self.export_path(name))

    def create(self) -> dict[str, Any]:
        with self._lock, SETTINGS_WRITE_LOCK:
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(
                dir=self.directory, prefix=".tankarr-bundle-"
            ) as temporary:
                work = Path(temporary)
                snapshot = work / "tankarr.sqlite3"
                descriptor = os.open(
                    snapshot, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
                )
                os.close(descriptor)
                DatabaseBackups(self.settings.database_path, self.directory)._copy(
                    snapshot
                )
                files: dict[str, Path] = {"tankarr.sqlite3": snapshot}
                effective = work / "effective-settings.json"
                effective.write_text(self.settings.model_dump_json(), encoding="utf-8")
                effective.chmod(0o600)
                files[effective.name] = effective
                for name in sorted(
                    BUNDLE_FILES - {"tankarr.sqlite3", "effective-settings.json"}
                ):
                    source = self.settings.data_dir / name
                    if source.is_symlink():
                        raise ValueError(
                            f"Refusing symlinked configuration file: {name}"
                        )
                    if source.exists():
                        if not source.is_file() or source.stat().st_size > 32 * 1024**2:
                            raise ValueError(
                                f"Invalid or oversized configuration file: {name}"
                            )
                        copied = work / name
                        shutil.copyfile(source, copied)
                        copied.chmod(0o600)
                        files[name] = copied
                total = sum(path.stat().st_size for path in files.values())
                if (
                    total > MAX_BUNDLE_BYTES
                    or shutil.disk_usage(work).free < total + 64 * 1024**2
                ):
                    raise OSError(
                        "Insufficient space or application backup exceeds 4 GiB"
                    )
                manifest = {
                    "format": BUNDLE_FORMAT,
                    "created_at": datetime.now(UTC).isoformat(),
                    "files": {
                        name: {"size": path.stat().st_size, "sha256": sha256(path)}
                        for name, path in files.items()
                    },
                    "contains_secrets": True,
                    "excluded": [
                        "Library media and covers",
                        "Suwayomi engine database/JAR/extensions",
                        "Upload and staging payloads",
                    ],
                    "restore_requires": [
                        "Matching library identity/mount",
                        "Separate Suwayomi backup or source reconfiguration",
                        "Review interrupted imports with missing staging payloads",
                    ],
                }
                bundle = work / "bundle.partial"
                with bundle.open("xb") as handle:
                    os.chmod(bundle, 0o600)
                    with zipfile.ZipFile(
                        handle, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1
                    ) as archive:
                        archive.writestr("manifest.json", json.dumps(manifest))
                        for name, path in files.items():
                            archive.write(path, arcname=name)
                    handle.flush()
                    os.fsync(handle.fileno())
                verify_bundle(bundle)
                target = (
                    self.directory
                    / f"tankarr-{datetime.now(UTC):%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:12]}.zip"
                )
                publish_without_overwrite(bundle, target)
                fsync_directory(self.directory)
                previous = [path for path in self._entries() if path != target]
                for obsolete in previous[: max(0, len(previous) - self.retain + 1)]:
                    obsolete.unlink()
                return {
                    "name": target.name,
                    "size": target.stat().st_size,
                    "verified": True,
                    "format": BUNDLE_FORMAT,
                    "contains_secrets": True,
                    "excluded": manifest["excluded"],
                    "backups": self.list(),
                }


def create_pre_migration_backup(settings: Settings) -> dict[str, Any] | None:
    """Protect an existing database before a caller changes its schema.

    The caller must detect pending migrations and invoke this before opening a
    write transaction. Failure intentionally propagates: migration may proceed
    only after the snapshot has been verified and durably published.
    """
    source = settings.database_path
    if source.is_symlink():
        raise ValueError("Refusing a symlinked database before schema migration")
    if not source.exists() or source.stat().st_size == 0:
        return None
    effective = settings.model_copy(deep=True)
    # initialize() has not run. Read the old settings table without initializing
    # it or deleting obsolete/invalid overrides as normal startup loading does.
    from tankarr.settings_store import EDITABLE_SETTINGS, coerce_setting

    with sqlite3.connect(
        f"{source.resolve().as_uri()}?mode=ro", uri=True
    ) as connection:
        if connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='setting'"
        ).fetchone():
            for name, raw in connection.execute("SELECT key, value FROM setting"):
                if name in EDITABLE_SETTINGS:
                    try:
                        setattr(effective, name, coerce_setting(name, raw))
                    except ValueError:
                        # Match normal loading's effective fallback, without
                        # mutating the database we are about to protect.
                        continue
    return ApplicationBackups(effective).create()


def verify_bundle(path: Path) -> dict[str, Any]:
    if path.is_symlink():
        raise ValueError("Backup must not be a symlink")
    with zipfile.ZipFile(path) as archive:
        entries = archive.infolist()
        names = [item.filename for item in entries]
        if len(names) != len(set(names)) or set(names) - BUNDLE_FILES - {
            "manifest.json"
        }:
            raise ValueError("Backup contains duplicate or unexpected paths")
        if (
            "manifest.json" not in names
            or archive.getinfo("manifest.json").file_size > 64 * 1024
        ):
            raise ValueError("Invalid backup manifest")
        if sum(item.file_size for item in entries) > MAX_BUNDLE_BYTES:
            raise ValueError("Expanded backup exceeds 4 GiB")
        manifest = json.loads(archive.read("manifest.json"))
        if not isinstance(manifest, dict):
            raise ValueError("Invalid backup manifest")
        files = manifest.get("files")
        if manifest.get("format") != BUNDLE_FORMAT or not isinstance(files, dict):
            raise ValueError("Unsupported backup format")
        if set(files) != set(names) - {"manifest.json"} or not {
            "tankarr.sqlite3",
            "effective-settings.json",
        } <= set(files):
            raise ValueError("Backup is missing required data")
        scratch = Path("/var/tmp") if Path("/var/tmp").is_dir() else None
        if (
            shutil.disk_usage(scratch or tempfile.gettempdir()).free
            < archive.getinfo("tankarr.sqlite3").file_size + 64 * 1024**2
        ):
            raise OSError("Insufficient scratch space to verify backup database")
        with tempfile.TemporaryDirectory(
            prefix="tankarr-verify-", dir=scratch
        ) as temporary:
            database_path = Path(temporary) / "snapshot.sqlite3"
            for name, expected in files.items():
                if not isinstance(expected, dict) or archive.getinfo(
                    name
                ).file_size != expected.get("size"):
                    raise ValueError("Backup member size differs from manifest")
                digest = hashlib.sha256()
                with archive.open(name) as source:
                    if name == "tankarr.sqlite3":
                        with database_path.open("xb") as target:
                            while chunk := source.read(1024 * 1024):
                                digest.update(chunk)
                                target.write(chunk)
                        database_path.chmod(0o600)
                    else:
                        while chunk := source.read(1024 * 1024):
                            digest.update(chunk)
                if digest.hexdigest() != expected.get("sha256"):
                    raise ValueError(f"Backup checksum mismatch: {name}")
            connection = sqlite3.connect(f"{database_path.as_uri()}?mode=ro", uri=True)
            try:
                if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                    raise ValueError("Backup database integrity check failed")
            finally:
                connection.close()
        return {
            "format": BUNDLE_FORMAT,
            "verified": True,
            "created_at": manifest.get("created_at"),
            "files": sorted(files),
            "contains_secrets": True,
            "excluded": manifest.get("excluded", []),
        }


def restore_bundle(bundle: Path, destination: Path) -> dict[str, Any]:
    if bundle.is_symlink():
        raise ValueError("Backup must not be a symlink")
    bundle = bundle.resolve(strict=True)
    destination = destination.absolute()
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(
            "Restore destination must not exist; active installations are never overwritten"
        )
    original_digest = sha256(bundle)
    verified = verify_bundle(bundle)
    # Revalidate staged bytes and the container before publication; a bundle
    # replaced or changed in place during restore must not be trusted.
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        dir=destination.parent, prefix=".tankarr-restore-"
    ) as temporary:
        work = Path(temporary) / "data"
        work.mkdir(mode=0o700)
        with zipfile.ZipFile(bundle) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            total = 0
            for name in verified["files"]:
                expected = manifest["files"][name]
                target = work / (
                    "restored-import-operation.json"
                    if name == "import-operation.json"
                    else name
                )
                descriptor = os.open(
                    target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
                )
                with (
                    os.fdopen(descriptor, "wb") as handle,
                    archive.open(name) as source,
                ):
                    digest = hashlib.sha256()
                    size = 0
                    while chunk := source.read(1024 * 1024):
                        total += len(chunk)
                        size += len(chunk)
                        if total > MAX_BUNDLE_BYTES:
                            raise ValueError("Expanded backup exceeds 4 GiB")
                        digest.update(chunk)
                        handle.write(chunk)
                    if (
                        size != expected["size"]
                        or digest.hexdigest() != expected["sha256"]
                    ):
                        raise ValueError(f"Backup changed during restore: {name}")
                    handle.flush()
                    os.fsync(handle.fileno())
        with sqlite3.connect(work / "tankarr.sqlite3") as connection:
            if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                raise ValueError("Restored database integrity check failed")
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if "setting" in tables:
                connection.executemany(
                    "INSERT INTO setting (key,value,updated_at) VALUES (?, 'false', ?) ON CONFLICT(key) DO UPDATE SET value='false', updated_at=excluded.updated_at",
                    [
                        (key, datetime.now(UTC).isoformat())
                        for key in RESTORE_DISABLED_SETTINGS
                    ],
                )
            diagnostic = (
                "Restored backup: verify storage and sources before explicitly retrying"
            )
            if "download_job" in tables:
                connection.execute(
                    "UPDATE download_job SET status='failed', message=? WHERE status IN ('queued','running','downloading','packaging','importing')",
                    (diagnostic,),
                )
            if "torrent_download" in tables:
                connection.execute(
                    "UPDATE torrent_download SET status='review', message=? WHERE status NOT IN ('completed','failed','review')",
                    (diagnostic,),
                )
        effective = json.loads((work / "effective-settings.json").read_bytes())
        effective.update(
            {
                "data_dir": str(destination),
                "host": "127.0.0.1",
                "restored_safe_mode": True,
                **{key: False for key in RESTORE_DISABLED_SETTINGS},
            }
        )
        # JSON preserves literal secret characters; dotenv interpolation must
        # never silently change a restored password containing ${...}.
        Settings.model_validate(effective)
        restored = work / "restored-settings.json"
        restored.write_text(json.dumps(effective), encoding="utf-8")
        restored.chmod(0o600)
        with restored.open("rb") as handle:
            os.fsync(handle.fileno())
        if sha256(bundle) != original_digest:
            raise ValueError("Backup changed during restore")
        with (work / "tankarr.sqlite3").open("rb") as handle:
            os.fsync(handle.fileno())
        fsync_directory(work)
        publish_without_overwrite(work, destination)
        fsync_directory(destination.parent)
    return {
        **verified,
        "destination": str(destination),
        "started": False,
        "next_step": "Set TANKARR_DATA_DIR to the restored directory; verify mounts and source engine before enabling automation",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Verify or restore a Tankarr application backup; never overwrite a live installation"
    )
    parser.add_argument("--verify", type=Path)
    parser.add_argument("--restore", type=Path)
    parser.add_argument("--destination", type=Path)
    arguments = parser.parse_args()
    if arguments.restore and arguments.destination:
        print(
            json.dumps(
                restore_bundle(arguments.restore, arguments.destination), indent=2
            )
        )
    elif arguments.verify:
        print(json.dumps(verify_bundle(arguments.verify), indent=2))
    else:
        parser.error(
            "Use --verify BUNDLE or --restore BUNDLE --destination NEW_DIRECTORY"
        )
