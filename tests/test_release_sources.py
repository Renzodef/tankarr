from __future__ import annotations

import threading
from pathlib import Path

import pytest

from tankarr.database import Database
from tankarr.metadata.base import SeriesMatchAssessment, normalized_title
from tankarr.release_sources import ReleaseSourceManager


class FakeProvider:
    name = "alternate"
    label = "Alternate source"
    search_mode = "title"

    def __init__(self, candidates: list[dict], chapters: dict[str, list[dict]]):
        self.candidates = candidates
        self.chapters = chapters

    def supports_language(self, language: str) -> bool:
        return language == "en"

    async def search_with_diagnostics(self, query: str, language: str, limit: int = 20):
        return self.candidates[:limit], []

    async def get_manga(self, manga_id: str):
        return next(dict(item) for item in self.candidates if item["id"] == manga_id)

    async def list_chapters(self, manga_id: str, language: str):
        return [dict(item) for item in self.chapters.get(manga_id, [])]


def seed(database: Database) -> None:
    database.upsert_manga(
        {
            "id": "origin-id",
            "provider": "mangadex",
            "title": "V.B. Rose",
            "description": "",
            "cover_url": None,
            "authors": ["Banri Hidaka"],
            "original_language": "ja",
            "status": "completed",
            "year": 2004,
            "last_chapter": "83",
            "last_volume": "14",
            "available_languages": ["en"],
            "source_url": "https://example.test/origin",
        },
        "en",
        "all",
    )


def candidate(identifier: str, title: str = "V.B. Rose") -> dict:
    return {
        "id": identifier,
        "provider": "alternate",
        "title": title,
        "alternate_titles": [],
        "authors": ["Banri Hidaka"],
        "year": 2004,
        "source_url": f"https://example.test/{identifier}",
        "source_name": "Alternate",
    }


def chapter(identifier: str, number: str) -> dict:
    return {
        "id": identifier,
        "provider": "alternate",
        "volume": "8",
        "chapter": number,
        "title": "",
        "language": "en",
        "groups": ["Group"],
        "publish_at": None,
        "source_url": f"https://example.test/chapter/{identifier}",
        "pages": 20,
        "version": 1,
    }


@pytest.mark.asyncio
async def test_discovery_persists_verified_source_and_indexes_its_releases(
    tmp_path: Path,
    monkeypatch,
):
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    seed(database)
    provider = FakeProvider(
        [candidate("alt-series")], {"alt-series": [chapter("alt-46", "46")]}
    )
    manager = ReleaseSourceManager(database, {"alternate": provider})
    event_thread = threading.get_ident()
    upsert = database.upsert_chapters

    def background_upsert(*args, **kwargs):
        assert threading.get_ident() != event_thread
        return upsert(*args, **kwargs)

    monkeypatch.setattr(database, "upsert_chapters", background_upsert)

    result = await manager.discover_and_refresh("origin-id", monitor_new=True)

    assert result["matched_sources"] == 1
    mapping = database.list_release_sources("origin-id")
    assert [(item["provider"], item["provider_manga_id"]) for item in mapping] == [
        ("alternate", "alt-series")
    ]
    releases = manager.releases_for_slot("origin-id", chapter="46", volume="8")
    assert [item["id"] for item in releases] == ["alt-46"]
    assert releases[0]["provider"] == "alternate"
    refreshed = await manager.refresh_mappings("origin-id", monitor_new=True)
    assert refreshed["seen"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("subtype", ["Novel", "Light Novel"])
async def test_prose_novel_does_not_match_comic_adaptation_by_title(
    tmp_path: Path, subtype: str
):
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    seed(database)
    database.save_series_metadata(
        "origin-id",
        {"classification": {"kind": "book", "subtype": subtype}},
        artwork_path=None,
        artwork_sha256=None,
        artwork_media_type=None,
        source_status=[],
    )
    provider = FakeProvider(
        [candidate("comic-adaptation")],
        {"comic-adaptation": [chapter("comic-1", "1")]},
    )
    manager = ReleaseSourceManager(database, {"alternate": provider})

    result = await manager.discover_and_refresh("origin-id", monitor_new=True)

    assert result["matched_sources"] == 0
    assert database.list_release_sources("origin-id") == []
    assert database.list_chapters("origin-id", "en") == []


@pytest.mark.asyncio
async def test_ambiguous_equal_candidates_are_not_correlated(tmp_path: Path):
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    seed(database)
    provider = FakeProvider(
        [candidate("first"), candidate("second")],
        {"first": [chapter("first-46", "46")], "second": [chapter("second-46", "46")]},
    )
    manager = ReleaseSourceManager(database, {"alternate": provider})

    result = await manager.discover_and_refresh("origin-id", monitor_new=True)

    assert result["matched_sources"] == 0
    assert any(item["state"] == "ambiguous" for item in result["sources"])
    assert database.list_release_sources("origin-id") == []


def test_provider_release_cannot_be_silently_reassigned_to_another_series(
    tmp_path: Path,
):
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    seed(database)
    database.upsert_manga(
        {
            "id": "other",
            "provider": "mangadex",
            "title": "Other",
            "description": "",
            "authors": [],
            "available_languages": ["en"],
        },
        "en",
        "none",
    )
    database.upsert_chapters("origin-id", [chapter("shared-id", "46")])

    with pytest.raises(ValueError, match="already assigned"):
        database.upsert_chapters("other", [chapter("shared-id", "1")])


def test_global_chapter_identity_joins_provider_releases_with_different_volume_hints(
    tmp_path: Path,
):
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    seed(database)
    first = chapter("first-46", "46")
    second = {**chapter("second-46", "46"), "volume": None, "provider": "mangadex"}
    database.upsert_chapters("origin-id", [first, second])

    updated = database.set_chapter_monitored("first-46", False)

    assert updated == 2
    assert all(
        item["monitored"] is False for item in database.list_chapters("origin-id", "en")
    )
    database.set_chapter_monitored("second-46", True)
    assert len(database.preferred_missing_releases("origin-id")) == 1


@pytest.mark.asyncio
async def test_refresh_drops_mappings_from_a_previous_suwayomi_instance(tmp_path: Path):
    from tankarr.providers.suwayomi import StaleSuwayomiIdentity

    class StaleProvider(FakeProvider):
        name = "suwayomi"

        async def list_chapters(self, manga_id: str, language: str):
            if manga_id == "suwayomi-1079":
                raise StaleSuwayomiIdentity("previous instance")
            return await super().list_chapters(manga_id, language)

    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    seed(database)
    provider = StaleProvider(
        [], {"suwayomi-abcd1234-4": [chapter("suwayomi-abcd1234-40", "1")]}
    )
    for provider_manga_id in ("suwayomi-1079", "suwayomi-abcd1234-4"):
        database.upsert_release_source(
            "origin-id",
            provider="suwayomi",
            provider_manga_id=provider_manga_id,
            title="V.B. Rose",
            source_url=None,
            source_name="Weeb Central (EN)",
            language="en",
            match_confidence=1.0,
            match_reason="test",
        )
    manager = ReleaseSourceManager(database, {"suwayomi": provider})

    result = await manager.refresh_mappings("origin-id", monitor_new=True)

    states = {item["provider_manga_id"]: item["state"] for item in result["sources"]}
    assert states == {"suwayomi-1079": "stale", "suwayomi-abcd1234-4": "matched"}
    assert [
        m["provider_manga_id"] for m in database.list_release_sources("origin-id")
    ] == ["suwayomi-abcd1234-4"]


@pytest.mark.asyncio
async def test_official_platform_link_maps_the_source_by_url_not_title(tmp_path: Path):
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    seed(database)
    database.save_series_metadata(
        "origin-id",
        {
            "official_links": [
                {
                    "name": "MANGA Plus",
                    "url": "https://mangaplus.shueisha.co.jp/titles/100020",
                    "language": "en",
                    "type": "webplatform",
                }
            ]
        },
        artwork_path=None,
        artwork_sha256=None,
        artwork_media_type=None,
        source_status=[],
    )
    # The official platform lists the work under a different title; discovery
    # still maps it because the catalogue names this exact page.
    odd = candidate("plus-1", "V.B.R. (Official)")
    odd["source_url"] = "https://mangaplus.shueisha.co.jp/titles/100020"
    odd["source_name"] = "MANGA Plus by SHUEISHA (EN)"
    provider = FakeProvider([odd], {"plus-1": [chapter("plus-c1", "1")]})
    manager = ReleaseSourceManager(database, {"alternate": provider})

    result = await manager.discover_and_refresh("origin-id", monitor_new=True)

    matched = [s for s in result["sources"] if s.get("state") == "matched"]
    assert [m["provider_manga_id"] for m in matched] == ["plus-1"]
    assert "Official platform page" in matched[0]["reason"]
    assert matched[0]["confidence"] == 1.0


def test_a_homonym_is_refused_without_asking(tmp_path: Path):
    """A different creator on a title that is not the work's own is another
    work: it is refused on the spot and kept only as history."""

    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    database.upsert_manga(
        {
            "id": "m1",
            "title": "Hansel and Gretel",
            "description": "",
            "cover_url": None,
            "authors": ["Junko Mizuno"],
            "original_language": "ja",
        },
        "en",
        "all",
    )
    manager = ReleaseSourceManager.__new__(ReleaseSourceManager)
    manager.database = database
    homonym = SeriesMatchAssessment(
        0.6,
        0.6,
        "primary matches candidate alias",
        "alias_exact",
        0.9,
        0.0,
        (),
        ("creator disagreement",),
    )
    near_miss = SeriesMatchAssessment(
        0.8, 0.8, "primary exact, creator", "primary_exact", 1.0, 1.0, (), ()
    )

    manager._queue_review(
        "m1",
        "suwayomi",
        {"id": "s1", "title": "Hansel & Gretel"},
        0.6,
        "alias",
        assessment=homonym,
    )
    manager._queue_review(
        "m1",
        "suwayomi",
        {"id": "s2", "title": "Hansel and Gretel"},
        0.8,
        "near miss",
        assessment=near_miss,
    )

    assert database.list_match_reviews() == []
    every = database.list_match_reviews(open_only=False)
    refused = next(item for item in every if item["candidate_id"] == "s1")
    assert refused["resolution"].startswith("rejected")
    near_miss_row = next(item for item in every if item["candidate_id"] == "s2")
    assert (
        near_miss_row["resolution"] == "rejected: not verified by the automatic rules"
    )


class SuwayomiLikeProvider(FakeProvider):
    name = "suwayomi"
    label = "Suwayomi sources"


def source_candidate(
    identifier: str, title: str, source: str, authors=("Banri Hidaka",)
) -> dict:
    return {
        **candidate(identifier, title),
        "provider": "suwayomi",
        "source_id": source.lower(),
        "source_name": source,
        "authors": list(authors),
    }


def resolutions(
    database: Database, manga_id: str = "origin-id"
) -> dict[str, str | None]:
    return {
        str(r["candidate_id"]): r.get("resolution")
        for r in database.list_match_reviews(open_only=False)
        if r["manga_id"] == manga_id
    }


@pytest.mark.asyncio
async def test_a_side_work_is_rejected_when_the_work_itself_is_served(tmp_path: Path):
    # Tower of God: Urek Mazino, live: same creator, and sixty chapters of the
    # side story merged into the tower's own numbering.
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    seed(database)
    provider = SuwayomiLikeProvider(
        [
            source_candidate("main", "V.B. Rose", "Webtoons"),
            source_candidate("side", "V.B. Rose: Side Story", "Asura"),
        ],
        {"main": [chapter("main-1", "1")], "side": [chapter("side-1", "1")]},
    )
    manager = ReleaseSourceManager(database, {"suwayomi": provider})

    await manager.discover_and_refresh("origin-id", monitor_new=True)

    mapped = {
        m["provider_manga_id"] for m in database.list_release_sources("origin-id")
    }
    assert mapped == {"main"}
    assert str(resolutions(database).get("side") or "").startswith(
        "rejected: side work"
    )


@pytest.mark.asyncio
async def test_a_source_already_serving_the_work_is_not_asked_again(tmp_path: Path):
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    seed(database)
    database.upsert_release_source(
        "origin-id",
        provider="suwayomi",
        provider_manga_id="first",
        title="V.B. Rose",
        source_url=None,
        source_name="Asura",
        language="en",
        match_confidence=0.95,
        match_reason="primary exact, creator",
    )
    provider = SuwayomiLikeProvider(
        [
            source_candidate("first", "V.B. Rose", "Asura"),
            source_candidate("second", "V.B. Rose", "Asura"),
        ],
        {"first": [chapter("f-1", "1")], "second": [chapter("s-1", "1")]},
    )
    manager = ReleaseSourceManager(database, {"suwayomi": provider})

    await manager.discover_and_refresh("origin-id", monitor_new=True)

    assert {
        m["provider_manga_id"] for m in database.list_release_sources("origin-id")
    } == {"first"}
    assert resolutions(database) == {}  # no review was opened


@pytest.mark.asyncio
async def test_an_unverifiable_exact_title_is_noise_for_a_well_served_work(
    tmp_path: Path,
):
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    seed(database)
    for n, source in enumerate(("Asura", "MangaK", "Weeb")):
        database.upsert_release_source(
            "origin-id",
            provider="suwayomi",
            provider_manga_id=f"s{n}",
            title="V.B. Rose",
            source_url=None,
            source_name=source,
            language="en",
            match_confidence=0.95,
            match_reason="primary exact, creator",
        )
    # Two entries with the work's exact title and nothing else: the shape
    # XCOMIC gave live for Tower of God and Ultra Heaven.
    provider = SuwayomiLikeProvider(
        [
            source_candidate("x", "V.B. Rose", "XCOMIC", authors=()),
            source_candidate("y", "V.B. Rose", "XCOMIC", authors=()),
        ],
        {"x": [], "y": []},
    )
    manager = ReleaseSourceManager(database, {"suwayomi": provider})

    await manager.discover_and_refresh("origin-id", monitor_new=True)

    mapped = {
        m["provider_manga_id"] for m in database.list_release_sources("origin-id")
    }
    assert not ({"x", "y"} & mapped)
    decided = [r for r in resolutions(database).values() if r]
    assert decided and all(str(r).startswith("rejected: unverifiable") for r in decided)


@pytest.mark.asyncio
async def test_the_webtoons_page_flag_marks_a_pause_the_extension_does_not_report(
    tmp_path: Path,
):
    from types import SimpleNamespace

    from tankarr.release_sources import ReleaseSourceManager

    class Official:
        name = "suwayomi"

        async def get_manga(self, manga_id: str) -> dict:
            return {"id": manga_id, "status": "ongoing"}

    manager = ReleaseSourceManager.__new__(ReleaseSourceManager)
    manager.fetch_page = None
    # The desktop URL is what the source stores; the flag lives on the
    # mobile page, so that is the one fetched.
    pages = {
        "https://m.webtoons.com/en/super-hero/unordinary/list?title_no=679": (
            '{"title":{"titleNo":679,"titleStatusString":"On hiatus"}}'
        )
    }

    async def fetch(url: str) -> str:
        return pages[url]

    manager.fetch_page = fetch
    mapping = {
        "provider_manga_id": "wt-679",
        "source_role": "primary_official",
        "source_url": "https://www.webtoons.com/en/super-hero/unordinary/list?title_no=679",
    }
    assert await manager._official_status(Official(), mapping) == "hiatus"
    # A non-official mapping is never asked; a page without the flag keeps
    # the extension's word; a fetch failure is silent.
    assert (
        await manager._official_status(
            Official(), {**mapping, "source_role": "alternative"}
        )
        is None
    )
    pages[ReleaseSourceManager.webtoons_status_url(mapping["source_url"])] = (
        "<html>no json here</html>"
    )
    assert await manager._official_status(Official(), mapping) == "ongoing"

    async def broken(url: str) -> str:
        raise OSError("offline")

    manager.fetch_page = broken
    assert await manager._official_status(Official(), mapping) == "ongoing"
    assert (
        await manager._official_status(
            SimpleNamespace(name="mangadex"),
            {**mapping, "source_url": "https://mangadex.org/x"},
        )
        is None
    )


@pytest.mark.asyncio
async def test_the_creator_note_will_return_is_a_pause_too(tmp_path: Path):
    from tankarr.release_sources import ReleaseSourceManager

    manager = ReleaseSourceManager.__new__(ReleaseSourceManager)

    async def fetch(url: str) -> str:
        return '<div class="note">unOrdinary will return!</div>'

    manager.fetch_page = fetch
    assert (
        await manager._webtoons_page_status(
            "https://www.webtoons.com/en/super-hero/unordinary/list?title_no=679"
        )
        == "hiatus"
    )


def doujin_list(count: int) -> list[dict]:
    return [
        {
            **chapter(f"dj-{index}", str(index)),
            "title": f"doujin {index}",
            "volume": None,
        }
        for index in range(1, count + 1)
    ]


@pytest.mark.asyncio
async def test_a_source_whose_list_is_not_the_work_is_refused_and_not_remapped(
    tmp_path: Path,
):
    """Niadd, 2026-09-04: an exact title whose chapter list was 2 163 doujin
    entries. The title matched, so the old code mapped it and indexed all
    of it. Now the list is judged before it enters, the mapping is undone
    and the next discovery pass does not retry it."""

    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    seed(database)
    provider = FakeProvider([candidate("niadd-vb")], {"niadd-vb": doujin_list(40)})
    manager = ReleaseSourceManager(database, {"alternate": provider})

    result = await manager.discover_and_refresh("origin-id", monitor_new=True)

    assert database.list_release_sources("origin-id") == []
    assert database.list_chapters("origin-id", "en") == []
    states = [item.get("state") for item in result["sources"]]
    assert "refused" in states
    assert database.release_source_rejections("origin-id") == {
        (
            "alternate",
            "niadd-vb",
        ): "chapter list refused: 40 of 40 entries are doujin/fan works"
    }

    again = await manager.discover_and_refresh("origin-id", monitor_new=True)
    assert database.list_release_sources("origin-id") == []
    assert "rejected" in [item.get("state") for item in again["sources"]]

    # The operator's hand lifts the ban.
    assert (
        database.clear_release_source_rejection("origin-id", "alternate", "niadd-vb")
        == 1
    )


def test_a_chapter_list_far_beyond_the_catalogue_is_implausible():
    from tankarr.release_sources import implausible_chapter_list

    target = {"chapter_count": 83, "title": "V.B. Rose"}
    plausible = [chapter(f"c{n}", str(n)) for n in range(1, 90)]
    assert implausible_chapter_list(plausible, target) is None
    # Extras and a few decimals are fine; five hundred numbered entries are not.
    assert implausible_chapter_list(
        [chapter(f"c{n}", str(n)) for n in range(1, 501)], target
    ).startswith("500 chapters against a catalogue of 83")
    assert "reach 1200" in implausible_chapter_list(
        [chapter("c1", "1"), chapter("c2", "1200")] + plausible[:20], target
    )
    # Without a trusted catalogue count only the absolute cap and the
    # unrelated-entry rule apply.
    assert implausible_chapter_list(plausible * 40, {"chapter_count": None}) is not None
    assert implausible_chapter_list(plausible, {"chapter_count": None}) is None
    assert implausible_chapter_list([], target) is None


def test_a_finished_or_small_work_refuses_a_list_twice_its_size():
    """MangaK offered the 112 chapters of "Sweat and Soap" for the 7 of
    "Sweat and Honey"; XCOMIC listed 163 webtoon episodes for the 76 chapters
    of Blade of the Phantom Master. A finished work has a firm count."""

    from tankarr.release_sources import implausible_chapter_list

    def rows(count: int) -> list[dict]:
        return [
            {"chapter": str(n), "title": f"Chapter {n}"} for n in range(1, count + 1)
        ]

    small = {"chapter_count": 7, "status": "ended", "title": "Sweat and Honey"}
    assert implausible_chapter_list(rows(9), small) is None  # extras and splits
    assert implausible_chapter_list(rows(112), small) is not None
    finished = {
        "chapter_count": 76,
        "status": "ended",
        "title": "Blade of the Phantom Master",
    }
    assert implausible_chapter_list(rows(81), finished) is None  # 76 + gaiden
    assert "163 chapters" in implausible_chapter_list(rows(163), finished)
    # Three scanlation groups of the same 76 chapters are still 76 chapters.
    assert implausible_chapter_list(rows(76) * 3, finished) is None
    running = {"chapter_count": 76, "status": "ongoing", "title": "Something running"}
    assert implausible_chapter_list(rows(163), running) is None


def test_plus_in_a_title_distinguishes_a_sequel():
    assert normalized_title("Citrus+") == "citrus plus"
    assert normalized_title("Citrus+") != normalized_title("Citrus")


@pytest.mark.asyncio
async def test_finished_work_refuses_same_title_source_with_too_many_chapters(
    tmp_path: Path,
):
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    database.upsert_manga(
        {
            "id": "origin-id",
            "provider": "catalogue",
            "title": "Citrus",
            "description": "",
            "authors": ["Ayuko"],
            "status": "ended",
            "year": 2010,
            "last_chapter": "11",
            "available_languages": ["en"],
        },
        "en",
        "all",
    )
    database.save_series_metadata(
        "origin-id",
        {"authors": ["Ayuko"], "chapter_count": 11, "status": "ended"},
        artwork_path=None,
        artwork_sha256=None,
        artwork_media_type=None,
        source_status=[],
    )
    wrong_work = {
        **candidate("other-citrus", "Citrus"),
        "authors": ["Ayuko"],
        "year": 2010,
    }
    provider = FakeProvider(
        [wrong_work],
        {
            "other-citrus": [
                chapter(f"other-{number}", str(number)) for number in range(1, 42)
            ]
        },
    )
    manager = ReleaseSourceManager(database, {"alternate": provider})

    result = await manager.discover_and_refresh("origin-id", monitor_new=True)

    assert manager._target("origin-id")["status"] == "ended"
    assert database.list_release_sources("origin-id") == []
    assert database.list_chapters("origin-id", "en") == []
    assert "refused" in [item.get("state") for item in result["sources"]]


@pytest.mark.asyncio
async def test_refreshing_a_mapped_source_refuses_a_list_that_turned_unrelated(
    tmp_path: Path,
):
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    seed(database)
    provider = FakeProvider(
        [candidate("alt-series")], {"alt-series": [chapter("alt-46", "46")]}
    )
    manager = ReleaseSourceManager(database, {"alternate": provider})
    await manager.discover_and_refresh("origin-id", monitor_new=True)
    assert len(database.list_chapters("origin-id", "en")) == 1

    provider.chapters["alt-series"] = doujin_list(30)
    result = await manager.refresh_mappings("origin-id", monitor_new=True)

    assert [item["state"] for item in result["sources"]] == ["refused"]
    assert len(database.list_chapters("origin-id", "en")) == 1  # nothing new entered
    mapping = database.list_release_sources("origin-id")[0]
    assert str(mapping["last_error"]).startswith("Chapter list refused")
