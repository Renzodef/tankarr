from copy import deepcopy
from types import SimpleNamespace

import pytest

from tankarr.config import Settings
from tankarr.service import TankarrService


def evidence():
    chapters = [{"id": f"c{i}"} for i in range(1, 11)]
    books = [
        {"id": "book", "release_unit": "volume", "downloaded": True, "volume": "1"}
    ]
    index = {"mapped_chapter_slots": [{"volume": "1", "releases": chapters}]}
    rows = {
        c["id"]: {"verdict": "inside", "volume": "1", "pages": 20, "matched_pages": 20}
        for c in chapters
    }
    return chapters, books, index, rows


@pytest.mark.parametrize(
    "failure",
    [
        "missing",
        "unreadable",
        "weak",
        "wrong_book",
        "duplicate_book",
        "unnumbered_book",
    ],
)
def test_unverified_chapters_are_never_selected_for_retirement(failure):
    chapters, books, index, rows = evidence()
    if failure == "missing":
        rows.pop("c1")
    elif failure == "unreadable":
        rows["c1"]["verdict"] = "unreadable"
    elif failure == "weak":
        rows["c1"]["matched_pages"] = 15
    elif failure == "wrong_book":
        books.append({**books[0], "id": "other", "volume": "2"})
        rows["c1"]["volume"] = "2"
    elif failure == "duplicate_book":
        books.append({**books[0], "id": "duplicate"})
    else:
        books.append({**books[0], "id": "unknown", "volume": None})
    service = SimpleNamespace(
        database=SimpleNamespace(content_alignment=lambda _: rows)
    )
    vetoed = TankarrService.retirement_unverified_chapters(
        service, "series", books, chapters, index
    )
    assert "c1" in vetoed


def test_incomplete_book_cannot_condemn_any_of_its_chapters():
    chapters, books, index, rows = evidence()
    for number in (1, 2):
        rows[f"c{number}"]["verdict"] = "outside"
    service = SimpleNamespace(
        database=SimpleNamespace(content_alignment=lambda _: rows)
    )
    assert TankarrService.retirement_unverified_chapters(
        service, "series", books, chapters, index
    ) == {c["id"] for c in chapters}


def test_verified_book_preserves_unverified_extra_and_allows_proven_chapters():
    chapters, books, index, rows = evidence()
    rows["c1"]["verdict"] = "outside"
    original = deepcopy(rows)
    service = SimpleNamespace(
        database=SimpleNamespace(content_alignment=lambda _: rows)
    )
    assert TankarrService.retirement_unverified_chapters(
        service, "series", books, chapters, index
    ) == {"c1"}
    assert rows == original


async def test_off_never_inspects_or_retires_library(tmp_path):
    service = SimpleNamespace(
        settings=Settings(data_dir=tmp_path, retire_redundant_chapters="off")
    )
    result = await TankarrService.retire_redundant_chapter_files(service, "series")
    assert result["deleted"] == 0
    assert result["reason"] == "retirement disabled"


def test_environment_preserves_existing_guard_configuration(tmp_path, monkeypatch):
    monkeypatch.setenv("TANKARR_RETIRE_REDUNDANT_CHAPTERS", "verified")
    monkeypatch.setenv("TANKARR_RETIRE_RECYCLE", "true")
    settings = Settings(data_dir=tmp_path)
    assert settings.retire_redundant_chapters == "verified"
    assert settings.retire_recycle is True
