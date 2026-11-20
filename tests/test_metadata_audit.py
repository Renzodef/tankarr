from __future__ import annotations

from pathlib import Path

import pytest

from tankarr.config import Settings
from tankarr.database import Database
from tankarr.metadata_audit import (
    library_metadata_findings,
    series_metadata_findings,
    title_language_hint,
)


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Il re del calcio", "it"),
        ("Le Chevalier des ombres", "fr"),
        ("Die Chronik der Unterwelt", "de"),
        ("The King of Football", None),
        ("Eyes Behind", None),
        ("Les Misérables", None),
        ("Romance Killer", None),
        ("超霸世紀", None),
        ("", None),
    ],
)
def test_title_language_hint(title: str, expected: str | None):
    assert title_language_hint(title) == expected


def test_findings_name_each_gap():
    manga = {
        "title": "Il re del calcio",
        "authors": ["Hui King Sum"],
        "preferred_language": "en",
    }

    findings = series_metadata_findings(manga, [], has_external_metadata=False)

    assert [finding["code"] for finding in findings] == [
        "no_external_metadata",
        "authors_not_linked",
        "title_language_mismatch",
    ]
    assert findings[2]["detected_language"] == "it"
    assert findings[2]["series_language"] == "en"


def test_complete_series_has_no_findings():
    manga = {
        "title": "Il re del calcio",
        "authors": ["Hui King Sum"],
        "preferred_language": "it",
    }
    links = [{"id": "author-1", "name": "Hui King Sum"}]

    assert series_metadata_findings(manga, links, has_external_metadata=True) == []


def test_library_report_lists_only_series_with_findings(tmp_path: Path):
    settings = Settings(
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "library",
        metadata_enabled=False,
        monitor_enabled=False,
    )
    database = Database(settings.database_path)
    database.initialize()
    for manga_id, title in (("local-it", "Il re del calcio"), ("local-en", "Eyes")):
        database.upsert_manga(
            {
                "id": manga_id,
                "provider": "local",
                "title": title,
                "description": "",
                "authors": [],
                "status": "completed",
                "original_language": "zh",
                "available_languages": ["en"],
            },
            "en",
            "none",
        )
    database.save_metadata_source_record(
        "local-en",
        entity_type="work",
        entity_key="",
        source="mangabaka",
        external_id="7",
        match_confidence=1.0,
        match_reason="test",
        data={"external_id": "7", "title": "Eyes"},
        raw={},
    )

    report = library_metadata_findings(database)

    assert [item["manga_id"] for item in report["series"]] == ["local-it"]
    assert report["counts"] == {
        "no_external_metadata": 1,
        "title_language_mismatch": 1,
    }
