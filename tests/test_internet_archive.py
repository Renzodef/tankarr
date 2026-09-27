from __future__ import annotations

from pathlib import Path
from urllib.parse import unquote_plus

import pytest
import respx
from httpx import Response

from tankarr.config import Settings
from tankarr.internet_archive import (
    InternetArchiveClient,
    normalize_item_files,
    pseudo_hash,
    search_query,
    search_titles,
)
from tankarr.volume_hunt import rank_release

ITEM = {
    "identifier": "ultra-heaven-koike",
    "title": "MANGA: Ultra Heaven",
    "downloads": 412,
    "publicdate": "2025-03-01T00:00:00Z",
}
FILES = [
    {
        "name": "Ultra Heaven v01.cbz",
        "format": "Comic Book ZIP",
        "size": "83000000",
        "source": "original",
    },
    {
        "name": "Ultra Heaven v02.cbz",
        "format": "Comic Book ZIP",
        "size": "91000000",
        "source": "original",
    },
    {
        "name": "Ultra Heaven v03.pdf",
        "format": "Text PDF",
        "size": "77000000",
        "source": "original",
    },
    # the archive's own derivatives duplicate the upload
    {
        "name": "Ultra Heaven v01_text.pdf",
        "format": "Text PDF",
        "size": "60000000",
        "source": "derivative",
    },
    # a cover, a thumbnail, a metadata file
    {
        "name": "cover.cbz",
        "format": "Comic Book ZIP",
        "size": "120000",
        "source": "original",
    },
    {
        "name": "ultra-heaven-koike_meta.xml",
        "format": "Metadata",
        "size": "900",
        "source": "original",
    },
]


def test_the_query_asks_for_the_title_as_a_phrase_and_books_only():
    query = search_query('Ultra "Heaven" (Koike)')
    assert query.startswith('title:("Ultra Heaven Koike")')
    assert "mediatype:(texts)" in query
    assert 'format:("Comic Book ZIP")' in query and 'format:("Text PDF")' in query


def test_one_release_per_original_book_file():
    releases = normalize_item_files(ITEM, FILES, query="Ultra Heaven", language="en")

    assert [r["title"] for r in releases] == [
        "Ultra Heaven v01",
        "Ultra Heaven v02",
        "Ultra Heaven v03",
    ]
    first = releases[0]
    assert first["provider"] == "internetarchive"
    assert first["protocol"] == "http"
    assert first["volume"] == "1" and first["chapter"] is None
    assert first["size_bytes"] == 83_000_000
    assert first["downloads"] == 412
    assert first["download_ref"] == (
        "https://archive.org/download/ultra-heaven-koike/Ultra%20Heaven%20v01.cbz"
    )
    assert first["source_url"] == "https://archive.org/details/ultra-heaven-koike"
    assert first["info_hash"] == pseudo_hash(
        "ultra-heaven-koike", "Ultra Heaven v01.cbz"
    )
    assert len(first["info_hash"]) == 40
    assert first["id"] == first["info_hash"]  # URL- and form-safe
    assert first["archive_file"] == "Ultra Heaven v01.cbz"
    assert first["archive_format"] == ".cbz"
    assert releases[2]["archive_format"] == ".pdf"


def test_foreign_pack_directory_is_not_offered_as_english():
    files = [
        {
            **FILES[0],
            "name": (
                "Mobile Suit Gundam v01-24 (Digital) (BookWalker) (JP)/"
                "Mobile Suit Gundam v12.cbr"
            ),
            "format": "Comic Book RAR",
        }
    ]

    assert normalize_item_files(ITEM, files, query="Gundam", language="en") == []


@pytest.mark.asyncio
@respx.mock
async def test_archive_item_declaring_another_language_is_not_offered_as_english():
    item = {**ITEM, "identifier": "foreign-book"}
    respx.get("https://archive.org/advancedsearch.php").mock(
        return_value=Response(200, json={"response": {"docs": [item]}})
    )
    respx.get("https://archive.org/metadata/foreign-book").mock(
        return_value=Response(
            200,
            json={"metadata": {"language": "vie"}, "files": FILES},
        )
    )
    client = InternetArchiveClient(Settings())
    try:
        assert await client.search("Ultra Heaven", "en") == []
    finally:
        await client.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_archive_item_declaring_english_is_offered_as_english():
    respx.get("https://archive.org/advancedsearch.php").mock(
        return_value=Response(200, json={"response": {"docs": [ITEM]}})
    )
    respx.get("https://archive.org/metadata/ultra-heaven-koike").mock(
        return_value=Response(
            200,
            json={"metadata": {"language": "eng"}, "files": FILES},
        )
    )
    client = InternetArchiveClient(Settings())
    try:
        assert len(await client.search("Ultra Heaven", "en")) == 3
    finally:
        await client.aclose()


def test_a_direct_release_ranks_as_a_fallback_below_indexers_but_never_dead():
    direct = normalize_item_files(ITEM, FILES, query="Ultra Heaven", language="en")[0]
    torrent = {
        "title": "Ultra Heaven v01 (Digital)",
        "protocol": "torrent",
        "seeders": 3,
        "indexer_priority": 25,
    }
    dead = {**torrent, "seeders": 0}
    assert rank_release(direct, None)[0] == 1  # a candidate, not (-1)
    assert rank_release(torrent, None) > rank_release(direct, None)
    assert rank_release(dead, None)[0] == -1


@pytest.mark.asyncio
@respx.mock
async def test_search_reads_items_then_their_files_with_tankarrs_user_agent():
    search = respx.get("https://archive.org/advancedsearch.php").mock(
        return_value=Response(200, json={"response": {"numFound": 1, "docs": [ITEM]}})
    )
    meta = respx.get("https://archive.org/metadata/ultra-heaven-koike").mock(
        return_value=Response(200, json={"files": FILES})
    )
    settings = Settings()
    settings.internet_archive_enabled = True
    client = InternetArchiveClient(settings)
    try:
        assert client.enabled
        releases = await client.search("Ultra Heaven", "en")
    finally:
        await client.aclose()

    assert [r["title"] for r in releases] == [
        "Ultra Heaven v01",
        "Ultra Heaven v02",
        "Ultra Heaven v03",
    ]
    assert search.called and meta.called
    sent = search.calls[0].request
    assert sent.headers["User-Agent"].startswith("Tankarr/")
    assert 'title:("Ultra Heaven")' in sent.url.params["q"]


@pytest.mark.asyncio
@respx.mock
async def test_download_streams_to_a_part_file_then_renames(tmp_path: Path):
    body = b"PK" + b"\x00" * 4000
    respx.get("https://archive.org/download/x/book.cbz").mock(
        return_value=Response(
            200, content=body, headers={"Content-Length": str(len(body))}
        )
    )
    settings = Settings()
    client = InternetArchiveClient(settings)
    progress: list[tuple[int, int]] = []

    async def report(written: int, total: int) -> None:
        progress.append((written, total))

    try:
        path = await client.download(
            "https://archive.org/download/x/book.cbz",
            tmp_path / "1" / "book.cbz",
            expected_size=len(body),
            progress=report,
        )
    finally:
        await client.aclose()

    assert path.read_bytes() == body
    assert not (tmp_path / "1" / "book.cbz.part").exists()
    assert progress[-1] == (len(body), len(body))


@pytest.mark.asyncio
@respx.mock
async def test_a_short_download_is_refused_not_imported(tmp_path: Path):
    respx.get("https://archive.org/download/x/book.cbz").mock(
        return_value=Response(200, content=b"PK" + b"\x00" * 10)
    )
    client = InternetArchiveClient(Settings())
    try:
        with pytest.raises(Exception) as caught:
            await client.download(
                "https://archive.org/download/x/book.cbz",
                tmp_path / "book.cbz",
                expected_size=5000,
            )
    finally:
        await client.aclose()
    assert "archive.org lists 5000" in str(caught.value)
    assert not (tmp_path / "book.cbz").exists()


@pytest.mark.asyncio
@respx.mock
async def test_the_probe_times_one_real_search_and_reports_the_failure_itself():
    respx.get("https://archive.org/advancedsearch.php").mock(
        return_value=Response(200, json={"response": {"numFound": 1, "docs": [ITEM]}})
    )
    respx.get("https://archive.org/metadata/ultra-heaven-koike").mock(
        return_value=Response(200, json={"files": FILES})
    )
    client = InternetArchiveClient(Settings())
    try:
        report = await client.probe("Ultra Heaven")
    finally:
        await client.aclose()
    assert report["ok"] is True
    assert report["items"] == 1 and report["releases"] == 3
    assert report["sample"][0] == "Ultra Heaven v01"
    assert report["user_agent"].startswith("Tankarr/")
    assert isinstance(report["latency_ms"], int)

    respx.get("https://archive.org/advancedsearch.php").mock(
        return_value=Response(503, text="down")
    )
    client = InternetArchiveClient(Settings())
    try:
        report = await client.probe("Ultra Heaven")
    finally:
        await client.aclose()
    assert report["ok"] is False and "503" in report["error"]


def test_a_work_is_asked_for_under_the_names_uploaders_actually_use():
    """Dark Horse's Ghost in the Shell is filed as "MANGA: Ghost in the Shell 1".

    The catalogue title never matches that phrase, so the books were invisible
    and the series kept following chapters for want of any book to offer.
    """

    assert search_titles("Appleseed") == ["Appleseed"]
    assert search_titles("The Ghost in the Shell") == [
        "The Ghost in the Shell",
        "Ghost in the Shell",
    ]
    assert search_titles("The Ghost in the Shell 2: Man-Machine Interface") == [
        "The Ghost in the Shell 2: Man-Machine Interface",
        "Ghost in the Shell 2 Man Machine Interface",
        "Man Machine Interface",
    ]
    # The 1.5 volume is filed under "01.5": only the subtitle reaches it.
    assert "Human Error Processor" in search_titles(
        "The Ghost in the Shell 1.5: Human-Error Processor"
    )
    # A subtitle too short to identify anything is never searched on its own,
    # though dropping the punctuation is still worth one attempt.
    assert search_titles("Nana: Two") == ["Nana: Two", "Nana Two"]
    assert search_titles("") == []


@respx.mock
async def test_search_falls_back_only_while_a_phrasing_finds_nothing():
    empty = {"response": {"numFound": 0, "docs": []}}
    found = {"response": {"numFound": 1, "docs": [ITEM]}}
    search = respx.get("https://archive.org/advancedsearch.php").mock(
        side_effect=[Response(200, json=empty), Response(200, json=found)]
    )
    respx.get("https://archive.org/metadata/ultra-heaven-koike").mock(
        return_value=Response(200, json={"files": FILES})
    )
    settings = Settings()
    settings.internet_archive_enabled = True
    client = InternetArchiveClient(settings)
    try:
        releases = await client.search("The Ultra Heaven", "en")
    finally:
        await client.aclose()

    assert [r["title"] for r in releases][:1] == ["Ultra Heaven v01"]
    assert search.call_count == 2
    asked = [unquote_plus(str(call.request.url)) for call in search.calls]
    assert 'title:("The Ultra Heaven")' in asked[0]
    assert 'title:("Ultra Heaven")' in asked[1]


@respx.mock
async def test_a_title_that_answers_at_once_costs_one_request():
    search = respx.get("https://archive.org/advancedsearch.php").mock(
        return_value=Response(200, json={"response": {"numFound": 1, "docs": [ITEM]}})
    )
    respx.get("https://archive.org/metadata/ultra-heaven-koike").mock(
        return_value=Response(200, json={"files": FILES})
    )
    settings = Settings()
    settings.internet_archive_enabled = True
    client = InternetArchiveClient(settings)
    try:
        await client.search("The Ultra Heaven", "en")
    finally:
        await client.aclose()
    assert search.call_count == 1


@respx.mock
async def test_a_phrasing_that_finds_items_holding_no_book_is_not_the_answer():
    """Items are not books.

    "The Ghost in the Shell" matches three archive items that hold no
    importable book at all. Stopping there left the series with nothing,
    while "Ghost in the Shell" reaches Dark Horse's volumes.
    """

    barren = {"identifier": "some-scan", "title": "The Ultra Heaven", "downloads": 3}
    search = respx.get("https://archive.org/advancedsearch.php").mock(
        side_effect=[
            Response(200, json={"response": {"numFound": 1, "docs": [barren]}}),
            Response(200, json={"response": {"numFound": 1, "docs": [ITEM]}}),
        ]
    )
    respx.get("https://archive.org/metadata/some-scan").mock(
        return_value=Response(200, json={"files": [{"name": "cover.jpg"}]})
    )
    respx.get("https://archive.org/metadata/ultra-heaven-koike").mock(
        return_value=Response(200, json={"files": FILES})
    )
    settings = Settings()
    settings.internet_archive_enabled = True
    client = InternetArchiveClient(settings)
    try:
        releases = await client.search("The Ultra Heaven", "en")
    finally:
        await client.aclose()

    assert [r["title"] for r in releases][:1] == ["Ultra Heaven v01"]
    assert search.call_count == 2
