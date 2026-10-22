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
