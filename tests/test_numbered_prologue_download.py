from pathlib import Path

import pytest

from tankarr.archive import validate_cbz
from tankarr.service import TankarrService
from tests.test_deletion import (
    PageProvider,
    RecordingKomga,
    chapter,
    make_service,
    manga,
)


@pytest.mark.asyncio
async def test_numbered_prologue_downloads_without_replacing_ordinary_chapter(
    tmp_path: Path,
):
    database, base, _ = make_service(tmp_path)
    series = manga()
    database.upsert_manga(series, "en", "all")
    ordinary = {**chapter("ordinary", "1"), "provider": "suwayomi"}
    prelude = {
        **chapter("prelude", "1"),
        "provider": "suwayomi",
        "title": "Prologue 1",
        "canonical_chapter": None,
        "numbering_method": "special_title",
        "numbering_status": "unmapped",
    }
    database.upsert_chapters(series["id"], [ordinary, prelude])
    service = TankarrService(base.settings, database, PageProvider(), RecordingKomga())
    ordinary_job = await service.create_manual_download_job("ordinary")
    await service.process_download_job(ordinary_job["id"])
    old = database.get_chapter("ordinary")
    old_path = Path(old["library_path"])
    old_bytes = old_path.read_bytes()
    job = await service.create_manual_download_job("prelude")
    await service.process_download_job(job["id"])
    completed = database.get_job(job["id"])
    assert completed["status"] == "completed", completed["message"]
    new_path = Path(completed["result_path"])
    assert new_path != old_path
    assert "prologue-001" in new_path.name
    assert validate_cbz(new_path)["page_count"] == 1
    assert old_path.read_bytes() == old_bytes
    assert database.get_chapter("ordinary")["downloaded"] is True
    assert database.get_chapter("prelude")["downloaded"] is True
