import os
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from tankarr.config import Settings
from tankarr.import_limits import (
    ImportLimitError,
    ImportLimits,
    check_page_geometry,
    run_decoder,
)
from tankarr.importer import LibraryImporter, _pdf_extract


def importer(settings):
    instance = object.__new__(LibraryImporter)
    instance.settings = settings
    return instance


@pytest.mark.parametrize("budget", ["pages", "bytes"])
def test_zip_budget_rejects_before_any_member_is_expanded(
    tmp_path, monkeypatch, budget
):
    source = tmp_path / "source.zip"
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("one.jpg", b"x" * (1024**2))
        archive.writestr("two.jpg", b"x" * (1024**2))
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path,
        import_max_pages=1 if budget == "pages" else 100,
        import_max_expanded_bytes=1024**2 if budget == "bytes" else 16 * 1024**3,
    )
    monkeypatch.setattr(
        zipfile.ZipFile,
        "open",
        lambda *args, **kwargs: pytest.fail("oversized archive was expanded"),
    )
    with pytest.raises(ImportLimitError):
        importer(settings)._extract_pages(source, tmp_path / "pages")
    with pytest.raises(ImportLimitError):
        LibraryImporter._fast_source_content_sha256(
            source, ImportLimits.from_settings(settings)
        )


def test_import_stops_before_writing_when_disk_reserve_is_missing(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        "tankarr.import_limits.shutil.disk_usage", lambda _: SimpleNamespace(free=100)
    )
    with pytest.raises(ImportLimitError, match="free space"):
        ImportLimits(reserve_bytes=101).check(0, 0, tmp_path)


@pytest.mark.skipif(os.name != "posix", reason="POSIX hard process budgets")
@pytest.mark.parametrize("kind", ["memory", "file", "timeout"])
def test_real_decoder_is_terminated_at_its_resource_budget(tmp_path, kind):
    target = tmp_path / "output"
    target.mkdir()
    code = {
        "memory": "allocation = bytearray(256 * 1024**2)",
        "file": f"open({str(target / 'page')!r}, 'wb').write(b'x' * 8192)",
        "timeout": "import time; time.sleep(30)",
    }[kind]
    with pytest.raises(ImportLimitError):
        run_decoder(
            [sys.executable, "-c", code],
            target,
            ImportLimits(expanded_bytes=4096, memory_mb=128, reserve_bytes=0),
            timeout=0.3 if kind == "timeout" else 5,
        )
    if kind == "file":
        assert (target / "page").stat().st_size <= 4097


def test_pdf_checks_page_budget_before_rendering_and_stops_incrementally(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("tankarr.importer.pdf_tools_available", lambda: True)
    monkeypatch.setattr("tankarr.importer._pdf_page_count", lambda *args, **kwargs: 10)
    calls = []

    def render(command, *_args, **_kwargs):
        calls.append(command)
        Path(command[-1]).with_suffix(".jpg").write_bytes(b"x" * 600)

    monkeypatch.setattr("tankarr.importer.run_decoder", render)
    with pytest.raises(ImportLimitError, match="page"):
        _pdf_extract(
            tmp_path / "source.pdf",
            tmp_path / "pages",
            limits=ImportLimits(pages=1, reserve_bytes=0),
        )
    assert not calls
    with pytest.raises(ImportLimitError, match="expanded-size"):
        _pdf_extract(
            tmp_path / "source.pdf",
            tmp_path / "pages",
            limits=ImportLimits(expanded_bytes=1000, reserve_bytes=0),
        )
    assert len(calls) == 2
    assert all("-singlefile" in command for command in calls)


def test_pixel_bomb_is_refused_from_its_header_without_decoding(tmp_path, monkeypatch):
    from PIL import Image

    source = tmp_path / "header.png"
    Image.new("RGB", (2, 2)).save(source)
    monkeypatch.setattr("tankarr.import_limits.MAX_PAGE_PIXELS", 3)
    monkeypatch.setattr(
        Image.Image, "load", lambda _self: pytest.fail("pixel allocation attempted")
    )
    with pytest.raises(ImportLimitError, match="megapixel"):
        check_page_geometry(source)


def test_oversized_comic_info_is_rejected_before_xml_expansion(tmp_path, monkeypatch):
    from tankarr.archive import MAX_COMIC_INFO_BYTES, read_comic_info

    source = tmp_path / "metadata.zip"
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("page.jpg", b"fixture")
        archive.writestr("ComicInfo.xml", b"x" * (MAX_COMIC_INFO_BYTES + 1))
    monkeypatch.setattr(
        zipfile.ZipFile,
        "open",
        lambda *args, **kwargs: pytest.fail("metadata expanded before budget"),
    )
    with pytest.raises(ValueError, match="metadata limit"):
        read_comic_info(source)
