from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tankarr.app import create_app
from tankarr.config import Settings
from tests.test_deletion import provision_library_identity


@pytest.mark.parametrize(
    "reader,label",
    [("stump", "Stump"), ("kavita", "Kavita"), ("komga", "Komga"), ("none", "Reader")],
)
def test_status_uses_configured_reader_during_startup_or_failure(
    tmp_path, reader, label
):
    settings = Settings(
        data_dir=tmp_path / "data", library_dir=tmp_path / "library", reader_kind=reader
    )
    provision_library_identity(settings)
    app = create_app(settings)
    # No lifespan: exercise the serializer without starting network workers.
    client = TestClient(app, raise_server_exceptions=True)
    try:
        app.state.service.last_komga_library_alignment = {
            "configured": True,
            "ready": False,
            "initializing": True,
        }
        for endpoint in ("/api/system/health", "/api/system/status"):
            response = client.get(endpoint)
            assert response.status_code == 200
            alignment = response.json()["library_alignment"]
            assert alignment["reader"] == reader
            assert alignment["reader_label"] == label
            assert alignment["ready"] is False
    finally:
        client.close()
