"""A work measured both ways says both numbers, wherever it is shown."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tankarr.app import create_app
from tankarr.config import Settings
from tests.test_deletion import (
    add_downloaded_chapter,
    chapter,
    manga,
    provision_library_identity,
)


@pytest.fixture
def counted_client(tmp_path):
    settings = Settings(
        data_dir=tmp_path / "data", library_dir=tmp_path / "library", reader_kind="none"
    )
    provision_library_identity(settings)
    app = create_app(settings)
    database = app.state.database
    add_downloaded_chapter(database, settings.library_dir, manga(), chapter("c1", "1"))
    database.update_manga("manga-1", {"series_unit_override": "volumes"})
    database.save_series_metadata(
        "manga-1",
        {"volume_count": 22, "chapter_count": 249},
        artwork_path=None,
        artwork_sha256=None,
        artwork_media_type=None,
        source_status=[],
    )
    client = TestClient(app)
    try:
        yield client
    finally:
        client.close()


def test_the_card_and_the_page_agree_on_both_counts(counted_client):
    # The catalogues count 22 books and 249 chapters. The grid read them and
    # the series page did not, so the same work claimed "249 chapters" on one
    # screen and "? chapters" on the other.
    card = next(
        item
        for item in counted_client.get("/api/manga").json()
        if item["id"] == "manga-1"
    )
    page = counted_client.get("/api/manga/manga-1").json()
    assert card["chapter_total_count"] == 249
    assert card["book_total_count"] == 22
    assert page["chapter_total_count"] == card["chapter_total_count"]
    assert page["book_total_count"] == card["book_total_count"]


def test_a_total_typed_by_hand_is_the_number_everywhere(counted_client):
    # The operator counted the shelf and typed 24 books in Edit. The card
    # derived its own book count from the mapped slots and the catalogue,
    # so the two screens could disagree with the number the operator set.
    saved = counted_client.patch(
        "/api/manga/manga-1", json={"expected_count_override": 24}
    )
    assert saved.status_code == 200, saved.text
    card = next(
        item
        for item in counted_client.get("/api/manga").json()
        if item["id"] == "manga-1"
    )
    page = counted_client.get("/api/manga/manga-1").json()
    assert page["expected_count_unit_override"] == "volume"
    header = page["library_count"]["total_count"]
    assert header == 24
    assert card["book_total_count"] == header
    assert page["book_total_count"] == header


def test_the_grid_reads_the_map_the_way_the_page_does(tmp_path):
    # A catalogue that logged a handful of volumes for a 500-chapter webtoon
    # is a hint, not a measured map. The series page demotes it; the grid
    # read the stored rows straight from the table and believed it, so the
    # same work showed books on the card that its own page never lists.
    from tankarr.chapter_map import MapEntry
    from tankarr.database import Database
    from tankarr.library_snapshot import changed_inputs

    settings = Settings(
        data_dir=tmp_path / "data", library_dir=tmp_path / "library", reader_kind="none"
    )
    provision_library_identity(settings)
    app = create_app(settings)
    database: Database = app.state.database
    add_downloaded_chapter(database, settings.library_dir, manga(), chapter("c1", "1"))
    database.replace_chapter_map(
        "manga-1",
        "mangaupdates",
        [
            MapEntry(
                volumes=(str(volume),),
                chapters=(str(volume * 20),),
                exact=True,
                source="mangaupdates",
            )
            for volume in range(1, 5)
        ],
    )
    from_page = database.chapter_map("manga-1")
    from_grid = changed_inputs(database, ["manga-1"])["manga-1"]["chapter_map"]
    assert from_grid == from_page


def test_manual_chapter_count_ignores_specials_and_split_file_count(counted_client):
    app = counted_client.app
    db = app.state.database
    settings = app.state.settings
    for label in ["1.5", "2.1", "2.2"]:
        add_downloaded_chapter(
            db, settings.library_dir, manga(), chapter(f"c-{label}", label)
        )
    db.update_manga("manga-1", {"series_unit_override": "chapters"})
    # Four physical files cover chapters 1 and 2 plus an extra. The manual
    # main-chapter total can be two, but not one: no file is removed.
    saved = counted_client.patch(
        "/api/manga/manga-1", json={"expected_count_override": 2}
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["expected_count_unit_override"] == "chapter"
    assert len([r for r in db.list_chapters("manga-1", "en") if r["downloaded"]]) == 4
    rejected = counted_client.patch(
        "/api/manga/manga-1", json={"expected_count_override": 1}
    )
    assert rejected.status_code == 409
    assert db.get_manga("manga-1")["expected_count_override"] == 2


def test_complete_printed_book_map_beats_catalogue_count_in_both_headers(
    counted_client,
):
    database = counted_client.app.state.database
    database.update_manga("manga-1", {"edition_book_count": 2})
    response = counted_client.put(
        "/api/manga/manga-1/chapter-map/ocr",
        json={
            "books": [
                {"volume": "1", "first_chapter": "1", "last_chapter": "7"},
                {"volume": "2", "first_chapter": "8", "last_chapter": "13"},
            ]
        },
    )
    assert response.status_code == 200
    page = counted_client.get("/api/manga/manga-1").json()
    card = next(
        m for m in counted_client.get("/api/manga").json() if m["id"] == "manga-1"
    )
    assert page["chapter_total_count"] == card["chapter_total_count"] == 13
