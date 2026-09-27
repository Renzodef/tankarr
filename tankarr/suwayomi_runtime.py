"""Managed Suwayomi runtime: the source engine lives inside Tankarr.

Suwayomi-Server is a headless Kotlin service that runs Mihon/Tachiyomi-compatible
extensions from the repository the operator configures. Tankarr does not reimplement those sources; it embeds
the official server JAR, supervises the Java process, and talks to it over
GraphQL on the container's loopback interface. Without a Tankarr login the
server listens on loopback only, with authentication off; once a login exists
it also serves its own web UI inside the container, behind HTTP Basic
authentication with that login. Its credentials live in its ``server.conf``
(owner-only), never on the command line, and the JVM does not inherit
Tankarr's environment.

Responsibilities:

* **install** — download the official release JAR from GitHub, verify it
  against the published SHA-256 checksums, swap it into place atomically and
  keep the previous JAR for rollback;
* **supervise** — start the JVM with a bounded heap, restart it with backoff
  when it dies, stop it cleanly on shutdown;
* **manage sources** — list/install/uninstall extensions and probe the
  enabled sources for coverage and latency, so Settings → Sources can show
  what actually works from where Tankarr runs.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import time
import uuid
from collections.abc import AsyncIterator, Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from tankarr.archive import fsync_directory, publish_without_overwrite

logger = logging.getLogger(__name__)

RELEASES_API = "https://api.github.com/repos/Suwayomi/Suwayomi-Server/releases/latest"
RELEASE_PAGE = "https://github.com/Suwayomi/Suwayomi-Server/releases"
CHECKSUM_ASSET = "Checksums.sha256"
DEFAULT_PORT = 4567
READY_TIMEOUT_SECONDS = 180.0
STOP_TIMEOUT_SECONDS = 25.0
MAX_LOG_BYTES = 4 * 1024 * 1024
MAX_CONSECUTIVE_CRASHES = 6
RESTART_BACKOFF_CAP_SECONDS = 120.0

# Titles that every serious catalogue for a language should resolve. They
# are used only to measure whether a source answers, and how fast.
PROBE_TITLES: dict[str, tuple[str, ...]] = {
    "en": ("One Piece", "Berserk", "Solo Leveling"),
    "it": ("One Piece", "Berserk", "L'attacco dei giganti"),
    "es": ("One Piece", "Berserk", "Solo Leveling"),
    "fr": ("One Piece", "Berserk", "Solo Leveling"),
    "pt-br": ("One Piece", "Berserk", "Solo Leveling"),
    "de": ("One Piece", "Berserk", "Solo Leveling"),
}
PROBE_TITLES_DEFAULT = ("One Piece", "Berserk", "Naruto")
# Errors that mean "the site put up an anti-bot challenge". Nothing here can
# solve one: the sources are reported and ranked last.
CHALLENGE_PATTERN = re.compile(
    r"cloudflare|ddos-guard|just a moment|challenge|\b403\b|\b503\b", re.I
)

ABOUT_QUERY = "query TankarrAbout { aboutServer { version revision } }"

FETCH_EXTENSIONS = """
mutation TankarrRefreshExtensions($input: FetchExtensionsInput!) {
  fetchExtensions(input: $input) { extensions { pkgName } }
}
"""

LIST_EXTENSIONS = """
query TankarrExtensions {
  extensions(first: 5000) {
    nodes {
      pkgName name lang versionName contentWarning storeIndexUrl
      isInstalled hasUpdate isObsolete iconUrl
    }
  }
}
"""

UPDATE_EXTENSION = """
mutation TankarrUpdateExtension($input: UpdateExtensionInput!) {
  updateExtension(input: $input) { extension { pkgName isInstalled hasUpdate } }
}
"""

LIST_SOURCES = """
query TankarrSources {
  sources(first: 2000) {
    nodes { id name displayName lang contentWarning homeUrl extension { pkgName isObsolete } }
  }
}
"""

SEARCH_SOURCE = """
mutation TankarrProbe($input: FetchSourceMangaInput!) {
  fetchSourceManga(input: $input) { mangas { id title } }
}
"""


class SuwayomiRuntimeError(RuntimeError):
    """The managed Suwayomi runtime could not do what was asked."""


class SuwayomiRuntimeBusy(SuwayomiRuntimeError):
    """An install or restart is already in progress."""


@dataclass(frozen=True)
class ReleaseAssets:
    tag: str
    jar_name: str
    jar_url: str
    jar_size: int
    checksums_url: str
    published_at: str | None


def parse_checksums(text: str) -> dict[str, str]:
    """Parse ``sha256sum``-style lines into ``{filename: digest}``."""

    result: dict[str, str] = {}
    for line in text.splitlines():
        parts = line.strip().split()
        if len(parts) < 2:
            continue
        digest, name = parts[0].casefold(), parts[-1].lstrip("*")
        if len(digest) == 64 and all(c in "0123456789abcdef" for c in digest):
            result[name] = digest
    return result


def select_release_assets(release: dict[str, Any]) -> ReleaseAssets:
    """Pick the server JAR and checksum file out of a GitHub release payload."""

    tag = str(release.get("tag_name") or "").strip()
    if not tag:
        raise SuwayomiRuntimeError("GitHub release has no tag")
    jar = None
    checksums = None
    for asset in release.get("assets") or []:
        name = str(asset.get("name") or "")
        if name == CHECKSUM_ASSET:
            checksums = asset
        elif name.startswith("Suwayomi-Server-") and name.endswith(".jar"):
            jar = asset
    if jar is None or checksums is None:
        raise SuwayomiRuntimeError(
            f"Release {tag} does not publish a server JAR with checksums"
        )
    return ReleaseAssets(
        tag=tag,
        jar_name=str(jar["name"]),
        jar_url=str(jar["browser_download_url"]),
        jar_size=int(jar.get("size") or 0),
        checksums_url=str(checksums["browser_download_url"]),
        published_at=str(release.get("published_at") or "") or None,
    )


def jvm_arguments(
    *,
    jar: Path,
    home: Path,
    port: int,
    heap_mb: int,
    extension_stores: Sequence[str] = (),
    max_sources_in_parallel: int = 4,
    credentials: tuple[str, str] | None = None,
) -> list[str]:
    """Command line for a headless Suwayomi server.

    Without credentials the server binds to loopback with no WebUI: only
    Tankarr can reach it. With credentials (Tankarr's own login) it binds to
    every interface behind HTTP Basic auth and serves the bundled WebUI, so
    Settings can link straight to it on the same trusted LAN as Tankarr.

    Every option is passed as a system property so the server's own
    ``server.conf`` never needs editing; the file it generates stays the
    upstream default and Tankarr's opinion is applied on every start.
    """

    prefix = "-Dsuwayomi.tachidesk.config.server."
    settings = server_settings(
        port=port,
        extension_stores=extension_stores,
        max_sources_in_parallel=max_sources_in_parallel,
        credentials=credentials,
    )
    heap = max(128, int(heap_mb))
    return [
        "java",
        f"-Xms{min(64, heap)}m",
        f"-Xmx{heap}m",
        "-XX:MinHeapFreeRatio=20",
        "-XX:MaxHeapFreeRatio=50",
        "-XX:-ShrinkHeapInSteps",
        "-XX:MaxMetaspaceSize=160m",
        "-XX:MaxDirectMemorySize=64m",
        "-XX:+ExitOnOutOfMemoryError",
        "-XX:+UseSerialGC",
        # Beyond the heap the JVM reserves stacks, JIT code cache and class
        # space by default sizes meant for servers: on the Pi that was 480 MB
        # resident for a 256 MB heap. Smaller stacks, a bounded code cache,
        # C1 only and a small class space keep the process near the heap.
        "-Xss512k",
        "-XX:ReservedCodeCacheSize=48m",
        "-XX:CompressedClassSpaceSize=64m",
        "-XX:TieredStopAtLevel=1",
        "-XX:CICompilerCount=1",
        "-Xshare:auto",
        "-Djava.awt.headless=true",
        f"-Duser.home={home}",
        *(
            f"{prefix}{key}={value}"
            for key, value in settings.items()
            # Credentials stay in server.conf: the command line of a process
            # is readable by every user of the host.
            if not isinstance(value, list) and key not in SECRET_SERVER_KEYS
        ),
        "-jar",
        str(jar),
    ]


def server_settings(
    *,
    port: int,
    extension_stores: Sequence[str] = (),
    max_sources_in_parallel: int = 4,
    credentials: tuple[str, str] | None = None,
) -> dict[str, str | list[str]]:
    """Tankarr's opinion on every Suwayomi server setting it cares about."""

    settings: dict[str, str | list[str]] = {
        "ip": "0.0.0.0" if credentials else "127.0.0.1",
        "port": str(port),
        "authMode": "basic_auth" if credentials else "none",
        "webUIEnabled": "true" if credentials else "false",
        "webUIChannel": "bundled",
        "webUIUpdateCheckInterval": "0",
        "initialOpenInBrowserEnabled": "false",
        "systemTrayEnabled": "false",
        "kcefEnabled": "false",
        "downloadAsCbz": "false",
        "autoDownloadNewChapters": "false",
        "globalUpdateInterval": "0",
        "updateMangas": "false",
        "maxSourcesInParallel": str(max_sources_in_parallel),
        "flareSolverrEnabled": "false",
        "backupInterval": "0",
        "debugLogsEnabled": "false",
        "maxLogFiles": "3",
        "maxLogFileSize": "5mb",
        "maxLogFolderSize": "20mb",
        "extensionStores": [str(store) for store in extension_stores if store],
    }
    if credentials:
        settings["authUsername"], settings["authPassword"] = credentials
    return settings


CONF_ENUMS = {"authMode", "webUIChannel", "webUIFlavor", "webUIInterface"}
SECRET_SERVER_KEYS = frozenset({"authUsername", "authPassword"})


def _conf_value(key: str, value: str | list[str]) -> str:
    if isinstance(value, list):
        return "[" + ", ".join(json.dumps(item) for item in value) + "]"
    if value in {"true", "false"} or value.lstrip("-").isdigit():
        return value
    if key in CONF_ENUMS:
        return value.upper()
    return json.dumps(value)


def render_server_conf(existing: str, settings: dict[str, str | list[str]]) -> str:
    """Apply Tankarr's settings to Suwayomi's ``server.conf`` text.

    Suwayomi honours scalar overrides passed as system properties but not
    list values, and it persists the file it generated on first start, so
    the file itself is edited the way the official Docker image does: each
    managed ``server.<key> = …`` line is rewritten in place (its trailing
    comment kept), missing keys are appended.
    """

    lines = existing.splitlines()
    seen: set[str] = set()
    for index, line in enumerate(lines):
        match = re.match(r"^(server\.([A-Za-z0-9_]+))\s*=\s*(.*?)(\s*#.*)?$", line)
        if not match:
            continue
        key = match.group(2)
        if key not in settings:
            continue
        seen.add(key)
        comment = match.group(4) or ""
        lines[index] = f"server.{key} = {_conf_value(key, settings[key])}{comment}"
    for key, value in settings.items():
        if key not in seen:
            lines.append(f"server.{key} = {_conf_value(key, value)}")
    return "\n".join(lines).rstrip("\n") + "\n"


class SuwayomiRuntime:
    def __init__(
        self,
        root: Path,
        *,
        port: int = DEFAULT_PORT,
        heap_mb: int = 384,
        credentials: tuple[str, str] | None = None,
        extension_store: str | None = None,
        java_executable: str = "java",
        command_builder: Callable[..., list[str]] = jvm_arguments,
        ready_probe: Callable[[], Any] | None = None,
        releases_api: str = RELEASES_API,
        sleep: Callable[[float], Any] = asyncio.sleep,
    ) -> None:
        self.root = Path(root)
        self.server_dir = self.root / "server"
        self.home = self.root / "home"
        self.log_path = self.root / "suwayomi.log"
        self.state_path = self.server_dir / "current.json"
        self.port = port
        self.heap_mb = heap_mb
        # Sources whose last probe failed on a Cloudflare/DDoS-guard challenge.
        # Reported to the operator as a fact about the source; the managed
        # runtime has no bypass to offer and never installs one.
        self.challenged_sources: list[str] = []
        self.credentials = credentials
        # The repository the server installs extensions from; None = none
        # configured, so the catalogue is empty until the operator sets one.
        self.extension_store = extension_store or None
        self.java_executable = java_executable
        self._command_builder = command_builder
        self._ready_probe = ready_probe
        self._releases_api = releases_api
        self._sleep = sleep
        self.url = f"http://127.0.0.1:{port}"
        self._process: asyncio.subprocess.Process | None = None
        self._supervisor: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._installing = False
        self._stopping = False
        self._started_at: float | None = None
        self._consecutive_crashes = 0
        self._restarts = 0
        self._last_error: str | None = None
        self._last_exit_code: int | None = None
        self._ready = False
        self._install_progress: str | None = None
        self._latest_version: str | None = None
        self._last_update_check_at: str | None = None
        self._last_extension_refresh_at: str | None = None
        self._last_extension_updates: list[str] = []
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0))

    # ------------------------------------------------------------------ state

    def configure(
        self,
        *,
        heap_mb: int | None = None,
        credentials: tuple[str, str] | None | object = ...,
        extension_store: str | None | object = ...,
    ) -> bool:
        """Apply new runtime options; returns True when a restart is needed."""

        changed = False
        if credentials is not ... and credentials != self.credentials:
            self.credentials = credentials  # type: ignore[assignment]
            changed = True
        if extension_store is not ... and (extension_store or None) != (
            self.extension_store
        ):
            self.extension_store = extension_store or None  # type: ignore[assignment]
            changed = True
        if heap_mb is not None and heap_mb != self.heap_mb:
            self.heap_mb = heap_mb
            changed = True
        return changed

    def installed(self) -> dict[str, Any] | None:
        try:
            state = json.loads(self.state_path.read_text())
        except (OSError, ValueError):
            return None
        jar = self.server_dir / str(state.get("jar") or "")
        if not jar.is_file():
            return None
        return {**state, "path": jar}

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    def status(self) -> dict[str, Any]:
        installed = self.installed()
        jar = installed["path"] if installed else None
        uptime = time.monotonic() - self._started_at if self._started_at else None
        return {
            "managed": True,
            "installed": installed is not None,
            "installing": self._installing,
            "install_progress": self._install_progress,
            "version": installed.get("tag") if installed else None,
            "installed_at": installed.get("installed_at") if installed else None,
            "jar_size_bytes": jar.stat().st_size if jar else 0,
            "running": self.running,
            "ready": self._ready and self.running,
            "pid": self._process.pid if self.running and self._process else None,
            "uptime_seconds": int(uptime) if uptime and self.running else None,
            "restarts": self._restarts,
            "consecutive_crashes": self._consecutive_crashes,
            "last_exit_code": self._last_exit_code,
            "last_error": self._last_error,
            "heap_mb": self.heap_mb,
            "exposed": self.credentials is not None,
            "extension_store": self.extension_store,
            "latest_version": self._latest_version,
            "update_available": bool(
                installed
                and self._latest_version
                and installed.get("tag") != self._latest_version
            ),
            "last_update_check_at": self._last_update_check_at,
            "last_extension_refresh_at": self._last_extension_refresh_at,
            "last_extension_updates": list(self._last_extension_updates),
            "url": self.url,
            "data_dir": str(self.home),
            "log_tail": self.log_tail(),
            "release_page": RELEASE_PAGE,
            "writable": self._writable(),
        }

    def _writable(self) -> bool:
        target = self.root if self.root.exists() else self.root.parent
        return os.access(target, os.W_OK)

    def log_tail(self, lines: int = 25) -> list[str]:
        try:
            with self.log_path.open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                handle.seek(max(0, size - 64 * 1024))
                text = handle.read().decode("utf-8", "replace")
        except OSError:
            return []
        return [line.rstrip() for line in text.splitlines()[-lines:]]

    # ---------------------------------------------------------------- install

    async def install(self, *, start: bool = True) -> dict[str, Any]:
        """Download and verify the latest official JAR, then (re)start."""

        if self._installing:
            raise SuwayomiRuntimeBusy("A Suwayomi installation is already running")
        self._installing = True
        self._install_progress = "Checking the latest release"
        try:
            self.server_dir.mkdir(parents=True, exist_ok=True)
            response = await self._http.get(
                self._releases_api,
                headers={"Accept": "application/vnd.github+json"},
            )
            response.raise_for_status()
            assets = select_release_assets(response.json())
            current = self.installed()
            if current and current.get("pending_update", {}).get("attempted"):
                if self.running:
                    await self.stop()
                await asyncio.to_thread(self._rollback_update, current)
                current = self.installed()
            if current and current.get("tag") == assets.tag:
                self._install_progress = None
                if start and not self.running:
                    await self.start()
                return {**self.status(), "updated": False}

            checksum_response = await self._http.get(
                assets.checksums_url, follow_redirects=True
            )
            checksum_response.raise_for_status()
            digests = parse_checksums(checksum_response.text)
            expected = digests.get(assets.jar_name)
            if not expected:
                raise SuwayomiRuntimeError(
                    f"{CHECKSUM_ASSET} does not list {assets.jar_name}"
                )

            self._install_progress = f"Downloading {assets.jar_name}"
            temporary = self.server_dir / f".{assets.jar_name}.part"
            digest = hashlib.sha256()
            received = 0
            with temporary.open("wb") as handle:
                async with self._http.stream(
                    "GET", assets.jar_url, follow_redirects=True
                ) as stream:
                    stream.raise_for_status()
                    async for chunk in stream.aiter_bytes(1024 * 256):
                        handle.write(chunk)
                        digest.update(chunk)
                        received += len(chunk)
                        self._install_progress = (
                            f"Downloading {assets.jar_name}: "
                            f"{received // (1024 * 1024)} MiB"
                        )
            if digest.hexdigest() != expected:
                temporary.unlink(missing_ok=True)
                raise SuwayomiRuntimeError(
                    f"SHA-256 mismatch for {assets.jar_name}; download discarded"
                )
            final = self.server_dir / assets.jar_name
            with temporary.open("rb") as handle:
                os.fsync(handle.fileno())
            os.replace(temporary, final)
            fsync_directory(self.server_dir)

            self._install_progress = "Switching to the new server"
            was_running = self.running
            if was_running:
                await self.stop()
            # A new JAR can migrate the engine database before its health probe
            # succeeds. Snapshot the stopped engine's entire home, not just its
            # binary, so rollback never pairs an old JAR with a new schema.
            rollback = self.root / f".rollback-home-{uuid.uuid4().hex}"
            try:
                await asyncio.to_thread(self._snapshot_home, rollback)
            except BaseException:
                if was_running:
                    await self.start()
                raise
            state = {
                "tag": assets.tag,
                "jar": assets.jar_name,
                "sha256": expected,
                "size": received,
                "published_at": assets.published_at,
                "installed_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "previous": current.get("jar") if current else None,
                "health_verified": False,
                "pending_update": {
                    "previous_state": {
                        key: value for key, value in current.items() if key != "path"
                    }
                    if current
                    else None,
                    "backup": rollback.name,
                    "failed_home": f".failed-update-{uuid.uuid4().hex}",
                    "attempted": False,
                    "was_running": was_running,
                },
            }
            try:
                self._write_state(state)
            except Exception:
                await asyncio.to_thread(self._rollback_update, state)
                if was_running:
                    await self._start_process()
                raise
            self._last_error = None
            self._consecutive_crashes = 0
            if start or was_running:
                await self._activate_update(state)
            return {**self.status(), "updated": True}
        except httpx.HTTPError as exc:
            self._last_error = f"Download failed: {type(exc).__name__}: {exc}"[:300]
            raise SuwayomiRuntimeError(self._last_error) from exc
        finally:
            self._installing = False
            self._install_progress = None

    def _write_state(self, state: dict[str, Any]) -> None:
        descriptor, name = tempfile.mkstemp(dir=self.server_dir, prefix=".current-")
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "w") as handle:
                json.dump(
                    {key: value for key, value in state.items() if key != "path"},
                    handle,
                    indent=2,
                )
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.state_path)
            fsync_directory(self.server_dir)
        finally:
            temporary.unlink(missing_ok=True)

    def _snapshot_home(self, destination: Path) -> None:
        if self.home.is_symlink():
            raise SuwayomiRuntimeError("Refusing to update a symlinked engine home")
        if any(path.is_symlink() for path in self.home.rglob("*")):
            raise SuwayomiRuntimeError(
                "Engine home contains linked data; a complete rollback snapshot cannot be guaranteed"
            )
        size = (
            sum(
                path.stat().st_size
                for path in self.home.rglob("*")
                if path.is_file() and not path.is_symlink()
            )
            if self.home.exists()
            else 0
        )
        if shutil.disk_usage(self.root).free < size + 64 * 1024**2:
            raise SuwayomiRuntimeError(
                "Insufficient space for engine rollback snapshot"
            )
        if self.home.exists():
            shutil.copytree(self.home, destination, symlinks=True)
        else:
            destination.mkdir(mode=0o700)
        destination.chmod(0o700)
        for path in destination.rglob("*"):
            if path.is_file() and not path.is_symlink():
                with path.open("rb") as handle:
                    os.fsync(handle.fileno())
        for path in sorted(
            (
                path
                for path in destination.rglob("*")
                if path.is_dir() and not path.is_symlink()
            ),
            key=lambda path: len(path.parts),
            reverse=True,
        ):
            fsync_directory(path)
        fsync_directory(destination)
        fsync_directory(self.root)

    def _rollback_update(self, state: dict[str, Any]) -> None:
        pending = state["pending_update"]
        backup_name, failed_name = pending["backup"], pending["failed_home"]
        if (
            not isinstance(backup_name, str)
            or not backup_name.startswith(".rollback-home-")
            or Path(backup_name).name != backup_name
            or not isinstance(failed_name, str)
            or not failed_name.startswith(".failed-update-")
            or Path(failed_name).name != failed_name
        ):
            raise SuwayomiRuntimeError("Invalid engine rollback journal")
        backup, failed = self.root / backup_name, self.root / failed_name
        if backup.is_symlink() or failed.is_symlink() or self.home.is_symlink():
            raise SuwayomiRuntimeError("Refusing symlinked engine rollback paths")
        if backup.is_dir():
            if self.home.exists():
                publish_without_overwrite(self.home, failed)
                fsync_directory(self.root)
            publish_without_overwrite(backup, self.home)
            fsync_directory(self.root)
        elif not self.home.is_dir() or not failed.is_dir():
            raise SuwayomiRuntimeError(
                "Engine rollback snapshot missing; manual recovery required"
            )
        previous = pending.get("previous_state")
        if previous:
            self._write_state(previous)
        else:
            self.state_path.unlink(missing_ok=True)
            fsync_directory(self.server_dir)

    async def _activate_update(self, state: dict[str, Any]) -> None:
        state["pending_update"]["attempted"] = True
        try:
            self._write_state(state)
            await self._start_process()
            if not await self.wait_ready(timeout=180):
                raise SuwayomiRuntimeError(
                    "Updated Suwayomi failed its readiness check"
                )
            stable = {
                key: value
                for key, value in state.items()
                if key not in {"pending_update", "path"}
            }
            stable["health_verified"] = True
            self._write_state(stable)
        except (Exception, asyncio.CancelledError) as exc:
            await self.stop()
            await asyncio.to_thread(self._rollback_update, state)
            if state["pending_update"].get("was_running") and self.installed():
                await self._start_process()
                await self.wait_ready(timeout=180)
            self._last_error = (
                "Suwayomi update failed; previous engine and data restored"
            )
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise SuwayomiRuntimeError(self._last_error) from exc
        # Retire only our private snapshot, after the healthy state is durable.
        backup = self.root / state["pending_update"]["backup"]
        if backup.is_dir() and not backup.is_symlink():
            with contextlib.suppress(OSError):
                await asyncio.to_thread(shutil.rmtree, backup)
        self._prune_jars(keep={str(state["jar"]), str(state.get("previous") or "")})

    def _prune_jars(self, *, keep: set[str]) -> None:
        for path in self.server_dir.glob("*.jar"):
            if path.name not in keep:
                with contextlib.suppress(OSError):
                    path.unlink()

    # -------------------------------------------------------------- lifecycle

    def _extension_stores(self) -> list[str]:
        return [self.extension_store] if self.extension_store else []

    def command(self) -> list[str]:
        installed = self.installed()
        if installed is None:
            raise SuwayomiRuntimeError("Suwayomi is not installed")
        argv = self._command_builder(
            jar=installed["path"],
            home=self.home,
            port=self.port,
            heap_mb=self.heap_mb,
            credentials=self.credentials,
            extension_stores=self._extension_stores(),
        )
        return [self.java_executable, *argv[1:]]

    async def start(self) -> dict[str, Any]:
        state = self.installed()
        if not self.running and state and state.get("pending_update"):
            if state["pending_update"].get("attempted"):
                await asyncio.to_thread(self._rollback_update, state)
            else:
                await self._activate_update(state)
                return self.status()
        return await self._start_process()

    async def _start_process(self) -> dict[str, Any]:
        async with self._lock:
            if self.running:
                return self.status()
            self._stopping = False
            self._consecutive_crashes = 0
            await self._spawn()
            if self._supervisor is None or self._supervisor.done():
                self._supervisor = asyncio.create_task(
                    self._supervise(), name="tankarr-suwayomi-supervisor"
                )
        return self.status()

    @property
    def server_conf_path(self) -> Path:
        return self.home / ".local" / "share" / "Tachidesk" / "server.conf"

    def write_server_conf(self) -> Path:
        """Persist Tankarr's settings into Suwayomi's own configuration file."""

        path = self.server_conf_path
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = path.read_text() if path.exists() else ""
        rendered = render_server_conf(
            existing,
            server_settings(
                port=self.port,
                credentials=self.credentials,
                extension_stores=self._extension_stores(),
            ),
        )
        if rendered != existing:
            temporary = path.with_suffix(".conf.tmp")
            temporary.unlink(missing_ok=True)
            descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(rendered)
            os.replace(temporary, path)
        # It holds the login: owner-only, also for files older installs wrote,
        # and in a directory other users cannot enter even if Suwayomi
        # rewrites the file with its own permissions.
        os.chmod(path, 0o600)
        os.chmod(path.parent, 0o700)
        return path

    async def _spawn(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        self.write_server_conf()
        self._rotate_log()
        argv = self.command()
        log = self.log_path.open("ab")
        try:
            log.write(
                f"\n=== Tankarr starting Suwayomi at "
                f"{datetime.now(UTC).isoformat(timespec='seconds')} ===\n".encode()
            )
            log.flush()
            self._process = await asyncio.create_subprocess_exec(
                *argv,
                stdout=log,
                stderr=asyncio.subprocess.STDOUT,
                stdin=asyncio.subprocess.DEVNULL,
                cwd=self.home,
                env={
                    # Extensions run inside this JVM: Tankarr's own settings
                    # and secrets are not theirs to read.
                    **{
                        name: value
                        for name, value in os.environ.items()
                        if not name.startswith("TANKARR_")
                    },
                    "HOME": str(self.home),
                    "LANG": "en_US.UTF-8",
                    "JAVA_TOOL_OPTIONS": "",
                },
            )
        finally:
            log.close()
        self._started_at = time.monotonic()
        self._ready = False
        logger.info("Managed Suwayomi started (pid %s)", self._process.pid)

    def _rotate_log(self) -> None:
        try:
            if self.log_path.stat().st_size > MAX_LOG_BYTES:
                os.replace(self.log_path, self.log_path.with_suffix(".log.1"))
        except OSError:
            pass

    async def _supervise(self) -> None:
        while True:
            process = self._process
            if process is None:
                return
            code = await process.wait()
            self._last_exit_code = code
            self._ready = False
            if self._stopping:
                return
            self._consecutive_crashes += 1
            self._restarts += 1
            self._last_error = f"Suwayomi exited with code {code}"
            logger.warning(
                "Managed Suwayomi exited with code %s (crash %s)",
                code,
                self._consecutive_crashes,
            )
            if not await self._respawn_until_running():
                return

    async def _respawn_until_running(self) -> bool:
        """Bring the engine back, however long it takes.

        Suwayomi is the only way Tankarr reaches a source: a run of crashes is
        a reason to try more slowly, never to stop trying, or the library goes
        quietly blind until somebody notices. The wait doubles up to a ceiling
        and stays there, and giving the operator that news is worth one line in
        the log, not one per attempt. ``False`` means the runtime is stopping.
        """

        announced = False
        while True:
            if self._stopping:
                return False
            attempt = max(1, self._consecutive_crashes)
            if attempt > MAX_CONSECUTIVE_CRASHES:
                self._last_error = (
                    f"Suwayomi crashed {attempt} times in a row; still retrying "
                    f"every {int(RESTART_BACKOFF_CAP_SECONDS)}s"
                )
                if not announced:
                    logger.error(self._last_error)
                    announced = True
            await self._sleep(
                min(5.0 * 2 ** (attempt - 1), RESTART_BACKOFF_CAP_SECONDS)
            )
            if self._stopping:
                return False
            try:
                await self._spawn()
                return True
            except Exception as exc:  # noqa: BLE001 - keep supervising
                self._consecutive_crashes += 1
                self._last_error = f"Restart failed: {type(exc).__name__}: {exc}"
                logger.error(self._last_error)

    async def stop(self) -> dict[str, Any]:
        async with self._lock:
            self._stopping = True
            process = self._process
            if process is not None and process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), STOP_TIMEOUT_SECONDS)
                except TimeoutError:
                    with contextlib.suppress(ProcessLookupError):
                        process.kill()
                    await process.wait()
            supervisor = self._supervisor
            if supervisor is not None and not supervisor.done():
                with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                    await asyncio.wait_for(supervisor, 5.0)
            self._supervisor = None
            self._process = None
            self._ready = False
            self._started_at = None
        return self.status()

    async def restart(self) -> dict[str, Any]:
        await self.stop()
        return await self.start()

    async def aclose(self) -> None:
        with contextlib.suppress(Exception):
            await self.stop()
        await self._http.aclose()

    async def wait_ready(self, timeout: float = READY_TIMEOUT_SECONDS) -> bool:
        """Poll GraphQL until the server answers; False on timeout/crash."""

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.running:
                return False
            if await self._probe():
                self._ready = True
                self._consecutive_crashes = 0
                return True
            await self._sleep(1.0)
        return False

    async def _probe(self) -> bool:
        if self._ready_probe is not None:
            result = self._ready_probe()
            if asyncio.iscoroutine(result):
                result = await result
            return bool(result)
        try:
            data = await self.graphql(ABOUT_QUERY, {}, timeout=5.0)
        except Exception:  # noqa: BLE001 - not ready yet
            return False
        return bool(data.get("aboutServer"))

    # ---------------------------------------------------------------- graphql

    async def graphql(
        self, query: str, variables: dict[str, Any], *, timeout: float = 60.0
    ) -> dict[str, Any]:
        response = await self._http.post(
            f"{self.url}/api/graphql",
            json={"query": query, "variables": variables},
            timeout=timeout,
            auth=httpx.BasicAuth(*self.credentials) if self.credentials else None,
        )
        response.raise_for_status()
        payload = response.json()
        errors = payload.get("errors") or []
        data = payload.get("data")
        if errors or not isinstance(data, dict):
            message = "; ".join(
                str(item.get("message") if isinstance(item, dict) else item)
                for item in errors[:3]
            )
            raise SuwayomiRuntimeError(
                f"Suwayomi GraphQL failed: {message or 'no data'}"
            )
        return data

    async def about(self) -> dict[str, Any]:
        data = await self.graphql(ABOUT_QUERY, {}, timeout=10.0)
        return dict(data.get("aboutServer") or {})

    async def list_extensions(
        self, *, languages: Iterable[str] | None = None, refresh: bool = True
    ) -> list[dict[str, Any]]:
        """Extensions of the configured repository, plus the installed ones.

        Whatever repository an installed extension came from, it stays
        listed so it can be updated or removed; only offers are limited to
        the repository the operator configured.
        """

        if refresh:
            with contextlib.suppress(SuwayomiRuntimeError, httpx.HTTPError):
                await self.graphql(FETCH_EXTENSIONS, {"input": {}}, timeout=120.0)
        data = await self.graphql(LIST_EXTENSIONS, {}, timeout=60.0)
        wanted = {code.casefold() for code in languages or ()}
        result: list[dict[str, Any]] = []
        for node in (data.get("extensions") or {}).get("nodes") or []:
            lang = str(node.get("lang") or "").casefold()
            if not node.get("isInstalled") and node.get("storeIndexUrl") not in (
                None,
                "",
                self.extension_store,
            ):
                continue
            if wanted and lang not in wanted and lang not in {"all", "multi"}:
                continue
            result.append(
                {
                    "pkg_name": str(node.get("pkgName") or ""),
                    "name": str(node.get("name") or ""),
                    "language": lang,
                    "version": str(node.get("versionName") or ""),
                    "nsfw": node.get("contentWarning") == "NSFW",
                    "installed": bool(node.get("isInstalled")),
                    "has_update": bool(node.get("hasUpdate")),
                    "obsolete": bool(node.get("isObsolete")),
                    # The runtime listens on the container's loopback, which a
                    # browser cannot reach: icons are served through Tankarr.
                    "icon_url": (
                        f"/api/system/suwayomi/extensions/{node['pkgName']}/icon"
                        if str(node.get("iconUrl") or "").startswith("/")
                        else None
                    ),
                    "icon_path": str(node.get("iconUrl") or ""),
                }
            )
        result.sort(
            key=lambda item: (
                not item["installed"],
                item["language"],
                item["name"].casefold(),
            )
        )
        return result

    async def list_languages(self) -> list[dict[str, Any]]:
        """Languages present in the extension store, with catalogue sizes."""

        counts: dict[str, int] = {}
        for item in await self.list_extensions(refresh=False):
            counts[item["language"]] = counts.get(item["language"], 0) + 1
        return [
            {"code": code, "extensions": count}
            for code, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        ]

    async def check_update(self) -> dict[str, Any]:
        """Compare the installed JAR with the latest GitHub release."""

        response = await self._http.get(
            self._releases_api, headers={"Accept": "application/vnd.github+json"}
        )
        response.raise_for_status()
        assets = select_release_assets(response.json())
        installed = self.installed()
        self._latest_version = assets.tag
        self._last_update_check_at = datetime.now(UTC).isoformat(timespec="seconds")
        return {
            "installed": installed.get("tag") if installed else None,
            "latest": assets.tag,
            "update_available": bool(installed and installed.get("tag") != assets.tag),
            "checked_at": self._last_update_check_at,
        }

    async def update_installed_extensions(self) -> list[str]:
        """Refresh the store and update every installed extension that has one."""

        updated: list[str] = []
        for item in await self.list_extensions(refresh=True):
            if item["installed"] and item["has_update"]:
                try:
                    await self.set_extension(item["pkg_name"], installed=True)
                    updated.append(item["pkg_name"])
                except Exception as exc:  # noqa: BLE001 - one package must not stop
                    logger.warning(
                        "Extension update failed for %s: %s", item["pkg_name"], exc
                    )
        self._last_extension_refresh_at = datetime.now(UTC).isoformat(
            timespec="seconds"
        )
        self._last_extension_updates = updated
        return updated

    async def set_extension(self, pkg_name: str, *, installed: bool) -> dict[str, Any]:
        patch = {"install": True, "update": True} if installed else {"uninstall": True}
        data = await self.graphql(
            UPDATE_EXTENSION,
            {"input": {"id": pkg_name, "patch": patch}},
            timeout=180.0,
        )
        extension = ((data.get("updateExtension") or {}).get("extension")) or {}
        return {
            "pkg_name": pkg_name,
            "installed": bool(extension.get("isInstalled")),
            "has_update": bool(extension.get("hasUpdate")),
        }

    async def install_catalogue(
        self,
        languages: Sequence[str],
        *,
        exclude: frozenset[str] | set[str] = frozenset(),
    ) -> list[str]:
        """Install every safe store extension for the given languages.

        Membership is broad on purpose: Tankarr's perennial health ranking
        orders sources by measured behaviour, so nothing needs curating out
        besides NSFW content and extensions this runtime cannot serve.
        """

        installed: list[str] = []
        excluded = set(exclude)
        for item in await self.list_extensions(languages=languages, refresh=True):
            if item["installed"]:
                continue
            if item["nsfw"] or item["obsolete"] or item["pkg_name"] in excluded:
                continue
            try:
                await self.set_extension(item["pkg_name"], installed=True)
                installed.append(item["pkg_name"])
            except Exception as exc:  # noqa: BLE001 - one bad package must not stop
                logger.warning(
                    "Suwayomi extension %s failed: %s", item["pkg_name"], exc
                )
        return installed

    async def fetch_icon(self, icon_path: str) -> tuple[bytes, str]:
        """One extension icon, read from the runtime on the operator's behalf."""

        if not icon_path.startswith("/"):
            raise SuwayomiRuntimeError("Unknown icon path")
        async with httpx.AsyncClient(
            timeout=15.0,
            auth=httpx.BasicAuth(*self.credentials) if self.credentials else None,
        ) as client:
            response = await client.get(f"{self.url}{icon_path}")
            response.raise_for_status()
            return response.content, str(
                response.headers.get("content-type") or "image/png"
            )

    async def list_sources(self) -> list[dict[str, Any]]:
        data = await self.graphql(LIST_SOURCES, {}, timeout=60.0)
        return [
            {
                "id": str(node.get("id") or ""),
                "name": str(node.get("displayName") or node.get("name") or ""),
                "language": str(node.get("lang") or "").casefold(),
                "nsfw": node.get("contentWarning") == "NSFW",
                "pkg_name": str((node.get("extension") or {}).get("pkgName") or ""),
                # The store no longer carries this extension: it still works
                # until the site changes, but nobody maintains it.
                "obsolete": bool((node.get("extension") or {}).get("isObsolete")),
                "home_url": str(node.get("homeUrl") or ""),
            }
            for node in (data.get("sources") or {}).get("nodes") or []
        ]

    async def test_sources_iter(
        self,
        sources: Sequence[dict[str, Any]],
        *,
        language: str = "en",
        titles: Sequence[str] | None = None,
        timeout: float = 15.0,
        parallel: int = 4,
    ) -> AsyncIterator[dict[str, Any]]:
        """Probe each source, yielding every verdict as soon as it lands."""

        probes = tuple(
            titles or PROBE_TITLES.get(language.casefold(), PROBE_TITLES_DEFAULT)
        )
        semaphore = asyncio.Semaphore(max(1, parallel))
        challenged: set[str] = set()

        async def probe(source: dict[str, Any]) -> dict[str, Any]:
            hits = 0
            latencies: list[float] = []
            error: str | None = None
            async with semaphore:
                for title in probes:
                    started = time.monotonic()
                    try:
                        data = await self.graphql(
                            SEARCH_SOURCE,
                            {
                                "input": {
                                    "source": str(source["id"]),
                                    "type": "SEARCH",
                                    "query": title,
                                    "page": 1,
                                }
                            },
                            timeout=timeout,
                        )
                        mangas = (data.get("fetchSourceManga") or {}).get(
                            "mangas"
                        ) or []
                        latencies.append(time.monotonic() - started)
                        if mangas:
                            hits += 1
                    except Exception as exc:  # noqa: BLE001 - reported per source
                        latencies.append(time.monotonic() - started)
                        error = f"{type(exc).__name__}: {exc}"[:160]
                        if CHALLENGE_PATTERN.search(str(exc)):
                            challenged.add(str(source.get("name") or source.get("id")))
            average = sum(latencies) / len(latencies) if latencies else None
            # A source that answers is fine even when it does not carry the
            # probe titles (official platforms and origin groups have small
            # catalogues); only errors and latency are verdicts.
            verdict = (
                "unreachable"
                if error and hits == 0
                else "slow"
                if average is not None and average > 6.0
                else "good"
            )
            return {
                "id": str(source["id"]),
                "name": source.get("name"),
                "language": source.get("language"),
                "hits": hits,
                "probes": len(probes),
                "average_seconds": round(average, 2) if average is not None else None,
                "error": error,
                "verdict": verdict,
            }

        tasks = [asyncio.ensure_future(probe(source)) for source in sources]
        try:
            for pending in asyncio.as_completed(tasks):
                yield await pending
        finally:
            for task in tasks:
                task.cancel()
            self.challenged_sources = sorted(challenged)

    async def test_sources(
        self,
        sources: Sequence[dict[str, Any]],
        *,
        language: str = "en",
        titles: Sequence[str] | None = None,
        timeout: float = 15.0,
        parallel: int = 4,
    ) -> list[dict[str, Any]]:
        """Search probe titles on each source; report hits and latency."""

        results = [
            item
            async for item in self.test_sources_iter(
                sources,
                language=language,
                titles=titles,
                timeout=timeout,
                parallel=parallel,
            )
        ]
        order = {"good": 0, "slow": 1, "unreachable": 2}
        return sorted(
            results,
            key=lambda item: (order[item["verdict"]], item["average_seconds"] or 99),
        )


def instance_token(root: Path) -> str:
    """Stable 8-hex token identifying one managed Suwayomi database.

    Created with the runtime's data directory and kept for its lifetime, so
    Tankarr can tell ids minted by this instance from ids of a previous one.
    """

    path = Path(root) / "instance.json"
    try:
        token = str(json.loads(path.read_text()).get("token") or "")
        if re.fullmatch(r"[0-9a-f]{8}", token):
            return token
    except (OSError, ValueError):
        pass
    token = uuid.uuid4().hex[:8]
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(
            {
                "token": token,
                "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
            }
        )
    )
    os.replace(temporary, path)
    return token


def default_java_executable() -> str:
    java_home = os.environ.get("JAVA_HOME")
    if java_home:
        candidate = Path(java_home) / "bin" / "java"
        if candidate.exists():
            return str(candidate)
    return shutil.which("java") or "java"


__all__ = [
    "PROBE_TITLES",
    "render_server_conf",
    "server_settings",
    "SuwayomiRuntime",
    "SuwayomiRuntimeBusy",
    "SuwayomiRuntimeError",
    "default_java_executable",
    "instance_token",
    "jvm_arguments",
    "parse_checksums",
    "select_release_assets",
]

if sys.platform == "win32":  # pragma: no cover - the Pi is Linux
    raise RuntimeError("The managed Suwayomi runtime targets Linux containers")
