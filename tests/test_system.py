from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from tankarr.app import create_app
from tankarr.config import Settings


def provision_library_identity(settings: Settings) -> None:
    identity = "0123456789abcdef0123456789abcdef"
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.library_dir.mkdir(parents=True, exist_ok=True)
    (settings.data_dir / ".tankarr-library-id").write_text(
        f"{identity}\n", encoding="utf-8"
    )
    (settings.library_dir / ".tankarr-library-id").write_text(
        f"{identity}\n", encoding="utf-8"
    )


def test_backup_endpoint_snapshots_the_database(tmp_path: Path):
    settings = Settings(
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "library",
        monitor_enabled=False,
    )
    provision_library_identity(settings)
    with TestClient(create_app(settings)) as client:
        first = client.post("/api/system/backup")
        assert first.status_code == 200
        payload = first.json()
        assert payload["name"].startswith("tankarr-")
        backup_path = settings.data_dir / "backups" / payload["name"]
        assert backup_path.exists()
        assert payload["size"] == backup_path.stat().st_size

        status = client.get("/api/system/status").json()
        assert [item["name"] for item in status["backups"]] == [payload["name"]]
        assert status["backups"][0]["size"] == payload["size"]


def test_source_alert_requires_every_source_down_over_a_day_and_series_still_wanted(
    tmp_path: Path, monkeypatch
):
    from tankarr import app as app_module
    from tests.test_release_sources import FakeProvider, seed

    # Status alerts come from the background snapshot. A short interval lets
    # this test see a write without running the heavy queries on the request.
    monkeypatch.setattr(app_module, "STATUS_SNAPSHOT_SECONDS", 0.05)

    class Provider(FakeProvider):
        async def aclose(self):
            pass

    settings = Settings(
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "library",
        monitor_enabled=False,
    )
    provision_library_identity(settings)
    app = create_app(settings)
    with TestClient(app) as client:
        database = app.state.database
        seed(database)
        with database.connect() as connection:
            connection.execute("UPDATE manga SET provider='catalogue'")
        app.state.providers["alternate"] = Provider([], {})
        for identifier in ("one", "two"):
            database.upsert_release_source(
                "origin-id",
                provider="alternate",
                provider_manga_id=identifier,
                title="V.B. Rose",
                source_url=f"https://example.test/{identifier}",
                source_name="Alternate",
                language="en",
                match_confidence=1.0,
                match_reason="Fixture identity",
                verified_by="manual",
            )
        old = (datetime.now(UTC) - timedelta(hours=25)).isoformat()

        def alerts():
            response = client.get("/api/system/status")
            assert response.status_code == 200
            return [
                item for item in response.json()["alerts"] if item["key"] == "sources"
            ]

        def wait_for(count: int) -> list[dict]:
            deadline = time.monotonic() + 5
            seen = alerts()
            while len(seen) != count and time.monotonic() < deadline:
                time.sleep(0.05)
                seen = alerts()
            return seen

        database.record_release_source_result(
            "origin-id", "alternate", "one", error="No chapters found"
        )
        with database.connect() as connection:
            connection.execute(
                "UPDATE manga_release_source SET error_since=? WHERE provider_manga_id='one'",
                (old,),
            )
        assert wait_for(0) == []  # Another source has not failed.
        database.record_release_source_result(
            "origin-id", "alternate", "two", error="HTTP error 522"
        )
        assert wait_for(0) == []  # This failure is new.
        with database.connect() as connection:
            connection.execute("UPDATE manga_release_source SET error_since=?", (old,))
        result = wait_for(1)
        assert len(result) == 1
        assert (
            result[0]["title"]
            == "1 Wanted series without a working source for over 24 hours"
        )
        assert result[0]["detail"] == "V.B. Rose"
        assert result[0]["href"] == "#/wanted"
        database.configure_monitor_mode("origin-id", "none")
        assert wait_for(0) == []
        database.configure_monitor_mode("origin-id", "all")
        assert len(wait_for(1)) == 1
        database.record_release_source_result("origin-id", "alternate", "two")
        assert wait_for(0) == []  # Recovery clears the alert on the next snapshot.
