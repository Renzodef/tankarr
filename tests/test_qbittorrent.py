from __future__ import annotations

import pytest
import respx
from httpx import Response

from tankarr.config import Settings
from tankarr.qbittorrent import QBitTorrentClient, QBitTorrentError

BASE_URL = "http://qbittorrent.test"
INFO_HASH = "d83a5fd49f4a638c93f4e90b085b1415cc7212fd"
MAGNET = f"magnet:?xt=urn:btih:{INFO_HASH}&dn=Example"


def settings() -> Settings:
    return Settings(
        qbittorrent_url=BASE_URL,
        qbittorrent_username="tankarr",
        qbittorrent_password="test-password",
        qbittorrent_category="tankarr",
        qbittorrent_save_path="/data/downloads/tankarr",
    )


def mock_session() -> None:
    respx.post(f"{BASE_URL}/api/v2/auth/login").mock(
        return_value=Response(200, text="Ok.")
    )
    respx.get(f"{BASE_URL}/api/v2/torrents/categories").mock(
        return_value=Response(
            200,
            json={"tankarr": {"savePath": "/data/downloads/tankarr"}},
        )
    )


@pytest.mark.asyncio
@respx.mock
async def test_add_magnet_accepts_qbittorrent_json_success_response():
    mock_session()
    added = respx.post(f"{BASE_URL}/api/v2/torrents/add").mock(
        return_value=Response(
            200,
            json={
                "added_torrent_ids": [INFO_HASH],
                "failure_count": 0,
                "pending_count": 0,
                "success_count": 1,
            },
        )
    )

    await QBitTorrentClient(settings()).add_magnet(
        MAGNET,
        release_id=f"10-{INFO_HASH}",
        expected_hash=INFO_HASH,
        paused=True,
    )

    assert added.called
    assert b"stopped=true" in added.calls[0].request.content


@pytest.mark.asyncio
@respx.mock
async def test_add_magnet_rejects_json_success_for_a_different_hash():
    mock_session()
    respx.post(f"{BASE_URL}/api/v2/torrents/add").mock(
        return_value=Response(
            200,
            json={
                "added_torrent_ids": ["a" * 40],
                "failure_count": 0,
                "success_count": 1,
            },
        )
    )

    with pytest.raises(QBitTorrentError, match="rejected the magnet"):
        await QBitTorrentClient(settings()).add_magnet(
            MAGNET,
            release_id=f"10-{INFO_HASH}",
            expected_hash=INFO_HASH,
            paused=True,
        )
