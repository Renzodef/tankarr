from __future__ import annotations

import io
import zipfile
from unittest.mock import AsyncMock, Mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from tankarr.translation_routes import register_translation_routes
from tests.test_translation import translation as translation


def client_for(manager):
    app = FastAPI()
    register_translation_routes(
        app, manager, Mock(search_wanted_series=AsyncMock(return_value={}))
    )
    return TestClient(app)


def source_cbz():
    image = io.BytesIO()
    Image.new("RGB", (600, 900), "white").save(image, format="PNG")
    result = io.BytesIO()
    with zipfile.ZipFile(result, "w") as archive:
        archive.writestr("0001.png", image.getvalue())
    return result.getvalue()


def test_book_upload_stages_and_never_publishes_source(translation):
    manager, database, _service, _source = translation
    client = client_for(manager)
    response = client.post(
        "/api/manga/manga-1/translations/upload?source_language=ja&volume=2",
        content=source_cbz(),
    )
    assert response.status_code == 200, response.text
    job = response.json()
    assert job["slot_key"] == "volume:2"
    assert (manager.directory(job) / "source.cbz").exists()
    assert all(not item["downloaded"] for item in database.list_all_chapters("manga-1"))
    duplicate = client.post(
        "/api/manga/manga-1/translations/upload?source_language=ja&volume=2",
        content=source_cbz(),
    )
    assert duplicate.status_code == 409
    assert (
        client.post(f"/api/translations/{job['id']}/cancel").json()["status"]
        == "cancelled"
    )
    assert client.post(f"/api/translations/{job['id']}/retry").json()["attempts"] == 1


def test_upload_rejects_disabled_same_language_and_missing_number(translation):
    manager, database, _service, _source = translation
    client = client_for(manager)
    for query in ("source_language=en&volume=1", "source_language=ja"):
        assert (
            client.post(
                "/api/manga/manga-1/translations/upload?" + query, content=source_cbz()
            ).status_code
            == 400
        )
    database.update_manga("manga-1", {"translation_enabled": False})
    assert (
        client.post(
            "/api/manga/manga-1/translations/upload?source_language=ja&volume=1",
            content=source_cbz(),
        ).status_code
        == 400
    )
    assert manager.store.list() == []


def test_queue_read_does_not_expose_provider_keys(translation):
    manager, _database, _service, source = translation
    manager.store.create("manga-1", source, "en")
    response = client_for(manager).get("/api/translations")
    assert response.status_code == 200
    assert "ai-test-secret" not in response.text
    assert "processor-secret" not in response.text
