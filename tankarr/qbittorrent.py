from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path, PurePosixPath
from typing import Any

import httpx

from tankarr import USER_AGENT
from tankarr.config import Settings
from tankarr.http import async_client
from tankarr.torrent_utils import magnet_info_hash


class QBitTorrentError(RuntimeError):
    pass


class QBitTorrentClient:
    """Small ownership-scoped client for Tankarr's qBittorrent category."""

    def __init__(self, settings: Settings):
        self.settings = settings

    @property
    def configured(self) -> bool:
        return bool(
            self.settings.qbittorrent_url
            and self.settings.qbittorrent_username
            and self.settings.qbittorrent_password
        )

    @property
    def import_ready(self) -> bool:
        root = self.settings.torrent_download_dir
        return self.configured and root is not None and Path(root).is_dir()

    @asynccontextmanager
    async def _session(self) -> AsyncIterator[httpx.AsyncClient]:
        if not self.configured:
            raise QBitTorrentError("qBittorrent is not configured")
        base_url = str(self.settings.qbittorrent_url).rstrip("/")
        async with async_client(
            base_url=base_url,
            timeout=httpx.Timeout(self.settings.request_timeout_seconds),
            follow_redirects=False,
            headers={"Referer": f"{base_url}/", "User-Agent": USER_AGENT},
        ) as client:
            response = await client.post(
                "/api/v2/auth/login",
                data={
                    "username": self.settings.qbittorrent_username,
                    "password": self.settings.qbittorrent_password,
                },
            )
            if response.status_code not in {
                200,
                204,
            } or response.text.strip().casefold().startswith("fail"):
                raise QBitTorrentError("qBittorrent authentication failed")
            yield client

    async def probe(self) -> dict[str, Any]:
        async with self._session() as client:
            version = await client.get("/api/v2/app/version")
            version.raise_for_status()
            api_version = await client.get("/api/v2/app/webapiVersion")
            api_version.raise_for_status()
            return {
                "ok": True,
                "version": version.text.strip(),
                "api_version": api_version.text.strip(),
                "category": self.settings.qbittorrent_category,
                "import_mount": self.import_ready,
            }

    async def ensure_category(self) -> dict[str, Any]:
        async with self._session() as client:
            response = await client.get("/api/v2/torrents/categories")
            response.raise_for_status()
            categories = response.json()
            if not isinstance(categories, dict):
                raise QBitTorrentError("qBittorrent returned an invalid category list")
            category = self.settings.qbittorrent_category
            expected_path = self.settings.qbittorrent_save_path.rstrip("/")
            current = categories.get(category)
            if current is not None:
                current_path = str((current or {}).get("savePath") or "").rstrip("/")
                if current_path != expected_path:
                    raise QBitTorrentError(
                        f"qBittorrent category {category!r} already exists with a "
                        "different save path"
                    )
                return {
                    "created": False,
                    "category": category,
                    "save_path": expected_path,
                }
            created = await client.post(
                "/api/v2/torrents/createCategory",
                data={"category": category, "savePath": expected_path},
            )
            if created.status_code == 409:
                raise QBitTorrentError(
                    f"qBittorrent category {category!r} was created concurrently; retry"
                )
            created.raise_for_status()
            return {"created": True, "category": category, "save_path": expected_path}

    async def add_torrent(
        self,
        payload: bytes,
        *,
        release_id: str,
        expected_hash: str,
        paused: bool = False,
    ) -> None:
        await self.ensure_category()
        async with self._session() as client:
            response = await client.post(
                "/api/v2/torrents/add",
                data={
                    "savepath": self.settings.qbittorrent_save_path,
                    "category": self.settings.qbittorrent_category,
                    "tags": "tankarr",
                    "paused": str(paused).casefold(),
                    "stopped": str(paused).casefold(),
                    "autoTMM": "false",
                    "root_folder": "true",
                },
                files={
                    "torrents": (
                        f"tankarr-{release_id}.torrent",
                        payload,
                        "application/x-bittorrent",
                    )
                },
            )
            response.raise_for_status()
            self._validate_add_response(response, expected_hash, "torrent")

    async def add_magnet(
        self,
        magnet_url: str,
        *,
        release_id: str,
        expected_hash: str,
        paused: bool = False,
    ) -> None:
        del release_id  # Kept parallel with add_torrent for provider-neutral callers.
        normalized_hash = str(expected_hash or "").casefold()
        try:
            actual_hash = magnet_info_hash(magnet_url)
        except ValueError as exc:
            raise QBitTorrentError(str(exc)) from exc
        if actual_hash != normalized_hash:
            raise QBitTorrentError("Magnet hash differs from the selected release")
        await self.ensure_category()
        async with self._session() as client:
            response = await client.post(
                "/api/v2/torrents/add",
                data={
                    "urls": magnet_url,
                    "savepath": self.settings.qbittorrent_save_path,
                    "category": self.settings.qbittorrent_category,
                    "tags": "tankarr",
                    "paused": str(paused).casefold(),
                    "stopped": str(paused).casefold(),
                    "autoTMM": "false",
                    "root_folder": "true",
                },
            )
            response.raise_for_status()
            self._validate_add_response(response, expected_hash, "magnet")

    @staticmethod
    def _validate_add_response(
        response: httpx.Response,
        expected_hash: str,
        payload_label: str,
    ) -> None:
        body = response.text.strip()
        if body in {"", "Ok."}:
            return
        try:
            result = response.json()
        except ValueError:
            result = None
        if isinstance(result, dict):
            added = {
                str(item).casefold()
                for item in result.get("added_torrent_ids", [])
                if isinstance(item, str)
            }
            try:
                failures = int(result.get("failure_count") or 0)
                successes = int(result.get("success_count") or 0)
            except (TypeError, ValueError):
                failures = 1
                successes = 0
            if failures == 0 and successes >= 1 and expected_hash.casefold() in added:
                return
        raise QBitTorrentError(
            f"qBittorrent rejected the {payload_label}: {body[:120]}"
        )

    async def torrent_info(self, info_hash: str) -> dict[str, Any] | None:
        async with self._session() as client:
            response = await client.get(
                "/api/v2/torrents/info", params={"hashes": info_hash.casefold()}
            )
            response.raise_for_status()
            payload = response.json()
        if not isinstance(payload, list):
            raise QBitTorrentError("qBittorrent returned an invalid torrent list")
        matches = [
            item
            for item in payload
            if isinstance(item, dict)
            and str(item.get("hash") or "").casefold() == info_hash.casefold()
        ]
        if len(matches) > 1:
            raise QBitTorrentError("qBittorrent returned a duplicate torrent hash")
        return matches[0] if matches else None

    async def list_category_torrents(self) -> list[dict[str, Any]]:
        """Every torrent in Tankarr's category (the only ones it may touch)."""

        async with self._session() as client:
            response = await client.get(
                "/api/v2/torrents/info",
                params={"category": self.settings.qbittorrent_category},
            )
            response.raise_for_status()
            payload = response.json()
        if not isinstance(payload, list):
            raise QBitTorrentError("qBittorrent returned an invalid torrent list")
        return [
            item
            for item in payload
            if isinstance(item, dict)
            and str(item.get("category") or "") == self.settings.qbittorrent_category
        ]

    async def torrent_files(self, info_hash: str) -> list[dict[str, Any]]:
        async with self._session() as client:
            response = await client.get(
                "/api/v2/torrents/files", params={"hash": info_hash.casefold()}
            )
            response.raise_for_status()
            payload = response.json()
        if not isinstance(payload, list) or any(
            not isinstance(item, dict) for item in payload
        ):
            raise QBitTorrentError("qBittorrent returned an invalid file inventory")
        return payload

    async def wait_for_torrent(
        self, info_hash: str, timeout_seconds: float = 20.0
    ) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds
        while loop.time() < deadline:
            item = await self.torrent_info(info_hash)
            if item is not None:
                return item
            await asyncio.sleep(0.5)
        raise QBitTorrentError("qBittorrent did not expose the added torrent")

    async def delete_torrent(self, info_hash: str, *, delete_files: bool) -> None:
        """Delete one exact hash; used by ownership-safe tests and future cleanup."""

        current = await self.torrent_info(info_hash)
        if current is None:
            return
        if str(current.get("category") or "") != self.settings.qbittorrent_category:
            raise QBitTorrentError(
                "Refusing to delete a torrent outside Tankarr's category"
            )
        async with self._session() as client:
            response = await client.post(
                "/api/v2/torrents/delete",
                data={
                    "hashes": info_hash.casefold(),
                    "deleteFiles": str(delete_files).casefold(),
                },
            )
            response.raise_for_status()

    def local_content_path(self, torrent: dict[str, Any]) -> Path:
        local_root_value = self.settings.torrent_download_dir
        if local_root_value is None:
            raise QBitTorrentError("Tankarr's torrent download mount is not configured")
        local_root = Path(local_root_value).resolve()
        if not local_root.is_dir():
            raise QBitTorrentError("Tankarr's torrent download mount is unavailable")
        remote_root = PurePosixPath(self.settings.qbittorrent_save_path)
        remote_text = str(torrent.get("content_path") or "").strip()
        if not remote_text:
            name = str(torrent.get("name") or "").strip()
            if not name or "/" in name or name in {".", ".."}:
                raise QBitTorrentError("qBittorrent returned an unsafe content name")
            remote = remote_root / name
        else:
            remote = PurePosixPath(remote_text)
        try:
            relative = remote.relative_to(remote_root)
        except ValueError as exc:
            raise QBitTorrentError(
                "qBittorrent content path is outside Tankarr's save path"
            ) from exc
        if not relative.parts:
            raise QBitTorrentError("qBittorrent content path resolves to the save root")
        if ".." in relative.parts:
            raise QBitTorrentError("qBittorrent returned an unsafe content path")
        candidate = (local_root / Path(*relative.parts)).resolve()
        if candidate == local_root or local_root not in candidate.parents:
            raise QBitTorrentError("Torrent content escapes Tankarr's download mount")
        return candidate
