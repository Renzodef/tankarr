from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from tankarr.app import create_app
from tankarr.config import Settings
from tankarr.metrics import CONTENT_TYPE


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "library",
        monitor_enabled=False,
        metadata_enabled=False,
        update_check_enabled=False,
        **overrides,
    )


def _manga(identifier: str, monitored: bool) -> dict:
    return {
        "id": identifier,
        "provider": "local",
        "title": f"Work {identifier}",
        "description": "",
        "authors": [],
        "original_language": "ja",
        "status": "ongoing",
        "available_languages": ["en"],
        "monitored": monitored,
    }


def test_metrics_expose_the_library_counts_and_task_state(tmp_path: Path):
    settings = _settings(tmp_path, auth_required=False)
    app = create_app(settings)
    database = app.state.database
    database.upsert_manga(_manga("one", True), "en", "all")
    database.upsert_manga(_manga("two", True), "en", "none")
    database.configure_monitor_mode("two", "none")
    database.upsert_chapters(
        "one",
        [
            {
                "id": f"one-{index}",
                "chapter": str(index),
                "volume": None,
                "title": f"Chapter {index}",
                "language": "en",
                "provider": "local",
                "groups": [],
                "pages": 20,
                "publish_at": "2026-01-01T00:00:00Z",
                "source_url": "",
            }
            for index in range(1, 4)
        ],
    )
    database.mark_chapter_downloaded("one-1", str(settings.library_dir / "one.cbz"))

    with TestClient(app) as client:
        response = client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"] == CONTENT_TYPE
    body = response.text
    assert 'tankarr_info{version="' in body
    assert "tankarr_series_total 2" in body
    assert "tankarr_releases_total 3" in body
    assert "tankarr_releases_downloaded 1" in body
    assert 'tankarr_task_running{task="release_monitor"} 0' in body
    assert 'tankarr_task_last_run_timestamp_seconds{task="update_check"} 0' in body
    assert 'tankarr_disk_free_bytes{volume="library"}' in body
    assert "tankarr_update_available 0" in body
    assert "# TYPE tankarr_download_jobs gauge" in body


def test_metrics_accept_the_api_key_as_a_bearer_token(tmp_path: Path):
    settings = _settings(
        tmp_path,
        auth_method="forms",
        auth_username="fixture-user",
        auth_password="correct horse battery staple",
    )
    client = TestClient(create_app(settings))
    key = (settings.data_dir / "api-key").read_text(encoding="utf-8").strip()

    assert client.get("/metrics").status_code == 401
    scraped = client.get("/metrics", headers={"Authorization": f"Bearer {key}"})
    assert scraped.status_code == 200
    assert "tankarr_series_total 0" in scraped.text
    assert (
        client.get(
            "/metrics", auth=("fixture-user", "correct horse battery staple")
        ).status_code
        == 200
    )
    for _ in range(5):
        wrong = client.get("/metrics", headers={"Authorization": "Bearer " + "0" * 64})
        assert wrong.status_code == 401
    throttled = client.get("/metrics", headers={"Authorization": "Bearer " + "0" * 64})
    assert throttled.status_code == 429
    client.close()
