from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tankarr.config import Settings
from tankarr.database import Database
from tankarr.operations import (
    ListPreview,
    PreviewTokens,
    acquisition_preview,
    export_list,
    list_preview,
    register_operations_routes,
)
from tankarr.read_model_cache import SnapshotValidators


@pytest.fixture
def context(tmp_path):
    settings = Settings(
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "library",
        internet_archive_enabled=False,
        auth_username="private-user",
        auth_password="private-secret",
    )
    settings.ensure_directories()
    database = Database(settings.database_path)
    database.initialize()
    database.upsert_manga(
        {
            "id": "mb:1",
            "source_id": "1",
            "provider": "catalogue",
            "title": "Private Title",
            "authors": [],
        },
        "en",
        "all",
    )
    return settings, database


def test_list_preview_never_guesses_titles_or_reimports(context):
    settings, database = context
    request = ListPreview.model_validate(
        {
            "items": [
                {"manga_id": "mb:1", "title": "Existing"},
                {"source_id": "2", "title": "New", "monitor_mode": "all"},
                {"source_id": "2", "title": "Duplicate"},
                {"title": "Ambiguous"},
                {
                    "provider": "local",
                    "source_id": "file:///etc/passwd",
                    "title": "Unsupported",
                },
                {"source_id": "3", "language": "it"},
            ]
        }
    )
    rows, plan = list_preview(request, settings, database)
    assert [row["status"] for row in rows] == [
        "existing",
        "ready",
        "existing",
        "ambiguous",
        "invalid",
        "invalid",
    ]
    assert len(plan) == 1
    assert (
        plan[0].manga_id == "mb:2"
        and plan[0].monitor_mode == "none"
        and plan[0].search_now is False
    )
    assert len(database.list_manga()) == 1
    exported = export_list(database)
    assert exported["version"] == 1
    assert set(exported["items"][0]) == {
        "manga_id",
        "source_id",
        "provider",
        "title",
        "language",
        "monitor_mode",
        "series_unit",
    }


def test_tokens_are_bounded_expiring_single_use():
    tokens = PreviewTokens(limit=1)
    old = tokens.put("list", [1])
    fresh = tokens.put("list", [2])
    with pytest.raises(ValueError):
        tokens.take(old, "list")
    assert tokens.take(fresh, "list") == [2]
    with pytest.raises(ValueError):
        tokens.take(fresh, "list")
    tokens.ttl = -1
    with pytest.raises(ValueError):
        tokens.take(tokens.put("repair", 1), "repair")


def workflow_app(context, *, probes=None):
    settings, database = context
    add = AsyncMock(return_value={})
    service = SimpleNamespace(
        library_status=lambda: {"available": True},
        organize_library=AsyncMock(
            return_value={"snapshot": "snapshot", "actions": [], "warnings": []}
        ),
    )
    app = FastAPI()
    register_operations_routes(
        app,
        settings=settings,
        database=database,
        service=service,
        add_manga=add,
        reader_link=AsyncMock(return_value={"available": False}),
        probes=lambda: probes or {},
    )
    return app, add, service


def test_import_requires_preview_and_never_downloads(context):
    app, add, _ = workflow_app(context)
    with TestClient(app) as client:
        assert (
            client.post(
                "/api/library/list/import", json={"token": "fabricated"}
            ).status_code
            == 409
        )
        preview = client.post(
            "/api/library/list/preview",
            json={"items": [{"source_id": "2", "title": "New", "monitor_mode": "all"}]},
        ).json()
        assert not add.called
        imported = client.post(
            "/api/library/list/import", json={"token": preview["token"]}
        )
        assert imported.json() == {"imported": 1, "skipped": 0, "errors": []}
        assert add.call_args.args[0].monitor_mode == "none"
        assert add.call_args.args[0].search_now is False
        assert (
            client.post(
                "/api/library/list/import", json={"token": preview["token"]}
            ).status_code
            == 409
        )


def test_diagnostics_export_has_no_private_values(context):
    app, _, _ = workflow_app(context)
    settings, _ = context
    with TestClient(app) as client:
        report = client.get("/api/system/diagnostics/export")
    assert report.status_code == 200
    assert "attachment" in report.headers["content-disposition"]
    for secret in (
        "private-user",
        "private-secret",
        "Private Title",
        str(settings.data_dir),
        str(settings.library_dir),
    ):
        assert secret not in report.text
    assert report.json()["counts"]["series"] == 1


def test_preflight_keeps_checks_independent_and_redacts_errors(context):
    failure = AsyncMock(side_effect=ValueError("https://private.test/?apiKey=secret"))
    okay = AsyncMock(return_value={"ok": True})
    app, _, _ = workflow_app(context, probes={"a": failure, "b": okay})
    with TestClient(app) as client:
        local = client.get("/api/system/preflight")
        assert local.json()["connections_checked"] is False
        assert not failure.called
        report = client.post("/api/system/preflight").json()
    statuses = {row["id"]: row["status"] for row in report["checks"]}
    assert statuses["connection_a"] == "error" and statuses["connection_b"] == "ok"
    assert report["ready"] is False
    assert "private.test" not in str(report) and "apiKey" not in str(report)


def test_preview_rejects_wrong_shape_and_oversize(context):
    app, _, _ = workflow_app(context)
    with TestClient(app) as client:
        assert client.post("/api/library/list/preview", json=[]).status_code == 400
        assert (
            client.post(
                "/api/library/list/preview", json={"version": 2, "items": []}
            ).status_code
            == 400
        )
        assert (
            client.post(
                "/api/library/list/preview", content=b"x" * (2 * 1024 * 1024 + 1)
            ).status_code
            == 413
        )
        assert (
            client.post(
                "/api/library/list/preview", json={"items": [{}] * 501}
            ).status_code
            == 400
        )


def test_repair_apply_uses_server_snapshot_only(context):
    app, _, service = workflow_app(context)
    with TestClient(app) as client:
        preview = client.post("/api/library/repair/preview").json()
        assert (
            client.post(
                "/api/library/repair/apply",
                json={"token": preview["token"], "snapshot": "forged"},
            ).status_code
            == 200
        )
        assert service.organize_library.call_args.kwargs == {
            "confirmation_snapshot": "snapshot"
        }
        assert (
            client.post(
                "/api/library/repair/apply", json={"token": preview["token"]}
            ).status_code
            == 409
        )


def test_acquisition_preview_uses_isolated_ranking_and_preserves_settings(context):
    settings, database = context
    ranking = database.source_ranking
    original_overrides = database.get_setting_overrides()
    preview = acquisition_preview(
        settings, database, "mb:1", {"release_acquisition_policy": "first_available"}
    )
    assert preview["candidates"] == []
    assert preview["preferences"]["release_acquisition_policy"] == "first_available"
    assert database.source_ranking is ranking
    assert settings.release_acquisition_policy is None
    assert database.get_setting_overrides() == original_overrides
    with pytest.raises(ValueError):
        acquisition_preview(settings, database, "mb:1", {"auth_password": "new"})


def test_snapshot_validators_hash_immutable_payload_once(monkeypatch):
    import tankarr.read_model_cache as module

    real_hash = module.hashlib.sha256
    calls = []

    def observed(payload):
        calls.append(payload)
        return real_hash(payload)

    monkeypatch.setattr(module.hashlib, "sha256", observed)
    validators = SnapshotValidators(limit=2)
    payload = b"large snapshot"
    first = validators.etag("library", payload)
    assert validators.etag("library", payload) == first
    assert len(calls) == 1
    validators.etag("wanted", b"one")
    validators.etag("wanted", b"two")
    assert len(validators.entries) == 2


def test_restored_safe_mode_keeps_api_open_but_never_starts_workers(tmp_path):
    from tankarr.app import create_app

    settings = Settings(
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "library",
        restored_safe_mode=True,
    )
    app = create_app(settings)
    with TestClient(app) as client:
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/system/health").json()["restored_safe_mode"] is True
        assert client.get("/api/ready").status_code == 503
        assert app.state.worker.task is None and app.state.monitor.task is None
        assert client.post("/api/monitor/run").status_code == 409
        assert (
            client.post("/api/library/list/import", json={"token": "x"}).status_code
            == 409
        )
        assert (
            client.post(
                "/api/acquisition/preview", json={"manga_id": "missing"}
            ).status_code
            == 404
        )
        assert client.get("/api/system/diagnostics/export").status_code == 200


def test_backup_verification_reports_truncation_as_validation_error(tmp_path):
    from tankarr.app import create_app

    settings = Settings(
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "library",
        monitor_enabled=False,
    )
    app = create_app(settings)
    directory = settings.data_dir / "backups"
    directory.mkdir()
    (directory / "tankarr-truncated.zip").write_bytes(b"PK truncated archive")
    with TestClient(app) as client:
        response = client.post("/api/system/backups/tankarr-truncated.zip/verify")
        assert response.status_code == 409
        assert response.json()["detail"] == "Backup is missing or failed validation"


def test_preflight_connections_link_to_their_configuration_step(context):
    app, _, _ = workflow_app(
        context,
        probes={
            "Reader": AsyncMock(side_effect=OSError("unavailable")),
            "Prowlarr": AsyncMock(return_value={"ok": True}),
            "Internet Archive": AsyncMock(return_value={"ok": True}),
            "qBittorrent": AsyncMock(return_value={"ok": True}),
            "SABnzbd": AsyncMock(return_value={"ok": True}),
            "suwayomi": AsyncMock(return_value={"ok": True}),
        },
    )
    with TestClient(app) as client:
        report = client.post("/api/system/preflight").json()
    links = {
        row["id"]: row["settings_tab"]
        for row in report["checks"]
        if row["id"].startswith("connection_")
    }
    assert links == {
        "connection_Reader": "reader",
        "connection_Prowlarr": "indexers",
        "connection_Internet Archive": "indexers",
        "connection_qBittorrent": "indexers",
        "connection_SABnzbd": "indexers",
        "connection_suwayomi": "sources",
    }
