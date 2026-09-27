from __future__ import annotations

import json

import pytest
import respx
from fastapi.testclient import TestClient
from httpx import Response

from tankarr.app import create_app
from tankarr.config import Settings
from tankarr.prowlarr import ProwlarrClient, ProwlarrError

BASE_URL = "http://prowlarr.test"
INFO_HASH = "d83a5fd49f4a638c93f4e90b085b1415cc7212fd"
MAGNET = f"magnet:?xt=urn:btih:{INFO_HASH}&dn=Example"

INDEXERS = [
    {
        "id": 10,
        "name": "Nyaa.si",
        "enable": True,
        "protocol": "torrent",
        "priority": 15,
        "capabilities": {
            "categories": [
                {
                    "id": 7000,
                    "name": "Books",
                    "subCategories": [
                        {"id": 7020, "name": "Books/EBook", "subCategories": []},
                        {"id": 7030, "name": "Books/Comics", "subCategories": []},
                    ],
                },
                {
                    "id": 156719,
                    "name": "Literature - English-translated",
                    "subCategories": [],
                },
            ]
        },
    },
    {
        "id": 11,
        "name": "Movies only",
        "enable": True,
        "protocol": "torrent",
        "priority": 20,
        "capabilities": {
            "categories": [{"id": 2000, "name": "Movies", "subCategories": []}]
        },
    },
    {
        "id": 12,
        "name": "Disabled books",
        "enable": False,
        "protocol": "usenet",
        "priority": 10,
        "capabilities": {
            "categories": [{"id": 7030, "name": "Books/Comics", "subCategories": []}]
        },
    },
]


@pytest.mark.asyncio
@respx.mock
async def test_probe_discovers_only_book_and_comic_capabilities():
    status = respx.get(f"{BASE_URL}/api/v1/system/status").mock(
        return_value=Response(
            200, json={"version": "2.5.2", "instanceName": "Prowlarr"}
        )
    )
    indexers = respx.get(f"{BASE_URL}/api/v1/indexer").mock(
        return_value=Response(200, json=INDEXERS)
    )
    settings = Settings(
        prowlarr_enabled=True,
        prowlarr_url=BASE_URL,
        prowlarr_api_key="0123456789abcdef0123456789abcdef",
    )

    result = await ProwlarrClient(settings).probe()

    assert status.called and indexers.called
    assert status.calls[0].request.headers["x-api-key"] == settings.prowlarr_api_key
    assert result["version"] == "2.5.2"
    assert result["enabled_indexers"] == 2
    assert result["compatible_indexers"] == 1
    assert [item["name"] for item in result["indexers"]] == [
        "Nyaa.si",
        "Movies only",
        "Disabled books",
    ]
    assert [item["selected"] for item in result["indexers"]] == [True, False, False]
    assert {item["id"] for item in result["categories"]} == {
        7000,
        7020,
        7030,
        156719,
    }
    assert next(item for item in result["categories"] if item["id"] == 7030) == {
        "id": 7030,
        "name": "Comics",
        "indexer_ids": [10, 12],
        "selected": True,
    }


@pytest.mark.asyncio
@respx.mock
async def test_probe_preserves_an_explicit_indexer_selection():
    respx.get(f"{BASE_URL}/api/v1/system/status").mock(
        return_value=Response(200, json={"version": "2.5.2"})
    )
    respx.get(f"{BASE_URL}/api/v1/indexer").mock(
        return_value=Response(200, json=INDEXERS)
    )
    settings = Settings(
        prowlarr_enabled=True,
        prowlarr_url=BASE_URL,
        prowlarr_api_key="0123456789abcdef0123456789abcdef",
        prowlarr_indexer_ids="12",
        prowlarr_categories="156719",
    )

    result = await ProwlarrClient(settings).probe()

    selected = [item["id"] for item in result["indexers"] if item["selected"]]
    assert selected == [12]
    selected_categories = [
        item["id"] for item in result["categories"] if item["selected"]
    ]
    assert selected_categories == [156719]


@pytest.mark.asyncio
@respx.mock
async def test_probe_reports_api_key_rejection_without_leaking_it():
    respx.get(f"{BASE_URL}/api/v1/system/status").mock(
        return_value=Response(401, json={"message": "Unauthorized"})
    )
    settings = Settings(
        prowlarr_enabled=True,
        prowlarr_url=BASE_URL,
        prowlarr_api_key="secret-api-key-value",
    )

    with pytest.raises(ProwlarrError, match="rejected the API key") as caught:
        await ProwlarrClient(settings).probe()

    assert settings.prowlarr_api_key not in str(caught.value)


@pytest.mark.asyncio
async def test_probe_requires_an_enabled_configuration():
    settings = Settings(prowlarr_enabled=False)

    with pytest.raises(ProwlarrError, match="disabled"):
        await ProwlarrClient(settings).probe()


@pytest.mark.parametrize(
    ("title", "categories", "expected"),
    [
        ("One Piece 1191 [JP]", ["Books/Comics"], True),
        ("[超RAW] ONE PIECE 第2巻", ["Books/Comics"], True),
        ("Example v01", ["Literature - Raw"], True),
        ("Example v01 [Digital]", ["Books/Comics"], False),
        ("Raw Hero v01 [Digital]", ["Books/Comics"], False),
    ],
)
def test_explicit_non_english_release_evidence_is_filtered_before_download(
    title: str, categories: list[str], expected: bool
):
    assert ProwlarrClient._clearly_non_english_release(title, categories) is expected


@respx.mock
def test_settings_endpoint_probes_unsaved_prowlarr_configuration(tmp_path):
    internal_url = "http://prowlarr"
    respx.get(f"{internal_url}/api/v1/system/status").mock(
        return_value=Response(200, json={"version": "2.5.2"})
    )
    respx.get(f"{internal_url}/api/v1/indexer").mock(
        return_value=Response(200, json=INDEXERS)
    )
    settings = Settings(
        host="127.0.0.1",
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "library",
        monitor_enabled=False,
        metadata_enabled=False,
    )
    identity = "0123456789abcdef0123456789abcdef"
    settings.data_dir.mkdir(parents=True)
    settings.library_dir.mkdir(parents=True)
    (settings.data_dir / ".tankarr-library-id").write_text(
        f"{identity}\n", encoding="utf-8"
    )
    (settings.library_dir / ".tankarr-library-id").write_text(
        f"{identity}\n", encoding="utf-8"
    )

    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/api/settings/test/prowlarr",
            json={
                "prowlarr_enabled": "true",
                "prowlarr_url": internal_url,
                "prowlarr_api_key": "0123456789abcdef0123456789abcdef",
                "prowlarr_indexer_ids": "10",
                "prowlarr_categories": "7000,7030",
            },
        )

    assert response.status_code == 200, response.text
    assert response.json()["ok"] is True
    assert response.json()["compatible_indexers"] == 1
    assert settings.prowlarr_enabled is False
    assert settings.prowlarr_api_key is None


@pytest.mark.asyncio
@respx.mock
async def test_search_uses_selected_indexers_and_normalizes_torrent_results():
    route = respx.get(f"{BASE_URL}/api/v1/search").mock(
        return_value=Response(
            200,
            json=[
                {
                    "guid": MAGNET,
                    "protocol": "torrent",
                    "infoHash": INFO_HASH,
                    "indexerId": 10,
                    "indexer": "Nyaa.si",
                    "indexerPriority": 15,
                    "title": "The Legend of Kamui v01-02",
                    "categories": [{"id": 7030, "name": "Books/Comics"}],
                    "size": 1024,
                    "seeders": 19,
                    "leechers": 2,
                    "grabs": 727,
                    "publishDate": "2025-12-13T18:15:00Z",
                    "infoUrl": "https://nyaa.si/view/2053711",
                    "indexerFlags": ["freeleech"],
                },
                {
                    "guid": "https://usenet.invalid/release",
                    "protocol": "usenet",
                    "infoHash": INFO_HASH,
                    "indexerId": 10,
                    "indexer": "Unsupported usenet",
                    "title": "Ignored",
                    "categories": [{"id": 7030, "name": "Books/Comics"}],
                },
            ],
        )
    )
    settings = Settings(
        prowlarr_enabled=True,
        prowlarr_url=BASE_URL,
        prowlarr_api_key="secret-api-key",
        prowlarr_indexer_ids="10",
        prowlarr_categories="7030",
    )

    result = await ProwlarrClient(settings).search("Legend of Kamui", limit=25)

    assert len(result) == 1
    assert result[0]["provider"] == "prowlarr"
    assert result[0]["indexer"] == "Nyaa.si"
    assert result[0]["indexer_priority"] == 15
    assert result[0]["id"] == f"10-{INFO_HASH}"
    assert result[0]["volume"] == "1-2"
    assert result[0]["download_ref"] == MAGNET
    assert "secret-api-key" not in json.dumps(result)
    parameters = route.calls[0].request.url.params
    assert parameters.get_list("indexerIds") == ["10"]
    assert parameters.get_list("categories") == ["7030"]
    assert parameters["type"] == "search"


@pytest.mark.asyncio
@respx.mock
async def test_search_strips_api_key_and_resolves_prowlarr_magnet_redirect():
    proxy_url = f"{BASE_URL}/10/download?apikey=secret-api-key&link=opaque&file=Example"
    respx.get(f"{BASE_URL}/api/v1/search").mock(
        return_value=Response(
            200,
            json=[
                {
                    "guid": "https://tracker.invalid/details/1",
                    "magnetUrl": proxy_url,
                    "protocol": "torrent",
                    "infoHash": INFO_HASH,
                    "indexerId": 10,
                    "indexer": "Private tracker",
                    "title": "Example v01",
                    "categories": [{"id": 7030, "name": "Books/Comics"}],
                }
            ],
        )
    )
    download = respx.get(f"{BASE_URL}/10/download?link=opaque&file=Example").mock(
        return_value=Response(301, headers={"Location": MAGNET})
    )
    settings = Settings(
        prowlarr_enabled=True,
        prowlarr_url=BASE_URL,
        prowlarr_api_key="secret-api-key",
        prowlarr_indexer_ids="10",
        prowlarr_categories="7030",
    )
    client = ProwlarrClient(settings)

    result = await client.search("Example")
    kind, payload = await client.resolve_download(
        result[0]["download_ref"], result[0]["info_hash"]
    )

    assert "apikey" not in result[0]["download_ref"].casefold()
    assert kind == "magnet"
    assert payload == MAGNET
    assert download.called
    assert download.calls[0].request.headers["x-api-key"] == "secret-api-key"
