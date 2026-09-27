from tankarr.database import Database
from tankarr.selection import explain_releases
from tankarr.source_circuit import source_gate_key
from tests.test_queue_management import seed_manga


def test_decisions_follow_actual_selector_and_surface_cooldown(tmp_path):
    database = Database(tmp_path / "db.sqlite3")
    database.initialize()
    seed_manga(database)
    releases = database.list_chapters("manga-1", "en")
    explained = explain_releases(database, "manga-1", releases)
    assert explained[0]["selection"]["selected"]
    assert explained[0]["selection"]["reasons"]
    key = source_gate_key("mangadex", "https://example.test/1")
    database.record_source_circuit(key, error="TimeoutError")
    database.record_source_circuit(key, error="TimeoutError")
    explained = explain_releases(database, "manga-1", releases)
    assert explained[0]["selection"]["next_retry_at"]
    database.block_release("chapter-1", reason="Broken archive")
    explained = explain_releases(database, "manga-1", releases)
    assert not explained[0]["selection"]["selected"]
