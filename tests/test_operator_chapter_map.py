from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tankarr.chapter_map import MapEntry
from tankarr.operator_map import OperatorChapterMap, register_chapter_map_routes
from tests.test_deletion import chapter, make_service, manga


@pytest.fixture
def mapped_series(tmp_path):
    database, service, _reader = make_service(tmp_path)
    database.upsert_manga(manga(), "en", "all")
    database.update_manga("manga-1", {"series_unit_override": "volumes"})
    database.upsert_chapters(
        "manga-1",
        [chapter(f"ch-{n}", n, volume="99") for n in ("0", "1", "2", "2.5", "3", "4")]
        + [
            {
                **chapter(f"book-{n}", "", volume=n),
                "chapter": None,
                "release_unit": "volume",
            }
            for n in ("1", "2")
        ],
    )
    database.mark_chapter_downloaded("book-1", tmp_path / "book-1.cbz")
    database.replace_chapter_map(
        "manga-1",
        "mangabaka",
        [MapEntry(volumes=("9",), chapters=("2",), exact=False, source="mangabaka")],
    )
    app = FastAPI()
    register_chapter_map_routes(app, database, service)
    with TestClient(app) as client:
        yield database, service, client


BOUNDARIES = {
    "boundaries": [{"volume": 1, "first_chapter": 0}, {"volume": 2, "first_chapter": 2}]
}
URL = "/api/manga/manga-1/chapter-map"


def save(client, body=BOUNDARIES):
    preview = client.put(URL, params={"dry_run": "true"}, json=body)
    assert preview.status_code == 200, preview.text
    response = client.put(
        URL,
        params={"confirmation_snapshot": preview.json()["confirmation_snapshot"]},
        json=body,
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_preview_apply_and_reset_preserve_other_sources(mapped_series):
    database, _service, client = mapped_series
    before = database.chapter_map("manga-1", raw=True)
    state = client.get(URL).json()
    assert state["boundaries"] == []
    assert state["suggestions"] == [{"volume": "9", "first_chapter": "2"}]
    assert state["last_known_chapter"] == "4"
    preview = client.put(URL, params={"dry_run": "true"}, json=BOUNDARIES).json()
    assert database.chapter_map("manga-1", raw=True) == before
    assert preview["intervals"][0]["chapters"] == ["0", "1"]
    assert preview["intervals"][1]["chapters"] == ["2", "2.5", "3", "4"]
    assert client.put(URL, json=BOUNDARIES).status_code == 409
    result = save(client)
    assert result["chapter_index"]["unit"] == "volume"
    slots = {
        slot["chapter"]: slot
        for slot in result["chapter_index"]["mapped_chapter_slots"]
    }
    assert slots["1"]["covered_by_volume"] == "1"
    assert slots["2"]["downloaded"] is False
    assert {entry.source for entry in database.chapter_map("manga-1", raw=True)} == {
        "operator",
        "mangabaka",
    }
    assert client.delete(URL).status_code == 409
    reset = client.delete(URL, params={"dry_run": "true"}).json()
    assert (
        client.delete(
            URL, params={"confirmation_snapshot": reset["confirmation_snapshot"]}
        ).status_code
        == 200
    )
    assert database.chapter_map("manga-1", raw=True) == before


@pytest.mark.parametrize("change", ["release", "metadata", "map", "monitoring"])
def test_changed_inputs_refuse_confirmation_without_writing(mapped_series, change):
    database, _service, client = mapped_series
    preview = client.put(URL, params={"dry_run": "true"}, json=BOUNDARIES).json()
    if change == "release":
        database.mark_chapter_downloaded("ch-2", database.path.parent / "ch2.cbz")
    elif change == "metadata":
        database.save_series_metadata(
            "manga-1",
            {"chapter_count": 5},
            artwork_path=None,
            artwork_sha256=None,
            artwork_media_type=None,
            source_status=[],
        )
    elif change == "map":
        database.replace_chapter_map(
            "manga-1",
            "official",
            [
                MapEntry(
                    volumes=("2",), chapters=("3", "4"), exact=True, source="official"
                )
            ],
        )
    else:
        database.update_manga("manga-1", {"monitor_mode": "none"})
    response = client.put(
        URL,
        params={"confirmation_snapshot": preview["confirmation_snapshot"]},
        json=BOUNDARIES,
    )
    assert response.status_code == 409
    assert not any(
        entry.source == "operator"
        for entry in database.chapter_map("manga-1", raw=True)
    )


@pytest.mark.parametrize(
    "body",
    [
        {"boundaries": []},
        {"boundaries": [{"volume": 0, "first_chapter": 1}]},
        {"boundaries": [{"volume": 1, "first_chapter": "NaN"}]},
        {
            "boundaries": [
                {"volume": 2, "first_chapter": 2},
                {"volume": 1, "first_chapter": 3},
            ]
        },
        {
            "boundaries": [
                {"volume": 1, "first_chapter": 3},
                {"volume": 2, "first_chapter": 3},
            ]
        },
        {"boundaries": [{"volume": 1, "first_chapter": 5}]},
    ],
)
def test_invalid_boundaries_fail_without_mutation(mapped_series, body):
    database, _service, client = mapped_series
    before = database.chapter_map("manga-1", raw=True)
    assert client.put(URL, params={"dry_run": "true"}, json=body).status_code == 422
    assert database.chapter_map("manga-1", raw=True) == before


def test_only_operator_map_enables_missing_book_chapters(mapped_series):
    database, _service, client = mapped_series
    assert not any(
        row["id"].startswith("ch-")
        for row in database.preferred_missing_releases("manga-1")
    )
    save(client)
    wanted = database.preferred_missing_releases("manga-1")
    assert {row["chapter"] for row in wanted if row["id"].startswith("ch-")} == {
        "2",
        "2.5",
        "3",
        "4",
    }
    assert all(row["volume"] == "2" for row in wanted if row["id"].startswith("ch-"))
    database.mark_chapter_downloaded("ch-2", database.path.parent / "ch-2.cbz")
    assert "ch-2" not in {
        row["id"] for row in database.preferred_missing_releases("manga-1")
    }
    database.mark_chapter_downloaded("book-2", database.path.parent / "book-2.cbz")
    assert database.preferred_missing_releases("manga-1") == []


def test_operator_fallback_respects_ignored_volume_and_explicit_completion(
    mapped_series,
):
    database, _service, client = mapped_series
    save(client)
    database.set_volume_monitor_override("manga-1", "2", "ignored")
    assert database.preferred_missing_releases("manga-1") == []
    database.set_volume_monitor_override("manga-1", "2", "automatic")
    assert database.preferred_missing_releases("manga-1")
    database.update_manga("manga-1", {"library_status_override": "up_to_date"})
    assert database.preferred_missing_releases("manga-1") == []


def test_confirmation_cannot_be_reused_for_different_boundaries(mapped_series):
    database, _service, client = mapped_series
    preview = client.put(URL, params={"dry_run": "true"}, json=BOUNDARIES).json()
    changed = {"boundaries": [{"volume": 1, "first_chapter": 1}]}
    response = client.put(
        URL,
        params={"confirmation_snapshot": preview["confirmation_snapshot"]},
        json=changed,
    )
    assert response.status_code == 409
    assert not any(
        entry.source == "operator"
        for entry in database.chapter_map("manga-1", raw=True)
    )


def test_refresh_keeps_operator_boundaries_and_reports_changed_range(mapped_series):
    database, service, client = mapped_series
    save(client)
    before = [
        entry
        for entry in database.chapter_map("manga-1", raw=True)
        if entry.source == "operator"
    ]
    database.upsert_chapters("manga-1", [chapter("ch-5", "5")])
    database.replace_chapter_map(
        "manga-1",
        "mangabaka",
        [
            MapEntry(
                volumes=("7",),
                chapters=("1", "2", "3", "4", "5"),
                exact=True,
                source="mangabaka",
            )
        ],
    )
    assert [
        entry
        for entry in database.chapter_map("manga-1", raw=True)
        if entry.source == "operator"
    ] == before
    state = OperatorChapterMap(database, service).read("manga-1")
    assert state["boundaries"] == [
        {"volume": "1", "first_chapter": "0"},
        {"volume": "2", "first_chapter": "2"},
    ]
    assert any("not been extended" in warning for warning in state["warnings"])


def test_safe_mode_blocks_apply_but_allows_preview(mapped_series):
    _database, service, client = mapped_series
    preview = client.put(URL, params={"dry_run": "true"}, json=BOUNDARIES).json()
    service.settings.restored_safe_mode = True
    assert client.get(URL).status_code == 200
    assert (
        client.put(
            URL,
            params={"confirmation_snapshot": preview["confirmation_snapshot"]},
            json=BOUNDARIES,
        ).status_code
        == 503
    )


def test_missing_series_is_404(mapped_series):
    _database, _service, client = mapped_series
    assert client.get("/api/manga/unknown/chapter-map").status_code == 404
    assert (
        client.put(
            "/api/manga/unknown/chapter-map",
            params={"dry_run": "true"},
            json=BOUNDARIES,
        ).status_code
        == 404
    )


def test_write_snapshot_rolls_back_nested_map_write(mapped_series):
    database, _service, _client = mapped_series
    before = database.chapter_map("manga-1", raw=True)
    with pytest.raises(RuntimeError, match="abort"):
        with database.write_snapshot():
            database.replace_chapter_map(
                "manga-1",
                "operator",
                [
                    MapEntry(
                        volumes=("1",), chapters=("1",), exact=True, source="operator"
                    )
                ],
            )
            raise RuntimeError("abort")
    assert database.chapter_map("manga-1", raw=True) == before


def test_a_reading_of_the_books_is_stored_as_its_own_source(mapped_series):
    database, _service, client = mapped_series
    url = "/api/manga/manga-1/chapter-map/ocr"
    body = {
        "books": [
            {"volume": "1", "first_chapter": "0", "last_chapter": "1"},
            {"volume": "2", "first_chapter": "2", "last_chapter": "4"},
        ]
    }
    assert client.put(url, json=body).json()["books"] == 2
    sources = {
        (e.source, e.volumes, e.chapters) for e in database.chapter_map("manga-1")
    }
    assert ("ocr", ("1",), ("0", "1")) in sources and (
        "ocr",
        ("2",),
        ("2", "3", "4"),
    ) in sources
    overlapping = {
        "books": [
            {"volume": "1", "first_chapter": "1", "last_chapter": "3"},
            {"volume": "2", "first_chapter": "3", "last_chapter": "4"},
        ]
    }
    assert client.put(url, json=overlapping).status_code == 422
    backwards = {"books": [{"volume": "1", "first_chapter": "5", "last_chapter": "3"}]}
    assert client.put(url, json=backwards).status_code == 422
