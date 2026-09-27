from __future__ import annotations

from datetime import UTC

from tankarr.chapter_map import MapEntry
from tankarr.wanted_recovery import (
    AMBIGUOUS,
    CHANNEL_INDEXER_BOOK,
    CHANNEL_INDEXER_CHAPTER,
    CHANNEL_SOURCES,
    ERROR,
    EXHAUSTED,
    GRABBED,
    MAX_CHAPTER_QUERIES,
    NEEDS_REVIEW,
    NOT_OFFERED,
    QUEUED,
    RECOVERING,
    RETRY_AFTER_DAYS,
    UNAVAILABLE,
    UNSEARCHED,
    chapter_queries,
    containing_volumes,
    pick_chapter_release,
    recently_answered,
    slot_key_for,
    slot_verdict,
)


def result(title: str, **extra):
    return {
        "id": title,
        "title": title,
        "provider": "prowlarr",
        "protocol": "torrent",
        "seeders": 12,
        "size_bytes": 20 * 1024 * 1024,
        **extra,
    }


def attempt(channel: str, outcome: str, detail: str = ""):
    return {
        "channel": channel,
        "outcome": outcome,
        "detail": detail,
        "attempted_at": "2026-09-01T00:00:00+00:00",
    }


def test_a_slot_is_keyed_by_its_chapter_not_by_the_release_that_fills_it():
    assert slot_key_for({"id": "a", "chapter": "82", "volume": None}) == "chapter:82"
    assert slot_key_for({"id": "b", "chapter": "82.0", "volume": None}) == "chapter:82"
    assert slot_key_for({"id": "c", "chapter": None, "volume": "3"}) == "volume:3"
    assert slot_key_for({"id": "d", "chapter": None, "volume": None}) == "release:d"


def test_chapter_queries_ask_the_numbered_forms_before_the_bare_series():
    queries = chapter_queries("Galaxy Express 999", "82", ["Kodansha"])

    # Two numbered forms and one series query: measured live, six searches
    # per chapter was what pushed a backlog pass past the request timeout.
    assert queries == [
        "Galaxy Express 999 chapter 82",
        "Galaxy Express 999 c82",
        "Galaxy Express 999 Kodansha",
    ]
    assert len(queries) <= MAX_CHAPTER_QUERIES


def test_a_recent_no_is_not_asked_again_but_an_error_is():
    from datetime import datetime, timedelta

    now = datetime(2026, 9, 2, tzinfo=UTC)
    recent = (now - timedelta(days=2)).isoformat()
    old = (now - timedelta(days=RETRY_AFTER_DAYS + 1)).isoformat()

    def answer(outcome: str, when: str):
        return [
            {
                "channel": CHANNEL_INDEXER_CHAPTER,
                "outcome": outcome,
                "attempted_at": when,
            }
        ]

    assert recently_answered(
        answer(NOT_OFFERED, recent), CHANNEL_INDEXER_CHAPTER, now=now
    )
    assert recently_answered(
        answer(AMBIGUOUS, recent), CHANNEL_INDEXER_CHAPTER, now=now
    )
    assert not recently_answered(
        answer(NOT_OFFERED, old), CHANNEL_INDEXER_CHAPTER, now=now
    )
    assert not recently_answered(
        answer(ERROR, recent), CHANNEL_INDEXER_CHAPTER, now=now
    )
    assert not recently_answered(
        answer(NOT_OFFERED, recent), CHANNEL_INDEXER_BOOK, now=now
    )
    assert not recently_answered([], CHANNEL_INDEXER_CHAPTER, now=now)


def test_a_timeout_never_erases_what_a_channel_already_answered(tmp_path):
    # Measured live: one slow indexer night turned a week of "nobody has
    # it" verdicts back into "error", and the slots stopped being exhausted.
    from tankarr.database import Database

    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    database.upsert_manga(
        {
            "id": "series",
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
    database.record_wanted_attempt(
        "series",
        "chapter:5",
        channel=CHANNEL_INDEXER_CHAPTER,
        outcome=NOT_OFFERED,
        detail="no indexer has it",
    )
    database.record_wanted_attempt(
        "series",
        "chapter:5",
        channel=CHANNEL_INDEXER_CHAPTER,
        outcome=ERROR,
        detail="timeout",
    )
    (item,) = database.wanted_attempts("series")["chapter:5"]
    assert item["outcome"] == NOT_OFFERED
    assert item["detail"] == "no indexer has it"
    assert item["attempts"] == 2

    # A real answer still replaces the old one either way.
    database.record_wanted_attempt(
        "series",
        "chapter:5",
        channel=CHANNEL_INDEXER_CHAPTER,
        outcome=AMBIGUOUS,
        detail="3 hits",
    )
    (item,) = database.wanted_attempts("series")["chapter:5"]
    assert item["outcome"] == AMBIGUOUS


def test_a_chapter_of_the_work_is_taken_when_it_is_unmistakable():
    chosen, reason = pick_chapter_release(
        [
            result("Galaxy Express 999 c082 (2019) (Digital)"),
            result("Some Other Manga c082"),
        ],
        title="Galaxy Express 999",
        chapter="82",
    )

    assert chosen is not None
    assert chosen["title"].startswith("Galaxy Express 999")
    assert reason == ""


def test_a_different_chapter_is_never_taken_for_this_slot():
    chosen, reason = pick_chapter_release(
        [result("Galaxy Express 999 c083")],
        title="Galaxy Express 999",
        chapter="82",
    )

    assert chosen is None
    assert reason == "no indexer result is this chapter of this work"


def test_a_title_that_only_contains_the_work_is_not_the_work():
    # The guard that would have refused another comic called "Garden".
    chosen, _reason = pick_chapter_release(
        [result("Secret Garden Chronicles c001")],
        title="Garden",
        chapter="1",
    )

    assert chosen is None


def test_a_single_word_title_needs_the_publisher_to_corroborate_it():
    results = [result("Garden c001 (2019) (Digital)")]

    chosen, reason = pick_chapter_release(
        results, title="Garden", chapter="1", publisher=["Breakdown Press"]
    )

    assert chosen is None
    assert "too loosely" in reason
    assert results[0]["_review"] == "single-word title without publisher corroboration"


def test_the_publisher_in_the_name_settles_a_single_word_title():
    chosen, _reason = pick_chapter_release(
        [result("Garden c001 (Breakdown Press) (Digital)")],
        title="Garden",
        chapter="1",
        publisher=["Breakdown Press"],
    )

    assert chosen is not None


def test_a_dead_torrent_is_not_a_candidate():
    chosen, _reason = pick_chapter_release(
        [result("Galaxy Express 999 c082", seeders=0)],
        title="Galaxy Express 999",
        chapter="82",
    )

    assert chosen is None


def test_a_usenet_release_needs_no_seeders():
    chosen, _reason = pick_chapter_release(
        [result("Galaxy Express 999 c082", seeders=0, protocol="usenet")],
        title="Galaxy Express 999",
        chapter="82",
    )

    assert chosen is not None


def test_a_book_is_not_a_chapter_candidate():
    chosen, _reason = pick_chapter_release(
        [result("Galaxy Express 999 v08 (Digital) (c2c)", size_bytes=300 * 1024**2)],
        title="Galaxy Express 999",
        chapter="82",
    )

    assert chosen is None


def test_the_catalogue_map_names_the_book_that_holds_a_chapter():
    entries = [
        MapEntry(
            volumes=("7",),
            chapters=tuple(str(n) for n in range(80, 91)),
            exact=True,
        ),
        MapEntry(
            volumes=("8",),
            chapters=tuple(str(n) for n in range(91, 101)),
            exact=True,
        ),
        # A span across two books is not evidence of which one holds a chapter.
        MapEntry(
            volumes=("9", "10"),
            chapters=tuple(str(n) for n in range(101, 121)),
            exact=False,
        ),
    ]

    assert containing_volumes("82", entries) == [7]
    assert containing_volumes("95", entries) == [8]
    assert containing_volumes("110", entries) == []
    assert containing_volumes("500", entries) == []
    assert containing_volumes("82", None) == []


def test_a_slot_nobody_has_searched_is_not_reported_as_unobtainable():
    assert slot_verdict([])["verdict"] == UNSEARCHED


def test_a_slot_is_recovering_as_soon_as_any_channel_acted():
    assert (
        slot_verdict(
            [
                attempt(CHANNEL_SOURCES, NOT_OFFERED),
                attempt(CHANNEL_INDEXER_CHAPTER, GRABBED, "Series c082"),
            ]
        )["verdict"]
        == RECOVERING
    )
    assert slot_verdict([attempt(CHANNEL_SOURCES, QUEUED)])["verdict"] == RECOVERING


def test_every_channel_asked_and_empty_reads_as_exhausted():
    verdict = slot_verdict(
        [
            attempt(CHANNEL_SOURCES, NOT_OFFERED),
            attempt(CHANNEL_INDEXER_CHAPTER, NOT_OFFERED),
            attempt(CHANNEL_INDEXER_BOOK, NOT_OFFERED),
        ]
    )

    assert verdict["verdict"] == EXHAUSTED
    assert verdict["summary"] == "no source and no indexer carries this release"
    assert len(verdict["channels"]) == 3


def test_an_unavailable_channel_never_lets_a_slot_read_as_exhausted():
    verdict = slot_verdict(
        [
            attempt(CHANNEL_SOURCES, NOT_OFFERED),
            attempt(CHANNEL_INDEXER_CHAPTER, UNAVAILABLE, "Prowlarr is not configured"),
            attempt(CHANNEL_INDEXER_BOOK, UNAVAILABLE, "Prowlarr is not configured"),
        ]
    )

    assert verdict["verdict"] == UNSEARCHED
    assert "unavailable" in verdict["summary"]


def test_something_the_indexers_offer_but_cannot_be_taken_asks_for_a_human():
    verdict = slot_verdict(
        [
            attempt(CHANNEL_SOURCES, NOT_OFFERED),
            attempt(CHANNEL_INDEXER_CHAPTER, AMBIGUOUS, "names the work too loosely"),
        ]
    )

    assert verdict["verdict"] == NEEDS_REVIEW


def test_one_empty_channel_is_never_enough_to_call_a_slot_unobtainable():
    # The sources rung answers "not offered" for every slot no extension
    # lists, which is exactly the population the indexers exist to cover.
    verdict = slot_verdict([attempt(CHANNEL_SOURCES, NOT_OFFERED)])

    assert verdict["verdict"] == UNSEARCHED
    assert verdict["summary"] == "not searched on every channel yet"


def test_a_book_slot_reaches_a_verdict_from_the_book_hunt():
    verdict = slot_verdict(
        [
            attempt(CHANNEL_SOURCES, NOT_OFFERED),
            attempt(CHANNEL_INDEXER_BOOK, NOT_OFFERED, "no indexer offers book 1"),
        ]
    )

    assert verdict["verdict"] == EXHAUSTED


def test_the_wanted_fingerprint_notices_a_recovery_answer(tmp_path):
    # A recovery pass changes what Wanted says without touching a release, so
    # the cached payload has to be invalidated by the ledger itself.
    from tankarr.database import Database

    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    database.upsert_manga(
        {
            "id": "series",
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
    before = database.wanted_revision()

    database.record_wanted_attempt(
        "series", "chapter:5", channel=CHANNEL_SOURCES, outcome=NOT_OFFERED
    )

    assert database.wanted_revision() != before
