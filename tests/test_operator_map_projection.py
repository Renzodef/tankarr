from __future__ import annotations

import pytest

from tankarr.operator_map import OperatorChapterMap, StaleChapterMap
from tests.test_deletion import chapter, make_service, manga

BOUNDARIES = [
    {"volume": "12", "first_chapter": "55.5"},
    {"volume": "13", "first_chapter": "60"},
]


@pytest.fixture
def pending_decimal(tmp_path):
    database, service, _reader = make_service(tmp_path)
    database.upsert_manga(manga(), "en", "all")
    database.update_manga("manga-1", {"series_unit_override": "volumes"})
    database.upsert_chapters(
        "manga-1",
        [
            {
                **chapter("extra", "55.5", volume="12"),
                "provider": "manual",
                "title": "Omake",
            },
            chapter("last", "60", volume="13"),
            {
                **chapter("book", "", volume="12"),
                "chapter": None,
                "release_unit": "volume",
            },
        ],
    )
    database.mark_chapter_downloaded("extra", tmp_path / "extra.cbz")
    database.mark_chapter_downloaded("book", tmp_path / "book.cbz")
    assert database.get_chapter("extra")["numbering_status"] == "unmapped"
    return database, service, OperatorChapterMap(database, service)


def persisted_state(database):
    with database.connect() as connection:
        return tuple(connection.iterdump())


def without_timestamps(value):
    # Actual reconciliation timestamps its updates at commit time. Everything
    # affecting slots, numbering, monitoring and coverage must match preview.
    if isinstance(value, dict):
        return {
            key: without_timestamps(item)
            for key, item in value.items()
            if key != "updated_at"
        }
    if isinstance(value, list):
        return [without_timestamps(item) for item in value]
    return value


def test_replace_projects_decimal_promotion_and_updated_suspect_coverage(
    pending_decimal,
):
    database, service, editor = pending_decimal
    assert "12" in service.suspect_covering_volumes("manga-1")
    before = persisted_state(database)
    revision = editor.read("manga-1")["revision"]

    preview = editor.preview("manga-1", BOUNDARIES)

    assert persisted_state(database) == before
    assert editor.read("manga-1")["revision"] == preview["revision"] == revision
    slots = {
        slot["chapter"]: slot
        for slot in preview["chapter_index"]["mapped_chapter_slots"]
    }
    assert slots["55.5"]["downloaded"] is True
    assert slots["55.5"]["releases"][0]["id"] == "extra"
    assert slots["55.5"]["releases"][0]["numbering_method"] == "canonical_decimal"
    assert slots["56"]["covered_by_volume"] == "12"

    applied = editor.commit("manga-1", BOUNDARIES, preview["confirmation_snapshot"])

    assert without_timestamps(preview["chapter_index"]) == without_timestamps(
        applied["chapter_index"]
    )
    assert database.get_chapter("extra")["chapter"] == "55.5"


def test_delete_projects_loss_of_operator_numbering_without_writing(pending_decimal):
    database, _service, editor = pending_decimal
    proposed = editor.preview("manga-1", BOUNDARIES)
    editor.commit("manga-1", BOUNDARIES, proposed["confirmation_snapshot"])
    assert database.get_chapter("extra")["numbering_status"] == "mapped"
    before = persisted_state(database)

    preview = editor.preview("manga-1", None)

    assert persisted_state(database) == before
    assert preview["intervals"] == []
    applied = editor.commit("manga-1", None, preview["confirmation_snapshot"])
    assert without_timestamps(preview["chapter_index"]) == without_timestamps(
        applied["chapter_index"]
    )
    assert database.get_chapter("extra")["numbering_status"] == "unmapped"
    assert database.get_chapter("extra")["chapter"] is None


def official_series(tmp_path, *, evidence_kind):
    database, service, _reader = make_service(tmp_path)
    database.upsert_manga({**manga(), "original_language": "ko"}, "en", "all")
    database.update_manga("manga-1", {"series_unit_override": "volumes"})
    database.save_series_metadata(
        "manga-1",
        {
            "official_links": [
                {"url": "https://webtoons.com/en/example", "language": "en"},
                {"url": "https://comic.naver.com/example", "language": "ko"},
            ]
        },
        artwork_path=None,
        artwork_sha256=None,
        artwork_media_type=None,
        source_status=[],
    )

    def release(source, number, language, host):
        return {
            **chapter(f"{source}-{number}", str(number), language=language),
            "source_key": source,
            "source_url": f"https://{host}/{number}",
        }

    releases = [
        release("primary", number, "en", "webtoons.com") for number in range(1, 6)
    ] + [
        release("mirror", number, "en", "mirror.example")
        for number in (1, 2, 3, 4, 5, 8)
    ]
    if evidence_kind == "releases":
        releases += [
            release("secondary", number, "ko", "comic.naver.com")
            for number in range(1, 9)
        ]
    database.upsert_chapters("manga-1", releases)
    if evidence_kind == "metadata":
        database.replace_official_edition_evidence(
            "manga-1",
            host="comic.naver.com",
            language="ko",
            items=[
                {"provider_index": str(number), "edition_chapter": str(number)}
                for number in range(1, 9)
            ],
        )
    assert database.get_chapter("mirror-8")["numbering_method"] == (
        "secondary_official_bridge"
    )
    return database, OperatorChapterMap(database, service)


@pytest.mark.parametrize("evidence_kind", ["releases", "metadata"])
def test_preview_uses_other_language_and_secondary_publisher_evidence(
    tmp_path, evidence_kind
):
    database, editor = official_series(tmp_path, evidence_kind=evidence_kind)
    boundaries = [{"volume": "1", "first_chapter": "1"}]
    before = persisted_state(database)

    preview = editor.preview("manga-1", boundaries)

    assert persisted_state(database) == before
    slot = next(
        slot
        for slot in preview["chapter_index"]["mapped_chapter_slots"]
        if slot["chapter"] == "8"
    )
    assert slot["releases"][0]["id"] == "mirror-8"
    assert slot["releases"][0]["numbering_method"] == "secondary_official_bridge"
    applied = editor.commit("manga-1", boundaries, preview["confirmation_snapshot"])
    assert without_timestamps(preview["chapter_index"]) == without_timestamps(
        applied["chapter_index"]
    )


@pytest.mark.parametrize("changed", ["foreign_release", "source_role", "evidence"])
def test_confirmation_binds_numbering_inputs_outside_managed_language(
    tmp_path, changed
):
    database, editor = official_series(
        tmp_path, evidence_kind="metadata" if changed == "evidence" else "releases"
    )
    boundaries = [{"volume": "1", "first_chapter": "1"}]
    preview = editor.preview("manga-1", boundaries)
    managed_before = database.list_chapters("manga-1", "en")
    with database.connect() as connection:
        if changed == "foreign_release":
            connection.execute("DELETE FROM chapter_release WHERE id='secondary-8'")
        elif changed == "source_role":
            connection.execute(
                "UPDATE manga_official_source SET source_role='alternative' "
                "WHERE host='comic.naver.com'"
            )
        else:
            connection.execute(
                "DELETE FROM official_edition_evidence WHERE provider_index='8'"
            )
    assert database.list_chapters("manga-1", "en") == managed_before
    before = persisted_state(database)

    with pytest.raises(StaleChapterMap):
        editor.commit("manga-1", boundaries, preview["confirmation_snapshot"])

    assert persisted_state(database) == before


def test_zero_canonical_label_does_not_fall_back_to_missing_compatibility(
    tmp_path, monkeypatch
):
    database, service, _reader = make_service(tmp_path)
    database.upsert_manga(manga(), "en", "all")
    database.upsert_chapters("manga-1", [chapter("zero", "0")])
    release = {**database.get_chapter("zero"), "canonical_chapter": 0, "chapter": None}
    monkeypatch.setattr(database, "list_chapters", lambda *_args: [release])

    assert OperatorChapterMap(database, service).read("manga-1")[
        "last_known_chapter"
    ] == ("0")
