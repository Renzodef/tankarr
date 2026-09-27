from pathlib import Path

import pytest

from tankarr.database import Database
from tests.test_organization import chapter, manga


@pytest.mark.parametrize("existing_database", [False, True])
def test_historical_job_paths_use_index_after_initialization(
    tmp_path: Path, existing_database: bool
):
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    database.upsert_manga(manga(), "en", "none")
    database.upsert_chapters("manga-1", [{**chapter(), "provider": "local"}])
    expected = []
    for status in ("completed", "failed", "cancelled", "queued"):
        job = database.create_job("manga-1", "chapter-1", "en")
        database.update_job(job["id"], status=status)
        expected.append((job["id"], status))

    if existing_database:
        # Existing installations already have the active-job partial index,
        # which excludes the history inspected during library organization.
        with database.connect() as connection:
            connection.execute("DROP INDEX idx_download_job_chapter")
        database.initialize()
        database.initialize()  # The schema upgrade remains idempotent.

    statements = []
    with database.read_snapshot(), database.connect() as connection:
        connection.set_trace_callback(statements.append)
        paths = database.list_chapter_job_paths("chapter-1")
        connection.set_trace_callback(None)
        query = next(sql for sql in statements if "FROM download_job" in sql)
        plan = [
            str(row[3]) for row in connection.execute("EXPLAIN QUERY PLAN " + query)
        ]

    assert [(job["id"], job["status"]) for job in paths] == expected
    assert any("SEARCH download_job USING INDEX" in step for step in plan), plan
    assert not any("SCAN download_job" in step for step in plan), plan
    assert not any("TEMP B-TREE" in step for step in plan), plan
