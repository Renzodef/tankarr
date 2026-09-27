from __future__ import annotations

from pathlib import Path

import pytest

from tankarr.archive import sha256
from tankarr.config import Settings
from tankarr.database import Database
from tankarr.naming import final_library_path
from tankarr.service import SeriesRenameError, TankarrService


def manga(*, title: str = "Example Series", authors: list[str] | None = None) -> dict:
    return {
        "id": "manga-1",
        "title": title,
        "description": "",
        "cover_url": None,
        "authors": ["Example Author"] if authors is None else authors,
        "original_language": "ja",
        "status": "ongoing",
        "year": 2026,
        "last_volume": "1",
        "last_chapter": "1",
        "available_languages": ["en"],
        "source_url": "https://mangadex.org/title/manga-1",
    }


def chapter() -> dict:
    return {
        "id": "chapter-1",
        "manga_id": "manga-1",
        "volume": "1",
        "chapter": "1",
        "title": "A title that must not enter the filename",
        "language": "en",
        "provider": "mangadex",
        "groups": [],
        "publish_at": "2026-01-01T00:00:00Z",
        "source_url": "https://mangadex.org/chapter/chapter-1",
        "pages": 1,
        "version": 1,
    }


class RecordingKomga:
    def __init__(self) -> None:
        self.calls = 0

    async def scan(self, _expected_relative_paths=()) -> dict:
        self.calls += 1
        return {"configured": True, "triggered": True, "library_id": "manga"}


class MoveAwareKomga(RecordingKomga):
    configured = True

    def __init__(self, *, fail_prepare: bool = False) -> None:
        super().__init__()
        self.fail_prepare = fail_prepare
        self.events: list[str] = []

    async def prepare_for_moves(self) -> dict:
        self.events.append("prepare")
        if self.fail_prepare:
            raise RuntimeError("hash verification unavailable")
        return {
            "configured": True,
            "ready": True,
            "hashed_books": 1,
        }

    async def scan(self, _expected_relative_paths=()) -> dict:
        self.events.append("scan")
        return await super().scan()


def make_service(tmp_path: Path) -> tuple[Database, TankarrService, RecordingKomga]:
    settings = Settings(
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "library",
    )
    settings.ensure_directories()
    identity = "0123456789abcdef0123456789abcdef"
    (settings.data_dir / ".tankarr-library-id").write_text(
        f"{identity}\n", encoding="utf-8"
    )
    (settings.library_dir / ".tankarr-library-id").write_text(
        f"{identity}\n", encoding="utf-8"
    )
    database = Database(settings.database_path)
    database.initialize()
    komga = RecordingKomga()
    service = TankarrService(
        settings,
        database,
        object(),  # type: ignore[arg-type]
        komga,  # type: ignore[arg-type]
    )
    return database, service, komga


def add_legacy_download(
    database: Database,
    service: TankarrService,
    *,
    content: bytes = b"tracked archive",
    persist_hash: bool = False,
) -> tuple[dict, dict, Path, Path]:
    manga_data = manga()
    chapter_data = chapter()
    database.upsert_manga(manga_data, "en", "none")
    database.upsert_chapters(manga_data["id"], [chapter_data])
    old_path = service.settings.library_dir / "Example Series" / "Chapter 1 [en].cbz"
    old_path.parent.mkdir(parents=True)
    old_path.write_bytes(content)
    digest = sha256(old_path) if persist_hash else None
    database.mark_chapter_downloaded(chapter_data["id"], old_path, digest)
    destination = final_library_path(
        service.settings.library_dir, manga_data, chapter_data
    )
    return manga_data, chapter_data, old_path, destination


@pytest.mark.asyncio
async def test_organizer_moves_legacy_file_and_updates_chapter_and_job_paths(
    tmp_path: Path,
):
    database, service, komga = make_service(tmp_path)
    manga_data, chapter_data, old_path, destination = add_legacy_download(
        database, service
    )
    digest = sha256(old_path)
    job = database.create_job(manga_data["id"], chapter_data["id"], "en")
    database.claim_queued_job(job["id"], old_path)
    database.update_job(
        job["id"],
        status="completed",
        progress=1,
        result_path=old_path,
        language_evidence={"archive": {"sha256": digest}},
    )

    result = await service.organize_library()

    assert result["organization_blocked"] is False
    assert result["moved"] == 1
    assert result["hashes_recorded"] == 0
    assert result["directories_removed"] == 1
    assert result["komga_scan"]["triggered"] is True
    assert komga.calls == 1
    assert destination.exists()
    assert not old_path.exists()
    assert not old_path.parent.exists()
    stored = database.get_chapter(chapter_data["id"])
    assert stored["library_path"] == str(destination)
    assert stored["library_sha256"] == digest
    updated_job = database.get_job(job["id"])
    assert updated_job["planned_path"] == str(destination)
    assert updated_job["result_path"] == str(destination)

    chapter_updated_at = stored["updated_at"]
    job_updated_at = updated_job["updated_at"]
    repeated = await service.organize_library()
    assert repeated["moved"] == 0
    assert repeated["unchanged"] == 1
    assert repeated["normalized_records"] == 0
    assert database.get_chapter(chapter_data["id"])["updated_at"] == chapter_updated_at
    assert database.get_job(job["id"])["updated_at"] == job_updated_at
    assert komga.calls == 1


@pytest.mark.asyncio
async def test_organizer_does_not_load_remote_only_release_inventory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    database, service, _ = make_service(tmp_path)
    manga_data, _chapter_data, _old_path, _destination = add_legacy_download(
        database, service
    )
    remote_only = []
    for number in range(2, 102):
        item = chapter()
        item["id"] = f"remote-{number}"
        item["chapter"] = str(number)
        remote_only.append(item)
    database.upsert_chapters(manga_data["id"], remote_only)

    def reject_full_inventory(*_args, **_kwargs):
        raise AssertionError("organizer loaded the complete remote inventory")

    monkeypatch.setattr(database, "list_all_chapters", reject_full_inventory)

    result = await service.organize_library()

    assert result["chapters_scanned"] == 101
    assert result["downloaded_files"] == 1


@pytest.mark.asyncio
async def test_series_rename_persists_override_and_reorganizes_owned_files(
    tmp_path: Path,
):
    database, service, _ = make_service(tmp_path)
    manga_data, chapter_data, old_path, _ = add_legacy_download(database, service)
    old_cover = old_path.parent / "cover.jpg"
    old_cover.write_bytes(b"series cover")

    result = await service.rename_manga(manga_data["id"], "Renamed Series")

    renamed_manga = database.get_manga(manga_data["id"])
    destination = final_library_path(
        service.settings.library_dir, renamed_manga, chapter_data
    )
    assert renamed_manga["title"] == "Renamed Series"
    assert renamed_manga["source_title"] == "Example Series"
    assert renamed_manga["title_override"] == "Renamed Series"
    assert renamed_manga["title_overridden"] is True
    assert result["organization"]["moved"] == 1
    assert result["sidecars_moved"] == 1
    assert destination.exists()
    assert (destination.parent / "cover.jpg").read_bytes() == b"series cover"
    assert not old_path.exists()
    assert not old_path.parent.exists()
    assert database.get_chapter(chapter_data["id"])["library_path"] == str(destination)

    provider_refresh = dict(manga_data)
    provider_refresh["title"] = "Provider Retitled Series"
    database.upsert_manga(provider_refresh, "en")
    refreshed = database.get_manga(manga_data["id"])
    assert refreshed["title"] == "Renamed Series"
    assert refreshed["source_title"] == "Provider Retitled Series"
    assert refreshed["title_override"] == "Renamed Series"


@pytest.mark.asyncio
async def test_verified_metadata_title_reorganizes_files_without_becoming_override(
    tmp_path: Path,
):
    database, service, _ = make_service(tmp_path)
    manga_data, chapter_data, old_path, _ = add_legacy_download(database, service)
    old_cover = old_path.parent / "cover.jpg"
    old_cover.write_bytes(b"series cover")

    result = await service.apply_metadata_title(
        manga_data["id"], "Canonical Metadata Series"
    )

    stored = database.get_manga(manga_data["id"])
    destination = final_library_path(service.settings.library_dir, stored, chapter_data)
    assert result["updated"] is True
    assert result["organization"]["moved"] == 1
    assert stored["source_title"] == "Example Series"
    assert stored["metadata_title"] == "Canonical Metadata Series"
    assert stored["title"] == "Canonical Metadata Series"
    assert stored["title_source"] == "metadata"
    assert stored["title_override"] is None
    assert stored["title_overridden"] is False
    assert destination.exists()
    assert (destination.parent / "cover.jpg").read_bytes() == b"series cover"
    assert not old_path.exists()
    assert not old_path.parent.exists()


@pytest.mark.asyncio
async def test_manual_title_remains_visible_when_canonical_metadata_title_changes(
    tmp_path: Path,
):
    database, service, _ = make_service(tmp_path)
    manga_data, chapter_data, _, _ = add_legacy_download(database, service)
    await service.apply_metadata_title(manga_data["id"], "Canonical Series")
    await service.rename_manga(manga_data["id"], "My Preferred Title")
    manual_path = final_library_path(
        service.settings.library_dir,
        database.get_manga(manga_data["id"]),
        chapter_data,
    )

    result = await service.apply_metadata_title(
        manga_data["id"], "Updated Canonical Series"
    )

    stored = database.get_manga(manga_data["id"])
    assert result["updated"] is True
    assert result["organization"]["moved"] == 0
    assert stored["metadata_title"] == "Updated Canonical Series"
    assert stored["title"] == "My Preferred Title"
    assert stored["title_source"] == "manual"
    assert stored["title_overridden"] is True
    assert manual_path.exists()


@pytest.mark.asyncio
async def test_series_rename_rejects_existing_destination_without_mutation(
    tmp_path: Path,
):
    database, service, _ = make_service(tmp_path)
    manga_data, chapter_data, old_path, _ = add_legacy_download(database, service)
    renamed = {**manga_data, "title": "Occupied Series"}
    destination = final_library_path(
        service.settings.library_dir, renamed, chapter_data
    )
    destination.parent.mkdir(parents=True)
    (destination.parent / "unmanaged.txt").write_text("keep", encoding="utf-8")

    with pytest.raises(FileExistsError, match="existing directory"):
        await service.rename_manga(manga_data["id"], "Occupied Series")

    stored = database.get_manga(manga_data["id"])
    assert stored["title"] == "Example Series"
    assert stored["title_override"] is None
    assert old_path.exists()
    assert not destination.exists()
    assert (destination.parent / "unmanaged.txt").read_text(encoding="utf-8") == "keep"


@pytest.mark.asyncio
async def test_series_rename_is_deferred_while_its_download_is_running(tmp_path: Path):
    database, service, _ = make_service(tmp_path)
    manga_data, chapter_data, old_path, _ = add_legacy_download(database, service)
    job = database.create_job(manga_data["id"], chapter_data["id"], "en")
    database.claim_queued_job(job["id"], old_path)

    with pytest.raises(SeriesRenameError, match="downloads are active"):
        await service.rename_manga(manga_data["id"], "Deferred Series")

    stored = database.get_manga(manga_data["id"])
    assert stored["title"] == "Example Series"
    assert stored["title_override"] is None
    assert old_path.exists()


@pytest.mark.asyncio
async def test_organizer_verifies_komga_hashes_before_the_first_rename(
    tmp_path: Path,
):
    database, service, _ = make_service(tmp_path)
    komga = MoveAwareKomga()
    service.komga = komga  # type: ignore[assignment]
    _, _, old_path, destination = add_legacy_download(database, service)

    result = await service.organize_library()

    assert result["organization_blocked"] is False
    assert result["moved"] == 1
    assert komga.events == ["prepare", "scan"]
    assert not old_path.exists()
    assert destination.exists()


@pytest.mark.asyncio
async def test_organizer_does_not_rename_when_komga_hashes_cannot_be_verified(
    tmp_path: Path,
):
    database, service, _ = make_service(tmp_path)
    komga = MoveAwareKomga(fail_prepare=True)
    service.komga = komga  # type: ignore[assignment]
    _, _, old_path, destination = add_legacy_download(database, service)

    result = await service.organize_library()

    assert result["organization_blocked"] is True
    assert "hashing could not be verified" in result["warnings"][0]
    assert komga.events == ["prepare"]
    assert old_path.exists()
    assert not destination.exists()


@pytest.mark.asyncio
async def test_organizer_dry_run_validates_without_mutating(tmp_path: Path):
    database, service, komga = make_service(tmp_path)
    _, chapter_data, old_path, destination = add_legacy_download(database, service)

    result = await service.organize_library(dry_run=True)

    assert result["dry_run"] is True
    assert result["planned_moves"] == 1
    assert result["hashes_to_record"] == 1
    assert result["moved"] == 0
    assert old_path.exists()
    assert not destination.exists()
    stored = database.get_chapter(chapter_data["id"])
    assert stored["library_path"] == str(old_path)
    assert stored["library_sha256"] is None
    assert komga.calls == 0


@pytest.mark.asyncio
async def test_organizer_never_overwrites_existing_destination(tmp_path: Path):
    database, service, komga = make_service(tmp_path)
    _, chapter_data, old_path, destination = add_legacy_download(
        database, service, persist_hash=True
    )
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"untracked different archive")

    result = await service.organize_library()

    assert result["organization_blocked"] is True
    assert result["moved"] == 0
    assert "Refusing to overwrite" in result["warnings"][0]
    assert old_path.read_bytes() == b"tracked archive"
    assert destination.read_bytes() == b"untracked different archive"
    assert database.get_chapter(chapter_data["id"])["library_path"] == str(old_path)
    assert komga.calls == 0


@pytest.mark.asyncio
async def test_organizer_adopts_hash_verified_destination_after_interrupted_move(
    tmp_path: Path,
):
    database, service, komga = make_service(tmp_path)
    _, chapter_data, old_path, destination = add_legacy_download(
        database, service, persist_hash=True
    )
    destination.parent.mkdir(parents=True)
    old_path.rename(destination)

    result = await service.organize_library()

    assert result["organization_blocked"] is False
    assert result["adopted"] == 1
    assert destination.exists()
    assert database.get_chapter(chapter_data["id"])["library_path"] == str(destination)
    assert komga.calls == 1


@pytest.mark.asyncio
async def test_organizer_refuses_unverified_interrupted_destination(tmp_path: Path):
    database, service, komga = make_service(tmp_path)
    _, chapter_data, old_path, destination = add_legacy_download(database, service)
    destination.parent.mkdir(parents=True)
    old_path.rename(destination)

    result = await service.organize_library()

    assert result["organization_blocked"] is True
    assert "without persisted identity evidence" in result["warnings"][0]
    assert destination.exists()
    assert database.get_chapter(chapter_data["id"])["library_path"] == str(old_path)
    assert komga.calls == 0


@pytest.mark.asyncio
async def test_organizer_rolls_file_back_when_database_commit_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    database, service, komga = make_service(tmp_path)
    _, chapter_data, old_path, destination = add_legacy_download(
        database, service, persist_hash=True
    )

    def fail_commit(*args, **kwargs):
        raise RuntimeError("simulated database failure")

    monkeypatch.setattr(database, "relocate_chapter_library_file", fail_commit)

    result = await service.organize_library()

    assert result["organization_blocked"] is True
    assert "simulated database failure" in result["warnings"][0]
    assert old_path.exists()
    assert not destination.exists()
    assert database.get_chapter(chapter_data["id"])["library_path"] == str(old_path)
    assert komga.calls == 0


@pytest.mark.asyncio
async def test_organizer_defers_without_blocking_while_job_is_active(tmp_path: Path):
    database, service, komga = make_service(tmp_path)
    manga_data, chapter_data, old_path, _ = add_legacy_download(
        database, service, persist_hash=True
    )
    job = database.create_job(manga_data["id"], chapter_data["id"], "en")
    database.claim_queued_job(job["id"], old_path)

    result = await service.organize_library()

    assert result["deferred"] is True
    assert result["organization_blocked"] is False
    assert old_path.exists()
    assert komga.calls == 0


@pytest.mark.asyncio
async def test_organizer_keeps_one_copy_when_two_downloads_share_a_chapter(
    tmp_path: Path,
):
    """Aligning a second source to the official numbering must not block the
    library: the copy already at the destination stays, the other is dropped."""

    database, service, komga = make_service(tmp_path)
    manga_data = manga()
    official = chapter()
    mirrored = {**chapter(), "id": "chapter-1-mirror", "title": "Chapter 1"}
    database.upsert_manga(manga_data, "en", "none")
    database.upsert_chapters(manga_data["id"], [official, mirrored])
    destination = final_library_path(service.settings.library_dir, manga_data, official)
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"official archive")
    database.mark_chapter_downloaded(official["id"], destination, sha256(destination))
    legacy = destination.parent / "Example Series - c016 [en].cbz"
    legacy.write_bytes(b"mirror archive")
    database.mark_chapter_downloaded(mirrored["id"], legacy, sha256(legacy))
    # The dropped copy's finished job recorded the very path the kept copy
    # owns; leaving it behind makes every later audit of the series refuse.
    job = database.create_job(manga_data["id"], mirrored["id"], "en")
    database.update_job(job["id"], status="completed", result_path=destination)

    preview = await service.organize_library(dry_run=True)
    assert preview["organization_blocked"] is False
    assert preview["planned_duplicate_removals"] == 1
    assert legacy.exists()

    result = await service.organize_library()

    assert result["organization_blocked"] is False, result["warnings"]
    assert result["duplicates_removed"] == 1
    assert result["scan_required"] is True
    assert destination.read_bytes() == b"official archive"
    assert not legacy.exists()
    assert database.get_chapter(official["id"])["library_path"] == str(destination)
    dropped = database.get_chapter(mirrored["id"])
    assert dropped["downloaded"] is False and dropped["library_path"] is None
    assert database.get_job(job["id"])["result_path"] is None
    assert komga.calls == 1


@pytest.mark.asyncio
async def test_organizer_removes_logical_duplicate_with_different_volume_metadata(
    tmp_path: Path,
):
    database, service, _ = make_service(tmp_path)
    manga_data = manga()
    with_volume = chapter()
    without_volume = {
        **chapter(),
        "id": "chapter-1-mirror",
        "volume": None,
        "source_key": "mirror",
    }
    database.upsert_manga(manga_data, "en", "none")
    database.upsert_chapters(manga_data["id"], [with_volume, without_volume])
    volume_path = final_library_path(
        service.settings.library_dir, manga_data, with_volume
    )
    plain_path = final_library_path(
        service.settings.library_dir, manga_data, without_volume
    )
    volume_path.parent.mkdir(parents=True)
    volume_path.write_bytes(b"first copy")
    plain_path.write_bytes(b"second copy")
    database.mark_chapter_downloaded(
        with_volume["id"], volume_path, sha256(volume_path)
    )
    database.mark_chapter_downloaded(
        without_volume["id"], plain_path, sha256(plain_path)
    )

    result = await service.organize_library()

    assert result["organization_blocked"] is False, result["warnings"]
    assert result["duplicates_removed"] == 1
    owned = database.list_downloaded_chapters([manga_data["id"]])
    assert len(owned) == 1
    assert sum(path.exists() for path in (volume_path, plain_path)) == 1
