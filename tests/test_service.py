from pathlib import Path

import pytest

from tankarr.config import Settings
from tankarr.database import Database
from tankarr.providers.base import ProviderUnavailableError
from tankarr.service import TankarrService


def manga(status: str, last_chapter: str) -> dict:
    return {
        "id": "manga-1",
        "title": "Example",
        "description": "",
        "cover_url": "/api/covers/manga-1/cover.jpg",
        "authors": ["Author"],
        "original_language": "ja",
        "status": status,
        "last_volume": "1",
        "last_chapter": last_chapter,
        "available_languages": ["en"],
        "source_url": "https://mangadex.org/title/manga-1",
    }


def chapter(number: str) -> dict:
    return {
        "id": f"chapter-{number}",
        "chapter": number,
        "volume": "1",
        "title": f"Chapter {number}",
        "language": "en",
        "provider": "mangadex",
        "groups": [],
        "publish_at": "2026-01-01T00:00:00Z",
        "source_url": f"https://mangadex.org/chapter/chapter-{number}",
        "pages": 20,
        "version": 1,
    }


class FakeProvider:
    def __init__(self, remote_manga: dict, chapters: list[dict]):
        self.remote_manga = remote_manga
        self.chapters = chapters

    async def get_manga(self, _: str) -> dict:
        return dict(self.remote_manga)

    async def list_chapters(self, _: str, __: str) -> list[dict]:
        return list(self.chapters)


def test_explicit_provider_registry_never_routes_remote_work_to_local(tmp_path: Path):
    service = TankarrService(
        Settings(data_dir=tmp_path / "data", library_dir=tmp_path / "library"),
        Database(tmp_path / "tankarr.sqlite3"),
        {"local": object()},  # type: ignore[dict-item]
        object(),  # type: ignore[arg-type]
    )

    with pytest.raises(ProviderUnavailableError, match="suwayomi.*not configured"):
        service.provider_for("suwayomi")


@pytest.mark.asyncio
async def test_indexer_offer_cannot_be_queued_as_a_provider_download(tmp_path: Path):
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    database.upsert_manga(manga("ongoing", "2"), "en", "all")
    indexer_offer = {**chapter("1"), "provider": "prowlarr"}
    direct_release = {**chapter("2"), "provider": "suwayomi"}
    database.upsert_chapters("manga-1", [indexer_offer, direct_release])
    service = TankarrService(
        Settings(data_dir=tmp_path / "data", library_dir=tmp_path / "library"),
        database,
        {"local": object(), "suwayomi": object()},  # type: ignore[dict-item]
        object(),  # type: ignore[arg-type]
    )

    jobs = await service.create_missing_download_jobs("manga-1")

    assert [job["chapter_id"] for job in jobs] == ["chapter-2"]
    assert database.list_jobs(manga_id="manga-1") == jobs
    stale = database.create_job("manga-1", "chapter-1", "en", origin="monitor")
    assert await service.rerank_queued_jobs("manga-1") == 1
    with pytest.raises(KeyError):
        database.get_job(stale["id"])


@pytest.mark.asyncio
async def test_offered_optional_half_chapter_is_queued_outside_wanted(tmp_path: Path):
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    database.upsert_manga(manga("ongoing", "2"), "en", "all")
    database.upsert_chapters(
        "manga-1",
        [chapter(number) for number in ("1", "1.1", "1.2", "1.5", "2")],
    )
    service = TankarrService(
        Settings(data_dir=tmp_path / "data", library_dir=tmp_path / "library"),
        database,
        {"mangadex": object()},  # type: ignore[dict-item]
        object(),  # type: ignore[arg-type]
    )

    wanted = service.list_wanted()
    assert "chapter:1.5" not in {
        release["slot_key"] for entry in wanted for release in entry["chapters"]
    }
    jobs = await service.create_missing_download_jobs("manga-1")
    assert {job["chapter_id"] for job in jobs} == {
        "chapter-1",
        "chapter-1.5",
        "chapter-2",
    }
    optional_job = next(job for job in jobs if job["chapter_id"] == "chapter-1.5")
    assert await service.rerank_queued_jobs("manga-1") == 0
    assert service._queued_job_is_acquisition_allowed(
        "manga-1", "chapter-1.5", optional_job["id"], origin="monitor"
    )
    assert await service.create_missing_download_jobs("manga-1") == []


@pytest.mark.asyncio
async def test_future_optional_special_job_survives_rerank(tmp_path: Path):
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    database.upsert_manga(manga("ongoing", "2"), "en", "future")
    database.upsert_chapters("manga-1", [chapter("1"), chapter("1.5")])
    service = TankarrService(
        Settings(data_dir=tmp_path / "data", library_dir=tmp_path / "library"),
        database,
        {"mangadex": object()},  # type: ignore[dict-item]
        object(),  # type: ignore[arg-type]
    )

    jobs = await service.create_missing_download_jobs("manga-1", ["chapter-1.5"])
    assert [job["chapter_id"] for job in jobs] == ["chapter-1.5"]
    assert await service.rerank_queued_jobs("manga-1") == 0
    assert database.get_job(jobs[0]["id"])["status"] == "queued"


@pytest.mark.asyncio
async def test_refresh_drops_future_scope_after_translation_reaches_final_chapter(
    tmp_path: Path,
):
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    database.upsert_manga(manga("ongoing", "2"), "en", "all")
    database.upsert_chapters("manga-1", [chapter("1")])
    service = TankarrService(
        Settings(data_dir=tmp_path / "data", library_dir=tmp_path / "library"),
        database,
        FakeProvider(  # type: ignore[arg-type]
            manga("completed", "2"), [chapter("1"), chapter("2")]
        ),
        object(),  # type: ignore[arg-type]
    )

    result = await service.refresh_manga("manga-1", "en")

    assert result["future_monitoring_allowed"] is False
    assert result["monitor_mode_before"] == "all"
    assert result["monitor_mode"] == "existing"
    stored = database.get_manga("manga-1")
    assert stored["status"] == "completed"
    assert stored["monitor_mode"] == "existing"


@pytest.mark.asyncio
async def test_completed_original_keeps_future_scope_for_incomplete_translation(
    tmp_path: Path,
):
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    database.upsert_manga(manga("completed", "2"), "en", "future")
    service = TankarrService(
        Settings(data_dir=tmp_path / "data", library_dir=tmp_path / "library"),
        database,
        FakeProvider(manga("completed", "2"), [chapter("1")]),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
    )

    result = await service.refresh_manga("manga-1", "en")

    assert result["future_monitoring_allowed"] is True
    assert database.get_manga("manga-1")["monitor_mode"] == "future"


@pytest.mark.asyncio
async def test_a_catalogue_series_still_settles_the_unit_of_its_mapped_releases(
    tmp_path: Path,
):
    """A catalogue series has no primary remote, and refresh used to stop
    there. But its chapters come from mapped sources, and a source that
    publishes whole tankobon through a chapter-shaped API leaves a 299-page
    "Volume 1" filed as a chapter with no number - which the queue refuses for
    ever (Urotsukidoji's FAKKU edition on XCOMIC)."""

    settings = Settings(data_dir=tmp_path / "data", library_dir=tmp_path / "library")
    identity = "0123456789abcdef0123456789abcdef"
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.library_dir.mkdir(parents=True, exist_ok=True)
    for root in (settings.data_dir, settings.library_dir):
        (root / ".tankarr-library-id").write_text(f"{identity}\n", encoding="utf-8")
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    catalogue = {**manga("completed", None), "provider": "catalogue"}
    database.upsert_manga(catalogue, "en", "all")
    database.save_series_metadata(
        "manga-1",
        {"volume_count": 4, "chapter_count": 19},
        artwork_path=None,
        artwork_sha256=None,
        artwork_media_type=None,
        source_status=[],
    )
    database.upsert_chapters(
        "manga-1",
        [
            {
                **chapter("1"),
                "id": "book-1",
                "chapter": None,
                "volume": None,
                "title": "Volume 1",
                "release_unit": "chapter",
                "pages": 299,
            }
        ],
    )
    service = TankarrService(
        settings,
        database,
        FakeProvider(catalogue, []),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
    )

    result = await service.refresh_manga("manga-1")

    assert result["seen"] == 0  # no remote was polled
    row = next(r for r in database.list_all_chapters("manga-1") if r["id"] == "book-1")
    assert row["release_unit"] == "volume"
    assert str(row["volume"]) == "1"


def test_a_number_past_the_editions_last_book_is_not_a_book_number():
    from tankarr.service import TankarrService as service_class

    assert service_class._within_book_count("3", 4) is True
    assert service_class._within_book_count("235", 10) is False
    assert service_class._within_book_count("0", 10) is False
    assert service_class._within_book_count("2", None) is True
