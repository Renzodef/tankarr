from __future__ import annotations

from pathlib import Path

from tankarr.catalogue import (
    CATALOGUE_PROVIDER,
    catalogue_card,
    manga_from_record,
    parse_catalogue_id,
)
from tankarr.database import Database
from tankarr.monitoring import evaluate_future_monitoring


def record(**overrides) -> dict:
    base = {
        "source": "mangabaka",
        "external_id": "12345",
        "title": "ワンピース",
        "localized_titles": {"en": "One Piece"},
        "alternate_titles": ["One Piece", "OP"],
        "description": "Pirates.",
        "authors": ["Eiichiro Oda"],
        "year": 1997,
        "publication_year": 1997,
        "status": "ongoing",
        "work_type": "Manga",
        "original_language": "ja",
        "volume_count": 110,
        "chapter_count": None,
        "latest_release_chapter": 1160,
        "rating": 9.1,
        "genres": ["Action", "Adventure"],
        "cover": {"url": "https://cdn.mangabaka.org/12345.jpg"},
        "external_sources": [],
    }
    base.update(overrides)
    return base


def test_parse_catalogue_id_accepts_prefixed_plain_and_url_forms():
    assert parse_catalogue_id("mb:123") == "123"
    assert parse_catalogue_id("mangabaka:123") == "123"
    assert parse_catalogue_id("123") == "123"
    assert parse_catalogue_id("https://mangabaka.org/12345/one-piece") == "12345"
    assert parse_catalogue_id("https://mangabaka.dev/series/77") == "77"
    assert parse_catalogue_id("suwayomi-7") is None
    assert parse_catalogue_id("") is None


def test_catalogue_card_prefers_english_title_and_carries_counts():
    card = catalogue_card(record())
    assert card["id"] == "mb:12345" and card["provider"] == CATALOGUE_PROVIDER
    assert card["title"] == "One Piece" and card["native_title"] == "ワンピース"
    assert card["cover_url"] == "https://cdn.mangabaka.org/12345.jpg"
    assert card["volume_count"] == 110 and card["latest_release_chapter"] == 1160
    assert card["year"] == 1997 and card["publication_year"] == 1997
    assert card["source_name"] == "MangaBaka"
    assert card["source_url"] == "https://mangabaka.org/12345"


def test_manga_from_record_is_a_valid_series_row(tmp_path: Path):
    manga = manga_from_record(record(), language="en")
    assert manga["provider"] == CATALOGUE_PROVIDER and manga["source_id"] == "12345"
    assert manga["last_volume"] == "110" and manga["last_chapter"] is None
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    saved = database.upsert_manga(manga, "en", "all")
    stored = database.get_manga(saved["id"])
    assert stored["title"] == "One Piece" and stored["provider"] == CATALOGUE_PROVIDER
    assert stored["source_id"] == "12345"
    # An ongoing work with no chapters yet can still be monitored.
    allowed, _reason = evaluate_future_monitoring(manga, [])
    assert allowed is True
