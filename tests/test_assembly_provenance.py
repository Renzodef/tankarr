from __future__ import annotations

from xml.etree import ElementTree as ET

import pytest

from tankarr.assembly_provenance import assembly_provenance, proven_assembly
from tankarr.chapter_map import MapEntry
from tankarr.chapter_mapping import suspect_volume_reasons
from tankarr.comicinfo import build_comic_info
from tankarr.series_units import build_series_units
from tankarr.source_ranking import SourceRanking
from tests.test_deletion import chapter, make_service, manga


def proof():
    return {
        "version": 1,
        "chapter_ids": ["c1", "c2"],
        "chapters": ["1", "2"],
        "volume": "1",
        "sources": ["Example source (EN)"],
        "notes": "Assembled from chapters 1-2; sources: Example source (EN)",
    }


def assembled():
    return {
        **chapter("assembled-1", "", volume="1"),
        "chapter": None,
        "release_unit": "volume",
        "provider": "assembled",
        "assembled_from": proof(),
        "library_sha256": "a" * 64,
    }


def test_provenance_persists_with_file_ledger_and_survives_comicinfo_refresh(tmp_path):
    database, _service, _reader = make_service(tmp_path)
    database.upsert_manga(manga(), "en", "all")
    result = database.publish_external_chapter(
        "manga-1", assembled(), tmp_path / "v1.cbz", "a" * 64, "b" * 64
    )
    assert result["assembled_from"] == proof()
    assert proven_assembly(result) == proof()
    xml = ET.fromstring(build_comic_info(manga(), result, 620))
    assert xml.findtext("Notes") == proof()["notes"]
    assert xml.findtext("Number") == "1"
    assert not database.get_manga("manga-1")["assemble_books_automatically"]
    database.update_manga("manga-1", {"assemble_books_automatically": True})
    assert database.get_manga("manga-1")["assemble_books_automatically"]


@pytest.mark.parametrize(
    "changes",
    [
        {"provider": "manual"},
        {"release_unit": "chapter"},
        {"library_sha256": None},
        {"volume": "2"},
        {"assembled_from": ["c1", "c2"]},
    ],
)
def test_unproven_rows_do_not_gain_assembly_trust(changes):
    assert proven_assembly({**assembled(), **changes}) is None


@pytest.mark.parametrize(
    "changes",
    [
        {"version": 2},
        {"chapter_ids": []},
        {"chapter_ids": ["c1", "c1"]},
        {"chapters": ["NaN"]},
        {"sources": "example"},
        {"notes": ""},
    ],
)
def test_invalid_provenance_is_rejected(changes):
    assert assembly_provenance({**proof(), **changes}) is None


@pytest.mark.parametrize("policy", ["prefer_official", "first_available"])
def test_external_book_outranks_assembly_even_when_older_or_smaller(policy):
    ranking = SourceRanking.from_settings(acquisition_policy=policy)
    candidate = assembled()
    candidate.update(pages=700, version=9, publish_at="2020-01-01T00:00:00Z")
    external = {
        **candidate,
        "id": "real-1",
        "provider": "prowlarr",
        "pages": 180,
        "version": 1,
        "publish_at": "2026-01-01T00:00:00Z",
        "assembled_from": None,
    }
    assert ranking.best([candidate, external])["id"] == "real-1"


def test_real_external_publication_clears_assembly_provenance(tmp_path):
    database, _service, _reader = make_service(tmp_path)
    database.upsert_manga(manga(), "en", "all")
    original = assembled()
    database.publish_external_chapter(
        "manga-1", original, tmp_path / "v1.cbz", "a" * 64, "b" * 64
    )
    external = {**original, "provider": "prowlarr", "assembled_from": None}
    result = database.publish_external_chapter(
        "manga-1", external, tmp_path / "v1.cbz", "c" * 64, "d" * 64
    )
    assert result["provider"] == "prowlarr"
    assert result["assembled_from"] is None


def test_changed_boundaries_remove_assembly_coverage_proof():
    release = {**assembled(), "pages": 700, "downloaded": True}
    original = [MapEntry(("1",), ("1", "2"), True, source="operator")]
    changed = [MapEntry(("1",), ("1", "2", "3"), True, source="operator")]
    assert suspect_volume_reasons([release], chapter_map=original) == {}
    assert "1" in suspect_volume_reasons([release], chapter_map=changed)


@pytest.mark.parametrize("last_missing", [False, True])
def test_volume_series_groups_complete_split_chapters_before_assembly(last_missing):
    parts = [
        {
            **chapter("p1", "28.1"),
            "downloaded": True,
            "monitored": True,
            "library_path": "/srv/library/p1.cbz",
        },
        {
            **chapter("p2", "28.2"),
            "downloaded": True,
            "monitored": True,
            "library_path": "/srv/library/p2.cbz",
        },
    ]
    if last_missing:
        parts.append({**chapter("p3", "28.3"), "downloaded": False, "monitored": True})
    result = build_series_units(
        {**manga(), "monitor_mode": "all", "series_unit_override": "volumes"},
        {"volume_count": 1},
        parts,
        [MapEntry(("1",), ("28",), True, source="operator")],
    )
    book = result["books"][0]
    assert book["can_assemble"] is not last_missing
    assert book["covered_by_chapters"] is not last_missing
    assert book["chapter_count"] == 1
    if not last_missing:
        assert [file["id"] for file in book["chapters"][0]["files"]] == ["p1", "p2"]
