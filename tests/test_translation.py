from __future__ import annotations

import asyncio
import json
import zipfile
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
import respx
from PIL import Image

from tankarr.archive import package_cbz, sha256
from tankarr.chapter_mapping import suspect_volume_reasons
from tankarr.config import Settings
from tankarr.database import Database
from tankarr.service import TankarrService
from tankarr.settings_store import SECRET_PLACEHOLDER, settings_view, update_settings
from tankarr.translation import TranslationManager
from tankarr.translation_policy import fallback_languages, normalize_fallback_languages
from tests.test_deletion import (
    RecordingKomga,
    chapter,
    manga,
    provision_library_identity,
)


@pytest.fixture
def translation(tmp_path):
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "library",
        translation_enabled=True,
        translation_processor_url="http://processor.local",
        translation_processor_token="processor-secret",
        translation_ai_url="https://ai.example.test/v1",
        translation_ai_api_key="ai-test-secret",
        translation_ai_model="example-model",
    )
    provision_library_identity(settings)
    database = Database(settings.database_path)
    database.initialize()
    database.upsert_manga({**manga(), "provider": "catalogue"}, "en", "all")
    database.update_manga("manga-1", {"translation_enabled": True})
    source = {**chapter("source-ja", "1", language="ja"), "provider": "suwayomi"}
    database.upsert_chapters("manga-1", [source], monitor_new=False)
    source = database.get_chapter("source-ja")
    provider = Mock()
    service = TankarrService(
        settings, database, {"suwayomi": provider}, RecordingKomga()
    )
    service._request_komga_reconciliation = AsyncMock(return_value={})
    sources = Mock(discover_and_refresh=AsyncMock(return_value={}))
    manager = TranslationManager(settings, database, service, sources)
    return manager, database, service, source


def stage(manager, source, tmp_path):
    job = manager.store.create("manga-1", source, "en")
    directory = manager.directory(job)
    directory.mkdir(parents=True)
    page = tmp_path / "page.png"
    Image.new("RGB", (600, 900), "white").save(page)
    package_cbz(
        directory / "source.cbz", [page], manager.database.get_manga("manga-1"), source
    )
    return job


def output_archive(manager, job, tmp_path, **changes):
    output = tmp_path / "result.cbz"
    page = tmp_path / "translated.png"
    Image.new("RGB", (600, 900), "gray").save(page)
    receipt = {
        "source_sha256": sha256(manager.directory(job) / "source.cbz"),
        "source_language": "ja",
        "target_language": "en",
        "page_count": 1,
        "translated_regions": 3,
        "untranslated_regions": 0,
        "detected_source_language": "ja",
        "detected_target_language": "en",
        "model": "example-model",
        **changes,
    }
    with zipfile.ZipFile(output, "w") as archive:
        archive.write(page, "00001.png")
        archive.writestr("translation.json", json.dumps(receipt))
    return output


def test_fallback_order_and_unknown_original():
    assert normalize_fallback_languages(" Original,fr,ja,FR ") == "original,fr,ja"
    work = {"preferred_language": "en", "original_language": "ja"}
    assert fallback_languages(work, "original,en,fr,ja") == ["ja", "fr"]
    assert fallback_languages({**work, "original_language": None}, "original") == []
    with pytest.raises(ValueError):
        normalize_fallback_languages("original,xx")


def test_translation_settings_defaults_secrets_and_validation(tmp_path):
    settings = Settings(_env_file=None, data_dir=tmp_path)
    database = Database(settings.database_path)
    database.initialize()
    assert not settings.translation_enabled
    with pytest.raises(ValueError, match="required"):
        update_settings(settings, database, {"translation_enabled": True})
    update_settings(
        settings,
        database,
        {
            "translation_ai_api_key": "hidden-provider-key",
            "translation_processor_token": "hidden-processor-token",
        },
    )
    assert "hidden-provider-key" not in json.dumps(database.get_setting_overrides())
    assert "hidden-provider-key" not in json.dumps(settings_view(settings, database))
    assert "hidden-processor-token" not in repr(settings)
    update_settings(settings, database, {"translation_ai_api_key": SECRET_PLACEHOLDER})
    assert settings.translation_ai_api_key == "hidden-provider-key"
    with pytest.raises(ValueError):
        update_settings(
            settings,
            database,
            {"translation_ai_url": "https://user:secret@example.test"},
        )


def test_local_fallback_only_requires_ai_settings(tmp_path):
    settings = Settings(_env_file=None, data_dir=tmp_path)
    database = Database(settings.database_path)
    database.initialize()
    update_settings(
        settings,
        database,
        {
            "translation_enabled": True,
            "translation_ai_url": "https://provider.example/v1",
            "translation_ai_model": "model",
            "translation_ai_api_key": "private-test-key",
        },
    )
    assert settings.translation_enabled
    assert settings.translation_processor_url is None
    assert settings.translation_processor_token is None


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["cancel", "disable", "stop"])
async def test_local_work_stops_without_publishing_on_interruption(
    translation, tmp_path, monkeypatch, action
):
    manager, _database, _service, source = translation
    manager.settings.translation_processor_url = None
    job = stage(manager, source, tmp_path)
    started, stopped = asyncio.Event(), asyncio.Event()

    async def running(_source, _output, _request, progress):
        progress(0, 2)
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    monkeypatch.setattr("tankarr.translation.translate_archive", running)
    manager._publish = AsyncMock()
    task = asyncio.create_task(manager.process(job))
    await asyncio.wait_for(started.wait(), 2)
    if action == "cancel":
        manager.cancel_job(job)
    elif action == "disable":
        manager.settings.translation_enabled = False
    else:
        task.cancel()
    if action == "stop":
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        await asyncio.wait_for(task, 2)
    assert stopped.is_set()
    manager._publish.assert_not_awaited()
    assert (manager.directory(job) / "source.cbz").exists()


@pytest.mark.asyncio
async def test_local_ocr_provider_and_library_import(translation):
    import shutil

    from tests.test_local_translation import fixture, response

    if not shutil.which("tesseract"):
        pytest.skip("Tesseract is not installed on this test host")
    manager, database, _service, source = translation
    manager.settings.translation_processor_url = None
    manager.settings.translation_ai_url = "https://provider.example/v1"
    database.update_manga("manga-1", {"preferred_language": "it"})
    source = {**source, "id": "source-en", "language": "en"}
    database.upsert_chapters("manga-1", [source], monitor_new=False)
    source = database.get_chapter("source-en")
    job = manager.store.create("manga-1", source, "it")
    manager.directory(job).mkdir(parents=True)
    fixture(manager.directory(job), pages=1)
    with zipfile.ZipFile(manager.directory(job) / "source.cbz", "a") as archive:
        archive.writestr("ComicInfo.xml", "<ComicInfo/>")
    with respx.mock as mock:
        mock.post("https://provider.example/v1/chat/completions").mock(
            side_effect=response
        )
        await manager.process(job)
    saved = manager.store.get(job["id"])
    assert saved["status"] == "completed"
    imported = database.get_chapter(saved["result_chapter_id"])
    assert imported["downloaded"]
    assert imported["language"] == "it"
    assert imported["provider"] == "translated"
    assert not manager.directory(job).exists()


@pytest.mark.asyncio
async def test_disabled_fallback_never_discovers_or_downloads(translation):
    manager, database, service, _source = translation
    database.update_manga("manga-1", {"translation_enabled": False})
    assert await manager.queue_missing("manga-1") == 0
    database.update_manga("manga-1", {"translation_enabled": True})
    manager.settings.translation_enabled = False
    assert await manager.queue_missing("manga-1") == 0
    manager.release_sources.discover_and_refresh.assert_not_called()
    assert manager.store.list() == []


@pytest.mark.asyncio
async def test_native_first_and_foreign_queue_idempotence(translation, monkeypatch):
    manager, database, service, source = translation
    missing = {"chapter": "1", "volume": "1", "expected": True, "provider": "expected"}
    wanted = [
        {
            "manga": {"id": "manga-1"},
            "chapters": [{**missing, "expected": False, "provider": "suwayomi"}],
        }
    ]
    monkeypatch.setattr(service, "list_wanted", lambda: wanted)
    assert await manager.queue_missing("manga-1") == 0
    wanted[0]["chapters"] = [missing]
    assert await manager.queue_missing("manga-1") == 1
    assert await manager.queue_missing("manga-1") == 0
    manager.release_sources.discover_and_refresh.assert_called_with(
        "manga-1", monitor_new=False, language="ja"
    )
    assert database.get_manga("manga-1")["preferred_language"] == "en"
    assert manager.store.list()[0]["source"]["language"] == "ja"
    assert not database.get_chapter(source["id"])["downloaded"]


@pytest.mark.asyncio
async def test_receipt_checked_and_machine_provenance_published(translation, tmp_path):
    manager, database, _service, source = translation
    job = stage(manager, source, tmp_path)
    output = output_archive(manager, job, tmp_path)
    await manager._publish(job, output, manager.directory(job) / "source.cbz")
    result = manager.store.get(job["id"])
    assert result["status"] == "completed"
    imported = database.get_chapter(result["result_chapter_id"])
    assert imported["provider"] == "translated"
    assert imported["language"] == "en"
    assert not database.get_chapter(source["id"])["downloaded"]
    with zipfile.ZipFile(imported["library_path"]) as archive:
        metadata = archive.read("ComicInfo.xml").decode()
    assert "Machine translation ja → en" in metadata
    assert "ai-test-secret" not in metadata


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "receipt",
    [
        {"target_language": "it"},
        {"source_sha256": "0" * 64},
        {"untranslated_regions": 2},
        {"translated_regions": 0},
        {"page_count": 5},
    ],
)
async def test_bad_translation_never_enters_library(translation, tmp_path, receipt):
    manager, database, _service, source = translation
    job = stage(manager, source, tmp_path)
    output = output_archive(manager, job, tmp_path, **receipt)
    with pytest.raises(ValueError):
        await manager._publish(job, output, manager.directory(job) / "source.cbz")
    assert not any(item["downloaded"] for item in database.list_all_chapters("manga-1"))


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["global", "series", "language", "cancelled"])
async def test_disable_or_profile_change_prevents_publication(
    translation, tmp_path, change
):
    manager, database, _service, source = translation
    job = stage(manager, source, tmp_path)
    output = output_archive(manager, job, tmp_path)
    if change == "global":
        manager.settings.translation_enabled = False
    elif change == "series":
        database.update_manga("manga-1", {"translation_enabled": False})
    elif change == "language":
        database.update_manga("manga-1", {"preferred_language": "it"})
    else:
        manager.store.update(job["id"], status="cancelled")
    await manager._publish(job, output, manager.directory(job) / "source.cbz")
    assert not any(item["downloaded"] for item in database.list_all_chapters("manga-1"))


@pytest.mark.asyncio
async def test_processor_protocol_and_offline_retry(translation, tmp_path):
    manager, database, _service, source = translation
    job = stage(manager, source, tmp_path)
    url = f"http://processor.local/jobs/{job['id']}-0"
    with respx.mock as mock:
        get = mock.get(url).mock(
            side_effect=httpx.ConnectError("connection unavailable")
        )
        await manager.tick()
        assert manager.store.get(job["id"])["status"] == "queued"
        assert "retrying" in manager.store.get(job["id"])["message"]
        get.mock(return_value=httpx.Response(404))
        mock.put(url + "/source").respond(200)
        submitted = mock.post(url).respond(202, json={"status": "queued"})
        await manager.tick()
        assert manager.store.get(job["id"])["status"] == "processing"
        assert (
            submitted.calls.last.request.headers["Authorization"]
            == "Bearer processor-secret"
        )
        assert (
            json.loads(submitted.calls.last.request.content)["ai"]["api_key"]
            == "ai-test-secret"
        )
        get.mock(return_value=httpx.Response(200, json={"status": "completed"}))
        mock.get(url + "/result").respond(
            200, content=output_archive(manager, job, tmp_path).read_bytes()
        )
        await manager.tick()
    assert manager.store.get(job["id"])["status"] == "completed"


def test_translated_book_never_retires_human_chapters():
    assert "1" in suspect_volume_reasons(
        [
            {
                "id": "mtl",
                "provider": "translated",
                "downloaded": True,
                "release_unit": "volume",
                "volume": "1",
                "chapter": None,
                "pages": 180,
            }
        ]
    )


def test_job_survives_reopen_without_credentials(translation):
    manager, database, _service, source = translation
    job = manager.store.create("manga-1", {**source, "api_key": "never-save"}, "en")
    database.initialize()
    assert manager.store.get(job["id"])["source"]["id"] == source["id"]
    assert "never-save" not in json.dumps(manager.store.list())


@pytest.mark.asyncio
async def test_remote_cancellation_retries_while_translation_is_disabled(translation):
    manager, _database, _service, source = translation
    job = manager.store.create("manga-1", source, "en")
    manager.cancel_job(job)
    root = manager.settings.staging_dir / "translations" / "cancel-requests"
    pending = next(root.glob("*.json"))
    assert "ai-test-secret" not in pending.read_text()
    manager.settings.translation_enabled = False
    with respx.mock as mock:
        route = mock.delete(f"http://processor.local/jobs/{job['id']}-0")
        route.mock(side_effect=httpx.ConnectError("offline"))
        await manager.tick()
        assert pending.exists()
        route.mock(return_value=httpx.Response(202, json={"status": "cancelling"}))
        await manager.tick()
        assert not pending.exists()
    assert manager.store.get(job["id"])["status"] == "cancelled"


@pytest.mark.asyncio
async def test_cancellation_never_sends_new_credentials_to_old_processor(translation):
    manager, _database, _service, source = translation
    job = manager.store.create("manga-1", source, "en")
    manager.cancel_job(job)
    manager.settings.translation_processor_url = "https://new.example.test"
    with respx.mock:
        await manager.flush_cancellations()
    assert list(
        (manager.settings.staging_dir / "translations" / "cancel-requests").glob(
            "*.json"
        )
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("backlog", ["waiting_native", "completed", "disabled"])
async def test_large_paused_or_finished_backlog_does_not_hide_runnable_jobs(
    translation, backlog
):
    manager, database, _service, source = translation
    database.upsert_manga(
        {**manga(), "id": "disabled-series", "provider": "catalogue"}, "en", "all"
    )
    for index in range(201):
        job = manager.store.create(
            "disabled-series" if backlog == "disabled" else "manga-1",
            {**source, "chapter": str(index + 10), "volume": None},
            "en",
        )
        manager.store.update(
            job["id"], status="queued" if backlog == "disabled" else backlog
        )
    queued = manager.store.create(
        "manga-1", {**source, "chapter": "1000", "volume": None}, "en"
    )
    running = manager.store.create(
        "manga-1", {**source, "chapter": "1001", "volume": None}, "en"
    )
    manager.store.update(running["id"], status="processing")
    manager.process = AsyncMock()
    await manager.tick()
    assert [call.args[0]["id"] for call in manager.process.await_args_list] == [
        running["id"],
        queued["id"],
    ]
    assert {running["id"], queued["id"]} <= {
        job["id"] for job in manager.store.list("manga-1")
    }


@pytest.mark.asyncio
async def test_native_external_release_can_replace_machine_translation(
    translation, tmp_path
):
    from tankarr.archive import package_cbz_validated
    from tankarr.service import local_page_content_sha256

    manager, database, service, source = translation
    job = stage(manager, source, tmp_path)
    await manager._publish(
        job,
        output_archive(manager, job, tmp_path),
        manager.directory(job) / "source.cbz",
    )
    native = {
        **source,
        "id": "native",
        "provider": "manual",
        "language": "en",
        "pages": 1,
    }
    page = tmp_path / "native.png"
    Image.new("RGB", (600, 900), "black").save(page)
    target = tmp_path / "native.cbz"
    _, info = package_cbz_validated(
        target, [page], database.get_manga("manga-1"), native
    )
    async with service._mutation_lock:
        result = service._publish_external_import_locked(
            "manga-1", native, target, info["sha256"], local_page_content_sha256([page])
        )
    assert result["chapter"]["provider"] == "manual"
    assert result["chapter"]["downloaded"]
    assert database.get_chapter(f"translated:{job['id']}")["provider"] == "manual"
