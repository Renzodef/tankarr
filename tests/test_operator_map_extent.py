from __future__ import annotations

from dataclasses import replace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tankarr.chapter_map import MapEntry, entries_from_boundaries
from tankarr.operator_map import register_chapter_map_routes
from tests.test_deletion import chapter, make_service, manga

URL = "/api/manga/manga-1/chapter-map"


@pytest.fixture
def context(tmp_path):
    database, service, _reader = make_service(tmp_path)
    database.upsert_manga(manga(), "en", "all")
    database.update_manga("manga-1", {"series_unit_override": "chapters"})
    app = FastAPI()
    register_chapter_map_routes(app, database, service)
    with TestClient(app) as client:
        yield database, service, client


def releases(context, numbers, *, downloaded=False):
    database, service, _client = context
    rows = [
        chapter(f"chapter-{number}", str(number), volume=None) for number in numbers
    ]
    database.upsert_chapters("manga-1", rows)
    if downloaded:
        with database.connect() as connection:
            connection.executemany(
                "UPDATE chapter_release SET downloaded=1, library_path=? WHERE id=?",
                [
                    (str(service.settings.library_dir / f"{row['id']}.cbz"), row["id"])
                    for row in rows
                ],
            )


def metadata(context, values):
    context[0].save_series_metadata(
        "manga-1",
        values,
        artwork_path=None,
        artwork_sha256=None,
        artwork_media_type=None,
        source_status=[],
    )


def expected(context, count, unit="chapter"):
    context[0].update_manga(
        "manga-1",
        {
            "expected_count_override": count,
            "expected_count_unit_override": unit,
        },
    )


def saved(context, boundaries, end):
    entries = [
        replace(entry, release_date="2024-03-01")
        for entry in entries_from_boundaries(boundaries, last_chapter=end)
    ]
    context[0].replace_chapter_map("manga-1", "operator", entries)
    return operator_entries(context)


def operator_entries(context):
    return [
        entry
        for entry in context[0].chapter_map("manga-1", raw=True)
        if entry.source == "operator"
    ]


def persisted(context):
    with context[0].connect() as connection:
        return tuple(connection.iterdump())


def preview(context, body):
    response = context[2].put(URL, params={"dry_run": True}, json=body)
    assert response.status_code == 200, response.text
    return response.json()


def confirm(context, body, reviewed):
    response = context[2].put(
        URL,
        params={"confirmation_snapshot": reviewed["confirmation_snapshot"]},
        json=body,
    )
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize(
    "source, last",
    [
        ("canonical", "189"),
        ("override", "213"),
        ("catalogue_last", "302"),
        ("catalogue_count", "302"),
        ("observed_beyond_catalogue", "303"),
    ],
)
def test_last_known_is_maximum_of_chapter_release_override_and_catalogue(
    context, source, last
):
    releases(context, [189])
    if source != "canonical":
        expected(context, 213)
    if source in {"catalogue_last", "observed_beyond_catalogue"}:
        metadata(context, {"last_chapter": "302", "chapter_count": 301})
    elif source == "catalogue_count":
        metadata(context, {"last_chapter": "299", "chapter_count": 302})
    if source == "observed_beyond_catalogue":
        releases(context, [303])
    before = persisted(context)

    current = context[2].get(URL).json()
    body = {"boundaries": [{"volume": "44", "first_chapter": last}]}
    reviewed = preview(context, body)

    assert current["last_known_chapter"] == reviewed["last_known_chapter"] == last
    assert reviewed["intervals"][0]["chapters"] == [last]
    assert persisted(context) == before
    applied = confirm(context, body, reviewed)
    assert applied["last_known_chapter"] == last
    assert operator_entries(context)[0].chapters == (last,)


def test_book_count_override_does_not_extend_known_chapter_numbers(context):
    releases(context, [189])
    expected(context, 213, unit="volume")
    assert context[2].get(URL).json()["last_known_chapter"] == "189"
    response = context[2].put(
        URL,
        params={"dry_run": True},
        json={
            "boundaries": [{"volume": "44", "first_chapter": "190"}],
        },
    )
    assert response.status_code == 422
    assert operator_entries(context) == []


def test_partial_prepend_preserves_every_omitted_saved_interval_beyond_current_extent(
    context,
):
    releases(context, [174])
    rows = [
        {"volume": str(volume), "first_chapter": str(135 + (volume - 13) * 10)}
        for volume in range(13, 30)
    ]
    original = saved(context, rows, "302")
    catalogue = MapEntry(("8", "9"), ("150", "174"), False, source="catalogue")
    context[0].replace_chapter_map("manga-1", "catalogue", [catalogue])
    before = persisted(context)
    body = {
        "boundaries": [
            {"volume": "11", "first_chapter": "115"},
            {"volume": "12", "first_chapter": "125"},
        ]
    }  # An omitted mode must merge, preserving all existing later books.

    assert context[2].get(URL).json()["last_known_chapter"] == "174"
    reviewed = preview(context, body)
    assert persisted(context) == before
    assert reviewed["last_known_chapter"] == "174"
    assert reviewed["intervals"][0]["chapters"] == [
        str(number) for number in range(115, 125)
    ]
    assert reviewed["intervals"][1]["chapters"] == [
        str(number) for number in range(125, 135)
    ]
    assert reviewed["intervals"][-1]["last_chapter"] == "302"

    confirm(context, body, reviewed)

    stored = operator_entries(context)
    assert [entry.volumes for entry in stored] == [
        (str(volume),) for volume in range(11, 30)
    ]
    assert stored[2:] == original
    assert [
        entry
        for entry in context[0].chapter_map("manga-1", raw=True)
        if entry.source == "catalogue"
    ] == [catalogue]
    assert context[2].get(URL).json()["last_known_chapter"] == "174"
    before_invalid = persisted(context)
    response = context[2].put(
        URL,
        params={"dry_run": True},
        json={
            "boundaries": [{"volume": "30", "first_chapter": "303"}],
        },
    )
    assert response.status_code == 422
    assert persisted(context) == before_invalid


def test_new_boundary_cannot_use_saved_future_extent_as_current_evidence(context):
    releases(context, [174])
    saved(context, [{"volume": "29", "first_chapter": "295"}], "302")
    before = persisted(context)
    response = context[2].put(
        URL,
        params={"dry_run": True},
        json={
            "boundaries": [{"volume": "28", "first_chapter": "175"}],
        },
    )
    assert response.status_code == 422
    assert persisted(context) == before


def test_replace_removes_omitted_operator_rows_and_keeps_other_sources(context):
    releases(context, [20])
    saved(
        context,
        [
            {"volume": "1", "first_chapter": "1"},
            {"volume": "2", "first_chapter": "5"},
            {"volume": "3", "first_chapter": "11"},
        ],
        "20",
    )
    catalogue = MapEntry(("5",), ("1", "2"), True, source="catalogue")
    context[0].replace_chapter_map("manga-1", "catalogue", [catalogue])
    body = {"mode": "replace", "boundaries": [{"volume": "2", "first_chapter": "5"}]}
    before = persisted(context)
    reviewed = preview(context, body)
    assert persisted(context) == before
    assert [row["volume"] for row in reviewed["boundaries"]] == ["2"]
    assert reviewed["intervals"][0]["last_chapter"] == "20"
    confirm(context, body, reviewed)
    assert [entry.volumes for entry in operator_entries(context)] == [("2",)]
    assert [
        entry
        for entry in context[0].chapter_map("manga-1", raw=True)
        if entry.source == "catalogue"
    ] == [catalogue]


@pytest.mark.parametrize(
    "provider, overlap", [("nyaa", False), ("manual", False), ("manual", True)]
)
def test_book_only_ending_extends_map_to_explicit_chapter_end_without_missing_slots(
    context,
    provider,
    overlap,
):
    releases(context, range(1, 191), downloaded=True)
    if overlap:
        releases(context, [191], downloaded=True)
    expected(context, 213)
    metadata(context, {"last_chapter": "219", "chapter_count": 219})
    original = saved(
        context,
        [
            {"volume": "39", "first_chapter": "179"},
            {"volume": "40", "first_chapter": "185"},
        ],
        "190",
    )
    database, service, _client = context
    for volume in range(41, 45):
        release = {
            **chapter(f"book-{volume}", None, volume=str(volume)),
            "provider": "nyaa",
            "release_unit": "volume",
            "pages": 180,
        }
        database.publish_external_chapter(
            "manga-1",
            release,
            service.settings.library_dir / f"book-{volume}.cbz",
            "a" * 64,
            "b" * 64,
        )
    if provider == "manual":
        # Keep the real persisted manual-book metadata without importing files.
        with database.connect() as connection:
            connection.execute(
                "UPDATE chapter_release SET provider='manual' WHERE id LIKE 'book-%'"
            )
        assert set(service.suspect_covering_volumes("manga-1")) == {
            "41",
            "42",
            "43",
            "44",
        }
    else:
        assert service.suspect_covering_volumes("manga-1") == {}
    body = {
        "last_chapter": "213",
        "boundaries": [
            {"volume": "41", "first_chapter": "191"},
            {"volume": "42", "first_chapter": "197"},
            {"volume": "43", "first_chapter": "201"},
            {"volume": "44", "first_chapter": "207"},
        ],
    }
    before = persisted(context)
    reviewed = preview(context, body)
    assert persisted(context) == before
    assert reviewed["last_known_chapter"] == "219"
    assert reviewed["intervals"][-1]["chapters"] == [
        str(number) for number in range(207, 214)
    ]
    applied = confirm(context, body, reviewed)
    assert operator_entries(context)[:2] == original
    for response in (reviewed, applied):
        index = response["chapter_index"]
        assert index["unit"] == "chapter"
        assert index["mapped_missing_count"] == index["raw_missing_count"] == 0
        assert index["unresolved_expected_count"] == 0
        slots = {row["chapter"]: row for row in index["slots"]}
        assert set(slots) == {str(number) for number in range(1, 214)}
        assert all(slots[str(number)]["downloaded"] for number in range(1, 191))
        assert all(
            slots[str(number)]["covered_by_volume"] == "44"
            for number in range(207, 214)
        )
        if overlap:
            assert slots["191"]["downloaded"] is True
            assert slots["191"]["duplicate_of_volume"] is None
    if overlap:
        assert database.get_chapter("chapter-191")["downloaded"] is True
        assert service.duplicate_chapter_files("manga-1") == []
    stored = operator_entries(context)
    full_map = {"mode": "replace", "boundaries": applied["boundaries"]}
    resaved = preview(context, full_map)
    assert resaved["intervals"][-1]["last_chapter"] == "213"
    confirm(context, full_map, resaved)
    assert operator_entries(context) == stored
    before_invalid = persisted(context)
    response = context[2].put(
        URL, params={"dry_run": True}, json={**full_map, "last_chapter": "220"}
    )
    assert response.status_code == 422
    assert persisted(context) == before_invalid
    metadata(context, {"chapter_count": 220})
    assert context[2].get(URL).json()["last_known_chapter"] == "220"
    assert operator_entries(context) == stored
    assert stored[-1].chapters[-1] == "213"


def test_replace_does_not_extend_retained_book_into_removed_future_tail(context):
    releases(context, [174])
    saved(
        context,
        [
            {"volume": "13", "first_chapter": "135"},
            {"volume": "14", "first_chapter": "145"},
            {"volume": "29", "first_chapter": "295"},
        ],
        "302",
    )
    body = {
        "mode": "replace",
        "boundaries": [{"volume": "13", "first_chapter": "135"}],
    }
    reviewed = preview(context, body)
    assert len(reviewed["intervals"]) == 1
    assert reviewed["intervals"][0]["last_chapter"] == "174"
    confirm(context, body, reviewed)
    assert operator_entries(context)[0].chapters[-1] == "174"


def test_explicit_final_chapter_can_shorten_saved_book_and_is_confirmation_bound(
    context,
):
    releases(context, [189])
    expected(context, 213)
    metadata(context, {"chapter_count": 219})
    saved(context, [{"volume": "44", "first_chapter": "207"}], "219")
    body = {
        "last_chapter": "213",
        "boundaries": [{"volume": "44", "first_chapter": "207"}],
    }
    reviewed = preview(context, body)
    assert reviewed["last_known_chapter"] == "219"
    assert reviewed["intervals"][-1]["last_chapter"] == "213"
    before = persisted(context)
    response = context[2].put(
        URL,
        params={"confirmation_snapshot": reviewed["confirmation_snapshot"]},
        json={**body, "last_chapter": "214"},
    )
    assert response.status_code == 409
    assert persisted(context) == before
    confirm(context, body, reviewed)
    assert operator_entries(context)[-1].chapters[-1] == "213"

    # Moving only the final book's beginning must not expand the saved edition
    # ending back to the longer catalogue's total.
    partial = {"boundaries": [{"volume": "44", "first_chapter": "208"}]}
    reviewed = preview(context, partial)
    assert reviewed["intervals"][-1]["last_chapter"] == "213"
    confirm(context, partial, reviewed)
    assert operator_entries(context)[-1].chapters == tuple(
        str(number) for number in range(208, 214)
    )


def test_confirmation_binds_mode_even_when_merge_and_replace_propose_identical_rows(
    context,
):
    releases(context, [20])
    body = {"boundaries": [{"volume": "1", "first_chapter": "1"}]}
    reviewed = preview(context, body)
    replacing = {**body, "mode": "replace"}
    replaced_preview = preview(context, replacing)
    assert reviewed["intervals"] == replaced_preview["intervals"]
    assert (
        reviewed["confirmation_snapshot"] != replaced_preview["confirmation_snapshot"]
    )
    before = persisted(context)
    response = context[2].put(
        URL,
        params={"confirmation_snapshot": reviewed["confirmation_snapshot"]},
        json=replacing,
    )
    assert response.status_code == 409
    assert persisted(context) == before


def test_equivalent_decimal_labels_and_explicit_default_mode_share_confirmation(
    context,
):
    releases(context, [20])
    reviewed = preview(
        context,
        {"mode": "merge", "boundaries": [{"volume": "1.0", "first_chapter": "1.00"}]},
    )
    applied = confirm(
        context, {"boundaries": [{"volume": 1, "first_chapter": 1}]}, reviewed
    )
    assert applied["boundaries"] == [{"volume": "1", "first_chapter": "1"}]


@pytest.mark.parametrize(
    "change", ["expected_total", "catalogue_total", "canonical_release"]
)
def test_changing_extent_invalidates_confirmation_without_touching_saved_map(
    context, change
):
    releases(context, [189])
    expected(context, 213)
    body = {"boundaries": [{"volume": "44", "first_chapter": "207"}]}
    reviewed = preview(context, body)
    if change == "expected_total":
        expected(context, 214)
    elif change == "catalogue_total":
        metadata(context, {"chapter_count": 302})
    else:
        releases(context, [303])
    before = persisted(context)
    response = context[2].put(
        URL,
        params={"confirmation_snapshot": reviewed["confirmation_snapshot"]},
        json=body,
    )
    assert response.status_code == 409
    assert persisted(context) == before
    assert operator_entries(context) == []


def test_a_wild_outlier_release_does_not_stretch_the_map(context):
    # Adekan ends at chapter 73 and one source listed a "Chapter 487": the map
    # used to run to 487, so the last book demanded every chapter up to it.
    # A chapter just past a stale catalogue still counts (covered above by
    # observed_beyond_catalogue); only a wild outlier is ignored.
    releases(context, range(1, 74))
    releases(context, [487])
    metadata(context, {"status": "ended", "chapter_count": 73, "last_chapter": "73"})

    assert context[2].get(URL).json()["last_known_chapter"] == "73"

    reviewed = preview(
        context, {"boundaries": [{"volume": "20", "first_chapter": "73"}]}
    )
    assert reviewed["intervals"][0]["chapters"] == ["73"]


def test_an_outlier_is_dropped_even_without_a_settled_end(context):
    # Adekan is still running, so the chapter index has no sequence end to
    # measure against and the guard above stands down. The numbering itself
    # is the reference: 1-73 then a lone "Chapter 487".
    releases(context, range(1, 74))
    releases(context, [487])
    metadata(context, {"status": "ongoing"})

    assert context[2].get(URL).json()["last_known_chapter"] == "73"


def test_a_sparse_map_keeps_its_only_release_as_the_extent(context):
    # One release and nothing else: there is no run, so nothing is an outlier.
    releases(context, [174])
    metadata(context, {"status": "ongoing"})

    assert context[2].get(URL).json()["last_known_chapter"] == "174"


def test_running_map_uses_available_numbering_not_catalogue_cardinality(context):
    releases(context, range(1, 66))
    metadata(context, {"status": "ongoing", "chapter_count": 70})

    assert context[2].get(URL).json()["last_known_chapter"] == "65"
