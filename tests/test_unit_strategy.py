"""Automatic chapters-vs-volumes choice.

A series stuck on chapter Wanted switches only when books can finish the
edition and chapters cannot. Foreign books count when translation is on.
"""

from __future__ import annotations

from pathlib import Path

from tankarr.config import Settings
from tankarr.database import Database
from tankarr.service import TankarrService
from tankarr.unit_strategy import decide_unit_strategy, whole_book_number

METADATA = {
    "status": "ended",
    "volume_count": 6,
    "chapter_count": 53,
    "count_confidence": {"volume": "agreed", "chapter": "agreed"},
}


def _chapter(number: int, **extra) -> dict:
    return {
        "id": f"en-{number}",
        "title": f"Chapter {number}",
        "chapter": str(number),
        "volume": None,
        "release_unit": "chapter",
        "language": "en",
        "provider": "suwayomi",
        "downloaded": False,
        "monitored": True,
        **extra,
    }


def _foreign_volume(number: int) -> dict:
    return {
        "id": f"zh-{number}",
        "title": f"第{number}卷",
        "chapter": str(number),
        "volume": None,
        "release_unit": "chapter",
        "language": "zh",
        "provider": "suwayomi",
        "downloaded": False,
        "monitored": True,
    }


def _manga(**extra) -> dict:
    return {"preferred_language": "en", "status": "ended", **extra}


def _decide(releases, **kwargs):
    return decide_unit_strategy(
        manga=kwargs.pop("manga", _manga()),
        metadata=kwargs.pop("metadata", METADATA),
        releases=releases,
        translation_enabled=kwargs.pop("translation_enabled", True),
        auto_unit_strategy=kwargs.pop("auto_unit_strategy", True),
    )


def test_whole_book_titles_are_volumes_and_chapter_titles_are_not():
    assert whole_book_number("第6卷") == 6
    assert whole_book_number("Volume 12 VF") == 12
    assert whole_book_number("제3권") == 3
    assert whole_book_number("第1话") is None
    assert whole_book_number("第3章") is None
    assert whole_book_number("Volume 1 Fragments") is None
    assert whole_book_number("Volume 2 VF Chapter 9") is None


def test_foreign_volume_pack_switches_when_translation_is_on():
    releases = [_foreign_volume(n) for n in range(1, 7)]
    releases.append(_chapter(40))
    decision = _decide(releases)

    assert decision.action == "switch"
    assert decision.unit == "volumes"
    assert decision.translation_used is True
    assert decision.languages == ("zh",)
    assert decision.volumes == (1, 2, 3, 4, 5, 6)
    assert "chapters do not cover" in decision.reason


def test_real_chapter_coverage_stays_on_chapters():
    releases = [_chapter(n) for n in range(1, 54)]
    releases.extend(_foreign_volume(n) for n in range(1, 7))
    decision = _decide(releases)

    assert decision.action == "stay"
    assert decision.unit == "chapters"
    assert decision.reason == "chapters already cover the edition"


def test_translation_off_does_not_count_foreign_only_volumes():
    releases = [_foreign_volume(n) for n in range(1, 7)]
    decision = _decide(releases, translation_enabled=False)

    assert decision.action == "stay"
    assert "translation is off" in decision.reason


def test_preferred_language_volumes_switch_without_translation():
    releases = [
        {
            "id": f"en-v{n}",
            "title": f"Volume {n}",
            "chapter": None,
            "volume": str(n),
            "release_unit": "volume",
            "language": "en",
            "provider": "suwayomi",
            "downloaded": False,
        }
        for n in range(1, 7)
    ]
    decision = _decide(releases, translation_enabled=False)

    assert decision.action == "switch"
    assert decision.translation_used is False
    assert decision.languages == ("en",)


def test_partial_volumes_and_a_lone_count_do_not_switch():
    partial = _decide([_foreign_volume(n) for n in range(1, 4)])
    assert partial.action == "stay"
    assert partial.reason == "volumes do not cover the edition"

    lone = _decide(
        [_foreign_volume(n) for n in range(1, 7)],
        metadata={
            **METADATA,
            "count_confidence": {"volume": "lone", "chapter": "agreed"},
        },
    )
    assert lone.action == "stay"
    assert lone.reason == "no reliable volume count"


def test_operator_choice_strategy_off_and_webtoon_stay():
    volumes = [_foreign_volume(n) for n in range(1, 7)]
    pinned = _decide(volumes, manga=_manga(series_unit_override="chapters"))
    assert pinned.action == "stay"
    assert pinned.reason == "the series unit was chosen for this series"

    disabled = _decide(volumes, auto_unit_strategy=False)
    assert disabled.action == "stay"
    assert disabled.reason == "automatic unit strategy is off"

    webtoon = _decide(
        volumes,
        metadata={**METADATA, "content_kind": "webtoon"},
    )
    assert webtoon.action == "stay"
    assert "webtoon" in webtoon.reason


def test_unknown_chapter_total_with_real_chapters_stays():
    decision = _decide(
        [_chapter(1), *_foreign_volumes()],
        metadata={
            "status": "ended",
            "volume_count": 6,
            "count_confidence": {"volume": "agreed", "chapter": "lone"},
        },
    )
    assert decision.action == "stay"
    assert "chapter total is unknown" in decision.reason


def test_books_on_disk_switch_a_finished_series():
    releases = [_chapter(n) for n in range(1, 54)]
    releases.extend(
        {
            "id": f"disk-{n}",
            "title": f"Volume {n}",
            "chapter": None,
            "volume": str(n),
            "release_unit": "volume",
            "language": "en",
            "provider": "manual",
            "downloaded": True,
        }
        for n in range(1, 7)
    )
    decision = _decide(releases, translation_enabled=False)

    assert decision.action == "switch"
    assert "already in the library" in decision.reason


def test_running_work_with_real_chapters_is_not_flipped_for_owned_books():
    releases = [_chapter(n) for n in range(1, 11)]
    releases.extend(
        {
            "id": f"disk-{n}",
            "title": f"Volume {n}",
            "volume": str(n),
            "chapter": None,
            "release_unit": "volume",
            "language": "en",
            "downloaded": True,
            "provider": "manual",
        }
        for n in range(1, 7)
    )
    decision = _decide(
        releases,
        manga=_manga(status="ongoing"),
        metadata={
            "status": "continuing",
            "volume_count": 6,
            "count_confidence": {"volume": "agreed", "chapter": "lone"},
        },
        translation_enabled=False,
    )
    assert decision.action == "stay"


def _foreign_volumes():
    return [_foreign_volume(n) for n in range(1, 7)]


class _Provider:
    name = "suwayomi"


class _Komga:
    pass


def _service(tmp_path: Path, **settings) -> tuple[Database, TankarrService]:
    library = tmp_path / "library"
    library.mkdir()
    data = tmp_path / "data"
    data.mkdir()
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    service = TankarrService(
        Settings(data_dir=data, library_dir=library, auth_required=False, **settings),
        database,
        _Provider(),  # type: ignore[arg-type]
        _Komga(),  # type: ignore[arg-type]
    )
    return database, service


def _seed(database: Database) -> None:
    database.upsert_manga(
        {
            "id": "rasputin",
            "title": "Rasputin",
            "description": "",
            "authors": ["Author"],
            "status": "ended",
            "original_language": "zh",
        },
        "en",
        "all",
    )
    database.save_series_metadata(
        "rasputin",
        METADATA,
        artwork_path=None,
        artwork_sha256=None,
        artwork_media_type=None,
        source_status=[],
    )
    rows = [
        {
            "id": "owned-1",
            "chapter": "1",
            "title": "Chapter 1",
            "language": "en",
            "provider": "suwayomi",
            "groups": [],
            "publish_at": None,
            "source_url": "",
        },
        {
            "id": "gap-40",
            "chapter": "40",
            "title": "Chapter 40",
            "language": "en",
            "provider": "suwayomi",
            "groups": [],
            "publish_at": None,
            "source_url": "",
        },
        {
            "id": "gap-41",
            "chapter": "41",
            "title": "Chapter 41",
            "language": "en",
            "provider": "suwayomi",
            "groups": [],
            "publish_at": None,
            "source_url": "",
        },
    ]
    rows.extend(
        {
            "id": f"zh-{n}",
            "chapter": str(n),
            "title": f"第{n}卷",
            "language": "zh",
            "provider": "suwayomi",
            "groups": [],
            "publish_at": None,
            "source_url": "",
        }
        for n in range(1, 7)
    )
    database.upsert_chapters("rasputin", rows)
    database.mark_chapter_downloaded(
        "owned-1", Path("Rasputin/Rasputin - c001 [en].cbz")
    )
    database.create_job("rasputin", "gap-40", "en", origin="monitor")
    database.create_job("rasputin", "gap-41", "en", origin="manual")


def test_apply_switches_clears_chapter_wanted_and_keeps_files(tmp_path: Path):
    database, service = _service(tmp_path, translation_enabled=True)
    _seed(database)

    result = service.apply_unit_strategy("rasputin")

    assert result["switched"] is True
    assert result["translation_used"] is True
    assert result["reclassified"] == 6
    assert result["unmonitored_chapters"] == 2
    assert result["cancelled_jobs"] == 1
    manga = database.get_manga("rasputin")
    assert manga["series_unit_override"] == "volumes"
    strategy = manga["series_unit_strategy"]
    assert strategy["operator"] is False
    assert strategy["translation_used"] is True
    assert strategy["languages"] == ["zh"]
    assert "chapters do not cover" in strategy["reason"]

    owned = database.get_chapter("owned-1")
    assert owned["downloaded"] is True
    assert owned["monitored"] is True
    assert owned["library_path"] == "Rasputin/Rasputin - c001 [en].cbz"
    assert owned["release_unit"] == "chapter"

    gap = database.get_chapter("gap-40")
    assert gap["downloaded"] is False
    assert gap["monitored"] is False
    assert database.get_chapter("gap-41")["monitored"] is False

    for number in range(1, 7):
        book = database.get_chapter(f"zh-{number}")
        assert book["release_unit"] == "volume"
        assert book["volume"] == str(number)
        assert book["chapter"] is None
        assert book["monitored"] is True

    with database.connect() as connection:
        jobs = [
            dict(row)
            for row in connection.execute(
                "SELECT chapter_id, origin FROM download_job WHERE manga_id=?",
                ("rasputin",),
            )
        ]
    assert jobs == [{"chapter_id": "gap-41", "origin": "manual"}]
    missing = database.preferred_missing_releases("rasputin")
    assert {item["id"] for item in missing}.isdisjoint({"gap-40", "gap-41"})

    again = service.apply_unit_strategy("rasputin")
    assert again["action"] == "maintain"
    assert again["switched"] is False
    assert database.get_chapter("owned-1")["downloaded"] is True


def test_apply_does_not_override_an_explicit_chapters_choice(tmp_path: Path):
    database, service = _service(tmp_path, translation_enabled=True)
    _seed(database)
    database.update_manga("rasputin", {"series_unit_override": "chapters"})

    result = service.apply_unit_strategy("rasputin")

    assert result["action"] == "stay"
    assert database.get_manga("rasputin")["series_unit_override"] == "chapters"
    assert database.get_chapter("gap-40")["monitored"] is True
    assert database.get_chapter("zh-1")["release_unit"] == "volume"
    assert database.get_chapter("owned-1")["library_path"]


def test_apply_leaves_chapters_when_translation_is_off(tmp_path: Path):
    database, service = _service(tmp_path, translation_enabled=False)
    _seed(database)

    result = service.apply_unit_strategy("rasputin")

    assert result["action"] == "stay"
    assert "translation is off" in result["reason"]
    assert database.get_manga("rasputin")["series_unit_override"] is None
    assert database.get_chapter("gap-40")["monitored"] is True
    assert database.get_chapter("zh-1")["release_unit"] == "volume"
