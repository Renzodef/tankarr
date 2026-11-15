import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from tankarr.artwork_thumbnails import ArtworkThumbnailCache


async def test_cancelled_renderer_is_killed_and_reaped(monkeypatch, tmp_path):
    process = Mock()
    process.returncode = None
    process.communicate = AsyncMock(side_effect=[asyncio.CancelledError(), (b"", b"")])

    async def spawn(*args, **kwargs):
        output = Path(args[4])
        (output.parent / "partial.tmp").write_bytes(b"partial image")
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(asyncio.CancelledError):
        await ArtworkThumbnailCache._render_in_child(
            tmp_path / "source", tmp_path / "target", 320
        )
    process.kill.assert_called_once()
    assert process.communicate.await_count == 2
    assert list(tmp_path.iterdir()) == []


async def test_duplicate_waiters_share_render_and_release_lock(monkeypatch, tmp_path):
    cache = ArtworkThumbnailCache(tmp_path / "cache")
    entered, finish = asyncio.Event(), asyncio.Event()
    renders = 0

    async def render(source, destination, width):
        nonlocal renders
        renders += 1
        entered.set()
        await finish.wait()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"thumbnail")

    monkeypatch.setattr(cache, "_render_in_child", render)
    first = asyncio.create_task(cache.get(tmp_path / "source", "a" * 64, 320))
    await entered.wait()
    second = asyncio.create_task(cache.get(tmp_path / "source", "a" * 64, 320))
    cancelled = asyncio.create_task(cache.get(tmp_path / "source", "a" * 64, 320))
    await asyncio.sleep(0)
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    assert len(cache._locks) == 1
    finish.set()
    assert await first == await second
    assert renders == 1
    assert cache._locks == {}
    assert cache._users == {}


async def test_failed_render_releases_lock_and_can_retry(monkeypatch, tmp_path):
    cache = ArtworkThumbnailCache(tmp_path)
    monkeypatch.setattr(
        cache, "_render_in_child", AsyncMock(side_effect=ValueError("fixture"))
    )
    for _ in range(2):
        with pytest.raises(ValueError):
            await cache.get(tmp_path / "source", "b" * 64, 320)
        assert not cache._locks
        assert not cache._users
