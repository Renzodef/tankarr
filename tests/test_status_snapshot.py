from __future__ import annotations

import sqlite3
from unittest.mock import Mock

import pytest

from tankarr.database import Database
from tankarr.status_snapshot import STATUS_INPUTS, read_status_updates
from tests.test_performance import chapter, manga


@pytest.fixture
def database(tmp_path):
    database = Database(tmp_path / "catalogue.sqlite3")
    database.initialize()
    database.upsert_manga(manga(), "en", "all")
    database.upsert_chapters("performance-work", [chapter()])
    database.save_volume_metadata("performance-work", "1", {})
    with database.connect() as connection:
        connection.execute(
            "INSERT INTO series_metadata(manga_id,data_json,last_enriched_at) "
            "VALUES ('performance-work','{}','2026-01-01')"
        )
        connection.execute(
            "INSERT INTO download_job(manga_id,chapter_id,requested_language,"
            "status,created_at,updated_at) VALUES "
            "('performance-work','performance-chapter','en','imported','a','a')"
        )
    yield database
    database.close()


def test_unchanged_library_and_unrelated_settings_do_not_repeat_statistics(
    database, monkeypatch
):
    revisions, initial = read_status_updates(database, {})
    assert initial["chapters"] == {"chapters": 1, "downloaded": 0}
    assert initial["jobs"] == {"jobs": 1, "failed": 0}
    for method, _tables in STATUS_INPUTS.values():
        monkeypatch.setattr(
            database, method, Mock(side_effect=AssertionError("unnecessary scan"))
        )
    database.save_setting("unrelated", "value")
    for _ in range(3):
        assert read_status_updates(database, revisions) == (revisions, {})


@pytest.mark.parametrize(
    "sql,changed",
    [
        (
            "UPDATE chapter_release SET updated_at='later', title='Refreshed', monitored=0",
            set(),
        ),
        ("UPDATE chapter_release SET downloaded=1", {"chapters"}),
        ("UPDATE chapter_release SET language='ja'", {"chapters"}),
        ("UPDATE chapter_release SET volume='2'", {"chapters", "coverage"}),
        ("UPDATE manga SET preferred_language='ja'", {"chapters", "alert_rows"}),
        ("UPDATE manga SET title='New title'", {"alert_rows"}),
        ("UPDATE manga SET monitored=0", {"manga"}),
        ("UPDATE manga SET last_checked_at='later'", set()),
        ("UPDATE series_metadata SET data_json='{}', last_enriched_at='later'", set()),
        ("UPDATE series_metadata SET artwork_path='cover.jpg'", {"coverage"}),
        ("UPDATE volume_metadata SET artwork_path='cover.jpg'", {"coverage"}),
        ("UPDATE download_job SET progress=0.5, updated_at='later'", set()),
        ("UPDATE download_job SET status='failed'", {"jobs"}),
        ("DELETE FROM download_job", {"jobs"}),
        ("DELETE FROM chapter_release", {"chapters", "coverage"}),
    ],
)
def test_only_affected_sections_refresh_even_for_external_writes(
    database, sql, changed
):
    revisions, _ = read_status_updates(database, {})
    # A different connection/process need not call a Tankarr invalidation hook.
    with sqlite3.connect(database.path) as connection:
        connection.execute(sql)
    new_revisions, updates = read_status_updates(database, revisions)
    assert set(updates) == changed
    assert read_status_updates(database, new_revisions)[1] == {}


def test_rolled_back_writes_do_not_invalidate_statistics(database):
    revisions, _ = read_status_updates(database, {})
    connection = sqlite3.connect(database.path)
    try:
        connection.execute("UPDATE chapter_release SET downloaded=1")
        connection.rollback()
    finally:
        connection.close()
    assert read_status_updates(database, revisions) == (revisions, {})


def test_write_during_a_snapshot_is_seen_on_the_next_refresh(database, monkeypatch):
    original = database.chapter_totals

    def count_then_write():
        result = original()
        with sqlite3.connect(database.path) as connection:
            connection.execute("UPDATE chapter_release SET downloaded=1")
        return result

    monkeypatch.setattr(database, "chapter_totals", count_then_write)
    revisions, initial = read_status_updates(database, {})
    assert initial["chapters"]["downloaded"] == 0
    monkeypatch.setattr(database, "chapter_totals", original)
    _, updates = read_status_updates(database, revisions)
    assert updates["chapters"]["downloaded"] == 1


def test_metadata_counts_books_once_across_sources_and_ignores_chapter_only_volumes(
    database,
):
    releases = [
        {**chapter(), "id": "book-one", "chapter": None, "release_unit": "volume"},
        {
            **chapter(),
            "id": "book-two",
            "chapter": None,
            "language": "ja",
            "release_unit": "volume",
        },
        {**chapter(), "id": "chapter-two", "volume": "2"},
        {
            **chapter(),
            "id": "book-three",
            "volume": "3",
            "chapter": None,
            "release_unit": "volume",
        },
    ]
    database.upsert_chapters("performance-work", releases)
    for volume in ("1", "2", "3", "4"):
        database.save_volume_metadata(
            "performance-work", volume, {}, artwork_path="cover.jpg"
        )
    overview = database.metadata_overview()
    assert overview["volumes_enriched"] == 2
    assert overview["volumes_with_artwork"] == 2
