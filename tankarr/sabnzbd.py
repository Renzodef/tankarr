"""SABnzbd: the Usenet download client, mirror of the qBittorrent client.

Prowlarr hands over an NZB URL; SABnzbd fetches and downloads it under
Tankarr's category; the finished directory is imported through the same
importer as torrents; the history entry is removed afterwards according
to the after-import action.
"""

from __future__ import annotations

import hashlib
from pathlib import Path, PurePosixPath
from typing import Any

import httpx

from tankarr.config import Settings
from tankarr.redaction import redact_secrets


class SABnzbdError(RuntimeError):
    pass


def nzb_pseudo_hash(guid: str) -> str:
    """A stable 40-hex id for an NZB release (the job table keys on it)."""

    return hashlib.sha1(str(guid).strip().encode("utf-8")).hexdigest()


class SABnzbdClient:
    def __init__(self, settings: Settings):
        self.settings = settings

    @property
    def configured(self) -> bool:
        return bool(self.settings.sabnzbd_url and self.settings.sabnzbd_api_key)

    @property
    def import_ready(self) -> bool:
        return self.configured and self.settings.usenet_download_dir is not None

    async def _call(self, mode: str, **params: Any) -> dict[str, Any]:
        if not self.configured:
            raise SABnzbdError("SABnzbd is not configured")
        query = {
            "mode": mode,
            "output": "json",
            "apikey": self.settings.sabnzbd_api_key,
            **params,
        }
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(
                    str(self.settings.sabnzbd_url).rstrip("/") + "/api", params=query
                )
                response.raise_for_status()
                payload = response.json()
        except httpx.HTTPStatusError as exc:
            # httpx quotes the full URL, API key included: report the status only.
            raise SABnzbdError(
                f"SABnzbd answered HTTP {exc.response.status_code}"
            ) from None
        except httpx.HTTPError as exc:
            raise SABnzbdError(
                redact_secrets(str(exc))
                or f"SABnzbd request failed: {type(exc).__name__}"
            ) from None
        if isinstance(payload, dict) and payload.get("status") is False:
            raise SABnzbdError(
                str(payload.get("error") or "SABnzbd refused the request")[:300]
            )
        if not isinstance(payload, dict):
            raise SABnzbdError("SABnzbd returned an invalid payload")
        return payload

    async def probe(self) -> dict[str, Any]:
        version = await self._call("version")
        return {"ok": True, "version": str(version.get("version") or "?")}

    async def ensure_category(self) -> dict[str, Any]:
        category = self.settings.sabnzbd_category
        categories = await self._call("get_cats")
        names = [str(item) for item in categories.get("categories") or []]
        if category in names:
            return {"category": category, "created": False}
        await self._call(
            "set_config",
            section="categories",
            keyword=category,
            name=category,
            dir=category,
            priority=-100,
            pp=3,
            script="None",
        )
        return {"category": category, "created": True}

    async def add_nzb_url(self, url: str, *, name: str) -> str:
        await self.ensure_category()
        payload = await self._call(
            "addurl", name=url, nzbname=name[:200], cat=self.settings.sabnzbd_category
        )
        ids = payload.get("nzo_ids") or []
        if not ids:
            raise SABnzbdError("SABnzbd accepted the NZB but returned no job id")
        return str(ids[0])

    async def job_state(self, nzo_id: str) -> dict[str, Any] | None:
        """Queue slot (downloading) or history slot (finished); None if gone."""

        queue = await self._call("queue", nzo_ids=nzo_id)
        for slot in (queue.get("queue") or {}).get("slots") or []:
            if str(slot.get("nzo_id")) == nzo_id:
                return {"where": "queue", **slot}
        history = await self._call("history", nzo_ids=nzo_id)
        for slot in (history.get("history") or {}).get("slots") or []:
            if str(slot.get("nzo_id")) == nzo_id:
                return {"where": "history", **slot}
        return None

    async def delete(self, nzo_id: str, *, delete_files: bool) -> None:
        state = await self.job_state(nzo_id)
        if state is None:
            return
        self._validate_category(state)
        if state["where"] == "queue":
            await self._call(
                "queue", name="delete", value=nzo_id, del_files=1 if delete_files else 0
            )
        else:
            await self._call(
                "history",
                name="delete",
                value=nzo_id,
                del_files=1 if delete_files else 0,
            )

    def _validate_category(self, state: dict[str, Any]) -> None:
        # Queue slots say "cat", history slots say "category".
        if (
            str(state.get("cat") or state.get("category") or "")
            != self.settings.sabnzbd_category
        ):
            raise SABnzbdError("Download is outside Tankarr's SABnzbd category")

    def local_content_path(self, history_slot: dict[str, Any]) -> Path:
        self._validate_category(history_slot)
        local_root_value = self.settings.usenet_download_dir
        if local_root_value is None:
            raise SABnzbdError("Tankarr's Usenet download mount is not configured")
        local_root = Path(local_root_value).resolve()
        if not local_root.is_dir():
            raise SABnzbdError("Tankarr's Usenet download mount is unavailable")
        remote_root = PurePosixPath(self.settings.sabnzbd_complete_path)
        storage = str(history_slot.get("storage") or "").strip()
        if not storage:
            raise SABnzbdError("SABnzbd did not report where the download was stored")
        try:
            relative = PurePosixPath(storage).relative_to(remote_root)
        except ValueError as exc:
            raise SABnzbdError(
                "SABnzbd storage path is outside Tankarr's Usenet directory"
            ) from exc
        if not relative.parts or any(part in {".", ".."} for part in relative.parts):
            raise SABnzbdError("SABnzbd returned an unsafe storage path")
        local = (local_root / Path(*relative.parts)).resolve()
        if local == local_root or local_root not in local.parents:
            raise SABnzbdError("SABnzbd storage path escapes the download mount")
        return local


__all__ = ["SABnzbdClient", "SABnzbdError", "nzb_pseudo_hash"]
