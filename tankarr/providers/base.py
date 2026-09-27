from __future__ import annotations

import abc
import asyncio
import heapq
import math
import random
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import httpx

ProgressCallback = Callable[[int, int], Awaitable[None]]
SearchDiagnostic = dict[str, str]

RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})


class ProviderRequestError(RuntimeError):
    """A provider rejected a request or returned unusable content.

    ``status_code`` carries the last HTTP status when the failure came from a
    response.
    """

    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class ProviderUnavailableError(ProviderRequestError):
    """The provider could not be reached after transport-level retries."""


class RateLimiter:
    """Paced priority queue shared by every request to one provider."""

    def __init__(self, requests_per_second: float):
        self.requests_per_second = max(requests_per_second, 0.1)
        self.interval = 1.0 / self.requests_per_second
        self._lock = asyncio.Lock()
        self._next_slot = 0.0
        self._sequence = 0
        self._waiters: list[tuple[int, int, float, asyncio.Future[float]]] = []
        self._runner: asyncio.Task[None] | None = None

    async def acquire(self, priority: int = 20) -> float:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[float] = loop.create_future()
        async with self._lock:
            self._sequence += 1
            heapq.heappush(
                self._waiters,
                (int(priority), self._sequence, loop.time(), future),
            )
            if self._runner is None or self._runner.done():
                self._runner = asyncio.create_task(
                    self._serve(), name="tankarr-provider-rate-limiter"
                )
        try:
            return await future
        except asyncio.CancelledError:
            future.cancel()
            raise

    async def _serve(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            async with self._lock:
                while self._waiters and self._waiters[0][3].cancelled():
                    heapq.heappop(self._waiters)
                if not self._waiters:
                    self._runner = None
                    return
                wait = max(0.0, self._next_slot - loop.time())
            if wait > 0:
                await asyncio.sleep(wait)
            async with self._lock:
                while self._waiters and self._waiters[0][3].cancelled():
                    heapq.heappop(self._waiters)
                if not self._waiters:
                    continue
                _priority, _sequence, queued_at, future = heapq.heappop(self._waiters)
                now = loop.time()
                self._next_slot = max(now, self._next_slot) + self.interval
            if not future.done():
                future.set_result(max(0.0, now - queued_at))


class ProviderHTTP:
    """Shared HTTP client with rate limiting and retry/backoff for one host."""

    def __init__(
        self,
        *,
        headers: dict[str, str] | None = None,
        auth: httpx.Auth | None = None,
        timeout_seconds: float = 30.0,
        requests_per_second: float = 3.0,
        max_attempts: int = 4,
        backoff_base_seconds: float = 1.0,
        backoff_cap_seconds: float = 30.0,
        follow_redirects: bool = True,
    ):
        self.limiter = RateLimiter(requests_per_second)
        self.max_attempts = max(1, max_attempts)
        self.backoff_base_seconds = backoff_base_seconds
        self.backoff_cap_seconds = backoff_cap_seconds
        self._client = httpx.AsyncClient(
            headers=headers or {},
            auth=auth,
            timeout=httpx.Timeout(timeout_seconds),
            follow_redirects=follow_redirects,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    def _retry_delay(self, attempt: int, response: httpx.Response | None) -> float:
        if response is not None:
            retry_after = response.headers.get("Retry-After", "")
            try:
                delay = float(retry_after)
            except ValueError:
                try:
                    retry_at = parsedate_to_datetime(retry_after)
                    if retry_at.tzinfo is None:
                        retry_at = retry_at.replace(tzinfo=UTC)
                    delay = (retry_at - datetime.now(UTC)).total_seconds()
                except (TypeError, ValueError, OverflowError):
                    delay = math.nan
            if math.isfinite(delay):
                return max(0.0, min(delay, self.backoff_cap_seconds))
        delay = self.backoff_base_seconds * (2**attempt)
        return min(delay, self.backoff_cap_seconds) * (0.5 + random.random() / 2)

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: Any = None,
        headers: dict[str, str] | None = None,
        json: Any = None,
        data: Any = None,
        priority: int = 20,
    ) -> httpx.Response:
        last_error: str = "no attempts were made"
        last_status: int | None = None
        retry_statuses: list[int] = []
        total_rate_wait = 0.0
        for attempt in range(self.max_attempts):
            total_rate_wait += await self.limiter.acquire(priority)
            started = asyncio.get_running_loop().time()
            try:
                response = await self._client.request(
                    method,
                    url,
                    params=params,
                    headers=headers,
                    json=json,
                    data=data,
                )
            except httpx.TransportError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt + 1 < self.max_attempts:
                    await asyncio.sleep(self._retry_delay(attempt, None))
                continue
            network_seconds = asyncio.get_running_loop().time() - started
            if response.status_code in RETRYABLE_STATUS_CODES:
                last_error = f"HTTP {response.status_code} from {url}"
                last_status = response.status_code
                if attempt + 1 < self.max_attempts:
                    retry_statuses.append(response.status_code)
                    await asyncio.sleep(self._retry_delay(attempt, response))
                    continue
                raise ProviderUnavailableError(
                    f"{method} {url} failed after retries: {last_error}",
                    status_code=last_status,
                )
            response.raise_for_status()
            response.extensions["tankarr_network_seconds"] = network_seconds
            response.extensions["tankarr_rate_wait_seconds"] = total_rate_wait
            response.extensions["tankarr_retry_statuses"] = tuple(retry_statuses)
            return response
        raise ProviderUnavailableError(
            f"{method} {url} failed after retries: {last_error}",
            status_code=last_status,
        )

    async def get_json(
        self,
        url: str,
        *,
        params: Any = None,
        headers: dict[str, str] | None = None,
        priority: int = 20,
    ) -> Any:
        response = await self.request(
            "GET", url, params=params, headers=headers, priority=priority
        )
        return response.json()

    async def get_text(
        self,
        url: str,
        *,
        params: Any = None,
        headers: dict[str, str] | None = None,
        priority: int = 20,
    ) -> str:
        response = await self.request(
            "GET", url, params=params, headers=headers, priority=priority
        )
        return response.text

    async def download_file(
        self,
        url: str,
        destination: Path,
        *,
        headers: dict[str, str] | None = None,
        priority: int = 10,
    ) -> Path:
        """Stream one file to disk atomically, retrying transient failures."""

        partial = destination.with_suffix(destination.suffix + ".part")
        last_error = "no attempts were made"
        try:
            for attempt in range(self.max_attempts):
                await self.limiter.acquire(priority)
                try:
                    retry_delay: float | None = None
                    async with self._client.stream(
                        "GET", url, headers=headers
                    ) as response:
                        if response.status_code in RETRYABLE_STATUS_CODES:
                            last_error = f"HTTP {response.status_code} from {url}"
                            if attempt + 1 >= self.max_attempts:
                                raise ProviderUnavailableError(
                                    f"GET {url} failed after retries: {last_error}",
                                    status_code=response.status_code,
                                )
                            retry_delay = self._retry_delay(attempt, response)
                        else:
                            response.raise_for_status()
                            with partial.open("wb") as handle:
                                async for chunk in response.aiter_bytes(256 * 1024):
                                    handle.write(chunk)
                    if retry_delay is not None:
                        # Release the HTTP connection before waiting, so other
                        # transfers can proceed during provider backoff.
                        await asyncio.sleep(retry_delay)
                        continue
                    if partial.stat().st_size == 0:
                        raise ProviderRequestError(f"Empty file returned for {url}")
                    partial.replace(destination)
                    return destination
                except httpx.TransportError as exc:
                    last_error = f"{type(exc).__name__}: {exc}"
                    partial.unlink(missing_ok=True)
                    if attempt + 1 < self.max_attempts:
                        await asyncio.sleep(self._retry_delay(attempt, None))
            raise ProviderUnavailableError(
                f"GET {url} failed after retries: {last_error}"
            )
        finally:
            # Covers cancellation, invalid content, HTTP errors and failed
            # atomic replacement as well as transport errors. Never alter an
            # existing destination until a complete nonempty transfer succeeds.
            partial.unlink(missing_ok=True)


class Provider(abc.ABC):
    """A manga source able to search titles, list releases, and fetch pages."""

    name: str = "provider"
    label: str = "Provider"
    search_mode: str = "title"
    languages: frozenset[str] | None = None  # None means any language.

    def supports_language(self, language: str) -> bool:
        return self.languages is None or language.lower() in self.languages

    @abc.abstractmethod
    async def search(
        self, query: str, language: str, limit: int = 20
    ) -> list[dict[str, Any]]: ...

    async def search_with_diagnostics(
        self, query: str, language: str, limit: int = 20
    ) -> tuple[list[dict[str, Any]], list[SearchDiagnostic]]:
        """Search while preserving optional source-level partial failures."""

        return await self.search(query, language, limit), []

    async def search_catalogue_with_diagnostics(
        self, query: str, language: str, limit: int = 20
    ) -> tuple[list[dict[str, Any]], list[SearchDiagnostic]]:
        """Search identities even when the selected translation is unavailable.

        Most download providers expose only language-specific catalogues, so their
        normal search remains the safest fallback. Providers with an independent
        work catalogue can override this for release-first discovery workflows.
        """

        return await self.search_with_diagnostics(query, language, limit)

    @abc.abstractmethod
    async def get_manga(self, manga_id: str) -> dict[str, Any]: ...

    @abc.abstractmethod
    async def list_chapters(
        self, manga_id: str, language: str
    ) -> list[dict[str, Any]]: ...

    @abc.abstractmethod
    async def get_cover(self, manga_id: str, filename: str) -> tuple[bytes, str]: ...

    @abc.abstractmethod
    async def download_pages(
        self,
        chapter_id: str,
        target_dir: Path,
        progress: ProgressCallback,
        concurrency: int = 4,
    ) -> list[Path]: ...

    async def aclose(self) -> None:
        return None

    async def healthcheck(self) -> dict[str, Any]:
        await self.search("one", "en", limit=1)
        return {"ok": True}
