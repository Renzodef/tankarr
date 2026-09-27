from __future__ import annotations

import asyncio
import os
import struct
import zipfile
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

import pytest
from PIL import Image

from tankarr.artwork_thumbnails import _render_thumbnail
from tankarr.database import Database
from tankarr.series_audit import SeriesAudit, StaleAudit
from tankarr.service import TankarrService


def page(color):
    payload = BytesIO()
    Image.new("RGB", (500, 700), color).save(payload, format="PNG")
    return payload.getvalue()


def write_archive(path, *, extra=None):
    with zipfile.ZipFile(path, "w") as archive:
        # Deliberately not lexicographic order: natural page 10 must be last.
        for name, color in (("10.png", "black"), ("2.png", "gray"), ("1.png", "white")):
            archive.writestr(name, page(color))
        if extra:
            archive.writestr(*extra)


@pytest.fixture
def context(tmp_path, monkeypatch):
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    database.upsert_manga(
        {"id": "series", "title": "Fixture", "authors": [], "status": "completed"},
        "en",
        "all",
    )
    library = tmp_path / "library"
    library.mkdir()
    service = SimpleNamespace(
        settings=SimpleNamespace(data_dir=tmp_path),
        _library_root=lambda: library,
        _recorded_library_path=lambda path, root: TankarrService._confined_library_path(
            Path(path), root
        ),
    )
    audit = SeriesAudit(database, service)
    renders = []

    async def render(source, destination, width):
        renders.append((source.name, width))
        await asyncio.to_thread(_render_thumbnail, source, destination, width)

    monkeypatch.setattr(audit.thumbnails, "_render_in_child", render)
    return database, audit, library, renders


def add(
    context,
    identifier="chapter",
    number="1",
    source="Alpha",
    provider="fixture",
    **changes,
):
    database, _audit, library, _renders = context
    path = library / f"{identifier}.cbz"
    write_archive(path)
    release = {
        "id": identifier,
        "chapter": number,
        "volume": None,
        "title": identifier,
        "provider": provider,
        "language": "en",
        "source_name": source,
        "release_unit": "chapter",
        "groups": [],
        "source_url": "https://nas.local/chapter",
        "pages": 3,
        "version": 1,
        "publish_at": None,
        **changes,
    }
    database.upsert_chapters("series", [release])
    database.mark_chapter_downloaded(identifier, path)
    return path


def map_source(database, identifier="mapped-alpha", name="Alpha"):
    database.upsert_release_source(
        "series",
        provider="fixture",
        provider_manga_id=identifier,
        title="Fixture",
        source_url=f"https://nas.local/{identifier}",
        source_name=name,
        language="en",
        match_confidence=1,
        match_reason="fixture",
    )


def thumb_key(report, identifier="chapter"):
    item = next(item for item in report["files"] if item["id"] == identifier)
    return parse_qs(urlsplit(item["first_thumbnail_url"]).query)["revision"][0]


def test_audit_get_uses_metadata_without_opening_archives(context, monkeypatch):
    path = add(context)
    database, audit, _library, _renders = context
    map_source(database)
    opened = Mock(side_effect=AssertionError("GET audit opened a ZIP"))
    monkeypatch.setattr(zipfile, "ZipFile", opened)
    report = audit.read("series")
    item = report["files"][0]
    assert item["size_bytes"] == path.stat().st_size and item["pages"] == 3
    assert item["thumbnail_status"] == "pending" and item["last_page_black"] is None
    assert item["provider_manga_id"] == "mapped-alpha" and item["can_reject_source"]
    assert item["anomalies"] == [{"code": "few_pages", "label": "Fewer than 8 pages"}]
    assert item["first_thumbnail_url"].startswith(
        "/api/manga/series/audit/files/chapter/first?revision="
    )
    assert report["sources"][0]["file_ids"] == ["chapter"]
    opened.assert_not_called()


async def test_lazy_thumbnails_read_edges_once_cache_both_and_report_black_last_page(
    context, monkeypatch
):
    add(context)
    _database, audit, _library, renders = context
    report = audit.read("series")
    key = thumb_key(report)
    extract = Mock(wraps=audit._extract)
    monkeypatch.setattr(audit, "_extract", extract)
    first, last = await asyncio.gather(
        audit.thumbnail("series", "chapter", "first", key),
        audit.thumbnail("series", "chapter", "last", key),
    )
    assert first[0].is_file() and last[0].is_file() and first[1] != last[1]
    assert len(renders) == 2 and extract.call_count == 1
    with Image.open(first[0]) as image:
        assert image.width == 192 and image.getpixel((0, 0))[0] > 240
    with Image.open(last[0]) as image:
        assert image.getpixel((0, 0))[0] < 10
    refreshed = audit.read("series")
    assert refreshed["revision"] == report["revision"]
    assert refreshed["files"][0]["thumbnail_status"] == "ready"
    assert refreshed["files"][0]["last_page_black"] is True
    assert "last_page_black" in {
        item["code"] for item in refreshed["files"][0]["anomalies"]
    }
    # A new worker instance reuses the durable metrics and WebP cache.
    restarted = SeriesAudit(context[0], audit.service)
    await restarted.thumbnail("series", "chapter", "first", key)
    assert list(audit.cache_root.glob(".pages-*")) == []


def test_quality_and_duplicate_source_anomalies_are_unit_scoped(context):
    database, audit, _library, _renders = context
    for number in range(1, 10):
        identifier = f"chapter-{number}"
        add(context, identifier, str(number), pages=20)
        database.record_page_quality(
            "series",
            identifier,
            verdict="degraded" if number == 9 else "ok",
            assessment={
                "normalized_height": 20000,
                "pages": 20,
                "measured_pages": 20,
                "median_aspect": 3 if number == 9 else 1.5,
            },
        )
    add(context, "alternative", "1", source="Beta")
    add(context, "book", None, release_unit="volume", volume="1", pages=180)
    files = {item["id"]: item for item in audit.read("series")["files"]}
    assert files["chapter-9"]["verdict"]["verdict"] == "degraded"
    assert "webtoon_shape" in {item["code"] for item in files["chapter-9"]["anomalies"]}
    assert "multiple_sources" in {
        item["code"] for item in files["chapter-1"]["anomalies"]
    }
    assert "multiple_sources" not in {
        item["code"] for item in files["book"]["anomalies"]
    }


def test_selected_retirement_review_is_signed_and_revalidates_files(context):
    path = add(context)
    _database, audit, _library, _renders = context
    reviewed = audit.preview("series", ["chapter"])
    assert audit.validate(
        "series", ["chapter"], confirmation_snapshot=reviewed["confirmation_snapshot"]
    )["chapter_ids"] == ["chapter"]
    for forged in ("forged", "é" * 64, "a" * 64):
        with pytest.raises(StaleAudit):
            audit.validate("series", ["chapter"], confirmation_snapshot=forged)
    before = path.stat()
    path.write_bytes(path.read_bytes() + b"changed")
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    with pytest.raises(StaleAudit):
        audit.validate(
            "series",
            ["chapter"],
            confirmation_snapshot=reviewed["confirmation_snapshot"],
        )
    assert path.exists() and context[0].get_chapter("chapter")["downloaded"]


def test_source_preview_selects_exact_mapping_and_rejects_ambiguous_sources(context):
    add(context, "alpha", source="Alpha")
    add(context, "beta", source="Beta")
    database, audit, _library, _renders = context
    map_source(database)
    map_source(database, "mapped-beta", "Beta")
    source = {"provider": "fixture", "provider_manga_id": "mapped-alpha"}
    reviewed = audit.preview("series", source=source)
    assert (
        reviewed["chapter_ids"] == ["alpha"] and reviewed["action"] == "reject_source"
    )
    assert audit.validate(
        "series", source=source, confirmation_snapshot=reviewed["confirmation_snapshot"]
    )["chapter_ids"] == ["alpha"]
    with pytest.raises(StaleAudit):
        audit.validate(
            "series",
            source={"provider": "fixture", "provider_manga_id": "mapped-beta"},
            confirmation_snapshot=reviewed["confirmation_snapshot"],
        )
    map_source(database, "ambiguous-alpha", "Alpha")
    report = audit.read("series")
    assert not next(item for item in report["files"] if item["id"] == "alpha")[
        "can_reject_source"
    ]
    with pytest.raises(ValueError, match="exact release source"):
        audit.preview("series", source=source)


def test_audit_includes_other_languages_and_disabled_source_files(context):
    add(context, "english", source="English")
    add(context, "italian", source="Italian", language="it")
    database, audit, _library, _renders = context
    map_source(database, "italian-source", "Italian")
    with database.connect() as connection:
        connection.execute(
            "UPDATE manga_release_source SET language='it', enabled=0 "
            "WHERE provider_manga_id='italian-source'"
        )
    report = audit.read("series")
    assert {item["language"] for item in report["files"]} == {"en", "it"}
    reviewed = audit.preview(
        "series", source={"provider": "fixture", "provider_manga_id": "italian-source"}
    )
    assert reviewed["chapter_ids"] == ["italian"]
    assert reviewed["files"][0]["provider_manga_id"] == "italian-source"


@pytest.mark.parametrize("kind", ["outside", "symlink", "missing"])
def test_untrusted_or_missing_library_paths_cannot_be_previewed_or_retired(
    context, tmp_path, kind
):
    path = add(context)
    database, audit, library, _renders = context
    outside = tmp_path / "outside.cbz"
    outside.write_bytes(b"sentinel")
    if kind == "outside":
        database.mark_chapter_downloaded("chapter", outside)
    elif kind == "symlink":
        path.unlink()
        path.symlink_to(outside)
    else:
        path.unlink()
    item = audit.read("series")["files"][0]
    assert not item["can_retire"] and item["first_thumbnail_url"] is None
    with pytest.raises(StaleAudit):
        audit.preview("series", ["chapter"])
    assert outside.read_bytes() == b"sentinel"


@pytest.mark.parametrize(
    "name", ["../escape.png", "folder\\escape.png", "/absolute.png"]
)
async def test_archive_member_paths_are_rejected_without_extracting_outside_cache(
    context, name
):
    path = add(context)
    write_archive(path, extra=(name, page("white")))
    _database, audit, _library, renders = context
    key = thumb_key(audit.read("series"))
    with pytest.raises(ValueError, match="preview is unavailable"):
        await audit.thumbnail("series", "chapter", "first", key)
    assert not renders and list(audit.cache_root.glob(".pages-*")) == []
    assert audit.read("series")["files"][0]["thumbnail_status"] == "unavailable"


async def test_archive_directory_limit_is_checked_before_zipfile_allocation(
    context, monkeypatch
):
    add(context)
    _database, audit, _library, _renders = context
    key = thumb_key(audit.read("series"))
    monkeypatch.setattr("tankarr.series_audit.MAX_ENTRIES", 1)
    opened = Mock(side_effect=AssertionError("opened oversized central directory"))
    monkeypatch.setattr(zipfile, "ZipFile", opened)
    with pytest.raises(ValueError, match="preview is unavailable"):
        await audit.thumbnail("series", "chapter", "first", key)
    opened.assert_not_called()


@pytest.mark.parametrize("comment_size", [0, 65535])
async def test_zip64_locator_cannot_bypass_classic_directory_limits(
    context, monkeypatch, comment_size
):
    path = add(context)
    payload = path.read_bytes()
    offset = payload.rfind(b"PK\x05\x06")
    end = bytearray(payload[offset:])
    struct.pack_into("<H", end, 20, comment_size)
    path.write_bytes(
        payload[:offset] + b"PK\x06\x07" + bytes(16) + end + b"a" * comment_size
    )
    _database, audit, _library, _renders = context
    key = thumb_key(audit.read("series"))
    opened = Mock(side_effect=AssertionError("opened unbounded ZIP64 directory"))
    monkeypatch.setattr(zipfile, "ZipFile", opened)
    with pytest.raises(ValueError, match="preview is unavailable"):
        await audit.thumbnail("series", "chapter", "first", key)
    opened.assert_not_called()


async def test_edge_size_limit_and_changed_thumbnail_revision_fail_closed(
    context, monkeypatch
):
    path = add(context)
    _database, audit, _library, renders = context
    key = thumb_key(audit.read("series"))
    monkeypatch.setattr("tankarr.series_audit.MAX_PAGE_BYTES", 1)
    with pytest.raises(ValueError, match="preview is unavailable"):
        await audit.thumbnail("series", "chapter", "last", key)
    assert not renders
    path.write_bytes(b"replacement")
    with pytest.raises(StaleAudit):
        await audit.thumbnail("series", "chapter", "first", key)


async def test_symlinked_thumbnail_cache_cannot_redirect_writes(context, tmp_path):
    add(context)
    _database, audit, _library, renders = context
    key = thumb_key(audit.read("series"))
    outside = tmp_path / "outside-cache"
    outside.mkdir()
    audit.cache_root.mkdir(parents=True)
    audit.thumbnails.root.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlinked"):
        await audit.thumbnail("series", "chapter", "first", key)
    assert not renders and list(outside.iterdir()) == []
