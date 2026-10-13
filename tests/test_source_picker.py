from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from tankarr.app import create_app
from tankarr.config import Settings
from tankarr.database import Database
from tests.test_deletion import chapter
from tests.test_release_sources import seed


class SourceProvider:
    name = "suwayomi"
    label = "Fixture source"
    languages = ["en"]

    def supports_language(self, language):
        return language == "en"

    async def aclose(self):
        pass

    async def search_with_diagnostics(self, query, language, limit=20):
        return [await self.get_manga("source-entry")], []

    async def get_manga(self, _identifier):
        return {
            "id": "source-entry",
            "title": "Fixture work",
            "source_id": "101",
            "source_name": "Fixture source",
            "available_languages": self.languages,
        }

    async def list_chapters(self, _identifier, language):
        return [
            {
                **chapter("fixture-chapter", "1"),
                "provider": "suwayomi",
                "language": language,
                "source_key": "suwayomi:101",
                "source_name": "Fixture source",
            }
        ]


def app_at(tmp_path):
    settings = Settings(
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "library",
        monitor_enabled=False,
        metadata_enabled=False,
        auth_required=False,
    )
    app = create_app(settings)
    seed(app.state.database)
    app.state.database.update_manga("origin-id", {"monitor_mode": "none"})
    app.state.providers["suwayomi"] = SourceProvider()
    return app


def test_source_picker_searches_maps_and_remembers_series_preference(tmp_path):
    app = app_at(tmp_path)
    with TestClient(app) as client:
        found = client.get("/api/manga/origin-id/release-source-candidates?q=Fixture")
        assert found.status_code == 200
        assert found.json()["candidates"][0]["source_name"] == "Fixture source"
        selected = client.post(
            "/api/manga/origin-id/release-sources",
            json={
                "provider": "suwayomi",
                "provider_manga_id": "source-entry",
                "prefer": True,
            },
        )
        assert selected.status_code == 201
        assert (
            app.state.database.get_manga("origin-id")["preferred_source_key"]
            == "suwayomi:101"
        )
        mapping = app.state.database.list_release_sources("origin-id")[0]
        assert mapping["verified_by"] == "manual"
        restarted = Database(app.state.settings.database_path)
        restarted.initialize()
        assert (
            restarted.get_manga("origin-id")["preferred_source_key"] == "suwayomi:101"
        )
        restarted.close()
        assert (
            client.delete("/api/manga/origin-id/release-source-preference").status_code
            == 200
        )
        assert app.state.database.get_manga("origin-id")["preferred_source_key"] is None
        assert len(app.state.database.list_release_sources("origin-id")) == 1


def test_source_picker_rejects_a_source_in_another_language(tmp_path):
    app = app_at(tmp_path)
    app.state.providers["suwayomi"].languages = ["fr"]
    with TestClient(app) as client:
        assert (
            client.get(
                "/api/manga/origin-id/release-source-candidates?q=Fixture"
            ).json()["candidates"]
            == []
        )
        response = client.post(
            "/api/manga/origin-id/release-sources",
            json={
                "provider": "suwayomi",
                "provider_manga_id": "source-entry",
                "prefer": True,
            },
        )
        assert response.status_code == 409
        assert not app.state.database.list_release_sources("origin-id")
        assert app.state.database.get_manga("origin-id")["preferred_source_key"] is None


@pytest.mark.parametrize(
    ("error_type", "message"),
    [
        (RuntimeError, "Source search failed. Try again later."),
        (TimeoutError, "Source search timed out. Try again later."),
    ],
)
def test_source_picker_keeps_partial_results_without_exposing_provider_errors(
    tmp_path, caplog, error_type, message
):
    app = app_at(tmp_path)
    broken = SourceProvider()
    private_detail = (
        "fixture-private-detail: SELECT * FROM credentials; "
        "https://fixture-user:fixture-password@nas.local/search?apikey=fixture-key"
    )
    broken.search_with_diagnostics = AsyncMock(side_effect=error_type(private_detail))
    app.state.providers["unavailable"] = broken
    with TestClient(app) as client:
        response = client.get(
            "/api/manga/origin-id/release-source-candidates?q=Fixture"
        )
        assert response.status_code == 200
        assert response.json()["candidates"][0]["provider"] == "suwayomi"
        assert response.json()["errors"] == [
            {"provider": "unavailable", "error": message}
        ]
        for sensitive in (
            "fixture-private-detail",
            "SELECT * FROM credentials",
            "fixture-password",
            "fixture-key",
        ):
            assert sensitive not in response.text
            assert sensitive not in caplog.text
