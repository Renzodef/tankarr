import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from tankarr.importer import LibraryImporter
from tankarr.page_order import restore_cover_first


def _pages(root: Path, *, reversed_scan: bool) -> tuple[list[Path], Path]:
    cover = root / "cover.png"
    Image.new("RGB", (40, 60), (180, 20, 35)).save(cover)
    pages = []
    for number in range(1, 26):
        path = root / f"{number:04d}.png"
        is_cover = (number == 25) if reversed_scan else (number == 1)
        color = (180, 20, 35) if is_cover else (245, 245, 245)
        Image.new("RGB", (40, 60), color).save(path)
        pages.append(path)
    return pages, cover


def test_reversed_scan_restores_cover_first(tmp_path: Path):
    pages, cover = _pages(tmp_path, reversed_scan=True)

    assert restore_cover_first(pages, cover)
    assert pages[0].name == "0001.png"
    assert pages[-1].name == "0025.png"
    with Image.open(pages[0]) as first:
        assert first.getpixel((0, 0)) == (180, 20, 35)


def test_normal_scan_remains_unchanged(tmp_path: Path):
    pages, cover = _pages(tmp_path, reversed_scan=False)

    assert not restore_cover_first(pages, cover)
    assert pages[0] == tmp_path / "0001.png"
    assert not (tmp_path / ".cover-first").exists()


def test_volume_import_packages_corrected_page_order(tmp_path: Path):
    pages, _cover = _pages(tmp_path, reversed_scan=True)
    importer = SimpleNamespace(
        settings=SimpleNamespace(data_dir=tmp_path),
        database=SimpleNamespace(
            list_volume_metadata=lambda _manga_id: [
                {
                    "volume_key": "1",
                    "artwork_path": "cover.png",
                    "data": {
                        "artwork_source": "catalogue",
                        "artwork_source_url": "https://covers.example.invalid/book.jpg",
                    },
                }
            ]
        ),
    )

    LibraryImporter._package_local_import(
        importer,
        tmp_path / "book.cbz",
        pages,
        {"id": "example", "title": "Example", "authors": []},
        {"volume": "1", "chapter": None, "language": "en", "provider": "manual"},
    )

    with zipfile.ZipFile(tmp_path / "book.cbz") as archive:
        with Image.open(archive.open("0001.png")) as first:
            assert first.getpixel((0, 0)) == (180, 20, 35)


@pytest.mark.parametrize(
    "artwork",
    [
        None,
        {},
        {"artwork_source": "local", "artwork_source_url": "local-volume:book:hash"},
        {
            "artwork_source": "local",
            "artwork_source_url": "https://covers.example.invalid/book.jpg",
        },
        {"artwork_source": "catalogue", "artwork_source_url": "file:///cover.png"},
        {"artwork_source": "catalogue", "artwork_source_url": "https://[invalid"},
    ],
)
def test_import_keeps_order_when_artwork_is_library_derived_or_unproven(
    tmp_path: Path, artwork
):
    # A matching final illustration is not evidence of a reversed book when
    # the reference itself was selected from the book, or has no provenance.
    pages, _cover = _pages(tmp_path, reversed_scan=True)
    expected = [page.read_bytes() for page in pages]
    importer = SimpleNamespace(
        settings=SimpleNamespace(data_dir=tmp_path),
        database=SimpleNamespace(
            list_volume_metadata=lambda _manga_id: [
                {"volume_key": "1", "artwork_path": "cover.png", "data": artwork}
            ]
        ),
    )

    LibraryImporter._package_local_import(
        importer,
        tmp_path / "book.cbz",
        pages,
        {"id": "example", "title": "Example", "authors": []},
        {"volume": "1", "chapter": None, "language": "en", "provider": "manual"},
    )

    assert not (tmp_path / ".cover-first").exists()
    with zipfile.ZipFile(tmp_path / "book.cbz") as archive:
        actual = [
            archive.read(name)
            for name in sorted(archive.namelist())
            if name.endswith(".png")
        ]
    assert actual == expected
