"""The library identity binds one configuration to one library volume.

A fresh installation must provision it on its own (issue #15: every new
Docker installation stayed on "Library identity" in setup because nothing
ever wrote the files); a recreated configuration adopts the marker the
library already carries; a token without a marker stays closed because the
volume may be unmounted.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from tankarr.config import Settings
from tankarr.database import Database
from tankarr.operations import local_preflight
from tankarr.service import TankarrService

IDENTITY = re.compile(r"^[0-9a-f]{32}$")


def make_service(tmp_path: Path, **overrides) -> tuple[Settings, TankarrService]:
    settings = Settings(
        data_dir=tmp_path / "data", library_dir=tmp_path / "library", **overrides
    )
    settings.ensure_directories()
    database = Database(settings.database_path)
    database.initialize()
    service = TankarrService(
        settings,
        database,
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
    )
    return settings, service


def identity_files(settings: Settings) -> tuple[Path, Path]:
    return (
        settings.data_dir / ".tankarr-library-id",
        settings.library_dir / ".tankarr-library-id",
    )


def test_a_fresh_installation_provisions_the_identity_on_first_use(tmp_path):
    settings, service = make_service(tmp_path)
    token, marker = identity_files(settings)
    assert not token.exists() and not marker.exists()

    status = service.library_status()

    assert status == {"available": True, "root": str(settings.library_dir.resolve())}
    assert token.read_text() == marker.read_text()
    assert IDENTITY.match(marker.read_text().strip())
    # Stable afterwards: a second call neither rewrites nor changes it.
    before = marker.read_text()
    assert service.library_status()["available"] is True
    assert marker.read_text() == before
    assert not list(settings.library_dir.glob("*.tmp"))


def test_a_recreated_configuration_adopts_the_library_it_is_pointed_at(tmp_path):
    settings, service = make_service(tmp_path)
    token, marker = identity_files(settings)
    marker.write_text("0123456789abcdef0123456789abcdef\n")

    assert service.library_status()["available"] is True
    assert token.read_text().strip() == "0123456789abcdef0123456789abcdef"


def test_a_token_without_a_marker_stays_closed_because_the_volume_may_be_unmounted(
    tmp_path,
):
    settings, service = make_service(tmp_path)
    token, marker = identity_files(settings)
    token.write_text("0123456789abcdef0123456789abcdef\n")

    status = service.library_status()

    assert status["available"] is False
    assert "may be unmounted" in status["reason"]
    assert str(token) in status["reason"]
    assert not marker.exists()


def test_a_configuration_with_recorded_files_never_adopts_an_unmarked_folder(
    tmp_path,
):
    settings, service = make_service(tmp_path)
    token, marker = identity_files(settings)
    service.database.upsert_manga(
        {
            "id": "manga-1",
            "title": "Recorded",
            "description": "",
            "cover_url": None,
            "authors": [],
            "original_language": "ja",
            "status": "ongoing",
            "last_volume": None,
            "last_chapter": None,
            "available_languages": ["en"],
            "source_url": "https://example.test/manga-1",
        },
        "en",
        "all",
    )
    service.database.upsert_chapters(
        "manga-1",
        [
            {
                "id": "chapter-1",
                "chapter": "1",
                "volume": "1",
                "title": "Chapter 1",
                "language": "en",
                "provider": "mangadex",
                "groups": [],
                "publish_at": "2026-01-01T00:00:00Z",
                "source_url": "https://example.test/chapter-1",
                "pages": 20,
                "version": 1,
            }
        ],
    )
    recorded = settings.library_dir / "Recorded" / "Recorded 001.cbz"
    recorded.parent.mkdir(parents=True)
    recorded.write_bytes(b"cbz")
    service.database.mark_chapter_downloaded("chapter-1", recorded)

    status = service.library_status()

    assert status["available"] is False
    assert "not provisioned" in status["reason"]
    assert not token.exists() and not marker.exists()


def test_a_marker_from_another_library_is_refused(tmp_path):
    settings, service = make_service(tmp_path)
    token, marker = identity_files(settings)
    token.write_text("0123456789abcdef0123456789abcdef\n")
    marker.write_text("fedcba9876543210fedcba9876543210\n")

    status = service.library_status()

    assert status["available"] is False
    assert "does not match" in status["reason"]


def test_restored_safe_mode_never_writes_identity_files(tmp_path):
    settings, service = make_service(tmp_path, restored_safe_mode=True)
    settings.library_dir.mkdir(parents=True, exist_ok=True)
    token, marker = identity_files(settings)

    status = service.library_status()

    assert status["available"] is False
    assert "restored safe mode" in status["reason"]
    assert not token.exists() and not marker.exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_an_unwritable_library_names_the_permission_fix(tmp_path):
    settings, service = make_service(tmp_path)
    settings.library_dir.chmod(0o555)
    try:
        status = service.library_status()
    finally:
        settings.library_dir.chmod(0o755)

    assert status["available"] is False
    assert "PUID/PGID" in status["reason"]
    assert not identity_files(settings)[0].exists()


def test_setup_preflight_shows_the_real_reason(tmp_path):
    settings, service = make_service(tmp_path)
    identity_files(settings)[0].write_text("0123456789abcdef0123456789abcdef\n")

    checks = {
        check["id"]: check for check in local_preflight(settings, service)["checks"]
    }

    assert checks["library_identity"]["status"] == "error"
    assert "may be unmounted" in checks["library_identity"]["detail"]
