from __future__ import annotations

import asyncio
import sqlite3
from unittest.mock import AsyncMock

import pytest

from tankarr.database import SCHEMA_VERSION
from tankarr.service import PRIMARY_SOURCE_FAILURE_NOTE, RecoveryBlocked
from tests.test_deletion import chapter, make_service, manga


@pytest.fixture
def context(tmp_path):
    database, service, _reader = make_service(tmp_path)
    remote = {
        **manga(),
        "provider": "suwayomi",
        "source_name": "Example Scans",
        "source_id": "7",
    }
    database.upsert_manga(remote, "en", "all")
    return database, service, remote


def mapping(database, *, language="en", source_name="Example Scans"):
    return database.upsert_release_source(
        "manga-1",
        provider="suwayomi",
        provider_manga_id="remote-1",
        title="Example",
        source_url="https://example.test/series/one",
        source_name=source_name,
        language=language,
        match_confidence=1,
        match_reason="Operator confirmed",
        verified_by="manual",
    )


def outcome(database, error=None, **kwargs):
    database.record_release_source_result(
        "manga-1", "suwayomi", "remote-1", error=error, **kwargs
    )
    return database.get_release_source("manga-1", "suwayomi", "remote-1")


def release(
    database,
    *,
    key="suwayomi:7",
    manga_id="manga-1",
    language="en",
    name="Example Scans",
):
    database.upsert_chapters(
        manga_id,
        [
            {
                **chapter(f"{manga_id}-{key}-{language}", "1", language=language),
                "provider": key.split(":", 1)[0],
                "source_key": key,
                "source_name": name,
            }
        ],
    )


def health(database):
    return {row["source_key"]: row for row in database.source_health_rows()}


def clock(monkeypatch, value):
    monkeypatch.setattr("tankarr.database.utc_now", lambda: value)


def test_mapping_failure_since_survives_repeated_failures_and_resets_on_success(
    context, monkeypatch
):
    database, _service, _remote = context
    mapping(database)
    clock(monkeypatch, "2026-09-01T08:00:00+00:00")
    first = outcome(database, "HTTP 503")
    clock(monkeypatch, "2026-09-03T08:00:00+00:00")
    second = outcome(database, "Connection closed")
    assert second["error_since"] == first["error_since"] == "2026-09-01T08:00:00+00:00"
    assert second["last_checked_at"] == "2026-09-03T08:00:00+00:00"
    assert second["last_error"] == "Connection closed"
    success = outcome(database, publication_status="On Hiatus")
    assert success["error_since"] is None
    assert success["last_error"] is None
    assert success["publication_status"] == "on_hiatus"
    clock(monkeypatch, "2026-09-04T08:00:00+00:00")
    again = outcome(database, "HTTP 500")
    assert again["error_since"] == "2026-09-04T08:00:00+00:00"
    assert again["publication_status"] == "on_hiatus"
    assert health(database)["suwayomi:examplescans"]["attempts"] == 4


def test_rediscovery_does_not_clear_mapping_error_streak(context, monkeypatch):
    database, _service, _remote = context
    mapping(database)
    first = outcome(database, "HTTP 503")
    clock(monkeypatch, "2030-01-01T00:00:00+00:00")
    rediscovered = mapping(database)
    for key in ("last_error", "error_since", "last_checked_at"):
        assert rediscovered[key] == first[key]
    assert next(iter(health(database).values()))["attempts"] == 1


@pytest.mark.parametrize("scope", ["series", "global"])
def test_mapping_health_uses_numeric_extension_identity_without_clearing_download_failures(
    context, scope
):
    database, _service, _remote = context
    mapping(database)
    manga_id = "manga-1"
    if scope == "global":
        manga_id = "other"
        database.upsert_manga({**manga("other"), "provider": "suwayomi"}, "en")
    release(database, manga_id=manga_id)
    database.record_source_failure("manga-1", "suwayomi:7", reason="Download failed")
    outcome(database, "List failed")
    outcome(database)
    assert set(health(database)) == {"suwayomi:7"}
    assert health(database)["suwayomi:7"]["attempts"] == 3
    assert health(database)["suwayomi:7"]["failures"] == 2
    with database.connect() as connection:
        row = connection.execute("SELECT * FROM source_failure").fetchone()
    assert row["failures"] == 1
    assert row["last_reason"] == "Download failed"


def test_numeric_identity_prefers_series_and_matches_language_and_exact_source_name(
    context,
):
    database, _service, _remote = context
    mapping(database)
    release(database)
    release(database, key="suwayomi:8", language="fr")
    release(database, key="suwayomi:9", name="Example Scans Plus")
    database.upsert_manga({**manga("other"), "provider": "suwayomi"}, "en")
    release(database, key="suwayomi:10", manga_id="other")
    outcome(database, "List failed")
    assert set(health(database)) == {"suwayomi:7"}


@pytest.mark.parametrize("scope", ["series", "global", "absent"])
def test_ambiguous_or_absent_numeric_identity_uses_specific_alias(context, scope):
    database, _service, _remote = context
    mapping(database)
    manga_id = "manga-1"
    if scope == "global":
        manga_id = "other"
        database.upsert_manga({**manga("other"), "provider": "suwayomi"}, "en")
    if scope != "absent":
        release(database, manga_id=manga_id)
        release(database, manga_id=manga_id, key="suwayomi:8")
    outcome(database, "List failed")
    assert set(health(database)) == {"suwayomi:examplescans"}


def test_unknown_mapping_never_records_health(context):
    database, _service, _remote = context
    with pytest.raises(KeyError):
        outcome(database, "List failed")
    assert health(database) == {}


def test_health_failure_rolls_back_mapping_result(context, monkeypatch):
    database, _service, _remote = context
    mapping(database)
    before = database.get_release_source("manga-1", "suwayomi", "remote-1")

    def fail(*_args, **_kwargs):
        raise sqlite3.OperationalError("Ledger unavailable")

    monkeypatch.setattr(database, "_record_source_health_connection", fail)
    with pytest.raises(sqlite3.OperationalError):
        outcome(database, "List failed")
    assert database.get_release_source("manga-1", "suwayomi", "remote-1") == before


def test_primary_failure_is_separate_from_monitor_and_has_consecutive_timestamp(
    context, monkeypatch
):
    database, _service, _remote = context
    with database.connect() as connection:
        connection.execute("UPDATE manga SET last_check_error='Reader unavailable'")
    clock(monkeypatch, "2026-09-01T08:00:00+00:00")
    database.record_primary_source_result("manga-1", error="HTTP 503")
    clock(monkeypatch, "2026-09-03T08:00:00+00:00")
    database.record_primary_source_result("manga-1", error="HTTP 500")
    stored = database.get_manga("manga-1")
    assert stored["primary_source_error_since"] == "2026-09-01T08:00:00+00:00"
    assert stored["primary_source_error"] == "HTTP 500"
    database.record_primary_source_result("manga-1")
    stored = database.get_manga("manga-1")
    assert stored["primary_source_error_since"] is None
    assert stored["primary_source_error"] is None
    assert stored["last_check_error"] == "Reader unavailable"
    assert set(health(database)) == {"suwayomi:7"}
    assert health(database)["suwayomi:7"]["attempts"] == 3


def old_database(context):
    database, _service, _remote = context
    mapping(database)
    with database.connect() as connection:
        connection.execute(
            "UPDATE manga_release_source SET last_error='HTTP 503', last_checked_at='2026-08-01T00:00:00+00:00'"
        )
        connection.execute("UPDATE manga SET last_check_error='Monitor failed'")
        connection.execute("ALTER TABLE manga_release_source DROP COLUMN error_since")
        connection.execute("ALTER TABLE manga DROP COLUMN primary_source_error")
        connection.execute("ALTER TABLE manga DROP COLUMN primary_source_error_since")
        connection.execute("PRAGMA user_version=2")
    return database


@pytest.mark.parametrize("last_checked", ["2026-08-01T00:00:00+00:00", None])
def test_schema_three_backs_up_before_migration_and_backfills_only_known_source_errors(
    context,
    monkeypatch,
    last_checked,
):
    database = old_database(context)
    now = "2026-09-01T00:00:00+00:00"
    clock(monkeypatch, now)
    with database.connect() as connection:
        connection.execute(
            "UPDATE manga_release_source SET last_checked_at=?", (last_checked,)
        )
    calls = []

    def backup():
        with database.connect() as connection:
            assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
            assert "error_since" not in {
                row[1]
                for row in connection.execute("PRAGMA table_info(manga_release_source)")
            }
        calls.append(True)

    database.initialize(before_migration=backup)
    database.initialize(before_migration=backup)
    assert calls == [True]
    assert database.get_release_source("manga-1", "suwayomi", "remote-1")[
        "error_since"
    ] == (last_checked or now)
    stored = database.get_manga("manga-1")
    assert stored["primary_source_error"] is None
    assert stored["primary_source_error_since"] is None
    assert stored["last_check_error"] == "Monitor failed"
    assert health(database) == {}
    with database.connect() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_failed_backup_keeps_version_two_schema_unchanged(context):
    database = old_database(context)

    def backup():
        raise OSError("Backup unavailable")

    with pytest.raises(OSError, match="Backup unavailable"):
        database.initialize(before_migration=backup)
    with database.connect() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        assert "error_since" not in {
            row[1]
            for row in connection.execute("PRAGMA table_info(manga_release_source)")
        }


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["get_manga", "list_chapters"])
async def test_refresh_records_only_provider_failures_and_preserves_exception(
    context, method
):
    database, service, remote = context
    provider = AsyncMock()
    provider.get_manga.return_value = remote
    provider.list_chapters.return_value = []
    problem = RuntimeError("Remote unavailable")
    getattr(provider, method).side_effect = problem
    service.providers = {"suwayomi": provider}
    with pytest.raises(RuntimeError) as caught:
        await service.refresh_manga("manga-1")
    assert caught.value is problem
    assert PRIMARY_SOURCE_FAILURE_NOTE in caught.value.__notes__
    assert (
        database.get_manga("manga-1")["primary_source_error"]
        == "RuntimeError: Remote unavailable"
    )
    assert health(database)["suwayomi:7"]["attempts"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure_stage", ["upsert_manga", "upsert_chapters", "reconcile", "guard"]
)
async def test_internal_refresh_errors_are_not_attributed_to_primary_source(
    context, monkeypatch, failure_stage
):
    database, service, remote = context
    database.record_primary_source_result("manga-1", error="Previous network error")
    provider = AsyncMock()
    provider.get_manga.return_value = remote
    provider.list_chapters.return_value = []
    service.providers = {"suwayomi": provider}
    problem = (
        RecoveryBlocked("Recovery needed")
        if failure_stage == "guard"
        else RuntimeError("Database or processing failed")
    )

    def fail(*_args, **_kwargs):
        raise problem

    if failure_stage == "reconcile":
        monkeypatch.setattr(
            service, "_reconcile_release_units", AsyncMock(side_effect=problem)
        )
    elif failure_stage == "guard":
        monkeypatch.setattr(service, "assert_mutations_allowed", fail)
    else:
        monkeypatch.setattr(database, failure_stage, fail)
    with pytest.raises(type(problem)) as caught:
        await service.refresh_manga("manga-1")
    assert caught.value is problem
    assert PRIMARY_SOURCE_FAILURE_NOTE not in getattr(problem, "__notes__", ())
    succeeded = failure_stage in {"upsert_chapters", "reconcile"}
    assert database.get_manga("manga-1")["primary_source_error"] == (
        None if succeeded else "Previous network error"
    )
    assert health(database)["suwayomi:7"]["attempts"] == (2 if succeeded else 1)


@pytest.mark.asyncio
async def test_cancelled_primary_request_does_not_record_failure(context):
    database, service, remote = context
    provider = AsyncMock()
    provider.get_manga.return_value = remote
    provider.list_chapters.side_effect = asyncio.CancelledError()
    service.providers = {"suwayomi": provider}
    with pytest.raises(asyncio.CancelledError):
        await service.refresh_manga("manga-1")
    assert database.get_manga("manga-1")["primary_source_error"] is None
    assert health(database) == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["local", "catalogue"])
async def test_nonremote_series_never_have_primary_outcomes(context, provider):
    database, service, _remote = context
    with database.connect() as connection:
        connection.execute("UPDATE manga SET provider=?", (provider,))
    database.record_primary_source_result("manga-1", error="Not a remote source")
    result = await service.refresh_manga("manga-1")
    assert result["seen"] == 0
    assert database.get_manga("manga-1")["primary_source_error"] is None
    assert health(database) == {}
