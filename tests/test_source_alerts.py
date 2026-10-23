from datetime import UTC, datetime, timedelta

import pytest

from tankarr.source_alerts import challenged_source_names, source_outages

NOW = datetime(2026, 1, 4, tzinfo=UTC)
OLD = (NOW - timedelta(hours=25)).isoformat()
MANGA = {
    "id": "series-1",
    "title": "Fixture series",
    "provider": "catalogue",
    "preferred_language": "en",
}


class Source:
    def supports_language(self, language):
        return language == "en"


def mapping(identifier="one", **changes):
    return {
        "manga_id": MANGA["id"],
        "provider": "alternate",
        "provider_manga_id": identifier,
        "enabled": True,
        "language": "en",
        "last_error": "No chapters found",
        "error_since": OLD,
        **changes,
    }


@pytest.mark.parametrize(
    "other",
    [
        {"last_error": None},
        {"error_since": None},
        {"error_since": "invalid"},
        {"error_since": (NOW - timedelta(hours=23)).isoformat()},
        {"error_since": (NOW - timedelta(hours=24)).isoformat()},
        {"error_since": (NOW + timedelta(hours=1)).isoformat()},
    ],
)
def test_one_healthy_untried_or_recently_failing_source_prevents_alert(other):
    assert (
        source_outages(
            [MANGA],
            [mapping(), mapping("two", **other)],
            {"alternate": Source()},
            now=NOW,
        )
        == []
    )


@pytest.mark.parametrize(
    "error", ["No chapters found", "HTTP error 522", "Cloudflare challenge", "KeyError"]
)
def test_all_sources_failing_over_24_hours_counts_series_once_regardless_of_error(
    error,
):
    assert source_outages(
        [MANGA],
        [mapping(last_error=error), mapping("two", last_error=error)],
        {"alternate": Source()},
        now=NOW,
    ) == [MANGA]


@pytest.mark.parametrize(
    "ignored",
    [
        {"enabled": False},
        {"provider": "disabled"},
        {"language": "it"},
        {"language": "unknown"},
    ],
)
def test_disabled_and_incompatible_mappings_do_not_count_as_working_sources(ignored):
    assert source_outages(
        [MANGA],
        [mapping(), mapping("two", last_error=None, **ignored)],
        {"alternate": Source()},
        now=NOW,
    ) == [MANGA]


def test_no_sources_or_provider_incompatible_with_series_does_not_raise_outage():
    assert source_outages([MANGA], [], {"alternate": Source()}, now=NOW) == []
    assert (
        source_outages(
            [{**MANGA, "preferred_language": "it"}],
            [mapping(language="it")],
            {"alternate": Source()},
            now=NOW,
        )
        == []
    )


def test_remote_primary_must_also_have_failed_for_over_24_hours():
    manga = {**MANGA, "provider": "primary"}
    providers = {"primary": Source(), "alternate": Source()}
    assert source_outages([manga], [mapping()], providers, now=NOW) == []
    manga.update(primary_source_error="Timed out", primary_source_error_since=OLD)
    assert source_outages([manga], [mapping()], providers, now=NOW) == [manga]
    assert source_outages([manga], [], providers, now=NOW) == [manga]


def test_legacy_duplicate_primary_is_not_counted_twice_but_distinct_mapping_is():
    manga = {
        **MANGA,
        "provider": "alternate",
        "primary_source_error": "Timed out",
        "primary_source_error_since": OLD,
    }
    providers = {"alternate": Source()}
    duplicate = mapping(MANGA["id"], last_error=None)
    assert source_outages([manga], [duplicate], providers, now=NOW) == [manga]
    assert (
        source_outages(
            [manga],
            [duplicate, mapping("different", last_error=None)],
            providers,
            now=NOW,
        )
        == []
    )


def _challenge_row(
    name: str,
    *,
    checked: datetime,
    error: str | None,
    enabled: object = 1,
    identifier: str | None = None,
) -> dict:
    return {
        "source_name": name,
        "enabled": enabled,
        "last_error": error,
        "last_checked_at": checked.isoformat(),
        "error_since": (checked - timedelta(days=30)).isoformat() if error else None,
        "provider_manga_id": identifier or name,
    }


def test_probe_names_are_reported_even_when_no_mapping_failed():
    assert challenged_source_names(
        [_challenge_row("Manga Ball", checked=NOW, error=None)],
        probes=["Asura Scans", ""],
        now=NOW,
    ) == ["Asura Scans"]


def test_disabled_and_month_old_challenge_errors_do_not_raise_the_notice():
    stale = NOW - timedelta(days=34)
    recent = NOW - timedelta(days=1)
    rows = [
        _challenge_row(
            "Manga Ball",
            checked=stale,
            error="Cloudflare bypass currently disabled",
            enabled=0,
        ),
        _challenge_row(
            "Manga Ball",
            checked=stale,
            error="Cloudflare bypass currently disabled",
            enabled=False,
            identifier="second",
        ),
    ]
    rows.extend(
        _challenge_row("Manga Ball", checked=recent, error=None, identifier=f"ok-{i}")
        for i in range(8)
    )
    assert challenged_source_names(rows, now=NOW) == []


def test_two_stale_challenge_rows_do_not_outvote_recent_successes():
    stale = NOW - timedelta(days=34)
    recent = NOW - timedelta(days=2)
    rows = [
        _challenge_row(
            "Manga Ball",
            checked=stale,
            error="HTTP 403 from cloudflare",
            identifier="old-a",
        ),
        _challenge_row(
            "Manga Ball",
            checked=stale,
            error="HTTP 503",
            identifier="old-b",
        ),
    ]
    rows.extend(
        _challenge_row("Manga Ball", checked=recent, error=None, identifier=f"ok-{i}")
        for i in range(20)
    )
    assert challenged_source_names(rows, now=NOW) == []


def test_a_challenge_superseded_by_a_later_success_is_not_current():
    rows = [
        _challenge_row(
            "Manga Ball",
            checked=NOW - timedelta(days=3),
            error="Cloudflare challenge",
        ),
        _challenge_row(
            "Manga Ball",
            checked=NOW - timedelta(days=1),
            error=None,
            identifier="later",
        ),
    ]
    assert challenged_source_names(rows, now=NOW) == []


def test_a_source_failing_most_recent_checks_is_reported():
    rows = [
        _challenge_row(
            "Manga Ball",
            checked=NOW - timedelta(days=1),
            error="Cloudflare challenge",
            identifier="a",
        ),
        _challenge_row(
            "Manga Ball",
            checked=NOW - timedelta(hours=12),
            error="Just a moment",
            identifier="b",
        ),
        _challenge_row(
            "Manga Ball",
            checked=NOW - timedelta(days=4),
            error=None,
            identifier="c",
        ),
    ]
    assert challenged_source_names(rows, probes=["Other"], now=NOW) == [
        "Manga Ball",
        "Other",
    ]


def test_one_current_challenge_with_no_other_checks_is_reported():
    assert challenged_source_names(
        [
            _challenge_row(
                "Manga Ball",
                checked=NOW - timedelta(days=2),
                error="ddos-guard",
            )
        ],
        now=NOW,
    ) == ["Manga Ball"]


def test_a_recent_streak_still_counts_when_it_started_over_a_month_ago():
    assert challenged_source_names(
        [
            {
                "source_name": "Manga Ball",
                "enabled": 1,
                "last_error": "Cloudflare bypass currently disabled",
                "last_checked_at": (NOW - timedelta(hours=6)).isoformat(),
                "error_since": (NOW - timedelta(days=40)).isoformat(),
            }
        ],
        now=NOW,
    ) == ["Manga Ball"]
