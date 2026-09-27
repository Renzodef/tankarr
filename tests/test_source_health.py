"""The perennial source ranking: failures sink a source, speed lifts it,
and a chronic failer stays usable when it is the only carrier."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from tankarr import source_health
from tankarr.source_ranking import SourceRanking

NOW = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)


def outcomes(sequence: str, *, bps: float = 0.0, now: datetime = NOW) -> dict:
    """Fold a string of outcomes ('s'/'f'/'t') into one health record."""

    row: dict | None = None
    for i, char in enumerate(sequence):
        row = source_health.update(
            row,
            ok=char == "s",
            transient=char == "t",
            bytes_downloaded=int(bps * 10) if char == "s" else 0,
            seconds=10.0 if char == "s" else 0.0,
            now=now + timedelta(minutes=i),
        )
    assert row is not None
    return dict(row)


def test_failures_sink_the_score_and_successes_recover_it() -> None:
    healthy = outcomes("ssssss")
    failing = outcomes("sfffff")
    assert source_health.score(healthy, now=NOW) > source_health.score(failing, now=NOW)
    recovered = outcomes("sfffffssssssss")
    assert source_health.score(recovered, now=NOW) > source_health.score(
        failing, now=NOW
    )


def test_transient_failures_count_less_than_hard_ones() -> None:
    wobbly = outcomes("sttt")
    broken = outcomes("sfff")
    assert source_health.score(wobbly, now=NOW) > source_health.score(broken, now=NOW)


def test_speed_orders_two_equally_reliable_sources() -> None:
    fast = outcomes("ssssss", bps=4 * 1024 * 1024)
    slow = outcomes("ssssss", bps=30 * 1024)
    assert source_health.score(fast, now=NOW) > source_health.score(slow, now=NOW)


def test_stale_records_slide_back_towards_neutral() -> None:
    failing = outcomes("ffffff")
    fresh = source_health.score(failing, now=NOW)
    later = source_health.score(failing, now=NOW + timedelta(days=120))
    assert fresh < later < source_health.NEUTRAL_SUCCESS + 0.2


def test_unhealthy_needs_enough_attempts_to_judge() -> None:
    assert not source_health.is_unhealthy(outcomes("ff"), now=NOW)
    assert source_health.is_unhealthy(outcomes("ffffffff"), now=NOW)
    assert not source_health.is_unhealthy(outcomes("ssssssss"), now=NOW)


def release(rid: str, source_id: str) -> dict:
    return {
        "id": rid,
        "provider": "suwayomi",
        "source_key": f"suwayomi:{source_id}",
        "source_name": f"Source {source_id}",
        "publish_at": (NOW - timedelta(days=400)).isoformat(),
        "pages": 20,
        "version": 1,
        "volume": None,
        "chapter": "1",
        "release_unit": "chapter",
    }


def test_standings_order_sources_inside_the_same_class() -> None:
    ranking = SourceRanking()
    good = {"suwayomi:good": {"source_key": "suwayomi:good", **outcomes("ssssss")}}
    bad = {"suwayomi:bad": {"source_key": "suwayomi:bad", **outcomes("sfffff")}}
    health = source_health.components(
        [good["suwayomi:good"], bad["suwayomi:bad"]], now=NOW
    )
    chosen = ranking.best(
        [release("r-bad", "bad"), release("r-good", "good")],
        now=NOW,
        health=health,
    )
    assert chosen is not None and chosen["id"] == "r-good"
    # Without standings the tie breaks on identity instead.
    fallback = ranking.best(
        [release("r-bad", "bad"), release("r-good", "good")], now=NOW
    )
    assert fallback is not None and fallback["id"] == "r-bad"


def test_unknown_sources_sit_between_good_and_bad() -> None:
    ranking = SourceRanking()
    rows = [
        {"source_key": "suwayomi:good", **outcomes("ssssss")},
        {"source_key": "suwayomi:bad", **outcomes("sfffff")},
    ]
    health = source_health.components(rows, now=NOW)
    best = ranking.best(
        [release("r-bad", "bad"), release("r-new", "new"), release("r-good", "good")],
        now=NOW,
        health=health,
    )
    assert best is not None and best["id"] == "r-good"
    without_good = ranking.best(
        [release("r-bad", "bad"), release("r-new", "new")],
        now=NOW,
        health=health,
    )
    assert without_good is not None and without_good["id"] == "r-new"


def test_sole_carrier_is_still_chosen_even_when_globally_demoted() -> None:
    ranking = SourceRanking()
    rows = [{"source_key": "suwayomi:bad", **outcomes("ffffffff")}]
    health = source_health.components(rows, now=NOW)
    demoted = source_health.unhealthy_keys(rows, now=NOW)
    assert "suwayomi:bad" in demoted
    only = ranking.best(
        [release("r-bad", "bad")],
        now=NOW,
        demoted_sources=demoted,
        health=health,
    )
    assert only is not None and only["id"] == "r-bad"


def test_database_ledger_demotes_and_recovers(tmp_path) -> None:
    from tankarr.database import Database

    database = Database(tmp_path / "test.sqlite3")
    database.initialize()
    for _ in range(source_health.MIN_ATTEMPTS + 3):
        database.record_source_health("suwayomi:9", ok=False, reason="HTTP 500")
    assert "suwayomi:9" in database.global_demoted_sources()
    components = database.source_health_components()
    assert components["suwayomi:9"] > source_health.NEUTRAL_COMPONENT
    for _ in range(20):
        database.record_source_health(
            "suwayomi:9", ok=True, bytes_downloaded=10_000_000, seconds=5.0
        )
    assert "suwayomi:9" not in database.global_demoted_sources()
    assert (
        database.source_health_components()["suwayomi:9"]
        < source_health.NEUTRAL_COMPONENT
    )


def test_per_work_failures_also_feed_the_global_ledger(tmp_path) -> None:
    from tankarr.database import Database

    database = Database(tmp_path / "test.sqlite3")
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
    database.record_source_failure("m1", "suwayomi:7", reason="HTTP 500")
    rows = database.source_health_rows()
    assert rows and rows[0]["source_key"] == "suwayomi:7"
    assert rows[0]["failures"] == 1
