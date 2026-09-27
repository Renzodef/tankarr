from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from test_deletion import add_downloaded_chapter, chapter, make_service, manga

from tankarr.service import RecoveryBlocked


def legacy_download(tmp_path):
    database, service, komga = make_service(tmp_path)
    canonical, job_id = add_downloaded_chapter(
        database, service.settings.library_dir, manga(), chapter("chapter-1", "1")
    )
    legacy = canonical.with_name("legacy.cbz")
    canonical.rename(legacy)
    database.mark_chapter_downloaded("chapter-1", legacy)
    database.update_job(job_id, status="completed", result_path=legacy)
    return database, service, komga, legacy, canonical, job_id


@pytest.mark.asyncio
async def test_repair_preview_is_read_only_and_confirmed_apply_moves_file(tmp_path):
    database, service, _komga, legacy, canonical, _job_id = legacy_download(tmp_path)
    revision = database.library_revision()
    blocked_state = {"organization_blocked": True, "warnings": ["Pending migration"]}
    service.last_library_organization = blocked_state
    first = await service.organize_library(dry_run=True)
    second = await service.organize_library(dry_run=True)
    assert service.last_library_organization is blocked_state
    with pytest.raises(RecoveryBlocked):
        service.assert_mutations_allowed()
    assert first["snapshot"] and first["snapshot"] == second["snapshot"]
    assert first["actions"] == second["actions"]
    assert first["actions"][0]["kind"] == "move"
    assert first["actions"][0]["safe"] is True
    assert database.library_revision() == revision
    assert legacy.read_bytes() == b"cbz"
    assert not canonical.exists()

    applied = await service.organize_library(confirmation_snapshot=first["snapshot"])
    assert applied["moved"] == 1
    assert not applied["organization_blocked"]
    assert not legacy.exists()
    assert canonical.read_bytes() == b"cbz"
    assert database.get_chapter("chapter-1")["library_path"] == str(canonical)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["file", "database", "job"])
async def test_repair_rejects_changed_inputs_before_files_or_komga_mutate(
    tmp_path, change
):
    database, service, komga, legacy, canonical, job_id = legacy_download(tmp_path)
    preview = await service.organize_library(dry_run=True)
    komga.configured = True
    komga.prepare_for_moves = AsyncMock(return_value={"ready": True})
    if change == "file":
        legacy.write_bytes(b"different content")
    elif change == "database":
        with database.connect() as connection:
            connection.execute("UPDATE manga SET title='Renamed' WHERE id='manga-1'")
    else:
        database.update_job(job_id, status="running")
    with pytest.raises(ValueError, match="stale"):
        await service.organize_library(confirmation_snapshot=preview["snapshot"])
    assert legacy.exists()
    assert not canonical.exists()
    assert database.get_chapter("chapter-1")["library_path"] == str(legacy)
    komga.prepare_for_moves.assert_not_awaited()


@pytest.mark.asyncio
async def test_guided_repair_never_deletes_duplicates(tmp_path):
    database, service, _komga, legacy, canonical, _job_id = legacy_download(tmp_path)
    add_downloaded_chapter(
        database, service.settings.library_dir, manga(), chapter("chapter-2", "1")
    )
    assert canonical.exists() and legacy.exists()
    preview = await service.organize_library(dry_run=True)
    assert preview["snapshot"]
    assert preview["planned_duplicate_removals"] == 1
    duplicate = next(
        action for action in preview["actions"] if action["kind"] == "duplicate"
    )
    assert duplicate["safe"] is False
    with pytest.raises(ValueError, match="manual review"):
        await service.organize_library(confirmation_snapshot=preview["snapshot"])
    assert canonical.read_bytes() == legacy.read_bytes() == b"cbz"
    assert database.get_chapter("chapter-1")["downloaded"]
    assert database.get_chapter("chapter-2")["downloaded"]


@pytest.mark.asyncio
async def test_restore_safe_mode_blocks_direct_mutations_but_allows_repair_preview(
    tmp_path,
):
    _database, service, _komga, legacy, canonical, _job_id = legacy_download(tmp_path)
    # model_copy also supports this test during migration of the Settings schema.
    service.settings = service.settings.model_copy(update={"restored_safe_mode": True})
    with pytest.raises(RecoveryBlocked, match="after restore"):
        service.assert_mutations_allowed(db_only=True)
    preview = await service.organize_library(dry_run=True)
    assert preview["snapshot"]
    with pytest.raises(RecoveryBlocked, match="after restore"):
        await service.organize_library(confirmation_snapshot=preview["snapshot"])
    assert legacy.exists() and not canonical.exists()
