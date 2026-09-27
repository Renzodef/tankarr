"""The audit and the replacement it drives, against a real service."""

from __future__ import annotations

import zipfile
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from tankarr.config import Settings
from tankarr.database import Database
from tankarr.naming import final_library_path
from tankarr.page_quality import DEGRADED, OK
from tankarr.service import TankarrService


class NoopProvider:
    name = "suwayomi"


class NoopKomga:
    configured = False

    async def scan(self, _expected_relative_paths=()) -> dict:
        return {"configured": False, "triggered": False}


def page(width: int, height: int) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (width, height), "white").save(buffer, format="PNG")
    return buffer.getvalue()


def provision(tmp_path: Path) -> tuple[Settings, Database, TankarrService]:
    settings = Settings(
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "library",
        monitor_enabled=False,
    )
    settings.ensure_directories()
    identity = "0123456789abcdef0123456789abcdef"
    (settings.data_dir / ".tankarr-library-id").write_text(f"{identity}\n")
    (settings.library_dir / ".tankarr-library-id").write_text(f"{identity}\n")
    database = Database(settings.database_path)
    database.initialize()
    return (
        settings,
        database,
        TankarrService(settings, database, NoopProvider(), NoopKomga()),
    )


def release(chapter: str, source: str, **extra) -> dict:
    return {
        "id": f"{source}-{chapter}",
        "volume": None,
        "chapter": chapter,
        "title": f"Chapter {chapter}",
        "language": "en",
        "provider": "suwayomi",
        "source_name": source,
        "source_url": f"https://{source}/chapter/{chapter}",
        "groups": [],
        "publish_at": None,
        "pages": 20,
        "version": 1,
        **extra,
    }


def seed(database: Database, settings: Settings) -> dict:
    manga = database.upsert_manga(
        {
            "id": "quality-series",
            "provider": "catalogue",
            "title": "Quality Series",
            "description": "",
            "cover_url": None,
            "authors": ["Someone"],
            "available_languages": ["en"],
        },
        "en",
        "all",
    )
    # Twelve good chapters, plus chapter 13 from two sources.
    releases = [release(str(number), "good.example") for number in range(1, 13)]
    releases.append(release("13", "fragments.example"))
    releases.append(release("13", "good.example"))
    database.upsert_chapters(manga["id"], releases, monitor_new=True)
    return manga


def publish(
    database: Database,
    settings: Settings,
    manga: dict,
    chapter_id: str,
    sizes: list[tuple[int, int]],
) -> Path:
    chapter = database.get_chapter(chapter_id)
    path = final_library_path(settings.library_dir, manga, chapter)
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("ComicInfo.xml", b"<ComicInfo />")
        for index, (width, height) in enumerate(sizes, start=1):
            archive.writestr(f"{index:04d}.png", page(width, height))
    database.mark_chapter_downloaded(chapter_id, path)
    return path


@pytest.mark.asyncio
async def test_the_audit_finds_a_fragment_among_healthy_chapters(tmp_path: Path):
    settings, database, service = provision(tmp_path)
    manga = seed(database, settings)
    for number in range(1, 13):
        publish(
            database,
            settings,
            manga,
            f"good.example-{number}",
            [(800, 1600)] * 10,
        )
    # The fragment: two tiles where the work's chapters carry sixteen thousand
    # pixels of strip.
    publish(
        database,
        settings,
        manga,
        "fragments.example-13",
        [(720, 720), (720, 720)],
    )

    audit = await service.audit_library_page_quality()

    assert audit["measured"] == 13
    assert [item["chapter_id"] for item in audit["degraded"]] == [
        "fragments.example-13"
    ]
    measured = database.page_quality(manga["id"])
    assert measured["good.example-1"]["verdict"] == OK
    assert measured["fragments.example-13"]["verdict"] == DEGRADED


@pytest.mark.asyncio
async def test_a_fragment_is_replaced_from_the_source_that_has_the_chapter(
    tmp_path: Path,
):
    settings, database, service = provision(tmp_path)
    manga = seed(database, settings)
    for number in range(1, 13):
        publish(database, settings, manga, f"good.example-{number}", [(800, 1600)] * 10)
    publish(database, settings, manga, "fragments.example-13", [(720, 720)] * 2)
    await service.audit_library_page_quality()

    outcome = await service.recover_degraded_chapters(manga["id"])

    # The slot already holds a file, so this can never come from the
    # missing-release path: the other source of the same slot is chosen.
    assert [item["replacement"] for item in outcome["requeued"]] == ["good.example-13"]
    assert outcome["kept"] == []
    # The fragment stays on disk until the replacement has passed every
    # gate (ONE PIECE 554, live: deleting first and refusing the replacement
    # cost the chapter). The job knows what it supersedes.
    assert database.get_chapter("fragments.example-13")["downloaded"] is True
    assert str(database.get_chapter("fragments.example-13")["id"]) in (
        database.blocked_releases(manga["id"])
    )
    job = database.get_job(int(outcome["requeued"][0]["job_id"]))
    assert job["chapter_id"] == "good.example-13"
    assert job["supersedes_chapter_id"] == "fragments.example-13"
    assert not job["quality_override"]


@pytest.mark.asyncio
async def test_two_sources_agreeing_on_a_short_chapter_are_believed(tmp_path: Path):
    # ONE PIECE 554, live: Weeb Central 6588, MangaK 6191 against a floor of
    # 7354. Two sources with the same length are the chapter being short.
    settings, database, service = provision(tmp_path)
    manga = seed(database, settings)
    for number in range(1, 13):
        publish(database, settings, manga, f"good.example-{number}", [(800, 1600)] * 10)
    short = {
        "normalized_height": 3000,
        "pages": 3,
        "measured_pages": 3,
        "median_aspect": 2.0,
    }
    database.record_page_quality(
        manga["id"],
        "fragments.example-13",
        verdict="refused",
        assessment={**short, "reason": "x"},
    )
    good = database.get_chapter("good.example-13")
    witness = service._length_corroborated(
        manga["id"], good, {**short, "normalized_height": 3200}
    )
    assert witness == "fragments.example"
    # And a refused download never talks the floor down, nor is it judged
    # as if it were a library file.
    await service.audit_library_page_quality()
    numbers = service._chapter_numbers(manga["id"])
    baseline = service._page_quality_baseline(manga["id"], None, numbers)
    assert baseline is not None and baseline["floor"] > 3000
    assert (
        database.page_quality(manga["id"])["fragments.example-13"]["verdict"]
        == "refused"
    )


def test_a_retry_can_waive_the_length_gate_and_a_job_can_name_what_it_supersedes(
    tmp_path: Path,
):
    settings, database, service = provision(tmp_path)
    manga = seed(database, settings)
    job = database.create_job(manga["id"], "good.example-13", "en", origin="manual")
    assert not job["quality_override"] and job["supersedes_chapter_id"] is None
    job = database.annotate_job(int(job["id"]), supersedes="fragments.example-13")
    assert job["supersedes_chapter_id"] == "fragments.example-13"
    database.update_job(
        int(job["id"]), status="failed", message="DegradedPagesError: short"
    )
    job = database.retry_job(int(job["id"]), quality_override=True)
    assert job["status"] == "queued" and job["quality_override"] == 1


@pytest.mark.asyncio
async def test_a_fragment_no_other_source_carries_is_kept(tmp_path: Path):
    settings, database, service = provision(tmp_path)
    manga = seed(database, settings)
    for number in range(1, 13):
        publish(database, settings, manga, f"good.example-{number}", [(800, 1600)] * 10)
    publish(database, settings, manga, "fragments.example-13", [(720, 720)] * 2)
    # The only alternative is unusable: it already failed here.
    database.block_release("good.example-13", reason="Download failed")
    await service.audit_library_page_quality()

    outcome = await service.recover_degraded_chapters(manga["id"])

    assert outcome["requeued"] == []
    assert [item["chapter_id"] for item in outcome["kept"]] == ["fragments.example-13"]
    # An unreadable file still beats a hole, and the block is lifted so the
    # slot is not left looking failed.
    assert database.get_chapter("fragments.example-13")["downloaded"] is True
    assert "fragments.example-13" not in database.blocked_releases(manga["id"])


@pytest.mark.asyncio
async def test_a_series_with_too_few_chapters_is_never_judged(tmp_path: Path):
    settings, database, service = provision(tmp_path)
    manga = database.upsert_manga(
        {
            "id": "tiny-series",
            "provider": "catalogue",
            "title": "Tiny Series",
            "description": "",
            "cover_url": None,
            "authors": [],
            "available_languages": ["en"],
        },
        "en",
        "all",
    )
    database.upsert_chapters(
        manga["id"],
        [release(str(number), "good.example") for number in range(1, 4)],
        monitor_new=True,
    )
    publish(database, settings, manga, "good.example-1", [(800, 1600)] * 10)
    publish(database, settings, manga, "good.example-2", [(800, 1600)] * 10)
    publish(database, settings, manga, "good.example-3", [(10, 10)])

    audit = await service.audit_library_page_quality()

    assert audit["measured"] == 3
    assert audit["degraded"] == []


@pytest.mark.asyncio
async def test_a_short_extra_is_never_called_unreadable(tmp_path: Path):
    # Kingdom c503.5 is two full-resolution pages of bonus art, and I'm No
    # Angel c10.5 is three: complete extras, not fragments of a chapter.
    settings, database, service = provision(tmp_path)
    manga = seed(database, settings)
    database.upsert_chapters(
        manga["id"], [release("13.5", "good.example")], monitor_new=True
    )
    for number in range(1, 13):
        publish(database, settings, manga, f"good.example-{number}", [(800, 1600)] * 10)
    publish(database, settings, manga, "good.example-13.5", [(800, 1600)] * 2)

    audit = await service.audit_library_page_quality()

    assert audit["degraded"] == []
    assert "good.example-13.5" not in database.page_quality(manga["id"]) or (
        database.page_quality(manga["id"])["good.example-13.5"]["verdict"] != DEGRADED
    )


@pytest.mark.asyncio
async def test_extras_do_not_talk_the_floor_down(tmp_path: Path):
    # A run of two-page omake must not become the shape the series is judged
    # by, or nothing could ever be too short again.
    settings, database, service = provision(tmp_path)
    manga = seed(database, settings)
    database.upsert_chapters(
        manga["id"],
        [release(f"{number}.5", "good.example") for number in range(1, 13)],
        monitor_new=True,
    )
    for number in range(1, 13):
        publish(database, settings, manga, f"good.example-{number}", [(800, 1600)] * 10)
        publish(database, settings, manga, f"good.example-{number}.5", [(800, 1600)])
    publish(database, settings, manga, "fragments.example-13", [(720, 720)] * 2)

    audit = await service.audit_library_page_quality()

    assert [item["chapter_id"] for item in audit["degraded"]] == [
        "fragments.example-13"
    ]


@pytest.mark.asyncio
async def test_an_un_sliced_strip_is_replaced_by_the_source_that_slices(
    tmp_path: Path,
):
    # Omniscient Reader c308: flamecomics serves ten 800x15711 strips of the
    # same episode WEBTOON slices into tiles. Same comic, unreadable shape.
    settings, database, service = provision(tmp_path)
    manga = seed(database, settings)
    for number in range(1, 13):
        publish(database, settings, manga, f"good.example-{number}", [(800, 1600)] * 10)
    publish(database, settings, manga, "fragments.example-13", [(800, 15711)] * 10)

    audit = await service.audit_library_page_quality()

    assert [item["chapter_id"] for item in audit["degraded"]] == [
        "fragments.example-13"
    ]
    assert "taller than they are wide" in audit["degraded"][0]["reason"]

    outcome = await service.recover_degraded_chapters(manga["id"])

    assert [item["replacement"] for item in outcome["requeued"]] == ["good.example-13"]


def test_page_quality_gains_its_shape_column_on_an_existing_database(tmp_path: Path):
    # The table shipped before shape was an axis, so opening a database that
    # already has it must add the column rather than fail on first write.
    import sqlite3

    path = tmp_path / "tankarr.sqlite3"
    Database(path).initialize()
    with sqlite3.connect(path) as connection:
        connection.execute("ALTER TABLE page_quality DROP COLUMN median_aspect")
        assert "median_aspect" not in {
            row[1] for row in connection.execute("PRAGMA table_info(page_quality)")
        }

    reopened = Database(path)
    reopened.initialize()

    with sqlite3.connect(path) as connection:
        assert "median_aspect" in {
            row[1] for row in connection.execute("PRAGMA table_info(page_quality)")
        }


@pytest.mark.asyncio
async def test_a_single_series_can_be_audited_without_walking_the_library(
    tmp_path: Path,
):
    # The background sweep is bounded, so a series at the far end of it is
    # days away. Naming it must measure it now.
    settings, database, service = provision(tmp_path)
    manga = seed(database, settings)
    other = database.upsert_manga(
        {
            "id": "other-series",
            "provider": "catalogue",
            "title": "Other Series",
            "description": "",
            "cover_url": None,
            "authors": [],
            "available_languages": ["en"],
        },
        "en",
        "all",
    )
    database.upsert_chapters(
        other["id"], [release("1", "other.example")], monitor_new=True
    )
    publish(database, settings, other, "other.example-1", [(800, 1600)] * 10)
    for number in range(1, 13):
        publish(database, settings, manga, f"good.example-{number}", [(800, 1600)] * 10)
    publish(database, settings, manga, "fragments.example-13", [(800, 15711)] * 10)

    audit = await service.audit_library_page_quality(manga_ids=[manga["id"]])

    assert audit["measured"] == 13
    assert database.page_quality(other["id"]) == {}
    assert [item["chapter_id"] for item in audit["degraded"]] == [
        "fragments.example-13"
    ]


def test_the_audit_cursor_rotates_the_walk_to_start_after_the_last_series():
    from tankarr.service import TankarrService

    manga_list = [{"id": "a"}, {"id": "b"}, {"id": "c"}, {"id": "d"}]
    rotate = TankarrService._rotate_from_quality_audit_cursor

    class _StubDatabase:
        def __init__(self, cursor: str | None):
            self._cursor = cursor

        def get_setting_overrides(self):
            return (
                {}
                if self._cursor is None
                else {TankarrService.QUALITY_AUDIT_CURSOR_SETTING: self._cursor}
            )

    class _Stub:
        QUALITY_AUDIT_CURSOR_SETTING = TankarrService.QUALITY_AUDIT_CURSOR_SETTING

        def __init__(self, cursor: str | None):
            self.database = _StubDatabase(cursor)

    # No cursor yet (first pass ever): walk unchanged, starts at the top.
    assert rotate(_Stub(None), manga_list) == manga_list
    # Previous pass last visited "b": this pass starts right after it, and
    # wraps "a" and "b" to the end so they are revisited once everything
    # past them has had a turn.
    assert [m["id"] for m in rotate(_Stub("b"), manga_list)] == ["c", "d", "a", "b"]
    # The cursor was the last series in the list: wraps to the very start.
    assert [m["id"] for m in rotate(_Stub("d"), manga_list)] == ["a", "b", "c", "d"]
    # A cursor naming a series that no longer exists (deleted since) does
    # not crash the walk - it just starts from the top, same as no cursor.
    assert rotate(_Stub("gone"), manga_list) == manga_list


@pytest.mark.asyncio
async def test_a_tight_budget_reaches_every_series_across_passes_not_just_the_first(
    tmp_path: Path,
):
    """The bug this guards: an alphabetical, cursor-less walk gave every
    pass's whole budget to whichever series sorts first that still has
    anything unmeasured. A large, actively-growing series early in the
    alphabet could keep the rest of the library - including much larger,
    later-sorting series - permanently unmeasured. The fix is a resume
    cursor so each pass covers new ground instead of re-starting at "A".
    """

    settings, database, service = provision(tmp_path)
    first = database.upsert_manga(
        {
            "id": "aaa-first",
            "provider": "catalogue",
            "title": "AAA First",
            "description": "",
            "cover_url": None,
            "authors": [],
            "available_languages": ["en"],
        },
        "en",
        "all",
    )
    second = database.upsert_manga(
        {
            "id": "zzz-second",
            "provider": "catalogue",
            "title": "ZZZ Second",
            "description": "",
            "cover_url": None,
            "authors": [],
            "available_languages": ["en"],
        },
        "en",
        "all",
    )
    database.upsert_chapters(
        first["id"],
        [release(str(n), "first.example") for n in range(1, 4)],
        monitor_new=True,
    )
    database.upsert_chapters(
        second["id"],
        [release(str(n), "second.example") for n in range(1, 4)],
        monitor_new=True,
    )
    for number in range(1, 4):
        publish(database, settings, first, f"first.example-{number}", [(800, 1600)] * 5)
        publish(
            database, settings, second, f"second.example-{number}", [(800, 1600)] * 5
        )

    # A budget too small to cover both series in one pass.
    first_pass = await service.audit_library_page_quality(limit=3)
    assert first_pass["measured"] == 3
    assert database.page_quality(second["id"]) == {}

    second_pass = await service.audit_library_page_quality(limit=3)

    assert second_pass["measured"] == 3
    assert set(database.page_quality(second["id"]).keys()) == {
        f"second.example-{n}" for n in range(1, 4)
    }
