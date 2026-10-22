from datetime import UTC, datetime

from tankarr.database import Database
from tankarr.providers.suwayomi import BACKOFF_MESSAGE
from tankarr.source_circuit import circuit_delay_seconds, http_overload, source_gate_key
from tests.test_queue_management import seed_manga


def test_source_cooldown_survives_restart_without_blocking_healthy_source(tmp_path):
    database = Database(tmp_path / "db.sqlite3")
    database.initialize()
    seed_manga(database)
    first = database.create_job("manga-1", "chapter-1", "en")
    healthy = {
        **database.get_chapter("chapter-1"),
        "id": "chapter-2",
        "chapter": "2",
        "canonical_chapter": "2",
        "source_url": "https://healthy.test/2",
    }
    database.upsert_chapters("manga-1", [healthy])
    second = database.create_job("manga-1", "chapter-2", "en")
    key = source_gate_key("mangadex", "https://example.test/1")
    assert database.record_source_circuit(key, error="TimeoutError") == 0
    deadline = database.record_source_circuit(key, error="TimeoutError")
    assert deadline > datetime.now(UTC).timestamp()
    database = Database(database.path)
    database.initialize()
    assert database.source_retry_at(key) == deadline
    waiting = database.get_job(first["id"])
    assert waiting["next_retry_at"] == deadline
    assert waiting["failure_scope"] == "source"
    assert waiting["failure_code"] == "TimeoutError"
    assert database.get_job(second["id"])["next_retry_at"] == 0
    assert [row["id"] for row in database.list_queued_job_heads()] == [second["id"]]
    assert database.claim_queued_job(first["id"], tmp_path / "file.cbz") is None
    database.record_source_circuit(key)
    assert database.list_queued_job_heads()[0]["id"] == first["id"]


def test_cooldown_caps_and_success_resets_streak(tmp_path):
    database = Database(tmp_path / "db.sqlite3")
    database.initialize()
    for _ in range(30):
        deadline = database.record_source_circuit("a", error="TimeoutError")
    assert 590 < deadline - datetime.now(UTC).timestamp() <= 600
    database.record_source_circuit("a")
    assert database.record_source_circuit("a", error="TimeoutError") == 0


def test_an_overload_waits_longer_than_a_timeout_and_the_backoff_text_does_not_retrigger():
    assert circuit_delay_seconds(1, "TimeoutError") == 0
    assert circuit_delay_seconds(2, "TimeoutError") == 30
    assert http_overload("upstream answered HTTP 429")
    assert http_overload("HTTP 522 from the source")
    assert http_overload("Too many requests")
    assert not http_overload("cloudflare challenge")
    assert circuit_delay_seconds(1, "HTTP 429") == 15 * 60
    assert circuit_delay_seconds(2, "503") == 3600
    assert circuit_delay_seconds(4, "500") == 12 * 3600
    assert not http_overload(BACKOFF_MESSAGE)
    detail = "429 Too Many Requests"
    database_delay = circuit_delay_seconds(1, f"ProviderRequestError\n{detail}")
    assert database_delay == 15 * 60
