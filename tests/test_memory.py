from tankarr.database import Database
from tankarr.memory import release_native_heap
from tests.test_queue_management import seed_manga


def test_release_native_heap_trims_when_malloc_trim_exists(monkeypatch):
    import tankarr.memory as memory

    calls: list[int] = []
    monkeypatch.setattr(memory, "_malloc_trim", lambda size: calls.append(size) or 1)
    assert memory.release_native_heap() is True
    assert calls == [0]


def test_release_native_heap_is_a_noop_without_malloc_trim(monkeypatch):
    import tankarr.memory as memory

    monkeypatch.setattr(memory, "_malloc_trim", None)
    assert memory.release_native_heap() is False


def test_an_unchanged_chapter_upsert_does_not_touch_the_row(tmp_path):
    database = Database(tmp_path / "db.sqlite3")
    database.initialize()
    seed_manga(database)
    before = database.get_chapter("chapter-1")
    revision = database.library_revision()
    database.upsert_chapters(
        "manga-1",
        [
            {
                "id": "chapter-1",
                "chapter": "1",
                "volume": "1",
                "title": "One",
                "language": "en",
                "provider": "mangadex",
                "groups": [],
                "publish_at": "2026-01-01T00:00:00Z",
                "source_url": "https://example.test/1",
            }
        ],
    )
    after = database.get_chapter("chapter-1")
    assert after["updated_at"] == before["updated_at"]
    assert database.library_revision() == revision


def test_cleanup_never_drops_a_suwayomi_manga_tankarr_tracks(tmp_path):
    """Library, monitored, in-progress and imported identities stay.

    A search that merely aged out does not, and a chapter id is not a
    manga id. An id from another Suwayomi instance is not this file's row.
    """

    from tankarr.suwayomi_catalogue import delete_sql, keep_id_statements

    database = Database(tmp_path / "db.sqlite3")
    database.initialize()
    token = "aaaaaaaa"

    def series(manga_id: str, monitor_mode: str, provider: str = "catalogue") -> None:
        database.upsert_manga(
            {
                "id": manga_id,
                "provider": provider,
                "title": manga_id,
                "description": "",
                "authors": [],
                "original_language": "ja",
                "status": "ongoing",
                "year": None,
                "last_volume": None,
                "last_chapter": None,
                "available_languages": ["en"],
            },
            "en",
            monitor_mode,
        )

    def mapping(manga_id: str, provider_manga_id: str) -> None:
        database.upsert_release_source(
            manga_id,
            provider="suwayomi",
            provider_manga_id=provider_manga_id,
            title=manga_id,
            source_url="https://example.test/source",
            source_name="Example",
            language="en",
            match_confidence=1.0,
            match_reason="test",
        )

    def chapter(manga_id: str, chapter_id: str) -> None:
        database.upsert_chapters(
            manga_id,
            [
                {
                    "id": chapter_id,
                    "chapter": "1",
                    "volume": "1",
                    "title": "One",
                    "language": "en",
                    "provider": "suwayomi",
                    "groups": [],
                    "publish_at": "2026-01-01T00:00:00Z",
                    "source_url": "https://example.test/1",
                }
            ],
        )

    # In the library, not monitored, and the Suwayomi id is the series id.
    series(f"suwayomi-{token}-101", "none", provider="suwayomi")
    # Monitored, with the Suwayomi id only on the series itself.
    series(f"suwayomi-{token}-102", "all", provider="suwayomi")
    # Monitored catalogue work whose download source is Suwayomi.
    series("mb-monitored", "all")
    mapping("mb-monitored", f"suwayomi-{token}-202")
    # Download in progress. The chapter number must not be kept as a manga.
    series("mb-downloading", "none")
    mapping("mb-downloading", f"suwayomi-{token}-303")
    chapter("mb-downloading", f"suwayomi-{token}-9001")
    # Imported file, and a job still copying it into the library.
    series("mb-imported", "none")
    mapping("mb-imported", f"suwayomi-{token}-404")
    chapter("mb-imported", "import-chapter")
    now = "2026-06-01T00:00:00+00:00"
    with database.connect() as connection:
        connection.execute(
            """
            UPDATE chapter_release
            SET downloaded=1, library_path=?, local_import_sha256=?
            WHERE id=?
            """,
            ("Example/book.cbz", "ab" * 32, "import-chapter"),
        )
        connection.executemany(
            """
            INSERT INTO download_job (
                manga_id, chapter_id, requested_language, status, message,
                created_at, updated_at
            ) VALUES (?, ?, 'en', ?, '', ?, ?)
            """,
            [
                ("mb-downloading", f"suwayomi-{token}-9001", "downloading", now, now),
                ("mb-imported", "import-chapter", "importing", now, now),
                (f"suwayomi-{token}-707", "finished", "completed", now, now),
            ],
        )
    # A search Tankarr no longer tracks.
    database.touch_suwayomi_manga([505])
    with database.connect() as connection:
        connection.execute(
            "UPDATE suwayomi_catalogue_touch SET seen_at=? WHERE manga_id=?",
            ("2000-01-01T00:00:00+00:00", 505),
        )
    # Same number, previous server. Not a row in this database.
    series("suwayomi-bbbbbbbb-606", "all", provider="suwayomi")

    kept = database.suwayomi_catalogue_keep_ids(instance_token=token, max_age_hours=24)
    assert {101, 102, 202, 303, 404} <= kept
    assert 505 not in kept
    assert 606 not in kept
    assert 707 not in kept
    assert 9001 not in kept
    script = "\n".join(keep_id_statements(sorted(kept)))
    for value in (101, 102, 202, 303, 404):
        assert f"({value})" in script
    assert "(505)" not in script
    assert "NOT IN (SELECT ID FROM TANKARR_KEEP)" in delete_sql(
        "MangaTable",
        {"id": "id"},
        10,
    )
    monitored = database.get_manga(f"suwayomi-{token}-102")
    library_only = database.get_manga(f"suwayomi-{token}-101")
    assert monitored["monitored"] == 1
    assert library_only["monitored"] == 0


def test_recent_suwayomi_touches_are_kept_and_old_ones_pruned(tmp_path):
    database = Database(tmp_path / "db.sqlite3")
    database.initialize()
    database.touch_suwayomi_manga([11, 12])
    with database.connect() as connection:
        connection.execute(
            "UPDATE suwayomi_catalogue_touch SET seen_at=? WHERE manga_id=?",
            ("2000-01-01T00:00:00+00:00", 12),
        )
    kept = database.suwayomi_catalogue_keep_ids(instance_token=None, max_age_hours=24)
    assert 11 in kept
    assert 12 not in kept


def test_the_real_allocator_returns_without_raising():
    assert release_native_heap() in {True, False}
