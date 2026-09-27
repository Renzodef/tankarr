"""Bounded, explicit operator workflows; previews never acquire content."""

from __future__ import annotations

import asyncio
import json
import os
import platform
import secrets
import shutil
import tempfile
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError

from tankarr import __version__
from tankarr.catalogue import parse_catalogue_id
from tankarr.config import Settings
from tankarr.database import Database
from tankarr.maintenance import MaintenanceWorker, guided_repair_report
from tankarr.models import AddMangaRequest
from tankarr.readers import _managed_library_books, effective_reader_kind
from tankarr.settings_store import preview_settings

ACQUISITION_PREFERENCES = {
    "release_acquisition_policy",
    "official_upgrade_enabled",
    "preferred_unit",
    "source_priority_fresh",
    "release_preference_profile",
    "source_priority_backfill",
}


class ListItem(BaseModel):
    title: str = Field(default="", max_length=500)
    provider: str = Field(default="catalogue", max_length=32)
    source_id: str | None = Field(default=None, max_length=1000)
    manga_id: str | None = Field(default=None, max_length=1000)
    language: str = Field(default="en", max_length=16)
    monitor_mode: str = Field(default="none", max_length=20)
    series_unit: str = Field(default="automatic", max_length=20)


class ListPreview(BaseModel):
    version: int = Field(default=1, ge=1, le=1)
    items: list[ListItem] = Field(max_length=500)


class PreviewTokens:
    """Short-lived, bounded, single-use server plans, not client-trusted commands."""

    def __init__(self, *, ttl: float = 600, limit: int = 16):
        self.ttl, self.limit = ttl, limit
        self.items: OrderedDict[str, tuple[float, str, Any]] = OrderedDict()

    def put(self, kind: str, plan: Any) -> str:
        token = secrets.token_urlsafe(24)
        self.items[token] = (time.monotonic() + self.ttl, kind, plan)
        while len(self.items) > self.limit:
            self.items.popitem(last=False)
        return token

    def take(self, token: object, kind: str) -> Any:
        if not isinstance(token, str):
            raise ValueError("A fresh preview token is required")
        item = self.items.pop(token, None)
        if item is None or item[0] <= time.monotonic() or item[1] != kind:
            raise ValueError("Preview expired or already used; review a new preview")
        return item[2]


async def bounded_json(request: Request) -> dict[str, Any]:
    payload = bytearray()
    async for chunk in request.stream():
        payload.extend(chunk)
        if len(payload) > 2 * 1024 * 1024:
            raise HTTPException(413, "Request exceeds the 2 MiB limit")
    try:
        result = json.loads(payload)
    except (ValueError, UnicodeError) as exc:
        raise HTTPException(400, "Invalid JSON") from exc
    if not isinstance(result, dict):
        raise HTTPException(400, "Expected a JSON object")
    return result


def local_preflight(
    settings: Settings, service: Any, *, verify_writes: bool = False
) -> dict[str, Any]:
    checks: list[dict[str, str]] = []

    def add(key: str, label: str, status: str, detail: str, tab: str = "general"):
        checks.append(
            dict(id=key, label=label, status=status, detail=detail, settings_tab=tab)
        )

    for name, path, writable in (
        ("data", settings.data_dir, True),
        ("library", settings.library_dir, True),
        ("import", settings.import_dir, False),
        ("torrent_download", settings.torrent_download_dir, False),
        ("usenet_download", settings.usenet_download_dir, False),
    ):
        if path is None:
            continue
        required = os.R_OK | os.X_OK | (os.W_OK if writable else 0)
        accessible = path.is_dir() and os.access(path, required)
        add(
            name,
            f"{name.replace('_', ' ').title()} directory",
            "ok" if accessible else "error",
            "Directory permissions are available"
            if accessible
            else "Directory is missing or permissions are insufficient",
        )
        if accessible:
            if writable and verify_writes:
                try:
                    with tempfile.TemporaryFile(
                        prefix=".tankarr-preflight-", dir=path
                    ) as probe:
                        probe.write(b"Tankarr storage preflight\n")
                        probe.flush()
                        os.fsync(probe.fileno())
                    write_ok = True
                except OSError:
                    write_ok = False
                add(
                    f"{name}_write",
                    f"{name.title()} write check",
                    "ok" if write_ok else "error",
                    "Temporary write and sync verified; probe removed"
                    if write_ok
                    else "Temporary write failed; check the mount and disk",
                )
            free = shutil.disk_usage(path).free
            minimum = int(getattr(settings, "import_disk_reserve_bytes", 512 * 1024**2))
            add(
                f"{name}_space",
                f"{name.title()} free space",
                "ok" if free >= minimum else "error",
                f"{free // (1024**2):,} MiB free; reserve {minimum // (1024**2):,} MiB",
            )
    library = service.library_status()
    if settings.restored_safe_mode:
        add(
            "restored_safe_mode",
            "Restored safe mode",
            "error",
            "Automation and mutations are disabled. Verify this installation, then restart with TANKARR_RESTORED_SAFE_MODE=false.",
        )
    add(
        "library_identity",
        "Library identity",
        "ok" if library.get("available") else "error",
        "Library identity matches"
        if library.get("available")
        else "Library identity is unavailable; check the library mount",
    )
    add(
        "authentication",
        "Authentication",
        "ok" if settings.auth_configured else "warning",
        "Credentials configured"
        if settings.auth_configured
        else "Configure authentication before exposing Tankarr",
    )
    has_sources = (
        settings.suwayomi_enabled
        or settings.prowlarr_enabled
        or settings.internet_archive_enabled
    )
    add(
        "sources",
        "Acquisition sources",
        "ok" if has_sources else "warning",
        "At least one source enabled; run connection checks to verify access"
        if has_sources
        else "No acquisition source is enabled",
        "sources",
    )
    reader = effective_reader_kind(settings)
    add(
        "reader",
        "Reader",
        "warning" if reader == "none" else "ok",
        "No reader selected; files remain portable"
        if reader == "none"
        else f"{reader.title()} selected; connection not yet tested",
        "reader",
    )
    add(
        "backup_destination",
        "Independent backup copy",
        "ok" if getattr(settings, "backup_directory", None) else "warning",
        "Custom backup destination configured; verify it is on an independent device"
        if getattr(settings, "backup_directory", None)
        else "Backups use the data disk; configure an independent destination",
    )
    return {
        "checks": checks,
        "ready": all(c["status"] != "error" for c in checks),
        "connections_checked": False,
    }


def export_list(database: Database) -> dict[str, Any]:
    # Allow-list: never export paths, provider credentials, source URLs or files.
    with database.read_snapshot():
        items = [
            {
                "manga_id": row["id"],
                "provider": row.get("provider") or "catalogue",
                "source_id": row.get("source_id"),
                "title": row["title"],
                "language": row.get("preferred_language") or "en",
                "monitor_mode": row.get("monitor_mode") or "none",
                "series_unit": row.get("series_unit_override") or "automatic",
            }
            for row in database.list_manga()
        ]
    return {"version": 1, "exported_at": datetime.now(UTC).isoformat(), "items": items}


def list_preview(
    request: ListPreview, settings: Settings, database: Database
) -> tuple[list[dict], list[AddMangaRequest]]:
    existing = export_list(database)["items"]
    by_id = {str(row["manga_id"]) for row in existing}
    identities = {
        (row["provider"], str(row.get("source_id") or "")) for row in existing
    }
    seen: set[tuple[str, str]] = set()
    rows, plan = [], []
    for index, item in enumerate(request.items):
        external = (
            parse_catalogue_id(item.source_id or item.manga_id)
            if item.provider == "catalogue"
            else None
        )
        identity = item.provider, str(external or item.source_id or item.manga_id or "")
        row = {
            "key": str(index),
            "title": item.title or f"Catalogue {external or '?'}",
            "status": "ready",
            "reason": "Will add by catalogue ID, unmonitored; no download will start",
        }
        if item.manga_id in by_id or identity in identities or identity in seen:
            row.update(
                status="existing",
                reason="Already present or duplicated in this list; existing settings will not change",
            )
        elif item.provider != "catalogue":
            row.update(
                status="invalid",
                reason="Only stable MangaBaka catalogue identities can be imported; add other sources explicitly",
            )
        elif not external:
            row.update(
                status="ambiguous",
                reason="A title alone is not an identity; supply a MangaBaka source_id or mb: manga_id",
            )
        elif item.language not in settings.search_language_set:
            row.update(status="invalid", reason="Language is not enabled in Settings")
        elif item.series_unit not in {"automatic", "chapters", "volumes"}:
            row.update(status="invalid", reason="Unknown acquisition unit")
        else:
            plan.append(
                AddMangaRequest(
                    manga_id=f"mb:{external}",
                    provider="catalogue",
                    language=item.language,
                    monitor_mode="none",
                    search_now=False,
                    series_unit=item.series_unit,
                )
            )
        seen.add(identity)
        rows.append(row)
    return rows, plan


def acquisition_preview(
    settings: Settings, database: Database, manga_id: str, preferences: dict
) -> dict[str, Any]:
    candidate = preview_settings(settings, preferences, allowed=ACQUISITION_PREFERENCES)
    # A separate read-only view carries hypothetical ranking: never mutate the
    # live Database.source_ranking while a worker is choosing a release.
    view = Database(database.path)
    view.source_ranking = candidate.source_ranking
    view.provider_priority = candidate.provider_priority_order
    view.deprioritised_sources = database.deprioritised_sources
    with view.read_snapshot():
        releases = view.list_all_chapters(manga_id)
        explanations: dict[str, list[str]] = {}
        preferred = {
            str(row["id"]): row
            for row in view.preferred_missing_releases(manga_id, explain=explanations)
        }
    entries = []
    for release in releases:
        release_id = str(release["id"])
        selected = release_id in preferred
        rejections = list(explanations.get(release_id, []))
        if not selected and not rejections:
            rejections.append("Not selected by the current acquisition rules")
        chosen = selected and not rejections
        entries.append(
            {
                "id": release_id,
                "title": str(
                    release.get("title")
                    or release.get("chapter")
                    or release.get("volume")
                    or release_id
                ),
                "source": str(
                    release.get("source_name") or release.get("provider") or ""
                ),
                "selected": chosen,
                "reasons": ["Preferred eligible release for this missing slot"]
                if chosen
                else [],
                "rejections": rejections,
            }
        )
    entries.sort(key=lambda item: (not item["selected"], item["id"]))
    return {
        "candidates": entries[:500],
        "total_candidates": len(entries),
        "truncated": len(entries) > 500,
        "preferences": {
            name: getattr(candidate, name) for name in sorted(ACQUISITION_PREFERENCES)
        },
        "explanation": "Read-only simulation using the same missing-slot selector as automation. No settings or files changed. "
        "The series' explicit unit, language and edition remain authoritative. Global preferred unit is only a display/default preference; "
        "quality upgrades are a separate opt-in pass, not acquisitions shown here.",
    }


def register_operations_routes(
    app: FastAPI,
    *,
    settings: Settings,
    database: Database,
    service: Any,
    add_manga: Callable[[AddMangaRequest], Awaitable[Any]],
    reader_link: Callable[[str], Awaitable[dict]],
    probes: Callable[[], dict[str, Callable[[], Awaitable[dict]]]],
    maintenance: MaintenanceWorker | None = None,
) -> None:
    tokens = PreviewTokens()
    import_lock = asyncio.Lock()
    probe_lock = asyncio.Lock()

    @app.get("/api/system/maintenance")
    async def maintenance_status():
        if maintenance is None:
            raise HTTPException(503, "Maintenance worker is unavailable")
        return maintenance.status()

    @app.post("/api/system/maintenance/repair/apply")
    async def maintenance_repair_apply(request: Request):
        if maintenance is None:
            raise HTTPException(503, "Maintenance worker is unavailable")
        body = await bounded_json(request)
        try:
            result = await maintenance.apply_repair(body.get("revision"))
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {
            "applied": True,
            "warnings": [
                str(item)[:1000] for item in (result.get("warnings") or [])[:20]
            ],
        }

    def download(payload: dict, filename: str):
        return JSONResponse(
            payload,
            headers={
                "Cache-Control": "no-store",
                "Content-Disposition": f'attachment; filename="{filename}"',
            },
        )

    @app.get("/api/system/preflight")
    async def preflight():
        return await asyncio.to_thread(local_preflight, settings, service)

    @app.post("/api/system/preflight")
    async def check_connections():
        if probe_lock.locked():
            raise HTTPException(409, "Connection checks are already running")
        async with probe_lock:
            report = await asyncio.to_thread(
                local_preflight, settings, service, verify_writes=True
            )
            semaphore = asyncio.Semaphore(3)

            async def probe(name: str, check: Callable):
                async with semaphore:
                    tab = (
                        "reader"
                        if name.casefold() == "reader"
                        else "indexers"
                        if name.casefold()
                        in {"prowlarr", "internet archive", "qbittorrent", "sabnzbd"}
                        else "sources"
                    )
                    try:
                        result = await asyncio.wait_for(check(), timeout=20)
                        okay = (result or {}).get("ok", True)
                        return dict(
                            id=f"connection_{name}",
                            label=f"{name} connection",
                            status="ok" if okay else "error",
                            detail="Connection verified"
                            if okay
                            else "Connection failed; review its Settings test",
                            settings_tab=tab,
                        )
                    except Exception as exc:  # noqa: BLE001 - each failed probe is independent
                        return dict(
                            id=f"connection_{name}",
                            label=f"{name} connection",
                            status="error",
                            detail=f"Connection failed ({type(exc).__name__}); review its Settings test",
                            settings_tab=tab,
                        )

            report["checks"].extend(
                await asyncio.gather(
                    *(probe(name, check) for name, check in probes().items())
                )
            )
            report["connections_checked"] = True
            report["ready"] = all(
                item["status"] != "error" for item in report["checks"]
            )
            return report

    @app.get("/api/system/diagnostics/export")
    async def diagnostics():
        def render():
            # Deliberately structured instead of best-effort regex redaction of
            # logs. Provider errors may embed API keys, URLs and private titles.
            checks = local_preflight(settings, service)["checks"]
            with database.connect() as connection:
                counts = {
                    name: connection.execute(
                        f"SELECT COUNT(*) FROM {table}"
                    ).fetchone()[0]
                    for name, table in (
                        ("series", "manga"),
                        ("releases", "chapter_release"),
                        ("jobs", "download_job"),
                    )
                }
            return {
                "version": 1,
                "tankarr_version": __version__,
                "created_at": datetime.now(UTC).isoformat(),
                "platform": {
                    "system": platform.system(),
                    "machine": platform.machine(),
                    "python": platform.python_version(),
                },
                "counts": counts,
                "checks": [
                    {key: check[key] for key in ("id", "status")} for check in checks
                ],
                "redaction": "Allow-listed aggregate data only. No credentials, URLs, paths, titles, logs or file contents.",
            }

        return download(await asyncio.to_thread(render), "tankarr-diagnostics.json")

    @app.get("/api/library/list/export")
    async def library_export():
        return download(
            await asyncio.to_thread(export_list, database), "tankarr-list.json"
        )

    @app.post("/api/library/list/preview")
    async def preview_import(request: Request):
        try:
            submitted = ListPreview.model_validate(await bounded_json(request))
            rows, plan = await asyncio.to_thread(
                list_preview, submitted, settings, database
            )
        except (ValueError, ValidationError) as exc:
            raise HTTPException(
                400, "Invalid list: use version 1 with at most 500 well-formed items"
            ) from exc
        return {
            "items": rows,
            "token": tokens.put("list", plan) if plan else None,
            "notice": "New series are imported unmonitored. Existing series are unchanged. No download will start.",
        }

    @app.post("/api/library/list/import")
    async def import_list(request: Request):
        body = await bounded_json(request)
        try:
            plan = tokens.take(body.get("token"), "list")
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        imported = skipped = 0
        errors = []
        async with import_lock:
            for item in plan:
                current, _ = await asyncio.to_thread(
                    list_preview,
                    ListPreview(
                        items=[
                            ListItem(
                                title="", manga_id=item.manga_id, language=item.language
                            )
                        ]
                    ),
                    settings,
                    database,
                )
                if current[0]["status"] == "existing":
                    skipped += 1
                    continue
                if current[0]["status"] != "ready":
                    errors.append(
                        f"{item.manga_id}: configuration changed; review a new preview"
                    )
                    continue
                try:
                    await asyncio.wait_for(add_manga(item), timeout=60)
                    imported += 1
                except Exception as exc:  # noqa: BLE001 - never hide partially completed imports
                    errors.append(
                        f"{item.manga_id}: {type(exc).__name__}; check the series before retrying"
                    )
        return {"imported": imported, "skipped": skipped, "errors": errors}

    @app.post("/api/acquisition/preview")
    async def preview_acquisition(request: Request):
        body = await bounded_json(request)
        if not isinstance(body.get("manga_id"), str) or not isinstance(
            body.get("preferences", {}), dict
        ):
            raise HTTPException(400, "A manga_id and preferences object are required")
        try:
            return await asyncio.to_thread(
                acquisition_preview,
                settings,
                database,
                body["manga_id"],
                body.get("preferences", {}),
            )
        except KeyError as exc:
            raise HTTPException(404, "Series not found") from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/api/system/reader/alignment")
    async def reader_alignment(manga_id: str):
        result = await reader_link(manga_id)
        count = result.get("managed_books")
        if count is None:
            count = len(
                await asyncio.to_thread(
                    _managed_library_books, settings, database, manga_id
                )
            )
        return {
            **result,
            "managed_books": count,
            "unmatched_books": max(0, count - int(result.get("matched_books") or 0)),
            "match_method": result.get(
                "match_method",
                "managed_path" if result.get("matched_books") else "unverified",
            ),
        }

    @app.post("/api/library/repair/preview")
    async def repair_preview():
        result = guided_repair_report(await service.organize_library(dry_run=True))
        snapshot = result["snapshot"]
        return {
            "actions": result["actions"],
            "warnings": result["warnings"],
            "token": tokens.put("repair", snapshot) if snapshot else None,
        }

    @app.post("/api/library/repair/apply")
    async def repair_apply(request: Request):
        body = await bounded_json(request)
        try:
            snapshot = tokens.take(body.get("token"), "repair")
            result = await service.organize_library(confirmation_snapshot=snapshot)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        if result.get("organization_blocked") or result.get("deferred"):
            raise HTTPException(
                409, "Library changed or repair is blocked; review a new preview"
            )
        return result
