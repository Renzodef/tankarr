from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from tankarr.providers.base import ProviderRequestError, ProviderUnavailableError
from tankarr.service import TankarrService
from tankarr.worker import DownloadWorker
from tests.test_deletion import chapter, make_service, manga
from tests.test_suwayomi import _image_bytes, provider


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [None, "bytes", "manifest"])
async def test_retry_reuses_only_verified_pages_from_the_same_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str | None
):
    client = provider()
    urls = ["/page/1?token=fixture-secret", "/page/2"]
    calls: list[str] = []
    fail_second = True
    expected_image = _image_bytes("PNG")

    async def pages(*_args, **_kwargs):
        return {"fetchChapterPages": {"pages": list(urls)}}

    async def request(_method, url, **_kwargs):
        calls.append(url)
        if url.endswith("/page/2") and fail_second:
            raise ProviderUnavailableError("temporary HTTP 503", status_code=503)
        return httpx.Response(
            200, content=expected_image, headers={"content-type": "image/png"}
        )

    async def probe():
        return True

    updates: list[tuple[int, int]] = []

    async def progress(done, total):
        updates.append((done, total))

    monkeypatch.setattr(client, "_graphql", pages)
    monkeypatch.setattr(client, "_request", request)
    monkeypatch.setattr(client, "_runtime_responding", probe)
    try:
        with pytest.raises(ProviderRequestError, match="page 2"):
            await client.download_pages(
                "suwayomi-70", tmp_path, progress, concurrency=1
            )
        assert (tmp_path / "0001.png").read_bytes() == expected_image
        assert (
            b"fixture-secret"
            not in (tmp_path / ".tankarr-page-cache.json").read_bytes()
        )
        if change == "bytes":
            # A different but valid image cannot stand in for the cached page.
            (tmp_path / "0001.png").write_bytes(_image_bytes("JPEG"))
        elif change == "manifest":
            urls[0] = "/page/changed?token=fixture-secret"
        fail_second = False
        updates.clear()
        result = await client.download_pages(
            "suwayomi-70", tmp_path, progress, concurrency=1
        )
    finally:
        await client.aclose()

    assert [path.name for path in result] == ["0001.png", "0002.png"]
    assert all(path.read_bytes() == expected_image for path in result)
    assert len(calls) == (3 if change is None else 4)
    assert updates == [(1, 2), (2, 2)]


class InterruptedProvider:
    def __init__(self, status: int | None, transport_failure: bool = False):
        self.status = status
        self.transport_failure = transport_failure

    async def download_pages(self, _chapter, directory, _progress, concurrency):
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "0001.png").write_bytes(_image_bytes("PNG"))
        if self.transport_failure:
            try:
                raise ProviderUnavailableError("connection interrupted")
            except ProviderUnavailableError as exc:
                raise ProviderRequestError(
                    "source page transfer interrupted", status_code=self.status
                ) from exc
        raise ProviderRequestError("interrupted page", status_code=self.status)


def interrupted_service(
    tmp_path: Path, status: int | None, transport_failure: bool = False
):
    database, base, komga = make_service(tmp_path)
    database.upsert_manga(manga(), "en", "all")
    database.upsert_chapters("manga-1", [chapter("first", "1")])
    service = TankarrService(
        base.settings, database, InterruptedProvider(status, transport_failure), komga
    )
    job = database.create_job("manga-1", "first", "en", origin="manual")
    return database, service, job


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [429, 500, 502, 503, 504, 403, None])
async def test_only_bounded_transient_retries_retain_private_pages(
    tmp_path: Path, status: int | None
):
    database, service, job = interrupted_service(tmp_path, status)
    await service.process_download_job(job["id"])
    saved = database.get_job(job["id"])
    retryable = status in {429, 500, 502, 503, 504}
    assert saved["status"] == ("queued" if retryable else "failed")
    assert saved["retry_count"] == (1 if retryable else 0)
    assert (saved["next_retry_at"] > 0) is retryable
    work = service.settings.staging_dir / f"job-{job['id']}"
    assert work.exists() is retryable
    assert database.get_chapter("first")["downloaded"] is False
    assert not list(service.settings.library_dir.rglob("*.cbz"))


@pytest.mark.asyncio
async def test_exhausted_retry_cleans_partial_pages_and_never_publishes(tmp_path: Path):
    database, service, job = interrupted_service(tmp_path, 503)
    await service.process_download_job(job["id"])
    work = service.settings.staging_dir / f"job-{job['id']}"
    assert work.exists()
    database.update_job(
        job["id"], retry_count=service.TRANSIENT_RETRIES, next_retry_at=0
    )
    await service.process_download_job(job["id"])
    assert database.get_job(job["id"])["status"] == "failed"
    assert not work.exists()
    assert database.get_chapter("first")["downloaded"] is False
    assert not list(service.settings.library_dir.rglob("*.cbz"))


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [None, 403])
async def test_source_wrapped_transport_failure_retries_without_retrying_forbidden(
    tmp_path: Path, status: int | None
):
    database, service, job = interrupted_service(tmp_path, status, True)
    await service.process_download_job(job["id"])
    saved = database.get_job(job["id"])
    retryable = status is None
    assert saved["status"] == ("queued" if retryable else "failed")
    assert saved["retry_count"] == (1 if retryable else 0)
    assert (saved["next_retry_at"] > 0) is retryable
    work = service.settings.staging_dir / f"job-{job['id']}"
    assert work.exists() is retryable
    assert database.get_chapter("first")["downloaded"] is False


@pytest.mark.asyncio
async def test_cancelled_retry_discards_private_pages_and_preserves_library(
    tmp_path: Path,
):
    database, service, job = interrupted_service(tmp_path, 503)
    await service.process_download_job(job["id"])
    work = service.settings.staging_dir / f"job-{job['id']}"
    assert work.exists()
    original = service.settings.library_dir / "existing.cbz"
    original.parent.mkdir(parents=True, exist_ok=True)
    original.write_bytes(b"fixture-existing-library-book")

    worker = DownloadWorker(database, service)
    await worker.remove(job["id"])

    with pytest.raises(KeyError):
        database.get_job(job["id"])
    assert not work.exists()
    assert original.read_bytes() == b"fixture-existing-library-book"
    assert database.get_chapter("first")["downloaded"] is False
