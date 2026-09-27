from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
import respx
from httpx import Request, Response
from pydantic import ValidationError

from tankarr.config import Settings
from tankarr.providers import build_providers
from tankarr.providers.base import ProviderRequestError, ProviderUnavailableError
from tankarr.providers.suwayomi import SuwayomiProvider

BASE_URL = "https://suwayomi.test"


def graphql_response(request: Request) -> Response:
    payload = json.loads(request.content)
    query = payload["query"]
    variables = payload["variables"]
    assert request.headers["authorization"].startswith("Basic ")
    if "TankarrSources" in query:
        return Response(
            200,
            json={
                "data": {
                    "sources": {
                        "nodes": [
                            {
                                "id": "101",
                                "name": "MangaDex",
                                "displayName": "MangaDex (EN)",
                                "lang": "en",
                                "contentWarning": "SAFE",
                                "homeUrl": "https://mangadex.org",
                            },
                            {
                                "id": "102",
                                "name": "Blocked source",
                                "displayName": "Blocked source (EN)",
                                "lang": "en",
                                "contentWarning": "SAFE",
                                "homeUrl": "https://blocked.test",
                            },
                        ]
                    }
                }
            },
        )
    if "TankarrSearch" in query:
        assert variables["input"]["source"] == "101"
        assert variables["input"]["query"] == "Example"
        return Response(
            200,
            json={
                "data": {
                    "fetchSourceManga": {
                        "mangas": [
                            {
                                "id": 7,
                                "sourceId": "101",
                                "title": "Example Manga",
                                "thumbnailUrl": "/api/v1/manga/7/thumbnail",
                                "author": "Example Author",
                                "artist": "Example Artist",
                                "description": "An example.",
                                "status": "ONGOING",
                                "realUrl": "https://source.test/example",
                            }
                        ],
                        "hasNextPage": False,
                    }
                }
            },
        )
    if "TankarrManga" in query:
        assert variables["input"] == {
            "id": 7,
            "fetchManga": True,
            "fetchChapters": True,
        }
        return Response(
            200,
            json={
                "data": {
                    "fetchMangaAndChapters": {
                        "manga": {
                            "id": 7,
                            "sourceId": "101",
                            "title": "Example Manga",
                            "thumbnailUrl": "/api/v1/manga/7/thumbnail",
                            "author": "Example Author",
                            "artist": "Example Artist",
                            "description": "An example.",
                            "status": "ONGOING",
                            "realUrl": "https://source.test/example",
                            "source": {
                                "id": "101",
                                "name": "MangaDex",
                                "displayName": "MangaDex (EN)",
                                "lang": "en",
                                "homeUrl": "https://mangadex.org",
                            },
                        },
                        "chapters": [
                            {
                                "id": 70,
                                "name": "Chapter 10",
                                "uploadDate": 1_767_225_600_000,
                                "chapterNumber": 10.0,
                                "scanlator": "Example Group",
                                "realUrl": "https://source.test/chapter/10",
                                "pageCount": 2,
                            },
                            {
                                "id": 69,
                                "name": "Chapter 9.5",
                                "uploadDate": 1_767_139_200_000,
                                "chapterNumber": 9.5,
                                "scanlator": None,
                                "realUrl": None,
                                "pageCount": 2,
                            },
                        ],
                    }
                }
            },
        )
    if "TankarrPages" in query:
        assert variables["input"] == {"chapterId": 70}
        return Response(
            200,
            json={
                "data": {
                    "fetchChapterPages": {
                        "pages": [
                            "/api/v1/manga/7/chapter/0/page/0",
                            "/api/v1/manga/7/chapter/0/page/1",
                        ],
                        "chapter": {"id": 70, "pageCount": 2},
                    }
                }
            },
        )
    raise AssertionError(f"Unexpected GraphQL operation: {query}")


def provider() -> SuwayomiProvider:
    return SuwayomiProvider(
        BASE_URL,
        username="tankarr",
        password="secret",
        source_ids={101},
    )


@pytest.mark.asyncio
async def test_settings_register_suwayomi_with_validated_source_ids(tmp_path: Path):
    settings = Settings(
        data_dir=tmp_path,
        suwayomi_enabled=True,
        suwayomi_mode="external",
        suwayomi_url="http://suwayomi:4567/",
        suwayomi_username="tankarr",
        suwayomi_password="secret",
        suwayomi_source_ids="101, 202,101",
    )
    providers = build_providers(settings)
    try:
        configured = providers["suwayomi"]
        assert isinstance(configured, SuwayomiProvider)
        assert configured.base_url == "http://suwayomi:4567/"
        assert configured.source_ids == frozenset({101, 202})
        assert configured.search_timeout_seconds == 30.0
    finally:
        for item in providers.values():
            await item.aclose()


@pytest.mark.parametrize(
    "changes",
    [
        {"suwayomi_url": "file:///etc/passwd"},
        {"suwayomi_url": "https://user:secret@suwayomi.test"},
        {"suwayomi_username": "user"},
        {"suwayomi_source_ids": "101,not-an-id"},
    ],
)
def test_settings_reject_invalid_suwayomi_configuration(changes):
    with pytest.raises(ValidationError):
        Settings(**changes)


@pytest.mark.asyncio
@respx.mock
async def test_search_uses_only_allowed_installed_english_sources():
    graphql = respx.post(f"{BASE_URL}/api/graphql").mock(side_effect=graphql_response)
    client = provider()
    try:
        results = await client.search("Example", "en")
    finally:
        await client.aclose()

    assert graphql.call_count == 2
    assert len(results) == 1
    assert results[0] == {
        "id": "suwayomi-7",
        "provider": "suwayomi",
        "title": "Example Manga",
        "description": "An example.",
        "cover_url": "/api/covers/suwayomi/suwayomi-7/cover",
        "authors": ["Example Author", "Example Artist"],
        "original_language": None,
        "status": "ongoing",
        "year": None,
        "last_volume": None,
        "last_chapter": None,
        "available_languages": ["en"],
        "source_url": "https://source.test/example",
        "source_name": "MangaDex (EN)",
        "source_id": "101",
        "preferred_language": "en",
    }


@pytest.mark.asyncio
@respx.mock
async def test_healthcheck_reports_allowed_source_count_without_remote_search():
    graphql = respx.post(f"{BASE_URL}/api/graphql").mock(side_effect=graphql_response)
    client = provider()
    try:
        result = await client.healthcheck()
    finally:
        await client.aclose()

    assert result == {
        "ok": True,
        "language": "en",
        "sources": 1,
        "source_details": [
            {
                "id": "101",
                "name": "MangaDex (EN)",
                "language": "en",
                "content_warning": "SAFE",
                "selected": True,
                "allowed": True,
                "enabled": True,
            },
            {
                "id": "102",
                "name": "Blocked source (EN)",
                "language": "en",
                "content_warning": "SAFE",
                "selected": False,
                "allowed": True,
                "enabled": False,
            },
        ],
    }
    assert graphql.call_count == 1


@pytest.mark.asyncio
async def test_source_catalog_covers_languages_and_exposes_safety_state(monkeypatch):
    client = SuwayomiProvider(
        BASE_URL,
        username="tankarr",
        password="secret",
        source_ids={101, 201},
    )

    async def installed_sources(language):
        if language == "en":
            return [
                {
                    "id": "101",
                    "displayName": "MangaDex (EN)",
                    "lang": "en",
                    "contentWarning": "MIXED",
                },
                {
                    "id": "102",
                    "displayName": "Other (EN)",
                    "lang": "en",
                    "contentWarning": "SAFE",
                },
            ]
        assert language == "it"
        return [
            {
                "id": "201",
                "displayName": "Adult (IT)",
                "lang": "it",
                "contentWarning": "NSFW",
            }
        ]

    monkeypatch.setattr(client, "_installed_sources", installed_sources)
    try:
        result = await client.source_catalog(["en", "it", "en"])
    finally:
        await client.aclose()

    # An installed NSFW extension is an operator decision: enabled like any other.
    assert [(item["id"], item["enabled"]) for item in result] == [
        ("101", True),
        ("201", True),
        ("102", False),
    ]
    assert result[1]["selected"] is True
    assert result[1]["allowed"] is True


@pytest.mark.asyncio
@respx.mock
async def test_search_queries_installed_suwayomi_sources_in_selected_language():
    def italian_graphql(request: Request) -> Response:
        payload = json.loads(request.content)
        if "TankarrSources" in payload["query"]:
            assert payload["variables"] == {"language": "it"}
            return Response(
                200,
                json={
                    "data": {
                        "sources": {
                            "nodes": [
                                {
                                    "id": "201",
                                    "name": "MangaWorld",
                                    "displayName": "MangaWorld (IT)",
                                    "lang": "it",
                                    "contentWarning": "SAFE",
                                    "homeUrl": "https://example.test",
                                }
                            ]
                        }
                    }
                },
            )
        assert "TankarrSearch" in payload["query"]
        assert payload["variables"]["input"]["source"] == "201"
        return Response(
            200,
            json={
                "data": {
                    "fetchSourceManga": {
                        "mangas": [
                            {
                                "id": 27,
                                "sourceId": "201",
                                "title": "Esempio",
                            }
                        ]
                    }
                }
            },
        )

    graphql = respx.post(f"{BASE_URL}/api/graphql").mock(side_effect=italian_graphql)
    client = SuwayomiProvider(BASE_URL, username="tankarr", password="secret")
    try:
        results = await client.search("Esempio", "it")
    finally:
        await client.aclose()

    assert graphql.call_count == 2
    assert results[0]["provider"] == "suwayomi"
    assert results[0]["source_name"] == "MangaWorld (IT)"
    assert results[0]["preferred_language"] == "it"


@pytest.mark.asyncio
@respx.mock
async def test_simple_login_cookie_is_created_after_api_unauthorized():
    def authenticated_graphql(request: Request) -> Response:
        if "JSESSIONID=tankarr-session" not in request.headers.get("cookie", ""):
            return Response(401, text="Unauthorized")
        return graphql_response(request)

    graphql = respx.post(f"{BASE_URL}/api/graphql").mock(
        side_effect=authenticated_graphql
    )
    login = respx.post(f"{BASE_URL}/login.html").mock(
        return_value=Response(
            303,
            headers={
                "location": "/",
                "set-cookie": "JSESSIONID=tankarr-session; Path=/; HttpOnly",
            },
        )
    )
    client = provider()
    try:
        results = await client.search("Example", "en")
    finally:
        await client.aclose()

    assert [item["title"] for item in results] == ["Example Manga"]
    assert login.call_count == 1
    assert graphql.call_count == 3  # rejected request, sources retry, search
    login_request = login.calls[0].request
    assert login_request.url.params["redirect"] == "/"
    assert login_request.content == b"user=tankarr&pass=secret"


@pytest.mark.asyncio
@respx.mock
async def test_simple_login_cookie_is_created_after_graphql_unauthorized_error():
    def authenticated_graphql(request: Request) -> Response:
        if "JSESSIONID=tankarr-session" not in request.headers.get("cookie", ""):
            return Response(
                200,
                json={"data": None, "errors": [{"message": "Unauthorized"}]},
            )
        return graphql_response(request)

    graphql = respx.post(f"{BASE_URL}/api/graphql").mock(
        side_effect=authenticated_graphql
    )
    login = respx.post(f"{BASE_URL}/login.html").mock(
        return_value=Response(
            303,
            headers={
                "location": "/",
                "set-cookie": "JSESSIONID=tankarr-session; Path=/; HttpOnly",
            },
        )
    )
    client = provider()
    try:
        results = await client.search("Example", "en")
    finally:
        await client.aclose()

    assert [item["title"] for item in results] == ["Example Manga"]
    assert login.call_count == 1
    assert graphql.call_count == 3


@pytest.mark.asyncio
@respx.mock
async def test_simple_login_rejects_invalid_credentials_cleanly():
    respx.post(f"{BASE_URL}/api/graphql").mock(
        return_value=Response(401, text="Unauthorized")
    )
    login = respx.post(f"{BASE_URL}/login.html").mock(
        return_value=Response(200, text="Invalid username or password")
    )
    client = provider()
    try:
        with pytest.raises(
            ProviderRequestError, match="rejected.*username or password"
        ):
            await client.search("Example", "en")
    finally:
        await client.aclose()

    assert login.call_count == 1


@pytest.mark.asyncio
async def test_search_round_robins_results_from_every_source(monkeypatch):
    client = provider()
    sources = [
        {
            "id": "101",
            "name": "First",
            "displayName": "First (EN)",
            "homeUrl": "https://first.test",
        },
        {
            "id": "102",
            "name": "Second",
            "displayName": "Second (EN)",
            "homeUrl": "https://second.test",
        },
    ]

    async def installed_sources(language):
        assert language == "en"
        return sources

    async def graphql(_query, variables):
        source_id = variables["input"]["source"]
        offset = 0 if source_id == "101" else 100
        return {
            "fetchSourceManga": {
                "mangas": [
                    {
                        "id": offset + index,
                        "sourceId": source_id,
                        "title": f"{source_id}-{index}",
                    }
                    for index in (1, 2, 3)
                ]
            }
        }

    monkeypatch.setattr(client, "_sources", installed_sources)
    monkeypatch.setattr(client, "_graphql", graphql)
    try:
        results = await client.search("Example", "en", limit=4)
    finally:
        await client.aclose()

    assert [item["source_name"] for item in results] == [
        "First (EN)",
        "Second (EN)",
        "First (EN)",
        "Second (EN)",
    ]


@pytest.mark.asyncio
async def test_search_bounds_a_stalled_source(monkeypatch):
    client = SuwayomiProvider(
        BASE_URL,
        username="tankarr",
        password="secret",
        search_timeout_seconds=0.01,
    )

    async def installed_sources(language):
        assert language == "en"
        return [{"id": "101", "displayName": "Stalled (EN)"}]

    async def stalled_graphql(_query, _variables):
        await asyncio.sleep(1)
        raise AssertionError("the source timeout should cancel this request")

    monkeypatch.setattr(client, "_sources", installed_sources)
    monkeypatch.setattr(client, "_graphql", stalled_graphql)
    try:
        with pytest.raises(ProviderRequestError, match="Stalled.*exceeded"):
            await client.search("Example", "en")
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_search_reports_a_stalled_source_when_another_source_succeeds(
    monkeypatch,
):
    client = SuwayomiProvider(
        BASE_URL,
        username="tankarr",
        password="secret",
        search_timeout_seconds=0.01,
    )
    sources = [
        {"id": "101", "displayName": "Healthy (EN)"},
        {"id": "102", "displayName": "Stalled (EN)"},
    ]

    async def installed_sources(language):
        assert language == "en"
        return sources

    async def graphql(_query, variables):
        if variables["input"]["source"] == "102":
            await asyncio.sleep(1)
            raise AssertionError("the source timeout should cancel this request")
        return {
            "fetchSourceManga": {
                "mangas": [{"id": 7, "sourceId": "101", "title": "Available Manga"}]
            }
        }

    monkeypatch.setattr(client, "_sources", installed_sources)
    monkeypatch.setattr(client, "_graphql", graphql)
    try:
        results, diagnostics = await client.search_with_diagnostics("Example", "en")
    finally:
        await client.aclose()

    assert [item["title"] for item in results] == ["Available Manga"]
    assert diagnostics == [
        {"provider": "Stalled (EN)", "error": "Search exceeded 0.01s"}
    ]


def test_graphql_error_summary_strips_backend_stack_trace():
    summary = SuwayomiProvider._graphql_error_summary(
        {
            "message": (
                "Exception while fetching data: HTTP error 500\r\n\r\n"
                "java.io.IOException: internal detail\n\tat private.backend.Call"
            )
        }
    )

    assert summary == "Exception while fetching data: HTTP error 500"


@pytest.mark.asyncio
@respx.mock
async def test_detail_chapters_cover_and_pages_use_tankarr_pipeline(tmp_path: Path):
    respx.post(f"{BASE_URL}/api/graphql").mock(side_effect=graphql_response)
    respx.get(f"{BASE_URL}/api/v1/manga/7/thumbnail").mock(
        return_value=Response(
            200, content=b"cover", headers={"content-type": "image/webp"}
        )
    )
    respx.get(f"{BASE_URL}/api/v1/manga/7/chapter/0/page/0").mock(
        return_value=Response(
            200, content=_image_bytes("JPEG"), headers={"content-type": "image/jpeg"}
        )
    )
    respx.get(f"{BASE_URL}/api/v1/manga/7/chapter/0/page/1").mock(
        return_value=Response(
            200, content=_image_bytes("PNG"), headers={"content-type": "image/png"}
        )
    )
    client = provider()
    try:
        manga = await client.get_manga("suwayomi-7")
        chapters = await client.list_chapters("suwayomi-7", "en")
        cover, content_type = await client.get_cover("suwayomi-7", "cover")
        updates: list[tuple[int, int]] = []

        async def progress(done: int, total: int) -> None:
            updates.append((done, total))

        pages = await client.download_pages("suwayomi-70", tmp_path, progress)
    finally:
        await client.aclose()

    assert manga["source_name"] == "MangaDex (EN)"
    assert [chapter["chapter"] for chapter in chapters] == ["9.5", "10"]
    assert [chapter["id"] for chapter in chapters] == [
        "suwayomi-69",
        "suwayomi-70",
    ]
    assert chapters[1]["groups"] == ["Example Group"]
    assert chapters[1]["language"] == "en"
    assert cover == b"cover"
    assert content_type == "image/webp"
    assert [page.name for page in pages] == ["0001.jpg", "0002.png"]
    assert [page.read_bytes() for page in pages] == [
        _image_bytes("JPEG"),
        _image_bytes("PNG"),
    ]
    assert sorted(updates) == [(1, 2), (2, 2)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("runtime_responding", "expected", "message"),
    [
        (True, ProviderRequestError, "source timed out"),
        (False, ProviderUnavailableError, "runtime stopped responding"),
    ],
)
async def test_page_prepare_timeout_classifies_source_and_runtime_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runtime_responding: bool,
    expected: type[ProviderRequestError],
    message: str,
):
    client = provider()
    client.page_prepare_timeout_seconds = 0.01

    async def stalled(*_args, **_kwargs):
        await asyncio.sleep(1)

    async def probe() -> bool:
        return runtime_responding

    async def progress(_done: int, _total: int) -> None:
        return None

    monkeypatch.setattr(client, "_graphql", stalled)
    monkeypatch.setattr(client, "_runtime_responding", probe)
    try:
        with pytest.raises(expected, match=message):
            await client.download_pages("suwayomi-70", tmp_path, progress)
    finally:
        await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime_responding", [True, False])
async def test_page_http_failure_distinguishes_source_from_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runtime_responding: bool,
):
    client = provider()

    async def pages(*_args, **_kwargs):
        return {"fetchChapterPages": {"pages": ["/page/1"]}}

    async def unavailable(*_args, **_kwargs):
        raise ProviderUnavailableError("HTTP 500 after retries", status_code=500)

    async def probe() -> bool:
        return runtime_responding

    async def progress(_done: int, _total: int) -> None:
        return None

    monkeypatch.setattr(client, "_graphql", pages)
    monkeypatch.setattr(client, "_request", unavailable)
    monkeypatch.setattr(client, "_runtime_responding", probe)
    try:
        with pytest.raises(ProviderRequestError) as caught:
            await client.download_pages("suwayomi-70", tmp_path, progress)
    finally:
        await client.aclose()

    assert type(caught.value) is (
        ProviderRequestError if runtime_responding else ProviderUnavailableError
    )


@pytest.mark.asyncio
@respx.mock
async def test_graphql_errors_fail_closed():
    respx.post(f"{BASE_URL}/api/graphql").mock(
        return_value=Response(200, json={"data": None, "errors": [{"message": "no"}]})
    )
    client = provider()
    try:
        with pytest.raises(ProviderRequestError, match="no"):
            await client.search("Example", "en")
    finally:
        await client.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_graphql_rate_limit_is_temporary_source_failure():
    respx.post(f"{BASE_URL}/api/graphql").mock(
        return_value=Response(
            200,
            json={
                "data": None,
                "errors": [
                    {
                        "message": "Exception while fetching data: API rate limit excessed!"
                    }
                ],
            },
        )
    )
    client = provider()
    try:
        with pytest.raises(ProviderUnavailableError, match="rate limited"):
            await client.search("Example", "en")
    finally:
        await client.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_page_proxy_never_leaks_basic_auth_to_an_external_origin(
    tmp_path: Path,
):
    def response(request: Request) -> Response:
        payload = json.loads(request.content)
        if "TankarrPages" in payload["query"]:
            return Response(
                200,
                json={
                    "data": {
                        "fetchChapterPages": {
                            "pages": ["https://attacker.test/page.jpg"],
                            "chapter": {"id": 70, "pageCount": 1},
                        }
                    }
                },
            )
        raise AssertionError("Unexpected operation")

    respx.post(f"{BASE_URL}/api/graphql").mock(side_effect=response)
    external = respx.get("https://attacker.test/page.jpg").mock(
        return_value=Response(200, content=b"bad")
    )
    client = provider()

    async def progress(_: int, __: int) -> None:
        return None

    try:
        with pytest.raises(ProviderRequestError, match="outside"):
            await client.download_pages("suwayomi-70", tmp_path, progress)
    finally:
        await client.aclose()

    assert external.call_count == 0


def test_instance_token_namespaces_ids_and_rejects_previous_instances():
    from tankarr.providers.suwayomi import (
        StaleSuwayomiIdentity,
        SuwayomiProvider,
        suwayomi_id_is_current,
    )

    client = SuwayomiProvider("http://suwayomi:4567", instance_token="abcd1234")
    assert client._public_id("7") == "suwayomi-abcd1234-7"
    assert client._numeric_id("suwayomi-abcd1234-7", "manga") == 7
    with pytest.raises(StaleSuwayomiIdentity):
        client._numeric_id("suwayomi-7", "manga")  # legacy container id
    with pytest.raises(StaleSuwayomiIdentity):
        client._numeric_id("suwayomi-ffff0000-7", "chapter")
    assert suwayomi_id_is_current("suwayomi-abcd1234-7", "abcd1234")
    assert not suwayomi_id_is_current("suwayomi-7", "abcd1234")
    assert suwayomi_id_is_current("suwayomi-7", None)
    # Without a token the legacy format is kept (tests and old deployments).
    legacy = SuwayomiProvider("http://suwayomi:4567")
    assert legacy._public_id("7") == "suwayomi-7"
    assert legacy._numeric_id("suwayomi-abcd1234-7", "manga") == 7


def _bare_provider() -> SuwayomiProvider:
    return SuwayomiProvider(BASE_URL, username="u", password="p", instance_token=None)


def test_parse_chapter_treats_a_bare_volume_name_as_a_whole_book():
    """Weeb Central lists Nana as "Volume 1".."Volume 21": books, not chapters."""

    provider = _bare_provider()
    release = provider._parse_chapter(
        {"id": 7, "name": "Volume 21", "chapterNumber": 21, "pageCount": 188},
        language="en",
        source={"id": "5", "displayName": "Weeb Central (EN)"},
    )
    assert release["release_unit"] == "volume"
    assert release["volume"] == "21"
    assert release["chapter"] is None
    assert release["source_chapter"] is None
    assert release["canonical_chapter"] is None

    subtitled = provider._parse_chapter(
        {"id": 8, "name": "Vol. 2: The Storm", "chapterNumber": 2},
        language="en",
        source={"id": "5"},
    )
    assert subtitled["release_unit"] == "volume"
    assert subtitled["volume"] == "2"
    assert subtitled["chapter"] is None


def test_parse_chapter_keeps_chapter_numbers_when_the_name_names_a_chapter():
    provider = _bare_provider()
    for name, volume, chapter, raw_number in (
        ("Vol.3 Ch.20", "3", "20", 20.0),
        ("Chapter 534 : V.37 C.13", "37", "534", 534),
        ("Chapter 563 : v.039 c10", "39", "563", 563),
        ("Chapter 20: version 2", None, "20", 20),
        ("Chapter 84", None, "84", 84),
        ("Volume 7 Chapter 22: Reunion", "7", "22", "22"),
        ("Komatsu Nana", None, "1", 1.0),
        ("Chapter 10.5", None, "10.5", 10.5),
    ):
        release = provider._parse_chapter(
            {"id": 9, "name": name, "chapterNumber": raw_number},
            language="en",
            source={"id": "5"},
        )
        assert release["release_unit"] == "chapter", name
        assert release["volume"] == volume, name
        assert release["chapter"] == chapter, name
        assert release["source_chapter"] == chapter, name


def _image_bytes(fmt="PNG"):
    from io import BytesIO

    from PIL import Image

    out = BytesIO()
    Image.new("RGB", (10, 10), "white").save(out, format=fmt)
    return out.getvalue()


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize("recovers", [True, False])
async def test_corrupt_page_retries_only_that_page(tmp_path, recovers):
    respx.post(f"{BASE_URL}/api/graphql").mock(side_effect=graphql_response)

    def bad():
        return Response(
            200, content=b"<html>broken</html>", headers={"content-type": "image/png"}
        )

    def good():
        return Response(
            200, content=_image_bytes(), headers={"content-type": "image/png"}
        )

    first = respx.get(f"{BASE_URL}/api/v1/manga/7/chapter/0/page/0").mock(
        side_effect=[bad(), good()] if recovers else [bad(), bad(), bad()]
    )
    second = respx.get(f"{BASE_URL}/api/v1/manga/7/chapter/0/page/1").mock(
        return_value=good()
    )

    async def progress(*_):
        pass

    client = provider()
    try:
        if recovers:
            pages = await client.download_pages("suwayomi-70", tmp_path, progress)
            assert len(pages) == 2
            assert first.call_count == 2
        else:
            with pytest.raises(ProviderRequestError, match="after 3 attempts"):
                await client.download_pages("suwayomi-70", tmp_path, progress)
            assert first.call_count == 3
            assert not (tmp_path / "0001.png").exists()
        assert second.call_count == 1
    finally:
        await client.aclose()
