from __future__ import annotations

import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tankarr import logs
from tankarr.app import create_app
from tankarr.config import Settings


@pytest.fixture
def clean_root_logger():
    root = logging.getLogger()
    level = root.level
    handlers = list(root.handlers)
    yield root
    for handler in list(root.handlers):
        if handler not in handlers:
            root.removeHandler(handler)
            handler.close()
    root.setLevel(level)


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "library",
        monitor_enabled=False,
        metadata_enabled=False,
        update_check_enabled=False,
        auth_required=False,
        **overrides,
    )


def test_levels_are_normalised_and_validated():
    assert logs.normalize_log_level(" Warning ") == "warning"
    assert logs.normalize_log_level("warn") == "warning"
    assert logs.normalize_log_level("critical") == "error"
    assert Settings(log_level="DEBUG").log_level == "debug"
    with pytest.raises(ValueError):
        logs.normalize_log_level("verbose")
    with pytest.raises(ValueError):
        Settings(log_level="loud")


def test_configure_logging_writes_a_rotating_file_and_keeps_one_handler(
    tmp_path: Path, clean_root_logger
):
    settings = _settings(tmp_path, log_level="debug")
    path = logs.configure_logging(settings)
    again = logs.configure_logging(settings)
    assert path == again == tmp_path / "data" / "logs" / "tankarr.log"
    file_handlers = [
        handler
        for handler in clean_root_logger.handlers
        if getattr(handler, "tankarr_file", False)
    ]
    assert len(file_handlers) == 1
    assert file_handlers[0].maxBytes == logs.MAX_BYTES
    assert file_handlers[0].backupCount == logs.BACKUP_COUNT
    assert clean_root_logger.level == logging.DEBUG
    # SABnzbd's API key travels in the query string: httpx must never log URLs.
    assert logging.getLogger("httpx").level == logging.WARNING
    logging.getLogger("httpx").info(
        "HTTP Request: GET http://sabnzbd:8080/api?apikey=fixture-secret"
    )

    logging.getLogger("tankarr.test").debug("a debug line")
    logging.getLogger("tankarr.test").warning("a warning line")
    logging.getLogger("tankarr.test").error("an error line\nwith a second line")
    for handler in file_handlers:
        handler.flush()

    everything = logs.tail_log(settings, lines=10)
    assert "fixture-secret" not in "\n".join(everything)
    assert [line.split(" ", 3)[2] for line in everything[:2]] == ["DEBUG", "WARNING"]
    assert everything[-1] == "with a second line"
    warnings = logs.tail_log(settings, lines=10, minimum_level="warning")
    assert len(warnings) == 3
    assert warnings[0].endswith("a warning line")
    assert warnings[-1] == "with a second line"
    assert logs.tail_log(settings, lines=1, minimum_level="error") == [
        "with a second line"
    ]
    assert [item["name"] for item in logs.log_files(settings)] == ["tankarr.log"]


def test_changing_the_level_in_settings_applies_at_once(
    tmp_path: Path, clean_root_logger
):
    settings = _settings(tmp_path)
    logs.configure_logging(settings)
    assert clean_root_logger.level == logging.INFO
    with TestClient(create_app(settings)) as client:
        saved = client.put("/api/settings", json={"log_level": "debug"})
        assert saved.status_code == 200
        assert clean_root_logger.level == logging.DEBUG
        assert (
            client.put("/api/settings", json={"log_level": "loud"}).status_code == 400
        )
        assert client.get("/api/settings").json()["log_level"]["value"] == "debug"


def test_the_system_page_lists_tails_and_downloads_the_log(
    tmp_path: Path, clean_root_logger
):
    settings = _settings(tmp_path)
    logs.configure_logging(settings)
    logging.getLogger("tankarr.test").info("hello from the test")
    for handler in clean_root_logger.handlers:
        handler.flush()
    (tmp_path / "data" / "logs" / "tankarr.log.1").write_text(
        "older\n", encoding="utf-8"
    )
    (tmp_path / "data" / "logs" / "notes.txt").write_text(
        "not a log\n", encoding="utf-8"
    )

    with TestClient(create_app(settings)) as client:
        listing = client.get("/api/system/logs").json()
        assert listing["level"] == "info"
        assert sorted(item["name"] for item in listing["files"]) == [
            "tankarr.log",
            "tankarr.log.1",
        ]
        tail = client.get("/api/system/logs/tail?lines=20").json()["lines"]
        assert any(line.endswith("hello from the test") for line in tail)
        assert client.get("/api/system/logs/tail?lines=0").status_code == 422
        assert client.get("/api/system/logs/tail?level=loud").status_code == 422
        download = client.get("/api/system/logs/tankarr.log.1/download")
        assert download.status_code == 200
        assert download.text == "older\n"
        assert download.headers["content-disposition"].endswith('"tankarr.log.1"')
        assert client.get("/api/system/logs/notes.txt/download").status_code == 404
        # A name that tries to leave the folder never reaches the database file.
        escaped = client.get("/api/system/logs/%2E%2E%2Ftankarr.sqlite3/download")
        assert escaped.status_code == 404 or "text/html" in escaped.headers.get(
            "content-type", ""
        )
        assert b"SQLite format" not in escaped.content


def test_request_lines_are_logged_only_at_debug(tmp_path: Path, clean_root_logger):
    settings = Settings(
        data_dir=tmp_path / "data", library_dir=tmp_path / "library", log_level="info"
    )
    logs.configure_logging(settings)
    access = logging.getLogger("uvicorn.access")
    record = access.makeRecord(
        "uvicorn.access", logging.INFO, __file__, 1, "GET /api/health 200", (), None
    )
    assert not access.filter(record)
    settings.log_level = "debug"
    logs.apply_log_level(settings)
    assert access.filter(record)
