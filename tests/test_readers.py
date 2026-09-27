from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx
from httpx import Response

from tankarr.config import Settings
from tankarr.database import Database
from tankarr.readers import effective_reader_kind, resolve_reader_link, template_url


def seed(tmp_path: Path) -> tuple[Settings, Database]:
    settings = Settings(data_dir=tmp_path / "data", library_dir=tmp_path / "library")
    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    database.upsert_manga(
        {
            "id": "m1",
            "title": "Blue Period",
            "description": "",
            "cover_url": None,
            "authors": ["Tsubasa Yamaguchi"],
            "original_language": "ja",
        },
        "en",
        "none",
    )
    return settings, database


def seed_downloaded_book(settings: Settings, database: Database) -> Path:
    chapter = {
        "id": "chapter-1",
        "manga_id": "m1",
        "volume": "1",
        "chapter": "1",
        "title": "",
        "language": "en",
        "provider": "test",
        "groups": [],
        "publish_at": "2026-01-01T00:00:00+00:00",
        "source_url": "https://example.test/chapter-1",
        "pages": 10,
        "version": 1,
    }
    database.upsert_chapters("m1", [chapter])
    path = (
        settings.library_dir
        / "Blue Period (Tsubasa Yamaguchi)"
        / "Blue Period - Chapter 1.cbz"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"book")
    database.mark_chapter_downloaded("chapter-1", path)
    return path


def test_auto_picks_komga_then_template_then_builtin(tmp_path: Path):
    settings, _ = seed(tmp_path)
    assert effective_reader_kind(settings) == "tankarr"
    settings.reader_series_url_template = "https://r.test/s?q={title}"
    assert effective_reader_kind(settings) == "url"
    settings.komga_link_enabled = True
    assert effective_reader_kind(settings) == "komga"
    settings.reader_kind = "none"
    assert effective_reader_kind(settings) == "none"


def test_template_fills_placeholders_safely():
    manga = {"id": "m1", "title": "Blue Period", "authors": ["Tsubasa Yamaguchi"]}
    assert (
        template_url("https://r.test/s?q={title}", manga)
        == "https://r.test/s?q=Blue%20Period"
    )
    assert template_url("https://r.test/{folder}", manga).endswith(
        "/Blue%20Period%20%28Tsubasa%20Yamaguchi%29"
    )
    assert (
        template_url("https://r.test/{id}/{title_raw}", manga)
        == "https://r.test/m1/Blue Period"
    )


@pytest.mark.asyncio
async def test_url_reader_returns_a_link_without_touching_the_network(tmp_path: Path):
    settings, database = seed(tmp_path)
    settings.reader_kind = "url"
    settings.reader_series_url_template = "https://reader.local/search?q={title}"
    link = await resolve_reader_link(settings, database, None, "m1")
    assert link == {
        "reader": "url",
        "label": "Reader",
        "configured": True,
        "available": True,
        "url": "https://reader.local/search?q=Blue%20Period",
    }


@pytest.mark.asyncio
@respx.mock
async def test_kavita_reader_resolves_the_series_by_exact_title(tmp_path: Path):
    settings, database = seed(tmp_path)
    settings.reader_kind = "kavita"
    settings.reader_url = "http://kavita.test:5000"
    settings.reader_api_key = "secret-key"
    respx.post("http://kavita.test:5000/api/Plugin/authenticate").mock(
        return_value=Response(200, json={"token": "jwt"})
    )
    search = respx.get("http://kavita.test:5000/api/Search/search").mock(
        return_value=Response(
            200,
            json={
                "series": [
                    {"seriesId": 12, "libraryId": 3, "name": "Blue Period"},
                    {"seriesId": 99, "libraryId": 3, "name": "Blue Period: Extras"},
                ]
            },
        )
    )
    async with httpx.AsyncClient() as client:
        link = await resolve_reader_link(
            settings, database, None, "m1", kavita_client=client
        )
    assert link["available"] is True and link["reader"] == "kavita"
    assert link["url"] == "http://kavita.test:5000/library/3/series/12"
    assert link["title"] == "Blue Period"
    assert search.calls[0].request.headers["Authorization"] == "Bearer jwt"


@pytest.mark.asyncio
@respx.mock
async def test_kavita_reader_opens_the_exact_managed_file_in_reading_mode(
    tmp_path: Path,
):
    settings, database = seed(tmp_path)
    path = seed_downloaded_book(settings, database)
    settings.reader_kind = "kavita"
    settings.reader_url = "http://kavita.test:5000"
    settings.reader_api_key = "secret-key"
    respx.post("http://kavita.test:5000/api/Plugin/authenticate").mock(
        return_value=Response(200, json={"token": "jwt"})
    )
    respx.get("http://kavita.test:5000/api/Search/search").mock(
        return_value=Response(
            200,
            json={"series": [{"seriesId": 12, "libraryId": 3, "name": "Blue Period"}]},
        )
    )
    volumes = respx.get("http://kavita.test:5000/api/Series/volumes").mock(
        return_value=Response(
            200,
            json=[
                {
                    "id": 7,
                    "chapters": [
                        {
                            "id": 42,
                            "files": [
                                {
                                    "filePath": (
                                        "/comics/"
                                        + path.relative_to(
                                            settings.library_dir
                                        ).as_posix()
                                    ),
                                    "format": 1,
                                }
                            ],
                        }
                    ],
                }
            ],
        )
    )

    async with httpx.AsyncClient() as client:
        link = await resolve_reader_link(
            settings, database, None, "m1", kavita_client=client
        )

    assert link["books"] == [
        {
            "chapter_id": "chapter-1",
            "book_id": "42",
            "url": "http://kavita.test:5000/library/3/series/12/manga/42",
        }
    ]
    assert link["matched_books"] == 1
    assert volumes.calls[0].request.headers["Authorization"] == "Bearer jwt"


@pytest.mark.asyncio
async def test_none_reader_reports_no_shortcut(tmp_path: Path):
    settings, database = seed(tmp_path)
    settings.reader_kind = "none"
    link = await resolve_reader_link(settings, database, None, "m1")
    assert link["available"] is False and link["reader"] == "none"


@pytest.mark.asyncio
async def test_reader_file_inspection_does_not_block_event_loop(tmp_path, monkeypatch):
    import threading

    import tankarr.readers as module

    settings, database = seed(tmp_path)
    settings.reader_kind = "kavita"
    loop_thread = threading.get_ident()
    threads = []

    def inspect(*_args):
        threads.append(threading.get_ident())
        return []

    monkeypatch.setattr(module, "_managed_library_books", inspect)
    result = await resolve_reader_link(settings, database, None, "m1")
    assert result["managed_books"] == 0
    assert threads and all(thread != loop_thread for thread in threads)


@pytest.mark.asyncio
@respx.mock
async def test_kavita_changed_title_resolves_by_catalogue_path_and_persists(tmp_path):
    settings, database = seed(tmp_path)
    path = seed_downloaded_book(settings, database)
    relative = path.relative_to(settings.library_dir).as_posix()
    settings.reader_kind = "kavita"
    settings.reader_url = "http://kavita.test:5000"
    settings.reader_api_key = "secret-key"
    respx.post("http://kavita.test:5000/api/Plugin/authenticate").respond(
        200, json={"token": "jwt"}
    )
    search = respx.get("http://kavita.test:5000/api/Search/search").respond(
        200, json={"series": []}
    )
    catalog = respx.post("http://kavita.test:5000/api/Series/all-v2").respond(
        200,
        json=[
            {
                "id": 12,
                "libraryId": 3,
                "name": "Unrelated translated title",
                "folderPath": "/comics/" + str(Path(relative).parent),
            },
        ],
    )
    respx.get("http://kavita.test:5000/api/Series/volumes").respond(
        200,
        json=[
            {
                "chapters": [
                    {
                        "id": 42,
                        "files": [{"filePath": "/comics/" + relative, "format": 1}],
                    }
                ]
            },
        ],
    )
    async with httpx.AsyncClient() as client:
        first = await resolve_reader_link(
            settings, database, None, "m1", kavita_client=client
        )
        second = await resolve_reader_link(
            settings, Database(database.path), None, "m1", kavita_client=client
        )
    assert first["available"] and first["match_method"] == "catalogue_path"
    assert first["books"][0]["book_id"] == "42"
    assert second["match_method"] == "persistent_path"
    assert search.call_count == 1 and catalog.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_kavita_homonyms_need_unique_file_evidence(tmp_path):
    settings, database = seed(tmp_path)
    path = seed_downloaded_book(settings, database)
    settings.reader_kind = "kavita"
    settings.reader_url = "http://kavita.test:5000"
    settings.reader_api_key = "secret-key"
    respx.post("http://kavita.test:5000/api/Plugin/authenticate").respond(
        200, json={"token": "jwt"}
    )
    respx.get("http://kavita.test:5000/api/Search/search").respond(
        200,
        json={
            "series": [
                {"seriesId": 12, "libraryId": 3, "name": "Blue Period"},
                {"seriesId": 13, "libraryId": 4, "name": "Blue Period"},
            ]
        },
    )
    respx.get("http://kavita.test:5000/api/Series/volumes").respond(
        200,
        json=[
            {
                "chapters": [
                    {
                        "id": 42,
                        "files": [
                            {
                                "filePath": "/comics/"
                                + path.relative_to(settings.library_dir).as_posix(),
                                "format": 1,
                            }
                        ],
                    }
                ]
            },
        ],
    )
    async with httpx.AsyncClient() as client:
        result = await resolve_reader_link(
            settings, database, None, "m1", kavita_client=client
        )
    assert result["available"] is False and result["match_method"] == "ambiguous_path"


@pytest.mark.asyncio
@respx.mock
async def test_kavita_error_does_not_echo_api_key(tmp_path):
    settings, database = seed(tmp_path)
    settings.reader_kind = "kavita"
    settings.reader_url = "http://kavita.test:5000"
    settings.reader_api_key = "secret-query-key"
    respx.post("http://kavita.test:5000/api/Plugin/authenticate").respond(401)
    async with httpx.AsyncClient() as client:
        result = await resolve_reader_link(
            settings, database, None, "m1", kavita_client=client
        )
    assert result["available"] is False
    assert "secret-query-key" not in str(result) and "apiKey" not in str(result)


@pytest.mark.asyncio
@respx.mock
async def test_stump_reader_resolves_the_series_by_library_folder_path(tmp_path: Path):
    settings, database = seed(tmp_path)
    settings.reader_kind = "stump"
    settings.reader_url = "http://stump.test:10801"
    settings.reader_username = "fixture-user"
    settings.reader_password = "pw"
    settings.reader_library_path = "/data/comics"
    respx.post("http://stump.test:10801/api/v2/auth/login").mock(
        return_value=Response(200, json={"accessToken": "jwt", "refreshToken": "r"})
    )
    graphql = respx.post("http://stump.test:10801/api/graphql").mock(
        return_value=Response(
            200,
            json={
                "data": {
                    "series": {
                        "nodes": [
                            {
                                "id": "abc-1",
                                "name": "Blue Period (Tsubasa Yamaguchi)",
                                "path": "/data/comics/Blue Period (Tsubasa Yamaguchi)",
                                "libraryId": "lib",
                            }
                        ]
                    }
                }
            },
        )
    )
    async with httpx.AsyncClient() as client:
        link = await resolve_reader_link(
            settings, database, None, "m1", kavita_client=client
        )
    assert (
        link["available"] is True
        and link["reader"] == "stump"
        and link["label"] == "Stump"
    )
    assert link["url"] == "http://stump.test:10801/series/abc-1"
    sent = graphql.calls[0].request
    assert sent.headers["Authorization"] == "Bearer jwt"
    assert "/data/comics/Blue Period (Tsubasa Yamaguchi)" in sent.content.decode()


@pytest.mark.asyncio
@respx.mock
async def test_stump_reader_opens_the_exact_managed_file_in_reading_mode(
    tmp_path: Path,
):
    settings, database = seed(tmp_path)
    path = seed_downloaded_book(settings, database)
    relative = path.relative_to(settings.library_dir).as_posix()
    settings.reader_kind = "stump"
    settings.reader_url = "http://stump.test:10801"
    settings.reader_username = "fixture-user"
    settings.reader_password = "pw"
    settings.reader_library_path = "/data/comics"
    respx.post("http://stump.test:10801/api/v2/auth/login").mock(
        return_value=Response(200, json={"accessToken": "jwt"})
    )
    graphql = respx.post("http://stump.test:10801/api/graphql").mock(
        side_effect=[
            Response(
                200,
                json={
                    "data": {
                        "series": {
                            "nodes": [
                                {
                                    "id": "series-1",
                                    "name": "Blue Period",
                                    "path": "/data/comics/Blue Period (Tsubasa Yamaguchi)",
                                    "libraryId": "library-1",
                                }
                            ]
                        }
                    }
                },
            ),
            Response(
                200,
                json={
                    "data": {
                        "media": {
                            "nodes": [
                                {
                                    "id": "book-1",
                                    "path": f"/data/comics/{relative}",
                                }
                            ]
                        }
                    }
                },
            ),
        ]
    )

    async with httpx.AsyncClient() as client:
        link = await resolve_reader_link(
            settings, database, None, "m1", kavita_client=client
        )

    assert link["books"] == [
        {
            "chapter_id": "chapter-1",
            "book_id": "book-1",
            "url": "http://stump.test:10801/books/book-1",
        }
    ]
    assert link["matched_books"] == 1
    assert len(graphql.calls) == 2
    media_request = graphql.calls[1].request.content.decode()
    assert '"id":"series-1"' in media_request


@pytest.mark.asyncio
@respx.mock
async def test_reader_internal_url_is_used_for_the_api_and_browser_url_for_links(
    tmp_path: Path,
):
    settings, database = seed(tmp_path)
    settings.reader_kind = "stump"
    settings.reader_url = "http://192.0.2.50:10801"
    settings.reader_internal_url = "http://stump:10801"
    settings.reader_username = "fixture-user"
    settings.reader_password = "pw"
    respx.post("http://stump:10801/api/v2/auth/login").mock(
        return_value=Response(200, json={"accessToken": "jwt"})
    )
    respx.post("http://stump:10801/api/graphql").mock(
        return_value=Response(
            200,
            json={
                "data": {
                    "series": {
                        "nodes": [
                            {
                                "id": "s1",
                                "name": "Blue Period",
                                "path": "/data/comics/Blue Period (Tsubasa Yamaguchi)",
                                "libraryId": "l",
                            }
                        ]
                    }
                }
            },
        )
    )
    async with httpx.AsyncClient() as client:
        link = await resolve_reader_link(
            settings, database, None, "m1", kavita_client=client
        )
    assert link["available"] is True
    assert link["url"] == "http://192.0.2.50:10801/series/s1"
