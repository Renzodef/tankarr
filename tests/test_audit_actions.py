from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import threading
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tankarr.audit_actions import AuditActions, register_audit_routes
from tankarr.auth import AuthenticationManager, AuthenticationMiddleware
from tankarr.database import ActiveDownloadJobsError
from tankarr.series_audit import StaleAudit
from tests.test_deletion import chapter, make_service, manga


@pytest.fixture
def context(tmp_path):
    database, service, reader = make_service(tmp_path)
    database.upsert_manga(manga(), "en", "all")
    app = FastAPI()
    audit = register_audit_routes(app, database, service)
    return database, service, audit, AuditActions(audit), app, reader


def add(
    context,
    identifier="one",
    *,
    source="Alpha",
    provider="fixture",
    language="en",
    downloaded=True,
    ledger=False,
):
    database, service, *_ = context
    release = {
        **chapter(identifier, identifier, language=language),
        "provider": provider,
        "source_name": source,
        "release_unit": "chapter",
    }
    database.upsert_chapters("manga-1", [release])
    path = service.settings.library_dir / f"{identifier}.cbz"
    if downloaded:
        payload = identifier.encode() + b" archive fixture"
        path.write_bytes(payload)
        database.mark_chapter_downloaded(
            identifier, path, hashlib.sha256(payload).hexdigest() if ledger else None
        )
    return path


def source(
    context, identifier="alpha", *, name="Alpha", language="en", provider="fixture"
):
    context[0].upsert_release_source(
        "manga-1",
        provider=provider,
        provider_manga_id=identifier,
        title="Example",
        source_url="https://example.test/source",
        source_name=name,
        language=language,
        match_confidence=1,
        match_reason="fixture",
    )
    return {"provider": provider, "provider_manga_id": identifier}


def test_api_selected_retirement_requires_preview_and_only_recycles_selection(context):
    selected, other = add(context, "one", ledger=True), add(context, "two")
    database, service, _audit, _actions, app, reader = context
    with TestClient(app) as client:
        report = client.get("/api/manga/manga-1/audit").json()
        body = {"chapter_ids": ["one"], "revision": report["revision"]}
        endpoint = "/api/manga/manga-1/audit/retire"
        assert client.post(endpoint, json=body).status_code == 409
        review = client.post(endpoint, json=body, params={"dry_run": True})
        assert review.status_code == 200 and selected.exists()
        confirmed = client.post(
            endpoint,
            json=body,
            params={"confirmation_snapshot": review.json()["confirmation_snapshot"]},
        )
        assert confirmed.status_code == 200
        result = confirmed.json()
    assert result["files_retired"] == 1 and result["files_deleted"] == 0
    assert result["chapters_reset"] == 1 and result["source_removed"] is False
    assert not selected.exists() and other.exists() and reader.calls == 1
    assert not database.get_chapter("one")["downloaded"]
    assert database.get_chapter("two")["downloaded"]
    retired = next(service.settings.library_dir.glob(".tankarr-delete-*"))
    assert next(retired.glob("*.quarantined")).read_bytes() == b"one archive fixture"
    assert (
        json.loads((retired / "manifest.json").read_text())["disposition"] == "retain"
    )


@pytest.mark.parametrize("change", ["file", "catalogue", "new_source_offer"])
async def test_confirmation_rejects_changed_review(context, change):
    path = add(context)
    mapping = source(context)
    database, _service, _audit, actions, *_ = context
    review = actions.preview("manga-1", source=mapping)
    if change == "file":
        path.write_bytes(b"changed file")
    elif change == "catalogue":
        database.upsert_manga({**manga(), "title": "Changed title"}, "en", "all")
    else:
        add(context, "new", downloaded=False)
    with pytest.raises(StaleAudit):
        await actions.apply(
            "manga-1",
            source=mapping,
            confirmation_snapshot=review["confirmation_snapshot"],
        )
    assert path.exists() and database.get_chapter("one")["downloaded"]
    assert database.list_deletion_operations() == []


async def test_source_rejection_includes_old_language_but_not_other_sources(context):
    italian = add(context, "italian", language="it")
    english = add(context, "english")
    other_provider = add(context, "other-provider", language="it", provider="other")
    beta = add(context, "beta", source="Beta", language="it")
    add(context, "italian-offer", language="it", downloaded=False)
    add(context, "english-offer", downloaded=False)
    add(context, "other-offer", language="it", provider="other", downloaded=False)
    mapping = source(context, "alpha-it", language="it")
    source(context, "alpha-en")
    source(context, "beta", name="Beta", language="it")
    database, _service, _audit, actions, *_ = context
    with database.connect() as connection:
        connection.execute(
            "UPDATE manga_release_source SET enabled=0 WHERE provider_manga_id='alpha-it'"
        )
    review = actions.preview("manga-1", source=mapping)
    assert review["chapter_ids"] == ["italian"]
    assert review["releases_to_remove"] == 2
    result = await actions.apply(
        "manga-1", source=mapping, confirmation_snapshot=review["confirmation_snapshot"]
    )
    assert result["files_retired"] == 1 and result["releases_removed"] == 2
    assert result["source_removed"] and not italian.exists()
    assert english.exists() and other_provider.exists() and beta.exists()
    assert {row["id"] for row in database.list_all_chapters("manga-1")} == {
        "english",
        "other-provider",
        "beta",
        "english-offer",
        "other-offer",
    }
    assert ("fixture", "alpha-it") in database.release_source_rejections("manga-1")
    assert {
        row["provider_manga_id"] for row in database.list_release_sources("manga-1")
    } == {"alpha-en", "beta"}


async def test_source_transaction_failure_rolls_back_all_database_changes_and_files(
    context, monkeypatch
):
    path = add(context)
    add(context, "offer", downloaded=False)
    mapping = source(context)
    database, service, _audit, actions, *_ = context
    review = actions.preview("manga-1", source=mapping)

    def fail(*_args):
        raise sqlite3.OperationalError("fixture reject write failure")

    monkeypatch.setattr(database, "add_release_source_rejection", fail)
    with pytest.raises(sqlite3.OperationalError):
        await actions.apply(
            "manga-1",
            source=mapping,
            confirmation_snapshot=review["confirmation_snapshot"],
        )
    assert path.read_bytes() == b"one archive fixture"
    assert database.get_chapter("one")["downloaded"]
    assert database.get_chapter("offer") and database.list_release_sources("manga-1")
    assert database.list_deletion_operations() == []
    assert list(service.settings.library_dir.glob(".tankarr-delete-*")) == []
    service.assert_mutations_allowed()


@pytest.mark.parametrize("ledger", [False, True])
async def test_changed_bytes_during_staging_restore_files_and_preserve_ownership(
    context, monkeypatch, ledger
):
    path = add(context, ledger=ledger)
    database, service, _audit, actions, *_ = context
    review = actions.preview("manga-1", ["one"])
    stage = service._stage_library_files

    def changed(paths, **kwargs):
        staged = stage(paths, **kwargs)
        target = staged.moved[0][1]
        before = target.stat()
        target.write_bytes(b"x" * before.st_size)
        os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
        return staged

    monkeypatch.setattr(service, "_stage_library_files", changed)
    with pytest.raises(StaleAudit, match="bytes changed"):
        await actions.apply(
            "manga-1", ["one"], confirmation_snapshot=review["confirmation_snapshot"]
        )
    assert path.exists() and database.get_chapter("one")["downloaded"]
    assert database.list_deletion_operations() == []


async def test_edit_while_preparing_hash_cannot_be_adopted_as_the_reviewed_file(
    context, monkeypatch
):
    path = add(context)
    database, _service, _audit, actions, *_ = context
    review = actions.preview("manga-1", ["one"])
    original_hash = actions._hash_file

    def replaced(*args, **kwargs):
        digest = original_hash(*args, **kwargs)
        path.write_bytes(b"changed after hashing")
        return digest

    monkeypatch.setattr(actions, "_hash_file", replaced)
    with pytest.raises(StaleAudit):
        await actions.apply(
            "manga-1", ["one"], confirmation_snapshot=review["confirmation_snapshot"]
        )
    assert path.exists() and database.get_chapter("one")["downloaded"]
    assert database.list_deletion_operations() == []


def test_active_download_blocks_preview_before_any_file_moves(context):
    path = add(context)
    database, _service, _audit, _actions, app, _reader = context
    job = database.create_job("manga-1", "one", "en", force=True)
    database.update_job(job["id"], status="downloading")
    with TestClient(app) as client:
        response = client.post(
            "/api/manga/manga-1/audit/retire?dry_run=true",
            json={"chapter_ids": ["one"]},
        )
    assert response.status_code == 409
    assert path.exists() and database.list_deletion_operations() == []


@pytest.mark.parametrize("change", ["catalogue", "job"])
async def test_database_or_job_change_during_staging_restores_files(
    context, monkeypatch, change
):
    path = add(context)
    database, service, _audit, actions, *_ = context
    review = actions.preview("manga-1", ["one"])
    stage = service._stage_library_files

    def changed(*args, **kwargs):
        quarantine = stage(*args, **kwargs)
        if change == "catalogue":
            add(context, "late-offer", downloaded=False)
        else:
            job = database.create_job("manga-1", "one", "en", force=True)
            database.update_job(job["id"], status="downloading")
        return quarantine

    monkeypatch.setattr(service, "_stage_library_files", changed)
    with pytest.raises((StaleAudit, ActiveDownloadJobsError)):
        await actions.apply(
            "manga-1", ["one"], confirmation_snapshot=review["confirmation_snapshot"]
        )
    assert path.exists() and database.get_chapter("one")["downloaded"]
    assert database.list_deletion_operations() == []


def test_source_route_can_reject_only_undownloaded_offers_without_quarantine(context):
    add(context, "offer", downloaded=False)
    mapping = source(context)
    database, service, _audit, _actions, app, reader = context
    endpoint = "/api/manga/manga-1/audit/reject-source"
    with TestClient(app) as client:
        assert client.post(endpoint, json=mapping).status_code == 409
        preview = client.post(endpoint, json=mapping, params={"dry_run": True})
        assert preview.status_code == 200
        response = client.post(
            endpoint,
            json=mapping,
            params={"confirmation_snapshot": preview.json()["confirmation_snapshot"]},
        )
    assert response.status_code == 200
    assert response.json()["files_retired"] == 0
    assert response.json()["releases_removed"] == 1
    assert response.json()["source_removed"]
    assert database.list_all_chapters("manga-1") == []
    assert list(service.settings.library_dir.glob(".tankarr-delete-*")) == []
    assert reader.calls == 0


async def test_cancellation_keeps_mutation_lock_until_staging_and_commit_finish(
    context, monkeypatch
):
    path = add(context)
    database, service, _audit, actions, *_ = context
    review = actions.preview("manga-1", ["one"])
    started, release = threading.Event(), threading.Event()
    stage = service._stage_library_files

    def waiting(*args, **kwargs):
        started.set()
        if not release.wait(5):
            raise RuntimeError("test stage was not released")
        return stage(*args, **kwargs)

    monkeypatch.setattr(service, "_stage_library_files", waiting)
    task = asyncio.create_task(
        actions.apply(
            "manga-1", ["one"], confirmation_snapshot=review["confirmation_snapshot"]
        )
    )
    try:
        assert await asyncio.to_thread(started.wait, 3)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert service._mutation_lock.locked() and not task.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not service._mutation_lock.locked() and not path.exists()
    assert not database.get_chapter("one")["downloaded"]


def test_thumbnail_route_is_authenticated_private_and_revalidates_etag(
    context, tmp_path, monkeypatch
):
    add(context)
    _database, service, audit, _actions, app, _reader = context
    service.settings.auth_method = "basic"
    service.settings.auth_username = "operator"
    service.settings.auth_password = "fixture-password"
    app.add_middleware(
        AuthenticationMiddleware, manager=AuthenticationManager(service.settings)
    )
    image = tmp_path / "thumbnail.webp"
    image.write_bytes(b"fixture webp")
    render = AsyncMock(return_value=(image, "a" * 64))
    monkeypatch.setattr(audit, "thumbnail", render)
    url = "/api/manga/manga-1/audit/files/one/first?revision=" + "b" * 64
    with TestClient(app) as client:
        assert client.get(url).status_code == 401
        render.assert_not_called()
        response = client.get(url, auth=("operator", "fixture-password"))
        assert response.status_code == 200 and response.content == b"fixture webp"
        assert response.headers["cache-control"].startswith("private")
        assert response.headers["etag"] == '"' + "a" * 64 + '"'
        cached = client.get(
            url,
            auth=("operator", "fixture-password"),
            headers={"If-None-Match": response.headers["etag"]},
        )
        assert cached.status_code == 304 and render.await_count == 2
        render.side_effect = StaleAudit("changed")
        stale = client.get(
            url,
            auth=("operator", "fixture-password"),
            headers={"If-None-Match": response.headers["etag"]},
        )
        assert stale.status_code == 409
