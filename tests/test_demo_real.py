from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import demo_real
import pytest

from tankarr.app import create_app
from tankarr.config import Settings
from tankarr.metadata.mangabaka import MangaBakaMetadataSource


@pytest.mark.asyncio
async def test_demo_enrichment_uses_catalogue_without_optional_network_lookups(
    tmp_path,
):
    app = create_app(
        Settings(
            _env_file=None,
            data_dir=tmp_path / "data",
            library_dir=tmp_path / "library",
            auth_required=False,
            suwayomi_enabled=False,
            komga_link_enabled=False,
            reader_kind="none",
        )
    )
    metadata = app.state.metadata
    # A slow or unavailable secondary catalogue must never be contacted.
    other = SimpleNamespace(
        name="optional-catalogue",
        aclose=AsyncMock(),
        get_series=AsyncMock(side_effect=AssertionError("unexpected network lookup")),
    )
    metadata.sources.append(other)
    catalogue = await demo_real.prepare_catalogue(metadata)
    record = MangaBakaMetadataSource._normalize(
        {
            "id": 123,
            "title": "Fixture Adventure",
            "type": "manga",
            "status": "completed",
            "authors": ["Fixture Author"],
            "total_chapters": 12,
            "source": {"anilist": {"id": 456}},
        }
    )
    catalogue.get_series = AsyncMock(return_value=record)
    catalogue.search_series = AsyncMock(
        side_effect=AssertionError("unexpected title search")
    )
    catalogue.search_series_by_author = AsyncMock(
        side_effect=AssertionError("unexpected author search")
    )
    app.state.service.last_deletion_recovery = {
        "recovery_blocked": False,
        "warnings": [],
    }
    app.state.service.last_library_organization = (
        app.state.service._organization_report()
    )
    try:
        fetched, manga = await demo_real.add_catalogue_work(
            app, catalogue, "123", "chapters"
        )
        saved = app.state.database.get_series_metadata(manga["id"])
        assert fetched == record
        assert saved["data"]["title"] == "Fixture Adventure"
        assert manga["source_id"] == "123"
        assert saved["data"]["external_ids"]["mangabaka"] == "123"
        catalogue_record = next(
            item
            for item in app.state.database.list_metadata_source_records(manga["id"])
            if item["source"] == "mangabaka"
        )
        assert catalogue_record["data"]["external_ids"]["anilist"] == "456"
        catalogue.search_series.assert_not_awaited()
        catalogue.search_series_by_author.assert_not_awaited()
        other.get_series.assert_not_awaited()
        other.aclose.assert_awaited_once()
        assert metadata.sources == [catalogue]
    finally:
        await catalogue.aclose()
        await metadata.artwork.aclose()
        app.state.database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("slow_stage", ["lookup", "enrichment"])
async def test_demo_deadline_covers_lookup_and_enrichment(monkeypatch, slow_stage):
    monkeypatch.setattr(demo_real, "SERIES_TIMEOUT_SECONDS", 0.01)
    cancelled = asyncio.Event()

    async def slow(*args, **kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    catalogue = SimpleNamespace(
        get_series=AsyncMock(return_value={"title": "Fixture", "external_id": "123"})
    )
    service = SimpleNamespace(
        add_catalogue_series=AsyncMock(return_value={"id": "fixture"})
    )
    metadata = SimpleNamespace(update_manual_correlations=AsyncMock())
    if slow_stage == "lookup":
        catalogue.get_series = slow
    else:
        metadata.update_manual_correlations = slow
    app = SimpleNamespace(state=SimpleNamespace(service=service, metadata=metadata))
    with pytest.raises(TimeoutError):
        await demo_real.add_catalogue_work(app, catalogue, "123", "chapters")
    assert cancelled.is_set()
    if slow_stage == "lookup":
        service.add_catalogue_series.assert_not_awaited()
        metadata.update_manual_correlations.assert_not_awaited()
