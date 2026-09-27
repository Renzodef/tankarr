from __future__ import annotations

import asyncio

import pytest
import respx
from httpx import Response

from tankarr.providers.base import (
    ProviderHTTP,
    ProviderUnavailableError,
    RateLimiter,
)

SEARCH_HTML = """
<div>
  <a href="/manga/7529/kagurabachi" class="relative block">
    <figure class="w-full h-52 overflow-hidden bg-card rounded-md">
      <img data-src="https://cdn.example.com/file/mangapill/i/7529.jpeg"
           alt="Kagurabachi" class="lazy"/>
    </figure>
  </a>
  <a href="/manga/7529/kagurabachi" class="mb-2">
    <div class="mt-3 font-black leading-tight line-clamp-2">Kagurabachi</div>
  </a>
</div>
"""

MANGA_HTML = """
<h1 class="font-bold text-lg md:text-2xl">Kagurabachi</h1>
<img data-src="https://cdn.example.com/file/mangapill/i/7529.jpeg" alt="cover"/>
<p class="text-secondary text-sm my-3">Chihiro trains under his father.</p>
<div><label class="text-secondary">Type</label><div>manga</div></div>
<div><label class="text-secondary">Status</label><div>finished</div></div>
<div><label class="text-secondary">Year</label><div>2023</div></div>
<div class="my-3 grid">
  <a class="border" href="/chapters/7529-10002000/kagurabachi-chapter-2"
     title="Chapter 2">Chapter 2</a>
  <a class="border" href="/chapters/7529-10001000/kagurabachi-chapter-1"
     title="Chapter 1">Chapter 1</a>
</div>
"""

CHAPTER_HTML = """
<picture><img data-src="https://cdn.example.com/file/mangap/7529/10001000/x/1.jpeg"></picture>
<picture><img data-src="https://cdn.example.com/file/mangap/7529/10001000/x/2.jpeg"></picture>
"""


@pytest.mark.asyncio
async def test_rate_limiter_prioritizes_active_downloads_without_reordering_peers():
    limiter = RateLimiter(requests_per_second=40)
    await limiter.acquire()
    order: list[str] = []

    async def acquire(name: str, priority: int) -> None:
        await limiter.acquire(priority)
        order.append(name)

    low_one = asyncio.create_task(acquire("refresh-1", 20))
    low_two = asyncio.create_task(acquire("refresh-2", 20))
    await asyncio.sleep(0)
    active = asyncio.create_task(acquire("page", 0))
    await asyncio.gather(low_one, low_two, active)

    assert order == ["page", "refresh-1", "refresh-2"]


@pytest.mark.asyncio
@respx.mock
async def test_provider_http_retries_transient_failures_then_succeeds():
    route = respx.get("https://api.example.com/data")
    route.side_effect = [
        Response(429, headers={"Retry-After": "0"}),
        Response(503),
        Response(200, json={"ok": True}),
    ]
    client = ProviderHTTP(requests_per_second=1000, backoff_base_seconds=0.01)
    try:
        payload = await client.get_json("https://api.example.com/data")
    finally:
        await client.aclose()
    assert payload == {"ok": True}
    assert route.call_count == 3


@pytest.mark.asyncio
@respx.mock
async def test_provider_http_gives_up_after_max_attempts():
    respx.get("https://api.example.com/data").mock(return_value=Response(503))
    client = ProviderHTTP(
        requests_per_second=1000, max_attempts=2, backoff_base_seconds=0.01
    )
    try:
        # The final attempt surfaces the real HTTP failure instead of retrying.
        with pytest.raises(ProviderUnavailableError):
            await client.get_json("https://api.example.com/data")
    finally:
        await client.aclose()
