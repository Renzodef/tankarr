from tankarr.official_upgrade import upgrade_candidates


def rel(chapter, host, downloaded=False, ident=None, **extra):
    return {
        "id": ident or f"{host}-{chapter}",
        "chapter": chapter,
        "volume": None,
        "downloaded": downloaded,
        "source_url": f"https://{host}/x/{chapter}",
        **extra,
    }


def test_official_release_replaces_a_scanlator_file_once_and_only_once():
    hosts = frozenset({"mangaplus.shueisha.co.jp"})
    chapters = [
        rel("1", "weebcentral.com", downloaded=True),
        rel("1", "mangaplus.shueisha.co.jp"),  # upgrade
        rel("2", "weebcentral.com", downloaded=True),
        rel(
            "2", "mangaplus.shueisha.co.jp", downloaded=True
        ),  # already official on disk
        rel("3", "weebcentral.com", downloaded=True),  # no official yet
        rel("4", "mangaplus.shueisha.co.jp"),  # nothing on disk: Wanted's job
        rel("5", "weebcentral.com", downloaded=True),
        rel("5", "mangaplus.shueisha.co.jp", queue_status="queued"),  # already queued
    ]
    assert [item["id"] for item in upgrade_candidates(chapters, hosts)] == [
        "mangaplus.shueisha.co.jp-1"
    ]
    assert upgrade_candidates(chapters, frozenset()) == []


def test_preferred_source_upgrades_do_not_churn_or_cross_language():
    from tankarr.official_upgrade import preferred_source_upgrades
    from tankarr.source_ranking import SourceRanking

    ranking = SourceRanking(backfill=("suwayomi:preferred", "suwayomi:old"))
    old = rel(
        "1",
        "old.test",
        True,
        provider="suwayomi",
        source_key="suwayomi:old",
        language="en",
    )
    new = rel(
        "1",
        "new.test",
        provider="suwayomi",
        source_key="suwayomi:preferred",
        language="en",
    )
    assert preferred_source_upgrades([old, new], ranking) == [new]
    assert preferred_source_upgrades([old, {**new, "language": "ja"}], ranking) == []
    assert (
        preferred_source_upgrades(
            [old, {**new, "numbering_status": "ambiguous"}], ranking
        )
        == []
    )
    assert preferred_source_upgrades([old, {**new, "blocked": True}], ranking) == []
    for unavailable in (
        {"title": "🔒 Chapter 1"},
        {"publish_at": "2999-01-01T00:00:00Z"},
        {"pages": 2},
        {"provider": "manual"},
    ):
        assert preferred_source_upgrades([old, {**new, **unavailable}], ranking) == []
    assert (
        preferred_source_upgrades(
            [old, {**new, "source_key": "suwayomi:old", "pages": 900}], ranking
        )
        == []
    )
    assert (
        preferred_source_upgrades(
            [{**old, "downloaded": False}, {**new, "downloaded": True}], ranking
        )
        == []
    )
