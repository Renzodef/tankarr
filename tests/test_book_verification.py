"""A person's proof of what an imported book holds (see book_verification)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tankarr.book_verification import (
    book_verification,
    verification_problem,
    verified_book,
)
from tankarr.chapter_map import MapEntry
from tankarr.chapter_mapping import (
    IMPORTED_BY_HAND,
    MACHINE_TRANSLATION,
    VERIFIED_BOOK,
    VERIFIED_TRANSLATION,
    build_chapter_index,
    suspect_volume_reasons,
)
from tankarr.verified_books import register_verified_book_routes
from tests.test_deletion import chapter, make_service, manga

DIGEST = "d" * 64
MAP = [MapEntry(("6",), ("30", "31"), True, "ocr")]
OFF_EDITION = "another edition or a wrong split"


def book(volume, pages, provider="prowlarr", digest=None):
    return {
        "id": f"{provider}-{volume}",
        "volume": str(volume),
        "chapter": None,
        "release_unit": "volume",
        "downloaded": True,
        "monitored": True,
        "pages": pages,
        "provider": provider,
        "language": "en",
        "local_import_sha256": digest,
        "verified_book": None,
    }


def proof_of(release, chapters, digest=None, **more):
    return {
        "version": 1,
        "release_id": release["id"],
        "volume": release["volume"],
        "content_sha256": digest or release["local_import_sha256"],
        "chapters": list(chapters),
        "source": "https://publisher.example/volume-6",
        "note": "Every page read against the publisher's table of contents",
        "verified_at": "2026-09-30T00:00:00+00:00",
        **more,
    }


def proven(release, chapters=("30", "31")):
    return {**release, "verified_book": proof_of(release, chapters)}


def edition(*extra):
    normal = ((1, 324), (2, 319), (3, 319), (4, 356), (5, 300))
    return [book(number, pages) for number, pages in normal] + list(extra)


def hand_built(pages=434, provider="manual"):
    return book(6, pages, provider, DIGEST)


def test_a_stored_proof_needs_a_digest_chapters_and_a_note():
    good = proof_of(hand_built(), ["30", "31"])
    assert book_verification(good)["chapters"] == ["30", "31"]
    assert book_verification(json.dumps(good)) == book_verification(good)
    assert book_verification({**good, "chapters": ["030", "31.50"]})["chapters"] == [
        "30",
        "31.5",
    ]
    for broken in (
        {"version": 2},
        {"content_sha256": "abc"},
        {"content_sha256": "D" * 64},
        {"chapters": []},
        {"chapters": ["30", "30"]},
        {"chapters": ["30", "thirty"]},
        {"note": "   "},
        {"release_id": ""},
        {"volume": "six"},
        {"source": "x" * 2000},
    ):
        assert book_verification({**good, **broken}) is None, broken
    assert book_verification(None) is None
    assert book_verification("not json") is None
    assert book_verification(["a list"]) is None


def test_a_proof_stops_applying_the_moment_the_book_or_the_map_changes():
    release = proven(hand_built())
    mapped = {"6": {"30", "31"}}
    assert verification_problem(release, mapped) is None
    assert verified_book(release)["volume"] == "6"
    for change, words in (
        ({"local_import_sha256": "e" * 64}, "content changed"),
        ({"local_import_sha256": None}, "content changed"),
        ({"downloaded": False}, "file is gone"),
        ({"provider": "assembled"}, "only an imported whole book"),
        ({"release_unit": "chapter", "chapter": "1"}, "only an imported whole book"),
        ({"id": "another"}, "another release"),
        ({"volume": "7"}, "volume changed"),
    ):
        broken = {**release, **change}
        assert words in verification_problem(broken, mapped), change
        assert verified_book(broken) is None, change
    assert "chapter map" in verification_problem(release, {"6": {"30"}})
    assert "chapter map" in verification_problem(release, {"6": {"30", "31", "32"}})
    assert "chapter map" in verification_problem(release, {})
    assert verification_problem({**release, "verified_book": None}) is not None


def test_a_hand_built_book_off_the_editions_size_is_accepted_only_with_a_valid_proof():
    plain = hand_built()
    assert OFF_EDITION in suspect_volume_reasons(edition(plain), chapter_map=MAP)["6"]
    assert suspect_volume_reasons(edition(proven(plain)), chapter_map=MAP) == {
        "6": VERIFIED_BOOK
    }


@pytest.mark.parametrize(
    "release, chapter_map",
    [
        pytest.param(
            {**proven(hand_built()), "local_import_sha256": "e" * 64}, MAP, id="content"
        ),
        pytest.param(
            proven(hand_built()),
            [MapEntry(("6",), ("30", "31", "32"), True, "ocr")],
            id="map",
        ),
        pytest.param(proven(hand_built()), [], id="no-entry"),
        pytest.param(proven(hand_built()), None, id="map-unknown"),
        pytest.param(proven(hand_built(), ("30",)), MAP, id="other-chapters"),
    ],
)
def test_a_proof_for_other_content_or_another_map_changes_nothing(release, chapter_map):
    reasons = suspect_volume_reasons(edition(release), chapter_map=chapter_map)
    assert OFF_EDITION in reasons["6"]


def test_a_proof_lifts_a_doubt_and_never_adds_or_removes_authority():
    normal = proven(book(6, 320, "prowlarr", DIGEST))
    assert suspect_volume_reasons(edition(normal), chapter_map=MAP) == {}
    by_hand = proven(book(6, 320, "manual", DIGEST))
    assert suspect_volume_reasons(edition(by_hand), chapter_map=MAP) == {
        "6": IMPORTED_BY_HAND
    }


def test_a_proof_is_no_reference_for_the_editions_size():
    """A verified 434-page book must not drag the edition's median up."""

    releases = [book(n, p) for n, p in ((1, 324), (2, 319), (3, 319))]
    releases += [proven(hand_built()), book(7, 330, "manual", "f" * 64)]
    reasons = suspect_volume_reasons(releases, chapter_map=MAP)
    assert reasons == {"6": VERIFIED_BOOK, "7": IMPORTED_BY_HAND}


def test_a_verified_oversized_book_is_no_longer_called_unverified_as_one_book():
    oversized = hand_built(pages=700)
    assert suspect_volume_reasons([oversized], chapter_map=MAP)["6"].startswith(
        "700 pages"
    )
    assert suspect_volume_reasons([proven(oversized)], chapter_map=MAP) == {
        "6": VERIFIED_BOOK
    }


def test_a_verified_machine_translation_keeps_a_reason_of_its_own():
    translated = book(6, 200, "translated", DIGEST)
    assert suspect_volume_reasons([translated], chapter_map=MAP) == {
        "6": MACHINE_TRANSLATION
    }
    assert suspect_volume_reasons([proven(translated)], chapter_map=MAP) == {
        "6": VERIFIED_TRANSLATION
    }
    assert suspect_volume_reasons([proven(translated)], chapter_map=None) == {
        "6": MACHINE_TRANSLATION
    }


MANGA = {
    "id": "mk",
    "title": "Master Keaton",
    "status": "ended",
    "monitor_mode": "all",
    "preferred_language": "en",
    "edition_book_count": 6,
}


def slot_six(releases):
    index = build_chapter_index(
        MANGA,
        {"status": "ended", "volume_count": 6},
        releases,
        {},
        chapter_map=MAP,
    )
    return next(slot for slot in index["slots"] if slot.get("volume") == "6")


def test_a_verified_hand_built_book_stops_asking_for_the_real_one():
    assert OFF_EDITION in slot_six(edition(hand_built()))["replaceable"]
    proved = slot_six(edition(proven(hand_built())))
    assert proved["downloaded"] is True
    assert "replaceable" not in proved


def chapter_slots(releases, source="ocr"):
    manga_row = {
        "status": "completed",
        "series_unit_override": "chapters",
        "monitor_mode": "all",
    }
    human = {
        "id": "mangadex-1",
        "chapter": "1",
        "volume": None,
        "provider": "mangadex",
        "downloaded": True,
        "monitored": True,
        "version": 1,
        "pages": 20,
        "publish_at": None,
    }
    index = build_chapter_index(
        manga_row,
        {"status": "ended"},
        [*releases, human],
        chapter_map=[MapEntry(("1",), ("1", "2"), True, source)],
    )
    return {slot["chapter"]: slot for slot in index["slots"]}


def test_a_verified_translation_satisfies_wanted_but_condemns_no_chapter_file():
    translated = {**book(1, 200, "translated", DIGEST)}
    verified = {**translated, "verified_book": proof_of(translated, ["1", "2"])}
    open_slots = chapter_slots([translated])
    assert open_slots["2"]["covered_by_volume"] is None
    slots = chapter_slots([verified])
    assert slots["2"]["covered_by_volume"] == "1"
    assert not slots["2"]["covered_unmapped"]
    assert slots["1"]["downloaded"] is True
    assert slots["1"]["duplicate_of_volume"] is None


def test_a_verified_hand_built_book_covers_wanted_and_condemns_no_chapter_file():
    by_hand = book(1, 700, "manual", DIGEST)
    verified = {**by_hand, "verified_book": proof_of(by_hand, ["1", "2"])}
    slots = chapter_slots([verified])
    assert slots["2"]["covered_by_volume"] == "1"
    assert slots["1"]["duplicate_of_volume"] is None


ONE_BOOK = {
    "id": "book-6",
    "chapter": None,
    "release_unit": "volume",
    "volume": "6",
    "language": "en",
}


def add_book(
    database, tmp_path: Path, number, pages, provider, digest=None, release=None
):
    identifier = release or f"book-{number}"
    row = {**chapter(identifier, "", volume=str(number)), **ONE_BOOK}
    row.update(id=identifier, volume=str(number), provider=provider, pages=pages)
    path = tmp_path / f"{identifier}.cbz"
    path.write_bytes(b"cbz")
    database.publish_external_chapter(
        "manga-1",
        row,
        path,
        hashlib.sha256(f"file-{identifier}".encode()).hexdigest(),
        digest or hashlib.sha256(f"content-{identifier}".encode()).hexdigest(),
    )


@pytest.fixture
def series(tmp_path):
    database, service, _komga = make_service(tmp_path)
    database.upsert_manga(manga(), "en", "all")
    database.update_manga("manga-1", {"series_unit_override": "volumes"})
    for number, pages in ((1, 324), (2, 319), (3, 319), (4, 356), (5, 300)):
        add_book(database, tmp_path, number, pages, "prowlarr")
    add_book(database, tmp_path, 6, 434, "manual", DIGEST)
    database.upsert_chapters(
        "manga-1",
        [chapter("ch-30", "30", volume=None), chapter("ch-31", "31", volume=None)],
    )
    for identifier in ("ch-30", "ch-31"):
        database.mark_chapter_downloaded(identifier, tmp_path / f"{identifier}.cbz")
    database.replace_chapter_map("manga-1", "ocr", MAP)
    app = FastAPI()
    register_verified_book_routes(app, database, service)
    with TestClient(app) as client:
        yield database, service, client


LISTING = "/api/manga/manga-1/verified-books"
PROOF = "/api/manga/manga-1/volumes/6/verification"


def request(**over):
    return {
        "content_sha256": DIGEST,
        "chapters": ["30", "31"],
        "source": "https://publisher.example/volume-6",
        "note": "Every page read against the publisher's table of contents",
        **over,
    }


def test_the_listing_gives_a_person_what_a_proof_needs(series):
    _database, _service, client = series
    listing = client.get(LISTING).json()
    books = {entry["volume"]: entry for entry in listing["books"]}
    assert sorted(books) == ["1", "2", "3", "4", "5", "6"]
    six = books["6"]
    assert six["content_sha256"] == DIGEST
    assert six["mapped_chapters"] == ["30", "31"]
    assert OFF_EDITION in six["suspect_reason"]
    assert six["verification"] is None
    assert books["1"]["suspect_reason"] is None
    assert client.get("/api/manga/nowhere/verified-books").status_code == 404


def test_a_proof_is_refused_unless_it_matches_the_book_and_the_map_as_they_are(series):
    database, _service, client = series
    stale = client.put(PROOF, json=request(content_sha256="e" * 64))
    assert stale.status_code == 409 and DIGEST in stale.json()["detail"]
    wrong_map = client.put(PROOF, json=request(chapters=["30"]))
    assert wrong_map.status_code == 409
    assert "30, 31" in wrong_map.json()["detail"]
    assert client.put(PROOF, json=request(chapters=["30", "30"])).status_code == 422
    assert client.put(PROOF, json=request(chapters=["x"])).status_code == 422
    assert client.put(PROOF, json=request(note="   ")).status_code == 422
    assert client.put(PROOF, json=request(note="")).status_code == 422
    assert client.put(PROOF, json=request(extra="no")).status_code == 422
    assert client.put(PROOF, json=request(release_id="nothing")).status_code == 404
    assert (
        client.put(
            "/api/manga/manga-1/volumes/9/verification", json=request()
        ).status_code
        == 404
    )
    assert (
        client.put(
            "/api/manga/nowhere/volumes/6/verification", json=request()
        ).status_code
        == 404
    )
    assert database.get_chapter("book-6")["verified_book"] is None


def test_a_valid_proof_lifts_the_size_doubt_and_condemns_no_chapter_file(series):
    database, service, client = series
    response = client.put(PROOF, json=request())
    assert response.status_code == 200, response.text
    verification = response.json()["verification"]
    assert verification["valid"] is True and verification["problem"] is None
    assert verification["chapters"] == ["30", "31"]
    assert service.suspect_covering_volumes("manga-1")["6"] == VERIFIED_BOOK
    assert service.duplicate_chapter_files("manga-1") == []
    stored = database.get_chapter("book-6")["verified_book"]
    assert stored["content_sha256"] == DIGEST and stored["release_id"] == "book-6"
    assert stored["note"].startswith("Every page read")
    listing = client.get(LISTING).json()["books"]
    six = next(entry for entry in listing if entry["volume"] == "6")
    assert six["suspect_reason"] == VERIFIED_BOOK and six["verification"]["valid"]
    assert (
        service._hand_built_book_off_edition("manga-1", database.get_chapter("book-6"))
        is False
    )


def test_the_replacement_guard_asks_for_the_edition_until_the_book_is_verified(series):
    database, service, client = series
    book_six = database.get_chapter("book-6")
    assert service._hand_built_book_off_edition("manga-1", book_six) is True
    assert client.put(PROOF, json=request()).status_code == 200
    assert service._hand_built_book_off_edition("manga-1", book_six) is False
    assert client.delete(PROOF).status_code == 200
    assert service._hand_built_book_off_edition("manga-1", book_six) is True


def test_a_proof_goes_stale_by_itself_when_the_map_or_the_content_changes(
    series, tmp_path
):
    database, service, client = series
    assert client.put(PROOF, json=request()).status_code == 200

    database.replace_chapter_map(
        "manga-1", "ocr", [MapEntry(("6",), ("30", "31", "32"), True, "ocr")]
    )
    six = next(e for e in client.get(LISTING).json()["books"] if e["volume"] == "6")
    assert six["verification"]["valid"] is False
    assert "chapter map" in six["verification"]["problem"]
    assert OFF_EDITION in service.suspect_covering_volumes("manga-1")["6"]

    database.replace_chapter_map("manga-1", "ocr", MAP)
    assert service.suspect_covering_volumes("manga-1")["6"] == VERIFIED_BOOK

    path = tmp_path / "book-6.cbz"
    database.publish_external_chapter(
        "manga-1",
        {**database.get_chapter("book-6"), "chapter": None},
        path,
        "1" * 64,
        "2" * 64,
    )
    changed = next(e for e in client.get(LISTING).json()["books"] if e["volume"] == "6")
    assert changed["verification"]["valid"] is False
    assert "content changed" in changed["verification"]["problem"]
    assert OFF_EDITION in service.suspect_covering_volumes("manga-1")["6"]


def test_a_reimport_of_the_same_book_keeps_its_proof(series, tmp_path):
    database, _service, client = series
    assert client.put(PROOF, json=request()).status_code == 200
    database.publish_external_chapter(
        "manga-1",
        {**database.get_chapter("book-6"), "chapter": None},
        tmp_path / "book-6.cbz",
        "3" * 64,
        DIGEST,
    )
    six = database.get_chapter("book-6")
    assert six["library_sha256"] == "3" * 64
    assert verified_book(six)["chapters"] == ["30", "31"]


def test_two_books_of_one_volume_need_the_release_named(series, tmp_path):
    database, _service, client = series
    add_book(database, tmp_path, 6, 320, "prowlarr", release="torrent-6")
    ambiguous = client.put(PROOF, json=request())
    assert ambiguous.status_code == 409
    assert "book-6" in ambiguous.json()["detail"]
    assert "torrent-6" in ambiguous.json()["detail"]
    assert client.put(PROOF, json=request(release_id="book-6")).status_code == 200
    assert database.get_chapter("torrent-6")["verified_book"] is None


def test_a_proof_can_be_withdrawn(series):
    database, _service, client = series
    assert client.put(PROOF, json=request()).status_code == 200
    removed = client.delete(PROOF)
    assert removed.status_code == 200 and removed.json()["removed"] == 1
    assert database.get_chapter("book-6")["verified_book"] is None
    assert client.delete(PROOF).status_code == 404


def test_an_assembled_book_carries_its_own_provenance_and_is_not_verified_here(
    series, tmp_path
):
    database, _service, client = series
    path = tmp_path / "book-7.cbz"
    path.write_bytes(b"cbz")
    database.publish_external_chapter(
        "manga-1",
        {
            **chapter("book-7", "", volume="7"),
            **ONE_BOOK,
            "id": "book-7",
            "volume": "7",
            "provider": "assembled",
            "pages": 300,
            "assembled_from": {
                "version": 1,
                "chapter_ids": ["ch-30", "ch-31"],
                "chapters": ["30", "31"],
                "volume": "7",
                "sources": [],
                "notes": "assembled from two chapter files",
            },
        },
        path,
        "6" * 64,
        "7" * 64,
    )
    response = client.put(
        "/api/manga/manga-1/volumes/7/verification",
        json=request(content_sha256="7" * 64),
    )
    assert response.status_code == 404
    listed = {entry["volume"] for entry in client.get(LISTING).json()["books"]}
    assert "7" not in listed
