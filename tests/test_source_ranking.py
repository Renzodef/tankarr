from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tankarr.source_ranking import (
    SourceRanking,
    group_phase,
    normalize_source_list,
    release_phase,
    release_source_keys,
    source_class,
)

NOW = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)


def release(
    rid: str,
    provider: str,
    *,
    source_name: str | None = None,
    source_id: str | None = None,
    days_ago: int = 400,
    pages: int = 20,
    version: int = 1,
    volume: str | None = None,
    chapter: str | None = "1",
    unit: str = "chapter",
) -> dict:
    return {
        "id": rid,
        "provider": provider,
        "source_key": f"{provider}:{source_id}" if source_id else provider,
        "source_name": source_name,
        "publish_at": (NOW - timedelta(days=days_ago)).isoformat(),
        "pages": pages,
        "version": version,
        "volume": volume,
        "chapter": chapter,
        "release_unit": unit,
    }


def test_source_classes_come_from_names_keys_or_providers():
    assert (
        source_class(provider="suwayomi", source_name="MANGA Plus by SHUEISHA (EN)")
        == "official"
    )


def test_curated_and_origin_source_classes():
    assert (
        source_class(provider="suwayomi", source_name="Weeb Central (EN)") == "scan_hq"
    )
    assert source_class(provider="suwayomi", source_name="Asura Scans (EN)") == "origin"
    assert source_class(provider="suwayomi", source_name="MangaK (EN)") == "aggregator"
    assert source_class(provider="mangadex") == "scan_hq"
    assert source_class(provider="mangapill") == "aggregator"
    assert (
        source_class(provider="suwayomi", source_name="Totally New Site") == "unknown"
    )
    assert (
        source_class(provider="suwayomi", source_key="suwayomi:mangafire")
        == "aggregator"
    )


def test_phase_is_fresh_inside_the_window_only():
    assert release_phase((NOW - timedelta(days=3)).isoformat(), now=NOW) == "fresh"
    assert release_phase((NOW - timedelta(days=40)).isoformat(), now=NOW) == "backfill"
    assert release_phase(None, now=NOW) == "backfill"
    assert release_phase("garbage", now=NOW) == "backfill"
    assert (
        group_phase(
            [release("a", "x", days_ago=100), release("b", "x", days_ago=2)], now=NOW
        )
        == "fresh"
    )


def test_fresh_chapter_prefers_official_then_origin_then_scans():
    ranking = SourceRanking()
    candidates = [
        release(
            "agg",
            "suwayomi",
            source_name="MangaK (EN)",
            source_id="2",
            days_ago=1,
            pages=40,
            version=3,
        ),
        release(
            "wc", "suwayomi", source_name="Weeb Central (EN)", source_id="3", days_ago=1
        ),
        release(
            "plus",
            "suwayomi",
            source_name="MANGA Plus by SHUEISHA (EN)",
            source_id="4",
            days_ago=1,
            pages=18,
        ),
        release("dex", "mangadex", days_ago=1, pages=19),
    ]
    assert ranking.best(candidates, now=NOW)["id"] == "plus"
    # Without the official one, curated scans beat aggregators regardless of pages.
    assert ranking.best(candidates[:2] + candidates[3:], now=NOW)["id"] in {"wc", "dex"}


def test_backfill_ranking_prefers_curated_scans_with_all_sources_eligible():
    ranking = SourceRanking()
    candidates = [
        release(
            "plus", "suwayomi", source_name="MANGA Plus by SHUEISHA (EN)", source_id="4"
        ),
        release(
            "agg",
            "suwayomi",
            source_name="MangaFire (EN)",
            source_id="5",
            pages=45,
            version=2,
        ),
        release("wc", "suwayomi", source_name="Weeb Central (EN)", source_id="3"),
    ]
    assert ranking.best(candidates, now=NOW)["id"] == "wc"
    volume = release(
        "vol",
        "suwayomi",
        source_name="MangaFire (EN)",
        source_id="5",
        volume="1",
        chapter=None,
        unit="volume",
    )
    first_available = SourceRanking()
    assert first_available.best(candidates + [volume], now=NOW)["id"] == "vol"


def test_explicit_lists_override_classes_and_match_names_or_ids():
    ranking = SourceRanking.from_settings(
        fresh="suwayomi:weebcentral", backfill="suwayomi:5,mangadex"
    )
    fresh = [
        release(
            "plus",
            "suwayomi",
            source_name="MANGA Plus by SHUEISHA (EN)",
            source_id="4",
            days_ago=1,
        ),
        release(
            "wc", "suwayomi", source_name="Weeb Central (EN)", source_id="3", days_ago=1
        ),
    ]
    assert ranking.best(fresh, now=NOW)["id"] == "wc"
    backfill = [
        release("wc", "suwayomi", source_name="Weeb Central (EN)", source_id="3"),
        release("fire", "suwayomi", source_name="MangaFire (EN)", source_id="5"),
        release("dex", "mangadex"),
    ]
    assert ranking.best(backfill, now=NOW)["id"] == "fire"
    assert release_source_keys(backfill[0]) == (
        "suwayomi:3",
        "suwayomi:weebcentral",
        "suwayomi",
    )


def test_backfill_inherits_provider_priority_when_no_list_is_set():
    ranking = SourceRanking.from_settings(provider_priority=("mangapill", "mangadex"))
    candidates = [release("dex", "mangadex"), release("pill", "mangapill")]
    assert ranking.best(candidates, now=NOW)["id"] == "pill"


def test_selection_is_deterministic_for_identical_candidates():
    ranking = SourceRanking()
    a = release("a", "suwayomi", source_name="MangaFire (EN)", source_id="5")
    b = {**a, "id": "b"}
    assert ranking.best([b, a], now=NOW)["id"] == "a"
    assert ranking.best([a, b], now=NOW)["id"] == "a"


def test_normalize_source_list_validates_keys():
    assert (
        normalize_source_list(" Suwayomi:MangaPlus , mangadex,suwayomi:mangaplus ")
        == "suwayomi:mangaplus,mangadex"
    )
    with pytest.raises(ValueError):
        normalize_source_list("suwayomi:bad key")


def test_official_hosts_from_the_catalogue_decide_which_release_is_official():
    from tankarr.source_ranking import official_hosts

    links = [
        {
            "name": "MANGA Plus",
            "url": "https://mangaplus.shueisha.co.jp/titles/100020",
            "language": "en",
            "type": "webplatform",
        },
        {
            "name": "Shounen Jump",
            "url": "https://www.shonenjump.com/j/rensai/onepiece.html",
            "language": "ja",
            "type": "webplatform",
        },
        {
            "name": "VIZ Media",
            "url": "https://www.viz.com/one-piece",
            "language": "en",
            "type": "publisher",
        },
    ]
    hosts = official_hosts(links, language="en")
    assert hosts == frozenset({"mangaplus.shueisha.co.jp", "viz.com"})
    ranking = SourceRanking()
    plus = {
        **release(
            "plus",
            "suwayomi",
            source_name="MANGA Plus by SHUEISHA (EN)",
            source_id="4",
            days_ago=1,
        ),
        "source_url": "https://mangaplus.shueisha.co.jp/viewer/1019999",
    }
    # A "Webtoons" source is an official platform in general, but not for this work.
    webtoons = {
        **release(
            "wt", "suwayomi", source_name="Webtoons.com (EN)", source_id="9", days_ago=2
        ),
        "source_url": "https://www.webtoons.com/en/x/y",
    }
    scan = {
        **release(
            "wc", "suwayomi", source_name="Weeb Central (EN)", source_id="3", days_ago=1
        ),
        "source_url": "https://weebcentral.com/chapters/1",
    }
    assert (
        ranking.best([webtoons, scan, plus], now=NOW, official_hosts=hosts)["id"]
        == "plus"
    )
    assert ranking.best([webtoons, scan], now=NOW, official_hosts=hosts)["id"] == "wc"


def test_a_lone_catalogue_count_does_not_close_an_ended_works_frontier():
    # Hansel & Gretel: MangaBaka alone said "1 chapter"; nothing corroborated
    # it, and it closed the frontier of a 22-chapter series.
    from tankarr.source_ranking import completed_chapter_limit

    metadata = {"chapter_count": 1, "count_confidence": {"chapter": "lone"}}

    assert completed_chapter_limit(metadata=metadata, status="ended") is None

    corroborated = {"chapter_count": 76, "count_confidence": {"chapter": "agreed"}}
    assert completed_chapter_limit(metadata=corroborated, status="ended") == 76
    # A record from before corroboration existed keeps its old standing.
    legacy = {"chapter_count": 55}
    assert completed_chapter_limit(metadata=legacy, status="ended") == 55


def test_series_source_preference_overrides_ranking_and_falls_back():
    ranking = SourceRanking()
    preferred = release(
        "preferred", "suwayomi", source_id="101", source_name="Fixture source"
    )
    other = release("other", "suwayomi", source_id="202", source_name="MangaDex")
    assert (
        ranking.best([preferred, other], now=NOW, preferred_source="suwayomi:101")["id"]
        == "preferred"
    )
    assert (
        ranking.best([other], now=NOW, preferred_source="suwayomi:101")["id"] == "other"
    )
    assert (
        ranking.best(
            [preferred, other],
            now=NOW,
            preferred_source="suwayomi:101",
            demoted_sources=frozenset({"suwayomi:101"}),
        )["id"]
        == "other"
    )
