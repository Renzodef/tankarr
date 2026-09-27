from pathlib import Path
from zipfile import ZipFile

import pytest

from tankarr.archive import (
    install_atomically,
    package_cbz,
    replace_comic_info,
    validate_cbz,
)


def test_package_and_atomically_install_cbz(tmp_path: Path, monkeypatch):
    page_1 = tmp_path / "0001.jpg"
    page_2 = tmp_path / "0002.png"
    page_1.write_bytes(b"fake-jpeg-content")
    page_2.write_bytes(b"fake-png-content")
    manga = {
        "title": "Example Manga",
        "description": "Summary",
        "authors": ["Author"],
        "metadata": {
            "description": "Enriched summary",
            "authors": ["Author"],
            "creators": [
                {"name": "Author", "role": "writer"},
                {"name": "Artist", "role": "penciller"},
            ],
            "publisher": "Example Press",
            "genres": ["Drama"],
            "tags": ["Manga"],
            "year": 2020,
            "chapter_count": 20,
            "volume_count": 3,
            "work_type": "Manga",
            "reading_direction": "RIGHT_TO_LEFT",
        },
    }
    chapter = {
        "title": "Arrival",
        "chapter": "1",
        "volume": "1",
        "language": "en",
        "provider": "mangadex",
        "source_url": "https://example.test/chapter/1",
        "metadata": {"title": "Volume 1", "isbn": "9780000000001"},
    }
    staging = tmp_path / "staging.cbz"
    package_cbz(staging, [page_1, page_2], manga, chapter)
    info = validate_cbz(staging)
    assert info["page_count"] == 2
    assert len(info["sha256"]) == 64
    with ZipFile(staging) as archive:
        comic_info = archive.read("ComicInfo.xml").decode()
        assert "<LanguageISO>en</LanguageISO>" in comic_info
        assert "<Series>Example Manga</Series>" in comic_info
        assert "<Title>Chapter 1</Title>" in comic_info
        assert "A provider-specific title" not in comic_info
        assert "<Publisher>Example Press</Publisher>" in comic_info
        assert "<Manga>YesAndRightToLeft</Manga>" in comic_info

    destination = tmp_path / "library" / "Example Manga" / "Chapter 1 [en].cbz"
    synced_directories: list[Path] = []
    monkeypatch.setattr("tankarr.archive.fsync_directory", synced_directories.append)
    install_atomically(staging, destination)
    assert destination.exists()
    assert validate_cbz(destination)["sha256"] == info["sha256"]
    assert synced_directories == [destination.parent]


def test_atomic_install_verifies_expected_staging_hash(tmp_path: Path):
    source = tmp_path / "source.cbz"
    source.write_bytes(b"prepared archive")
    destination = tmp_path / "library" / "book.cbz"

    with pytest.raises(ValueError, match="changed before publication"):
        install_atomically(source, destination, expected_sha256="0" * 64)

    assert destination.exists() is False
    assert list(destination.parent.glob("*.partial")) == []


def test_replace_comic_info_adopts_page_only_zip_atomically(tmp_path: Path):
    archive_path = tmp_path / "hand-imported.cbz"
    with ZipFile(archive_path, "w") as archive:
        archive.writestr("001.jpg", b"page-one")
        archive.writestr("002.png", b"page-two")

    result = replace_comic_info(
        archive_path,
        b"<ComicInfo><Volume>1</Volume></ComicInfo>",
        allow_missing=True,
    )

    assert result["changed"] is True
    assert validate_cbz(archive_path)["page_count"] == 2
    with ZipFile(archive_path) as archive:
        assert (
            archive.read("ComicInfo.xml")
            == b"<ComicInfo><Volume>1</Volume></ComicInfo>"
        )
        assert archive.read("001.jpg") == b"page-one"
        assert archive.read("002.png") == b"page-two"


def test_replace_comic_info_rejects_missing_metadata_by_default(tmp_path: Path):
    archive_path = tmp_path / "untrusted.cbz"
    with ZipFile(archive_path, "w") as archive:
        archive.writestr("001.jpg", b"page-one")

    with pytest.raises(ValueError, match="has no ComicInfo"):
        replace_comic_info(archive_path, b"<ComicInfo />")
