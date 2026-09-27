import sqlite3
from pathlib import Path

import pytest

from tankarr.database import Database, ReleaseBlocked, ReleaseSourceOwned
from tankarr.source_ranking import SourceRanking


def test_persists_primary_and_secondary_official_editions(tmp_path: Path):
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    database.upsert_manga(
        {
            "id": "manga-1",
            "title": "Example",
            "description": "",
            "cover_url": None,
            "authors": [],
            "original_language": "ko",
        },
        "en",
    )
    database.save_series_metadata(
        "manga-1",
        {
            "official_links": [
                {
                    "url": "https://www.webtoons.com/en/example/list?title_no=1",
                    "language": "en",
                },
                {
                    "url": "https://www.webtoons.com/es/example/list?title_no=1",
                    "language": "es",
                },
                {
                    "url": "https://comic.naver.com/webtoon/list?titleId=1",
                    "language": "ko",
                },
            ]
        },
        artwork_path=None,
        artwork_sha256=None,
        artwork_media_type=None,
        source_status=[],
    )

    with database.connect() as connection:
        rows = connection.execute(
            "SELECT host, source_role FROM manga_official_source "
            "WHERE manga_id=? ORDER BY host, source_url",
            ("manga-1",),
        ).fetchall()

    assert [(row["host"], row["source_role"]) for row in rows] == [
        ("comic.naver.com", "secondary_official"),
        ("webtoons.com", "primary_official"),
        ("webtoons.com", "alternative"),
    ]


def test_database_tracks_translation_language_and_jobs(tmp_path: Path):
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    database.upsert_manga(
        {
            "id": "manga-1",
            "title": "Example",
            "description": "",
            "cover_url": None,
            "authors": ["Author"],
            "original_language": "ja",
            "status": "ongoing",
            "year": 2026,
            "last_volume": "4",
            "last_chapter": "20",
            "available_languages": ["en", "it"],
            "source_url": "https://mangadex.org/title/manga-1",
            "source_name": "MangaDex (EN)",
            "source_id": "101",
        },
        "en",
        "all",
    )
    first_sync = database.upsert_chapters(
        "manga-1",
        [
            {
                "id": "chapter-en",
                "chapter": "1",
                "volume": "1",
                "title": "English release",
                "language": "en",
                "provider": "mangadex",
                "groups": ["Group"],
                "publish_at": "2026-01-01T00:00:00Z",
                "source_url": "https://example.test/en",
            },
            {
                "id": "chapter-it",
                "chapter": "1",
                "volume": "1",
                "title": "Italian release",
                "language": "it",
                "provider": "mangadex",
                "groups": [],
                "publish_at": "2026-01-01T00:00:00Z",
                "source_url": "https://example.test/it",
            },
        ],
    )
    assert first_sync["seen"] == 2
    assert set(first_sync["new_chapter_ids"]) == {"chapter-en", "chapter-it"}
    second_sync = database.upsert_chapters("manga-1", [])
    assert second_sync == {"seen": 0, "new_chapter_ids": []}
    assert [item["id"] for item in database.list_chapters("manga-1", "en")] == [
        "chapter-en"
    ]
    manga = database.get_manga("manga-1")
    assert manga["status"] == "ongoing"
    assert manga["year"] == 2026
    assert manga["last_chapter"] == "20"
    assert manga["available_languages"] == ["en", "it"]
    assert manga["source_name"] == "MangaDex (EN)"
    assert manga["source_id"] == "101"
    assert manga["monitor_mode"] == "all"
    assert manga["future_monitoring_allowed"] is True
    job = database.create_job("manga-1", "chapter-en", "en")
    assert job["manga_title"] == "Example"
    assert job["manga_source_name"] == "MangaDex (EN)"
    assert job["manga_source_id"] == "101"
    assert job["chapter_volume"] == "1"
    assert job["chapter_number"] == "1"
    assert job["chapter_title"] == "English release"
    assert job["chapter_groups"] == ["Group"]
    assert job["chapter_provider"] == "mangadex"
    duplicate = database.create_job("manga-1", "chapter-en", "en")
    assert duplicate["id"] == job["id"]
    assert duplicate["manga_title"] == "Example"
    assert database.update_job(job["id"], status="failed")["status"] == "failed"
    retry = database.create_job("manga-1", "chapter-en", "en")
    assert retry["id"] != job["id"]
    assert database.update_job(retry["id"], status="failed")["status"] == "failed"
    jobs = database.list_jobs()
    assert [item["status"] for item in jobs] == ["failed", "failed"]
    assert all(item["manga_title"] == "Example" for item in jobs)
    assert all(item["chapter_number"] == "1" for item in jobs)


def test_changing_translation_language_resets_monitor_baseline(tmp_path: Path):
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    database.upsert_manga(
        {
            "id": "manga-1",
            "title": "Example",
            "description": "",
            "cover_url": None,
            "authors": [],
            "original_language": "ja",
        },
        "en",
    )
    database.record_monitor_result("manga-1")
    assert database.get_manga("manga-1")["monitor_initialized"] is True
    database.update_manga("manga-1", {"preferred_language": "it"})
    manga = database.get_manga("manga-1")
    assert manga["preferred_language"] == "it"
    assert manga["monitor_initialized"] is False


def test_publication_status_override_survives_provider_refresh_and_can_be_reset(
    tmp_path: Path,
):
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    source = {
        "id": "manga-1",
        "title": "Example",
        "description": "",
        "cover_url": None,
        "authors": [],
        "original_language": "ja",
        "status": "ongoing",
    }
    database.upsert_manga(source, "en")

    overridden = database.update_manga("manga-1", {"status_override": "ended"})
    database.upsert_manga({**source, "status": "hiatus"}, "en")
    refreshed = database.get_manga("manga-1")
    automatic = database.update_manga("manga-1", {"status_override": "automatic"})

    assert overridden["status_override"] == "ended"
    assert overridden["status_overridden"] is True
    assert refreshed["status"] == "hiatus"
    assert refreshed["status_override"] == "ended"
    assert automatic["status_override"] is None
    assert automatic["status_overridden"] is False

    with pytest.raises(ValueError, match="Unsupported publication status override"):
        database.update_manga("manga-1", {"status_override": "invented"})


def test_library_and_edition_overrides_are_reversible_and_suppress_candidates(
    tmp_path: Path,
):
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    source = {
        "id": "manga-1",
        "title": "Collected Edition",
        "description": "",
        "cover_url": None,
        "authors": [],
        "original_language": "ja",
    }
    database.upsert_manga(source, "en", "all")
    database.upsert_chapters(
        "manga-1",
        [
            {
                "id": "chapter-1",
                "chapter": None,
                "volume": "2",
                "title": "Missing volume",
                "language": "en",
                "provider": "mangadex",
                "groups": [],
                "publish_at": None,
                "source_url": "https://example.test/volume-2",
            }
        ],
    )
    # A release with no chapter number is a book only once it is filed as
    # one; until then its numbering is unresolved and it cannot be queued.
    database.set_release_book("chapter-1", "2")

    overridden = database.update_manga(
        "manga-1",
        {
            "library_status_override": "up_to_date",
            "expected_count_override": 1,
            "expected_count_unit_override": "volume",
        },
    )
    database.upsert_manga(source, "en", "all")
    refreshed = database.get_manga("manga-1")

    assert overridden["library_status_override"] == "up_to_date"
    assert overridden["library_status_overridden"] is True
    assert overridden["expected_count_override"] == 1
    assert overridden["expected_count_unit_override"] == "volume"
    assert refreshed["library_status_override"] == "up_to_date"
    assert refreshed["expected_count_override"] == 1
    assert database.preferred_download_candidates("manga-1") == []

    automatic = database.update_manga(
        "manga-1",
        {
            "library_status_override": "automatic",
            "expected_count_override": "automatic",
        },
    )
    assert automatic["library_status_override"] is None
    assert automatic["expected_count_override"] is None
    assert automatic["expected_count_unit_override"] is None
    assert [
        item["id"] for item in database.preferred_download_candidates("manga-1")
    ] == ["chapter-1"]

    with pytest.raises(ValueError, match="Unsupported library status override"):
        database.update_manga("manga-1", {"library_status_override": "downloaded"})
    with pytest.raises(ValueError, match="positive integer"):
        database.update_manga(
            "manga-1",
            {
                "expected_count_override": 1.5,
                "expected_count_unit_override": "volume",
            },
        )


def test_initialize_migrates_existing_manga_rows_without_enabling_downloads(
    tmp_path: Path,
):
    path = tmp_path / "tankarr.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute(
        """
        CREATE TABLE manga (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            cover_url TEXT,
            authors_json TEXT NOT NULL DEFAULT '[]',
            original_language TEXT,
            preferred_language TEXT NOT NULL DEFAULT 'en',
            monitored INTEGER NOT NULL DEFAULT 1,
            auto_download INTEGER NOT NULL DEFAULT 0,
            monitor_initialized INTEGER NOT NULL DEFAULT 0,
            last_checked_at TEXT,
            last_check_error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        INSERT INTO manga (
            id, title, preferred_language, created_at, updated_at
        ) VALUES ('legacy', 'Legacy title', 'en', 'now', 'now')
        """
    )
    connection.commit()
    connection.close()

    database = Database(path)
    database.initialize()
    manga = database.get_manga("legacy")

    assert manga["monitor_mode"] == "none"
    assert manga["available_languages"] == []
    assert manga["status"] is None
    assert manga["status_override"] is None
    assert manga["library_status_override"] is None
    assert manga["expected_count_override"] is None
    assert manga["expected_count_unit_override"] is None


def test_create_job_rejects_stale_or_cross_manga_chapter(tmp_path: Path):
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    for manga_id in ("manga-1", "manga-2"):
        database.upsert_manga(
            {
                "id": manga_id,
                "title": manga_id,
                "description": "",
                "cover_url": None,
                "authors": [],
                "original_language": "ja",
            },
            "en",
            "none",
        )
    database.upsert_chapters(
        "manga-1",
        [
            {
                "id": "chapter-1",
                "chapter": "1",
                "volume": "1",
                "title": "Chapter 1",
                "language": "en",
                "provider": "mangadex",
                "groups": [],
                "publish_at": None,
                "source_url": "https://example.test/chapter-1",
            }
        ],
    )

    with pytest.raises(KeyError):
        database.create_job("manga-2", "chapter-1", "en")

    database.delete_manga_record("manga-1")
    with pytest.raises(KeyError):
        database.create_job("manga-1", "chapter-1", "en")
    assert database.list_jobs() == []


def test_provider_priority_dominates_release_preference(tmp_path: Path):
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    database.upsert_manga(
        {
            "id": "manga-1",
            "title": "Priority",
            "description": "",
            "cover_url": None,
            "authors": [],
            "original_language": "ja",
        },
        "en",
        "all",
    )
    database.upsert_chapters(
        "manga-1",
        [
            {
                "id": "release-mangapill",
                "chapter": "1",
                "volume": None,
                "title": "Chapter 1",
                "language": "en",
                "provider": "mangapill",
                "groups": [],
                "publish_at": "2026-02-01T00:00:00+00:00",
                "source_url": "https://example.test/mangapill-1",
                "pages": 40,
                "version": 3,
            },
            {
                "id": "release-mangadex",
                "chapter": "1",
                "volume": None,
                "title": "Chapter 1",
                "language": "en",
                "provider": "mangadex",
                "groups": [],
                "publish_at": "2026-01-01T00:00:00+00:00",
                "source_url": "https://example.test/mangadex-1",
                "pages": 20,
                "version": 1,
            },
        ],
    )

    # Default order prefers MangaDex even though the MangaPill release carries
    # higher version and page counts: those signals only rank within a source.
    assert [
        item["id"] for item in database.preferred_download_candidates("manga-1")
    ] == ["release-mangadex"]

    database.source_ranking = SourceRanking.from_settings(
        provider_priority=("mangapill", "mangadex", "suwayomi")
    )
    assert [
        item["id"] for item in database.preferred_download_candidates("manga-1")
    ] == ["release-mangapill"]

    # Providers outside the explicit order fall back to their source class:
    # MangaDex is a curated scan catalogue, MangaPill an aggregator.
    database.source_ranking = SourceRanking.from_settings(
        provider_priority=("suwayomi",)
    )
    preferred = database.preferred_download_candidates("manga-1")
    assert [item["id"] for item in preferred] == ["release-mangadex"]


def test_series_artwork_candidates_keep_manual_preference_across_refreshes(
    tmp_path: Path,
):
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    database.upsert_manga(
        {
            "id": "manga-1",
            "title": "Example",
            "description": "",
            "cover_url": None,
            "authors": [],
            "original_language": "ja",
        },
        "en",
        "none",
    )
    first = [
        {
            "candidate_id": "candidate-automatic",
            "source": "myanimelist",
            "source_url": "https://cdn.myanimelist.net/a.jpg",
            "path": "metadata/artwork/manga-1/a.jpg",
            "sha256": "a" * 64,
            "media_type": "image/jpeg",
            "source_width": 800,
            "source_height": 1200,
            "score": 100.0,
            "source_priority": 34,
        },
        {
            "candidate_id": "candidate-manual",
            "source": "mangaupdates",
            "source_url": "https://cdn.mangaupdates.com/b.jpg",
            "path": "metadata/artwork/manga-1/b.jpg",
            "sha256": "b" * 64,
            "media_type": "image/jpeg",
            "source_width": 600,
            "source_height": 900,
            "score": 90.0,
            "source_priority": 30,
        },
    ]
    database.replace_series_artwork_candidates(
        "manga-1", first, automatic_candidate_id="candidate-automatic"
    )
    database.set_series_artwork_preference("manga-1", "candidate-manual")

    database.replace_series_artwork_candidates(
        "manga-1", [first[0]], automatic_candidate_id="candidate-automatic"
    )

    assert database.get_series_artwork_preference("manga-1") == "candidate-manual"
    assert {
        item["candidate_id"]
        for item in database.list_series_artwork_candidates("manga-1")
    } == {"candidate-automatic", "candidate-manual"}

    database.set_series_artwork_preference("manga-1", None)
    database.replace_series_artwork_candidates(
        "manga-1", [first[0]], automatic_candidate_id="candidate-automatic"
    )
    assert [
        item["candidate_id"]
        for item in database.list_series_artwork_candidates("manga-1")
    ] == ["candidate-automatic"]


def test_uploaded_series_artwork_remains_available_after_automatic_refresh(
    tmp_path: Path,
):
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    database.upsert_manga(
        {
            "id": "manga-1",
            "title": "Example",
            "description": "",
            "cover_url": None,
            "authors": [],
            "original_language": "ja",
        },
        "en",
        "none",
    )
    automatic = {
        "candidate_id": "candidate-automatic",
        "source": "myanimelist",
        "source_url": "https://cdn.myanimelist.net/a.jpg",
        "path": "metadata/artwork/manga-1/a.jpg",
        "sha256": "a" * 64,
        "media_type": "image/jpeg",
        "source_width": 800,
        "source_height": 1200,
        "score": 100.0,
        "source_priority": 34,
    }
    uploaded = {
        "candidate_id": "upload-custom",
        "source": "upload",
        "source_url": "user-upload",
        "path": "metadata/artwork/manga-1/upload.jpg",
        "sha256": "b" * 64,
        "media_type": "image/jpeg",
        "source_width": 700,
        "source_height": 700,
        "score": 0,
        "source_priority": 0,
    }
    database.replace_series_artwork_candidates(
        "manga-1", [automatic], automatic_candidate_id="candidate-automatic"
    )
    database.upsert_series_artwork_candidate("manga-1", uploaded)
    database.set_series_artwork_preference("manga-1", "upload-custom")

    database.replace_series_artwork_candidates(
        "manga-1", [automatic], automatic_candidate_id="candidate-automatic"
    )
    database.set_series_artwork_preference("manga-1", None)
    database.replace_series_artwork_candidates(
        "manga-1", [automatic], automatic_candidate_id="candidate-automatic"
    )

    candidates = database.list_series_artwork_candidates("manga-1")
    assert {item["candidate_id"] for item in candidates} == {
        "candidate-automatic",
        "upload-custom",
    }
    assert (
        next(item for item in candidates if item["candidate_id"] == "upload-custom")[
            "is_automatic"
        ]
        is False
    )


def test_provider_refresh_keeps_reclassified_volume_units(tmp_path: Path):
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    database.upsert_manga(
        {
            "id": "manga-1",
            "title": "Beck",
            "description": "",
            "cover_url": None,
            "authors": [],
            "original_language": "ja",
        },
        "en",
        "all",
    )
    provider_rows = [
        {
            "id": f"pill-{number}",
            "chapter": str(number),
            "volume": None,
            "title": f"Chapter {number}",
            "language": "en",
            "provider": "mangapill",
            "groups": [],
            "publish_at": None,
            "source_url": f"https://example.test/{number}",
        }
        for number in (1, 2, 3)
    ]
    database.upsert_chapters("manga-1", provider_rows)

    assert database.reclassify_provider_releases_as_volumes("manga-1", "mangapill") == 3
    rows = {row["id"]: row for row in database.list_all_chapters("manga-1")}
    assert rows["pill-2"]["volume"] == "2"
    assert rows["pill-2"]["chapter"] is None
    assert rows["pill-2"]["release_unit"] == "volume"

    # The provider keeps calling them chapters on every refresh; the
    # reclassified unit must survive the upsert.
    database.upsert_chapters("manga-1", provider_rows)
    rows = {row["id"]: row for row in database.list_all_chapters("manga-1")}
    assert rows["pill-2"]["volume"] == "2"
    assert rows["pill-2"]["chapter"] is None
    assert rows["pill-2"]["release_unit"] == "volume"
    # Nothing left to reclassify, and unrelated providers are untouched.
    assert database.reclassify_provider_releases_as_volumes("manga-1", "mangapill") == 0


def test_restore_reclassified_chapters_uses_the_explicit_title_number(tmp_path: Path):
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    database.upsert_manga(
        {"id": "manga-1", "title": "Webtoon", "description": "", "authors": []},
        "en",
    )
    row = {
        "id": "episode-17",
        "chapter": "17",
        "volume": None,
        "title": "Episode 17 (ch. 17)",
        "language": "en",
        "provider": "suwayomi",
        "groups": [],
        "publish_at": None,
        "source_url": "https://example.test/17",
    }
    database.upsert_chapters("manga-1", [row])
    database.reclassify_provider_releases_as_volumes("manga-1", "suwayomi")

    assert database.restore_reclassified_chapters({"episode-17": "17"}) == 1
    restored = database.get_chapter("episode-17")
    assert restored["release_unit"] == "chapter"
    assert restored["volume"] is None
    assert restored["chapter"] == restored["canonical_chapter"] == "17"
    assert restored["numbering_method"] == "title_explicit"


def test_initialize_repairs_legacy_volume_rows_marked_as_chapters(tmp_path: Path):
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    database.upsert_manga(
        {
            "id": "manga-1",
            "title": "Basara",
            "description": "",
            "cover_url": None,
            "authors": [],
            "original_language": "ja",
        },
        "en",
        "all",
    )
    database.upsert_chapters(
        "manga-1",
        [
            {
                "id": "local-volume-1",
                "chapter": None,
                "volume": "1",
                "release_unit": "volume",
                "title": "Volume 1",
                "language": "en",
                "provider": "local",
                "groups": [],
                "publish_at": None,
                "source_url": "file:///library/Basara%20-%20v001.cbz",
            },
            {
                "id": "numbered-omake",
                "chapter": "1",
                "source_chapter": "1",
                "volume": "54",
                "title": "Vol.54 Kingdom Guidebook Omake 1",
                "language": "en",
                "provider": "suwayomi",
                "groups": [],
                "publish_at": None,
                "source_url": "https://mirror.example/guidebook-1",
            },
        ],
    )
    with database.connect() as connection:
        connection.execute(
            """
            UPDATE chapter_release
            SET release_unit='chapter', numbering_status='unmapped',
                numbering_method='source_identity'
            WHERE id='local-volume-1'
            """
        )
        connection.execute(
            """
            UPDATE chapter_release
            SET chapter=NULL, numbering_status='provisional',
                numbering_method='rank_after_specials'
            WHERE id='numbered-omake'
            """
        )

    database.initialize()

    repaired = database.get_chapter("local-volume-1")
    assert repaired["release_unit"] == "volume"
    assert repaired["numbering_status"] == "mapped"
    assert repaired["numbering_method"] == "volume_identity"
    omake = database.get_chapter("numbered-omake")
    assert omake["release_unit"] == "chapter"
    assert omake["numbering_status"] == "unmapped"
    assert omake["numbering_method"] == "special_title"


def test_pending_numbering_is_reconciled_automatically_on_refresh(tmp_path: Path):
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    database.upsert_manga(
        {
            "id": "manga-1",
            "title": "Example",
            "description": "",
            "cover_url": None,
            "authors": [],
            "original_language": "ja",
        },
        "en",
        "all",
    )
    provider_release = {
        "id": "source-412",
        "chapter": "412",
        "source_chapter": "412",
        "canonical_chapter": None,
        "numbering_status": "pending_evidence",
        "numbering_method": "awaiting_secondary_official_evidence",
        "title": "Episode 393",
        "volume": None,
        "language": "en",
        "provider": "suwayomi",
        "source_key": "suwayomi:webtoons",
        "source_name": "Webtoons.com (EN)",
        "groups": [],
        "publish_at": None,
        "source_url": "https://www.webtoons.com/episode/412",
    }
    database.upsert_chapters("manga-1", [provider_release])

    database.upsert_chapters(
        "manga-1", [{**provider_release, "title": "Episode 393 refreshed"}]
    )
    refreshed = database.get_chapter("source-412")
    assert refreshed["source_chapter"] == "412"
    assert refreshed["chapter"] is None
    assert refreshed["numbering_status"] == "pending_evidence"


def test_numbering_shadow_reports_without_writing(tmp_path: Path):
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    database.upsert_manga(
        {
            "id": "manga-shadow",
            "title": "Example",
            "description": "",
            "cover_url": None,
            "authors": [],
            "original_language": "ja",
        },
        "en",
        "all",
    )
    database.upsert_chapters(
        "manga-shadow",
        [
            {
                "id": "pending-release",
                "chapter": "8",
                "source_chapter": "8",
                "canonical_chapter": None,
                "numbering_status": "pending_evidence",
                "numbering_method": "awaiting_official_evidence",
                "title": "Episode 8",
                "volume": None,
                "language": "en",
                "provider": "suwayomi",
                "source_key": "suwayomi:mirror",
                "source_name": "Mirror",
                "groups": [],
                "publish_at": None,
                "source_url": "https://mirror.example/8",
            }
        ],
    )

    report = database.shadow_numbering(["manga-shadow"])

    assert report[0]["statuses"] == {"pending_evidence": 1}
    assert (
        database.get_chapter("pending-release")["numbering_status"]
        == "pending_evidence"
    )


def test_metadata_refresh_reconciles_without_touching_file_state_or_stale_jobs(
    tmp_path: Path,
):
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    database.upsert_manga(
        {
            "id": "finished-series",
            "title": "Finished series",
            "description": "",
            "cover_url": None,
            "authors": [],
            "original_language": "en",
            "status": "ongoing",
        },
        "en",
        "all",
    )

    def save_metadata() -> None:
        database.save_series_metadata(
            "finished-series",
            {
                "status": "ended",
                # This is a work cardinality, not a numeric identity ceiling.
                "chapter_count": 6,
                "official_links": [
                    {
                        "url": "https://www.webtoons.com/en/example/list",
                        "language": "en",
                    }
                ],
            },
            artwork_path=None,
            artwork_sha256=None,
            artwork_media_type=None,
            source_status=[],
        )

    official = [
        {
            "id": f"official-{number}",
            "chapter": str(number),
            "volume": None,
            "title": f"Chapter {number}",
            "language": "en",
            "provider": "suwayomi",
            "source_key": "suwayomi:webtoons",
            "source_name": "Webtoons.com (EN)",
            "groups": [],
            "publish_at": None,
            "source_url": f"https://www.webtoons.com/en/example/{number}",
        }
        for number in range(1, 6)
    ]
    mirror = [
        {
            **official[0],
            "id": f"mirror-{number}",
            "chapter": str(number),
            "title": f"Chapter {number}",
            "source_key": "suwayomi:mirror",
            "source_name": "Mirror (EN)",
            "source_url": f"https://mirror.example/{number}",
        }
        for number in [1, 2, 3, 4, 5, 7]
    ]
    database.upsert_chapters("finished-series", [*official, *mirror])

    # Before the catalogue identifies the official host, provider identity is
    # retained.  A queued job can therefore exist when stronger evidence later
    # reveals that the source sequence has a gap.
    assert database.get_chapter("mirror-7")["numbering_method"] == "source_identity"
    library_path = tmp_path / "library" / "chapter-7.cbz"
    database.mark_chapter_downloaded("mirror-7", library_path, "7" * 64)
    queued = database.create_job("finished-series", "mirror-7", "en")

    save_metadata()

    unresolved = database.get_chapter("mirror-7")
    assert unresolved["chapter"] is None
    assert unresolved["numbering_status"] == "pending_evidence"
    assert unresolved["numbering_method"] == ("awaiting_secondary_official_evidence")
    # The final atomic claim rechecks the now-unresolved identity and removes
    # stale work before any download or publication can start.
    assert database.claim_queued_job(queued["id"], library_path) is None
    with pytest.raises(KeyError):
        database.get_job(queued["id"])

    downloaded = database.get_chapter("mirror-7")
    assert downloaded["downloaded"] is True
    assert downloaded["library_path"] == str(library_path)
    assert downloaded["library_sha256"] == "7" * 64


def test_orphaned_volume_units_are_repaired(tmp_path: Path):
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    database.upsert_manga(
        {
            "id": "manga-1",
            "title": "Beck",
            "description": "",
            "cover_url": None,
            "authors": [],
            "original_language": "ja",
        },
        "en",
        "all",
    )
    row = {
        "id": "pill-5",
        "chapter": "5",
        "volume": None,
        "title": "",
        "language": "en",
        "provider": "mangapill",
        "groups": [],
        "publish_at": None,
        "source_url": "https://example.test/5",
    }
    database.upsert_chapters("manga-1", [row])
    # Simulate the pre-fix overwrite: unit says volume, numbers say chapter.
    with database.connect() as connection:
        connection.execute(
            "UPDATE chapter_release SET release_unit='volume', chapter='5', volume=NULL WHERE id='pill-5'"
        )
    assert database.repair_reclassified_releases("manga-1") == 1
    repaired = database.get_chapter("pill-5")
    assert repaired["volume"] == "5" and repaired["chapter"] is None
    assert database.repair_reclassified_releases("manga-1") == 0


def test_retire_stale_provider_identities_keeps_downloaded_and_busy_rows(
    tmp_path: Path,
):
    from tankarr.providers.suwayomi import suwayomi_id_is_current

    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    database.upsert_manga(
        {
            "id": "m1",
            "title": "Stale",
            "description": "",
            "cover_url": None,
            "authors": [],
            "original_language": "ja",
        },
        "en",
        "all",
    )
    for provider_manga_id in ("suwayomi-1079", "suwayomi-abcd1234-400"):
        database.upsert_release_source(
            "m1",
            provider="suwayomi",
            provider_manga_id=provider_manga_id,
            title="Stale",
            source_url=None,
            source_name="Weeb Central (EN)",
            language="en",
            match_confidence=1.0,
            match_reason="test",
        )

    def release(identifier: str, number: str) -> dict:
        return {
            "id": identifier,
            "chapter": number,
            "volume": None,
            "title": "",
            "language": "en",
            "provider": "suwayomi",
            "groups": [],
            "publish_at": None,
            "source_url": "https://x/" + identifier,
            "pages": 20,
            "version": 1,
        }

    database.upsert_chapters(
        "m1",
        [
            release("suwayomi-5027", "1"),
            release("suwayomi-5028", "2"),
            release("suwayomi-5029", "3"),
            release("suwayomi-abcd1234-9", "4"),
        ],
    )
    database.mark_chapter_downloaded("suwayomi-5027", tmp_path / "c1.cbz")
    busy = database.create_job("m1", "suwayomi-5029", "en")
    database.update_job(busy["id"], status="downloading")
    idle = database.create_job("m1", "suwayomi-5028", "en")
    database.update_job(idle["id"], status="failed")

    result = database.retire_stale_provider_identities(
        "suwayomi", lambda value: suwayomi_id_is_current(value, "abcd1234")
    )

    assert result == {"mappings_removed": 1, "releases_removed": 1, "jobs_removed": 1}
    assert [m["provider_manga_id"] for m in database.list_release_sources("m1")] == [
        "suwayomi-abcd1234-400"
    ]
    remaining = {c["id"] for c in database.list_all_chapters("m1")}
    assert remaining == {"suwayomi-5027", "suwayomi-5029", "suwayomi-abcd1234-9"}


def test_initialize_rewrites_the_legacy_local_monitoring_reason(tmp_path: Path):
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    database.upsert_manga(
        {
            "id": "m1",
            "title": "Old",
            "description": "",
            "cover_url": None,
            "authors": [],
            "original_language": "ja",
            "provider": "local",
        },
        "en",
        "none",
    )
    for legacy in (
        "Local series have no remote source",
        "No download source linked yet; use Interactive search or Map source",
    ):
        database.set_future_monitoring_capability("m1", allowed=False, reason=legacy)
        database.initialize()
        assert (
            database.get_manga("m1")["future_monitoring_reason"]
            == "No download source linked yet"
        )


def test_initialize_removes_blocks_created_by_the_legacy_local_fallback(
    tmp_path: Path,
):
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    database.upsert_manga(
        {
            "id": "m1",
            "title": "Remote work",
            "description": "",
            "cover_url": None,
            "authors": [],
            "original_language": "ko",
            "provider": "suwayomi",
        },
        "en",
        "all",
    )
    database.upsert_chapters(
        "m1",
        [
            {
                "id": "remote-1",
                "chapter": "1",
                "volume": None,
                "title": "Chapter 1",
                "language": "en",
                "provider": "suwayomi",
                "groups": [],
                "publish_at": "2026-01-01T00:00:00Z",
                "source_url": "https://example.test/1",
            }
        ],
    )
    database.block_release(
        "remote-1",
        reason="RuntimeError: Local series have no remote source",
    )
    database.record_source_failure(
        "m1",
        "suwayomi:broken",
        reason="RuntimeError: Local series have no remote source",
    )

    database.initialize()

    assert database.blocked_releases("m1") == {}
    assert database.demoted_sources("m1") == frozenset()
    assert database.source_health_rows() == []


def test_initialize_turns_direct_provider_series_into_catalogue_works(tmp_path: Path):
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    database.upsert_manga(
        {
            "id": "m1",
            "title": "Old",
            "description": "",
            "cover_url": "/api/covers/mangadex/m1/cover.jpg",
            "authors": [],
            "original_language": "ja",
            "provider": "mangadex",
        },
        "en",
        "all",
    )
    database.upsert_release_source(
        "m1",
        provider="mangapill",
        provider_manga_id="1.old",
        title="Old",
        source_url=None,
        source_name=None,
        language="en",
        match_confidence=1.0,
        match_reason="test",
    )
    rows = [
        {
            "id": f"dex-{n}",
            "chapter": str(n),
            "volume": None,
            "title": "",
            "language": "en",
            "provider": "mangadex",
            "groups": [],
            "publish_at": None,
            "source_url": f"https://x/{n}",
            "pages": 20,
            "version": 1,
        }
        for n in (1, 2)
    ]
    rows.append({**rows[0], "id": "suwa-1", "provider": "suwayomi"})
    database.upsert_chapters("m1", rows)
    database.mark_chapter_downloaded("dex-1", tmp_path / "dex-1.cbz")

    database.initialize()

    manga = database.get_manga("m1")
    assert manga["provider"] == "catalogue" and manga["cover_url"] is None
    assert database.list_release_sources("m1") == []
    assert {c["id"] for c in database.list_all_chapters("m1")} == {"dex-1", "suwa-1"}


def test_release_history_round_trips_dated_releases_only(tmp_path: Path):
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    database.upsert_manga(
        {
            "id": "m1",
            "title": "Rhythm",
            "description": "",
            "cover_url": None,
            "authors": [],
            "original_language": "ja",
        },
        "en",
        "all",
    )
    stored = database.replace_release_history(
        "m1",
        "mangaupdates",
        [
            {"chapter": "1160", "volume": None, "release_date": "2026-08-16"},
            {"chapter": "1161", "volume": "", "release_date": "2026-08-23"},
            {"chapter": "Extra", "volume": "5", "release_date": None},
            {"chapter": "1161", "volume": "", "release_date": "2026-08-23"},
        ],
    )
    assert stored == 3
    assert [
        (r["chapter"], r["release_date"]) for r in database.list_release_history("m1")
    ] == [
        ("1160", "2026-08-16"),
        ("1161", "2026-08-23"),
    ]


def test_queue_order_finishes_one_series_before_the_next_from_first_chapter(tmp_path):
    database = Database(tmp_path / "queue.sqlite3")
    database.initialize()
    for manga_id, title in (("a", "Alpha"), ("b", "Beta")):
        database.upsert_manga(
            {
                "id": manga_id,
                "provider": "catalogue",
                "title": title,
                "description": "",
                "cover_url": None,
                "authors": [],
                "status": "ongoing",
                "available_languages": ["en"],
            },
            "en",
            "all",
        )
        database.upsert_chapters(
            manga_id,
            [
                {
                    "id": f"{manga_id}-{n}",
                    "provider": "suwayomi",
                    "volume": None,
                    "chapter": str(n),
                    "title": "",
                    "language": "en",
                    "groups": [],
                    "publish_at": None,
                    "source_url": f"https://x.test/{manga_id}/{n}",
                }
                for n in (1, 2, 10)
            ],
        )
    # Queued out of order: Alpha has three chapters queued, Beta two.
    for chapter_id in ("a-10", "a-2", "b-10", "b-1", "a-1"):
        database.create_job(chapter_id.split("-")[0], chapter_id, "en")
    ordered = [
        database.get_job(job_id)["chapter_id"]
        for job_id in database.list_queued_job_ids()
    ]
    # The series with fewer chapters left goes first, from chapter 1 up; then the bigger one.
    assert ordered == ["b-1", "b-10", "a-1", "a-2", "a-10"]
    assert [
        database.get_job(job["id"])["chapter_id"]
        for job in database.list_queued_job_heads()
    ] == ["b-1", "a-1"]
    listed = [job["chapter_id"] for job in database.list_jobs(50, ["queued"])]
    assert listed == ordered
    assert [
        job["chapter_id"] for job in database.list_jobs(50, ["queued"], manga_id="a")
    ] == ["a-1", "a-2", "a-10"]
    summary = database.job_series_summary()
    assert [(s["manga_title"], s["queued"], s["working"]) for s in summary] == [
        ("Beta", 2, 0),
        ("Alpha", 3, 0),
    ]


def test_adopt_catalogue_identity_only_promotes_local_series(tmp_path):
    database = Database(tmp_path / "adopt.sqlite3")
    database.initialize()
    database.upsert_manga(
        {
            "id": "loc",
            "provider": "local",
            "title": "Bakune Young",
            "description": "",
            "cover_url": None,
            "authors": [],
            "status": None,
            "available_languages": ["en"],
        },
        "en",
        "none",
    )
    assert database.adopt_catalogue_identity("loc", "27211") is True
    manga = database.get_manga("loc")
    assert manga["provider"] == "catalogue" and manga["source_id"] == "27211"
    assert manga["source_url"] == "https://mangabaka.org/27211"
    assert manga["future_monitoring_allowed"] is True
    assert (
        database.adopt_catalogue_identity("loc", "27211") is False
    )  # already a catalogue work


def test_prune_duplicate_queued_jobs_keeps_one_job_per_chapter(tmp_path):
    database = Database(tmp_path / "prune.sqlite3")
    database.initialize()
    database.upsert_manga(
        {
            "id": "k",
            "provider": "catalogue",
            "title": "Kingdom",
            "description": "",
            "cover_url": None,
            "authors": [],
            "status": "ongoing",
            "available_languages": ["en"],
        },
        "en",
        "all",
    )
    database.upsert_chapters(
        "k",
        [
            {
                "id": "fire-1",
                "provider": "suwayomi",
                "volume": None,
                "chapter": "1",
                "title": "",
                "language": "en",
                "groups": [],
                "publish_at": None,
                "source_url": "https://f.test/1",
            },
            {
                "id": "k-1",
                "provider": "suwayomi",
                "volume": "1",
                "chapter": "1",
                "title": "",
                "language": "en",
                "groups": [],
                "publish_at": None,
                "source_url": "https://k.test/1",
            },
            {
                "id": "fire-2",
                "provider": "suwayomi",
                "volume": None,
                "chapter": "2",
                "title": "",
                "language": "en",
                "groups": [],
                "publish_at": None,
                "source_url": "https://f.test/2",
            },
        ],
    )
    for chapter_id in ("fire-1", "k-1", "fire-2"):
        database.create_job("k", chapter_id, "en")
    assert database.prune_duplicate_queued_jobs() == 1
    remaining = sorted(job["chapter_id"] for job in database.list_jobs(10, ["queued"]))
    assert remaining == ["fire-1", "fire-2"]
    assert database.prune_duplicate_queued_jobs() == 0

    fire_job = next(
        job
        for job in database.list_jobs(10, ["queued"])
        if job["chapter_id"] == "fire-1"
    )
    database.update_job(fire_job["id"], status="importing")
    in_flight_duplicate = database.create_job("k", "k-1", "en")
    assert (
        database.claim_queued_job(
            in_flight_duplicate["id"], tmp_path / "library" / "Kingdom - c1.cbz"
        )
        is None
    )
    with pytest.raises(KeyError):
        database.get_job(in_flight_duplicate["id"])

    queued_behind_active = database.create_job("k", "k-1", "en")
    assert database.prune_duplicate_queued_jobs() == 1
    with pytest.raises(KeyError):
        database.get_job(queued_behind_active["id"])

    completion_duplicate = database.create_job("k", "k-1", "en")
    database.mark_chapter_downloaded(
        "fire-1", tmp_path / "library" / "Kingdom - c1.cbz", "a" * 64
    )
    with pytest.raises(KeyError):
        database.get_job(completion_duplicate["id"])

    database.update_job(fire_job["id"], status="completed")
    recovery_job = database.create_job("k", "fire-1", "en")
    planned_path = tmp_path / "library" / "Kingdom - c1.cbz"
    assert database.claim_queued_job(recovery_job["id"], planned_path) is not None
    database.update_job(recovery_job["id"], status="queued")
    stale_legacy_job = database.create_job("k", "k-1", "en")
    assert database.prune_duplicate_queued_jobs() == 1
    assert database.get_job(recovery_job["id"])["status"] == "queued"
    with pytest.raises(KeyError):
        database.get_job(stale_legacy_job["id"])


def test_match_reviews_are_queued_once_and_resolved(tmp_path):
    database = Database(tmp_path / "review.sqlite3")
    database.initialize()
    database.upsert_manga(
        {
            "id": "k",
            "provider": "catalogue",
            "title": "Kingdom",
            "description": "",
            "cover_url": None,
            "authors": [],
            "status": "ongoing",
            "available_languages": ["en"],
        },
        "en",
        "all",
    )
    assert (
        database.add_match_review(
            "k",
            kind="source",
            provider="suwayomi",
            candidate_id="s-1",
            title="Kingdom (Hara)",
            confidence=0.78,
            reason="creator disagreement",
        )
        is True
    )
    assert (
        database.add_match_review(
            "k",
            kind="source",
            provider="suwayomi",
            candidate_id="s-1",
            title="Kingdom (Hara)",
        )
        is False
    )
    # Nothing waits for a person: the near-miss is refused by the rules and
    # kept as history with its reason.
    assert database.list_match_reviews() == []
    every = database.list_match_reviews(open_only=False)
    assert len(every) == 1 and every[0]["manga_title"] == "Kingdom"
    assert every[0]["resolution"] == "rejected: not verified by the automatic rules"
    resolved = database.resolve_match_review(every[0]["id"], "rejected")
    assert resolved["resolution"] == "rejected" and database.list_match_reviews() == []
    assert (
        database.add_match_review(
            "k", kind="source", provider="suwayomi", candidate_id="s-1", title="again"
        )
        is False
    )


def test_a_blocked_release_is_never_queued_automatically(tmp_path: Path):
    """Blocking records that a release already failed.

    Whatever discovers or re-queues work, the blocked release must not come
    back on its own; an explicit retry clears the block first."""

    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    database.upsert_manga(
        {
            "id": "m1",
            "title": "Example",
            "description": "",
            "cover_url": None,
            "authors": [],
            "original_language": "ja",
        },
        "en",
        "all",
    )
    database.upsert_chapters(
        "m1",
        [
            {
                "id": "c1",
                "manga_id": "m1",
                "volume": None,
                "chapter": "1",
                "title": "Chapter 1",
                "language": "en",
                "provider": "suwayomi",
                "groups": [],
                "publish_at": None,
                "source_url": "https://example.test/1",
                "pages": 20,
            }
        ],
    )
    database.block_release("c1", reason="Server error 500")

    with pytest.raises(ReleaseBlocked):
        database.create_job("m1", "c1", "en")

    database.unblock_release("c1")
    assert database.create_job("m1", "c1", "en")["status"] == "queued"


def test_manual_author_override_outlives_a_metadata_refresh(tmp_path: Path):
    """Sources credit an illustrator as the author, or miss a co-author.

    The operator's correction is stored beside the provider data, so the next
    refresh - which rewrites the provider fields - cannot undo it."""

    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    payload = {
        "id": "m1",
        "title": "Example",
        "description": "",
        "cover_url": None,
        "authors": ["Wrong Person"],
        "original_language": "ja",
    }
    database.upsert_manga(payload, "en", "all")

    database.set_manga_authors_override("m1", ["Seiichi Hayashi", "Co Author"])
    database.upsert_manga(payload, "en", "all")  # a refresh rewrites provider fields

    manga = database.get_manga("m1")
    assert manga["authors"] == ["Seiichi Hayashi", "Co Author"]
    assert manga["source_authors"] == ["Wrong Person"]
    assert manga["authors_overridden"] is True

    database.set_manga_authors_override("m1", None)
    restored = database.get_manga("m1")
    assert restored["authors"] == ["Wrong Person"]
    assert restored["authors_overridden"] is False


def test_a_source_that_keeps_failing_is_tried_last(tmp_path: Path):
    """One bad release is blocked; a source that fails on chapter after
    chapter is the problem itself, so every healthy source is preferred until
    it imports again."""

    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    database.upsert_manga(
        {
            "id": "m1",
            "title": "Example",
            "description": "",
            "cover_url": None,
            "authors": [],
            "original_language": "ja",
        },
        "en",
        "all",
    )
    failing = {"provider": "suwayomi", "source_key": "suwayomi:1", "source_name": "Bad"}

    for _ in range(database.SOURCE_DEMOTION_FAILURES - 1):
        database.record_source_failure("m1", "suwayomi:1", reason="500")
    assert database.demoted_sources("m1") == frozenset()

    database.record_source_failure("m1", "suwayomi:1", reason="500")
    assert database.demoted_sources("m1") == frozenset({"suwayomi:1"})

    healthy = {
        "id": "good",
        "provider": "suwayomi",
        "source_key": "suwayomi:2",
        "source_name": "Good",
        "chapter": "1",
        "publish_at": "2024-01-01T00:00:00+00:00",
    }
    bad = {
        **failing,
        "id": "bad",
        "chapter": "1",
        "publish_at": "2024-01-02T00:00:00+00:00",
    }
    chosen = database.source_ranking.best(
        [bad, healthy], demoted_sources=database.demoted_sources("m1")
    )
    assert chosen["id"] == "good"

    database.clear_source_failures("m1", "suwayomi:1")
    assert database.demoted_sources("m1") == frozenset()


def test_discovery_names_the_slot_not_the_release(tmp_path: Path):
    """Discovery hands back the releases it just saw.

    Queueing exactly those would give the slot to whichever source published
    last; the ranking must still choose among every release of that slot."""

    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    database.upsert_manga(
        {
            "id": "m1",
            "title": "Example",
            "description": "",
            "cover_url": None,
            "authors": [],
            "original_language": "ja",
        },
        "en",
        "all",
    )
    database.upsert_chapters(
        "m1",
        [
            {
                "id": f"{source}-1",
                "manga_id": "m1",
                "volume": None,
                "chapter": "1",
                "title": "Chapter 1",
                "language": "en",
                "provider": "suwayomi",
                "source_key": f"suwayomi:{index}",
                "source_name": source,
                "groups": [],
                "publish_at": f"2024-01-0{index}T00:00:00+00:00",
                "source_url": f"https://{source}.test/1",
                "pages": 20,
            }
            for index, source in enumerate(("healthy", "failing"), start=1)
        ],
    )
    for _ in range(database.SOURCE_DEMOTION_FAILURES):
        database.record_source_failure("m1", "suwayomi:2", reason="500")

    # Discovery saw only the failing source's release this cycle.
    candidates = database.preferred_download_candidates("m1", ["failing-1"])

    assert [item["id"] for item in candidates] == ["healthy-1"]


def test_repeated_transient_failures_demote_a_source(tmp_path: Path):
    """An engine restart must not condemn a good source, so transient errors
    are counted apart. A source that is "temporarily unavailable" over and
    over is simply broken, and stops being the first choice."""

    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    database.upsert_manga(
        {
            "id": "m1",
            "title": "Example",
            "description": "",
            "cover_url": None,
            "authors": [],
            "original_language": "ja",
        },
        "en",
        "all",
    )

    for _ in range(database.SOURCE_DEMOTION_FAILURES):
        database.record_source_failure("m1", "suwayomi:1", transient=True)
    assert database.demoted_sources("m1") == frozenset()

    remaining = database.TRANSIENT_DEMOTION_FAILURES - database.SOURCE_DEMOTION_FAILURES
    for _ in range(remaining):
        database.record_source_failure("m1", "suwayomi:1", transient=True)
    assert database.demoted_sources("m1") == frozenset({"suwayomi:1"})


def test_an_unmaintained_extension_is_the_last_choice_everywhere(tmp_path: Path):
    """The runtime says the extension is gone from the store: it may still be
    the only place a work is published, so it stays usable and keeps serving
    as a reference, but every maintained source is tried first."""

    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    database.upsert_manga(
        {
            "id": "m1",
            "title": "Example",
            "description": "",
            "cover_url": None,
            "authors": [],
            "original_language": "ja",
        },
        "en",
        "all",
    )
    database.deprioritised_sources = frozenset({"suwayomi:obsolete"})

    assert database.demoted_sources("m1") == frozenset({"suwayomi:obsolete"})

    obsolete = {
        "id": "old",
        "provider": "suwayomi",
        "source_key": "suwayomi:obsolete",
        "source_name": "Retired",
        "chapter": "1",
        "publish_at": "2024-01-02T00:00:00+00:00",
    }
    maintained = {
        "id": "new",
        "provider": "suwayomi",
        "source_key": "suwayomi:kept",
        "source_name": "Kept",
        "chapter": "1",
        "publish_at": "2024-01-01T00:00:00+00:00",
    }
    ranking = database.source_ranking
    demoted = database.demoted_sources("m1")
    assert ranking.best([obsolete, maintained], demoted_sources=demoted)["id"] == "new"
    # Alone, it is still the answer: a demoted source is not a removed one.
    assert ranking.best([obsolete], demoted_sources=demoted)["id"] == "old"


def test_an_uninstalled_source_stops_offering_releases(tmp_path: Path):
    """Its undownloaded releases and mappings go; the files it delivered stay."""

    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    database.upsert_manga(
        {
            "id": "m1",
            "title": "Example",
            "description": "",
            "cover_url": None,
            "authors": [],
            "original_language": "ja",
        },
        "en",
        "all",
    )
    database.upsert_chapters(
        "m1",
        [
            {
                "id": f"{source}-{n}",
                "manga_id": "m1",
                "volume": None,
                "chapter": str(n),
                "title": f"Chapter {n}",
                "language": "en",
                "provider": "suwayomi",
                "source_key": f"suwayomi:{source}",
                "source_name": f"{source} (EN)",
                "groups": [],
                "publish_at": None,
                "source_url": f"https://{source}.test/{n}",
                "pages": 20,
            }
            for source in ("kept", "gone")
            for n in (1, 2)
        ],
    )
    database.mark_chapter_downloaded("gone-1", tmp_path / "gone-1.cbz")
    database.upsert_release_source(
        "m1",
        provider="suwayomi",
        provider_manga_id="suwayomi-x-9",
        title="Example",
        source_url="https://gone.test",
        source_name="gone (EN)",
        language="en",
        match_confidence=1.0,
        match_reason="test",
    )

    retired = database.retire_uninstalled_sources({"suwayomi:kept"})

    assert retired == {"releases": 1, "mappings": 1}
    remaining = {c["id"] for c in database.list_all_chapters("m1")}
    assert remaining == {"kept-1", "kept-2", "gone-1"}


def test_one_source_entry_cannot_serve_two_series(tmp_path: Path):
    """A work and its spin-off can both match one entry on a source. Mapping
    it twice leaves the loser failing forever, because a chapter belongs to a
    single series — so the second claim is refused and surfaced instead."""

    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    for identifier, title in (
        ("main", "Princess Ai"),
        ("spinoff", "Princess Ai: Encounters"),
    ):
        database.upsert_manga(
            {
                "id": identifier,
                "title": title,
                "description": "",
                "cover_url": None,
                "authors": [],
                "original_language": "ja",
                "provider": "catalogue",
            },
            "en",
            "all",
        )
    mapping = dict(
        provider="suwayomi",
        provider_manga_id="suwayomi-11728",
        title="Princess Ai: Encounters",
        source_url=None,
        source_name="MangaK (EN)",
        language="en",
        match_confidence=0.9,
        match_reason="test",
    )
    database.upsert_release_source("main", **mapping)

    with pytest.raises(ReleaseSourceOwned, match="another series"):
        database.upsert_release_source("spinoff", **mapping)

    # Re-mapping the same series is still an ordinary refresh, not a conflict.
    database.upsert_release_source("main", **mapping)
    assert len(database.list_release_sources("main")) == 1
    assert database.list_release_sources("spinoff") == []


def test_failed_downloads_are_counted_from_the_last_acknowledgement(tmp_path: Path):
    """Dismissing "N failed downloads" has to hold: a queue that keeps failing
    would otherwise revive the alert on the very next failure."""

    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    database.upsert_manga(
        {
            "id": "m1",
            "title": "Example",
            "description": "",
            "cover_url": None,
            "authors": [],
            "original_language": "ja",
            "provider": "catalogue",
        },
        "en",
        "all",
    )
    database.upsert_chapters(
        "m1",
        [
            {
                "id": f"c{n}",
                "manga_id": "m1",
                "volume": None,
                "chapter": str(n),
                "title": "",
                "language": "en",
                "provider": "suwayomi",
                "groups": [],
                "publish_at": None,
                "source_url": "",
                "pages": 1,
                "version": 1,
            }
            for n in range(1, 4)
        ],
    )
    first = database.create_job("m1", "c1", "en")
    database.update_job(first["id"], status="failed")
    second = database.create_job("m1", "c2", "en")
    database.update_job(second["id"], status="failed")

    fresh, newest = database.failed_jobs_since(0)
    assert fresh == 2 and newest == second["id"]

    # Acknowledged at that point: nothing new to report.
    assert database.failed_jobs_since(newest) == (0, newest)

    third = database.create_job("m1", "c3", "en")
    database.update_job(third["id"], status="failed")
    assert database.failed_jobs_since(newest) == (1, third["id"])


def test_the_guard_names_the_series_that_already_holds_the_entry(tmp_path: Path):
    """Knowing who holds it is what lets discovery decide, rather than ask,
    when the entry names that series and not the one claiming it."""

    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    for identifier, title in (
        ("main", "Princess Ai"),
        ("spinoff", "Princess Ai: Encounters"),
    ):
        database.upsert_manga(
            {
                "id": identifier,
                "title": title,
                "description": "",
                "cover_url": None,
                "authors": [],
                "original_language": "ja",
                "provider": "catalogue",
            },
            "en",
            "all",
        )
    mapping = dict(
        provider="suwayomi",
        provider_manga_id="suwayomi-11728",
        title="Princess Ai: Encounters",
        source_url=None,
        source_name="MangaK (EN)",
        language="en",
        match_confidence=0.9,
        match_reason="test",
    )
    database.upsert_release_source("spinoff", **mapping)

    with pytest.raises(ReleaseSourceOwned) as caught:
        database.upsert_release_source("main", **mapping)

    assert caught.value.owner_id == "spinoff"
    assert caught.value.owner_title == "Princess Ai: Encounters"


def test_labels_that_swallowed_a_year_are_repaired_on_open(tmp_path):
    # "Tokyopop.Princess.Ai.Vol.03.2020" was once parsed as volume 3.202:
    # books on disk that cover none of the volumes the work has.
    from pathlib import Path

    from tankarr.database import Database

    path = tmp_path / "tankarr.sqlite3"
    database = Database(path)
    database.initialize()
    database.upsert_manga(
        {
            "id": "princess",
            "provider": "catalogue",
            "title": "Princess Ai",
            "description": "",
            "cover_url": None,
            "authors": [],
            "available_languages": ["en"],
        },
        "en",
        "all",
    )
    database.upsert_chapters(
        "princess",
        [
            {
                "id": "book-3",
                "volume": "3.202",
                "chapter": None,
                "title": "Volume 3.202",
                "language": "en",
                "provider": "prowlarr",
                "groups": [],
                "publish_at": None,
                "source_url": "https://example.invalid/3",
                "pages": 200,
                "version": 1,
                "release_unit": "volume",
            },
            {
                "id": "side-story",
                "volume": None,
                "chapter": "10.5",
                "title": "Extra",
                "language": "en",
                "provider": "prowlarr",
                "groups": [],
                "publish_at": None,
                "source_url": "https://example.invalid/10.5",
                "pages": 4,
                "version": 1,
            },
        ],
        monitor_new=True,
    )

    Database(path).initialize()

    assert database.get_chapter("book-3")["volume"] == "3"
    # A genuine decimal (two digits or fewer) is a real extra and untouched.
    assert database.get_chapter("side-story")["chapter"] == "10.5"
    assert Path(path).exists()


def test_a_split_part_remembers_the_chapter_it_belongs_to(tmp_path: Path):
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    database.upsert_manga(
        {
            "id": "m",
            "provider": "catalogue",
            "title": "Split",
            "description": "",
            "cover_url": None,
            "authors": [],
            "available_languages": ["en"],
        },
        "en",
        "all",
    )
    database.upsert_chapters(
        "m",
        [
            {
                "id": "p1",
                "chapter": "12.1",
                "volume": None,
                "title": "",
                "language": "en",
                "provider": "suwayomi",
                "groups": [],
                "publish_at": None,
                "source_url": "",
            }
        ],
    )
    database.record_page_quality(
        "m",
        "p1",
        verdict="degraded",
        assessment={
            "normalized_height": 10,
            "reason": "part 1/2 of chapter 12",
            "whole_chapter": "12",
        },
    )
    assert database.page_quality("m")["p1"]["whole_chapter"] == "12"
    database.record_page_quality(
        "m", "p1", verdict="ok", assessment={"normalized_height": 10, "reason": ""}
    )
    assert database.page_quality("m")["p1"]["whole_chapter"] == ""


def _seed_series(database: Database, manga_id: str = "m") -> None:
    database.upsert_manga(
        {
            "id": manga_id,
            "provider": "catalogue",
            "title": "Series",
            "description": "",
            "cover_url": None,
            "authors": [],
            "available_languages": ["en"],
        },
        "en",
        "all",
    )


def _release(identifier: str, chapter: str) -> dict:
    return {
        "id": identifier,
        "chapter": chapter,
        "volume": None,
        "title": "",
        "language": "en",
        "provider": "suwayomi",
        "groups": [],
        "publish_at": None,
        "source_url": "",
    }


def test_provider_refresh_preserves_measured_pages_of_downloaded_files(tmp_path):
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    _seed_series(database)
    rows = [
        {**_release("local", "1"), "pages": 100},
        {**_release("remote", "2"), "pages": 100},
    ]
    database.upsert_chapters("m", rows)
    database.mark_chapter_downloaded("local", tmp_path / "book.cbz")
    database.set_release_pages("local", 120)
    database.upsert_chapters(
        "m", [{**row, "pages": 12, "title": "New source title"} for row in rows]
    )
    assert database.get_chapter("local")["pages"] == 120
    assert database.get_chapter("remote")["pages"] == 12
    assert database.get_chapter("local")["title"] == "New source title"


def test_a_failure_whose_release_no_longer_exists_is_forgotten(tmp_path: Path):
    # Measured live: 210 of 248 failed downloads pointed at releases a retired
    # source had taken with it, and were still counted as work to do.
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    _seed_series(database)
    database.upsert_chapters("m", [_release("r1", "1"), _release("r2", "2")])
    ghost = database.create_job("m", "r1", "en")
    alive = database.create_job("m", "r2", "en")
    for job in (ghost, alive):
        database.update_job(int(job["id"]), status="failed", message="boom")
    database.forget_undownloaded_releases("m", ["r1"])

    assert database.prune_recovered_failures() == 1
    remaining = {
        int(j["id"]) for j in database.list_jobs(statuses=["failed"], limit=50)
    }
    assert remaining == {int(alive["id"])}


def test_a_quality_refusal_is_not_a_failed_download(tmp_path: Path):
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    _seed_series(database)
    database.upsert_chapters("m", [_release("r1", "1"), _release("r2", "2")])
    refused = database.create_job("m", "r1", "en")
    failed = database.create_job("m", "r2", "en")
    database.update_job(
        int(refused["id"]),
        status="failed",
        message="DegradedPagesError: pages are 12 times taller than they are wide",
    )
    database.update_job(
        int(failed["id"]), status="failed", message="ProviderRequestError: timeout"
    )

    fresh, newest = database.failed_jobs_since(0)
    assert fresh == 1
    assert newest == int(failed["id"])


def test_forgetting_a_source_keeps_the_files_it_delivered(tmp_path: Path):
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    _seed_series(database)
    database.upsert_chapters("m", [_release("r1", "1"), _release("r2", "2")])
    database.mark_chapter_downloaded("r2", Path("/library/x.cbz"))

    assert database.forget_undownloaded_releases("m", ["r1", "r2", "missing"]) == 1
    assert [c["id"] for c in database.list_chapters("m", "en")] == ["r2"]


def test_measurements_from_an_older_method_are_forgotten_once(tmp_path: Path):
    from tankarr.page_quality import MEASURE_VERSION

    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    _seed_series(database)
    database.upsert_chapters("m", [_release("r1", "1"), _release("r2", "2")])
    database.record_page_quality(
        "m", "r1", verdict="ok", assessment={"normalized_height": 10, "reason": ""}
    )
    database.block_release(
        "r2", reason="Unreadable pages: pages are 14 times taller than they are wide"
    )
    # Same version: nothing is touched.
    database.initialize()
    assert "r1" in database.page_quality("m")
    assert "r2" in database.blocked_releases("m")
    # An older method: everything measured goes, strip blocks go, the
    # version is stamped so it never happens again.
    with database.connect() as connection:
        connection.execute(
            "UPDATE setting SET value='1' WHERE key='page_quality_measure_version'"
        )
    database.initialize()
    assert database.page_quality("m") == {}
    assert "r2" not in database.blocked_releases("m")
    assert (
        database.get_setting_overrides()["page_quality_measure_version"]
        == MEASURE_VERSION
    )


def test_official_release_source_publication_status_is_kept_per_source(tmp_path):
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    database.upsert_manga(
        {
            "id": "m1",
            "title": "unOrdinary",
            "provider": "catalogue",
            "status": "ongoing",
        },
        "en",
        "none",
    )
    for provider, pid, role in (
        ("suwayomi", "wt-1", "primary_official"),
        ("suwayomi", "wc-1", "alternative"),
    ):
        database.upsert_release_source(
            "m1",
            provider=provider,
            provider_manga_id=pid,
            title="unOrdinary",
            source_url=None,
            source_name="Webtoons" if role == "primary_official" else "Weeb Central",
            language="en",
            match_confidence=1.0,
            match_reason="test",
            verified_by="test",
        )
        with database.connect() as connection:
            connection.execute(
                "UPDATE manga_release_source SET source_role=? "
                "WHERE manga_id='m1' AND provider_manga_id=?",
                (role, pid),
            )
    assert database.official_publication_status("m1") is None
    # A refresh without details leaves the earlier word in place.
    database.record_release_source_result(
        "m1", "suwayomi", "wc-1", publication_status="hiatus"
    )
    assert database.official_publication_status("m1") is None  # not an official source
    database.record_release_source_result(
        "m1", "suwayomi", "wt-1", publication_status="On Hiatus"
    )
    assert database.official_publication_status("m1") == "on_hiatus"
    database.record_release_source_result("m1", "suwayomi", "wt-1")
    assert database.official_publication_status("m1") == "on_hiatus"
    database.record_release_source_result(
        "m1", "suwayomi", "wt-1", publication_status="ongoing"
    )
    assert database.official_publication_status("m1") == "ongoing"


def test_publication_signals_and_pause_transitions(tmp_path):
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    database.upsert_manga(
        {"id": "m1", "title": "Nana", "provider": "catalogue", "status": "ongoing"},
        "en",
        "none",
    )
    with database.connect() as connection:
        for source, status in (
            ("mangabaka", "ongoing"),
            ("myanimelist", "hiatus"),
            ("anilist", "hiatus"),
        ):
            connection.execute(
                "INSERT INTO metadata_source_record (manga_id, entity_type, entity_key, source, external_id, "
                "match_confidence, match_reason, data_json, raw_json, fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    "m1",
                    "work",
                    "",
                    source,
                    "1",
                    1.0,
                    "test",
                    f'{{"status": "{status}"}}',
                    "{}",
                    "2026-09-05T00:00:00+00:00",
                ),
            )
    assert database.publication_signals("m1") == {
        "mangabaka": "ongoing",
        "myanimelist": "hiatus",
    }

    # First sighting records the state without announcing anything.
    first = database.record_publication_pause(
        "m1", paused=True, sources=["myanimelist"]
    )
    assert first == {"paused": None, "sources": [], "changed_at": None}
    assert (
        database.record_publication_pause("m1", paused=True, sources=["myanimelist"])
        is None
    )
    back = database.record_publication_pause("m1", paused=False, sources=[])
    assert back["paused"] is True and back["sources"] == ["myanimelist"]
    assert database.publication_pause("m1")["paused"] is False


def test_a_chapter_range_file_that_is_a_mapped_volume_becomes_that_volume(tmp_path):
    """Billy Bat: the "Vol. 01-20 (Scans)" pack named its books by chapter
    range ("c001-009"); the confirmed map says volume 1 is chapters 1-9. Those files
    are the volumes, and the series can be managed as books."""

    from tankarr.chapter_map import entries_from_releases

    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    database.upsert_manga(
        {"id": "bb", "title": "Billy Bat", "provider": "catalogue"}, "en", "none"
    )
    database.replace_chapter_map(
        "bb",
        "operator",
        entries_from_releases(
            [{"volume": "1", "chapter": "1-9"}, {"volume": "2", "chapter": "10-18"}],
            source="operator",
        ),
    )
    rows = []
    for cid, ch in (("r1", "1-9"), ("r2", "10-18"), ("r3", "19-20"), ("c5", "5")):
        rows.append(
            {
                "id": cid,
                "chapter": ch,
                "volume": None,
                "title": "",
                "language": "en",
                "provider": "prowlarr",
                "groups": [],
                "publish_at": None,
                "source_url": f"local://{cid}",
                "pages": 200,
                "version": 1,
            }
        )
    database.upsert_chapters("bb", rows)

    assert database.reclassify_mapped_ranges_as_volumes("bb") == ["1", "2"]
    by_id = {row["id"]: row for row in database.list_all_chapters("bb")}
    assert (
        by_id["r1"]["release_unit"],
        by_id["r1"]["volume"],
        by_id["r1"]["chapter"],
    ) == ("volume", "1", None)
    assert (by_id["r2"]["release_unit"], by_id["r2"]["volume"]) == ("volume", "2")
    # A short range that is not a mapped book, and a single chapter, stay chapters.
    assert (
        by_id["r3"]["release_unit"] != "volume"
        and by_id["c5"]["release_unit"] != "volume"
    )
    assert database.reclassify_mapped_ranges_as_volumes("bb") == []


def test_a_full_run_of_range_files_is_the_volumes_in_order_even_without_a_map(tmp_path):
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    database.upsert_manga(
        {"id": "bb", "title": "Billy Bat", "provider": "catalogue"}, "en", "none"
    )
    spans = ["1-9", "10-18", "19-27", "28-37", "38-45"]
    database.upsert_chapters(
        "bb",
        [
            {
                "id": f"r{i}",
                "chapter": span,
                "volume": None,
                "title": "",
                "language": "en",
                "provider": "prowlarr",
                "groups": [],
                "publish_at": None,
                "source_url": f"local://r{i}",
                "pages": 200,
                "version": 1,
            }
            for i, span in enumerate(spans, start=1)
        ],
    )
    # Four files for a five-volume work: no proof, nothing changes.
    assert (
        database.reclassify_mapped_ranges_as_volumes(
            "bb", volume_count=6, chapter_total=45
        )
        == []
    )
    assert database.reclassify_mapped_ranges_as_volumes(
        "bb", volume_count=5, chapter_total=45
    ) == ["1", "2", "3", "4", "5"]
    by_id = {row["id"]: row for row in database.list_all_chapters("bb")}
    assert [
        (by_id[f"r{i}"]["release_unit"], by_id[f"r{i}"]["volume"]) for i in range(1, 6)
    ] == [("volume", str(i)) for i in range(1, 6)]


def test_the_display_helper_strips_the_sources_status_emoji():
    from tankarr.database import clean_release_title

    assert clean_release_title("🔒 251. New Message") == "251. New Message"
    assert clean_release_title("🔥 Chapter 12 🆕") == "Chapter 12"
    assert clean_release_title("Episode 393 (ch. 393)") == "Episode 393 (ch. 393)"
    assert clean_release_title(None) == ""


def test_a_hand_imported_book_without_its_file_is_never_queued(tmp_path: Path):
    """Master Keaton v16-18: rows left by a manual import whose files were
    removed kept turning into download jobs that only ever failed with
    "Provider 'manual' is not configured"."""
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    source = {
        "id": "manga-1",
        "title": "Master Keaton",
        "description": "",
        "cover_url": None,
        "authors": [],
        "original_language": "ja",
    }
    database.upsert_manga(source, "en", "all")
    rows = []
    for identifier, provider in (("manual-16", "manual"), ("mangadex-16", "mangadex")):
        rows.append(
            {
                "id": identifier,
                "chapter": None,
                "volume": "16",
                "title": "Volume 16",
                "language": "en",
                "provider": provider,
                "groups": [],
                "publish_at": None,
                "source_url": f"https://example.test/{identifier}",
            }
        )
    database.upsert_chapters("manga-1", rows)
    for row in rows:
        database.set_release_book(row["id"], "16")

    candidates = database.preferred_download_candidates("manga-1")
    assert [candidate["id"] for candidate in candidates] == ["mangadex-16"]


def test_numbered_prologue_can_download_without_claiming_ordinary_chapter(tmp_path):
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    database.upsert_manga({"id": "series", "title": "Example"}, "en")
    database.upsert_chapters(
        "series",
        [
            {
                "id": "prelude",
                "chapter": "1",
                "title": "Prologue 1",
                "canonical_chapter": None,
                "numbering_status": "unmapped",
                "numbering_method": "special_title",
                "language": "en",
                "provider": "suwayomi",
                "source_url": "https://example.test/release",
            },
            {
                "id": "ordinary",
                "chapter": "1",
                "title": "Chapter 1",
                "language": "en",
                "provider": "suwayomi",
                "source_url": "https://example.test/release",
            },
            {
                "id": "unknown",
                "chapter": None,
                "title": "Unidentified release",
                "language": "en",
                "provider": "suwayomi",
                "source_url": "https://example.test/release",
            },
        ],
    )
    prelude = database.get_chapter("prelude")
    assert prelude["canonical_chapter"] is None
    assert prelude["numbering_method"] == "special_title"
    job = database.create_job("series", "prelude", "en", origin="manual")
    assert job["chapter_id"] == "prelude"
    claimed = database.claim_queued_job(job["id"], tmp_path / "prologue-1.cbz")
    assert claimed is not None
    assert claimed["status"] == "running"
    assert database.get_chapter("ordinary")["canonical_chapter"] == "1"
    with pytest.raises(ValueError, match="numbering is unresolved"):
        database.create_job("series", "unknown", "en", origin="manual")


def test_prologue_changed_to_ambiguous_is_not_claimed(tmp_path):
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    database.upsert_manga({"id": "series", "title": "Example"}, "en")
    database.upsert_chapters(
        "series",
        [
            {
                "id": "prelude",
                "chapter": None,
                "title": "Prologue 1",
                "canonical_chapter": None,
                "numbering_status": "unmapped",
                "numbering_method": "special_title",
                "language": "en",
                "provider": "suwayomi",
                "source_url": "https://example.test/release",
            }
        ],
    )
    job = database.create_job("series", "prelude", "en", origin="manual")
    with database.connect() as connection:
        connection.execute(
            "UPDATE chapter_release SET numbering_status='ambiguous' WHERE id='prelude'"
        )
    assert database.claim_queued_job(job["id"], tmp_path / "prelude.cbz") is None
    with pytest.raises(KeyError):
        database.get_job(job["id"])


def test_verified_publication_requires_evidence_and_can_be_cleared(tmp_path):
    database = Database(tmp_path / "db.sqlite3")
    database.initialize()
    database.upsert_manga({"id": "verified", "title": "Example", "authors": []}, "en")
    with pytest.raises(ValueError, match="source"):
        database.update_manga("verified", {"verified_chapter_count": 3})
    with pytest.raises(ValueError, match="between"):
        database.update_manga(
            "verified",
            {"verified_chapter_count": True, "verified_chapter_source": "publisher"},
        )
    manga = database.update_manga(
        "verified",
        {"verified_chapter_count": 3, "verified_chapter_source": "publisher"},
    )
    assert manga["verified_chapter_count"] == 3
    assert manga["verified_chapter_source"] == "publisher"
    assert manga["verified_chapter_checked_at"]
    manga = database.update_manga("verified", {"verified_chapter_count": "automatic"})
    assert manga["verified_chapter_count"] is None
    assert manga["verified_chapter_source"] is None
    assert manga["verified_chapter_checked_at"] is None
