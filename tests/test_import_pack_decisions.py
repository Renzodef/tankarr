from __future__ import annotations

import shutil
import zipfile
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image

from tankarr.archive import sha256
from tankarr.importer import (
    LanguageReviewRequired,
    LibraryImporter,
    TorrentImportAmbiguous,
)
from tankarr.language_audit import (
    audit_english_pages,
    automatic_english_decision,
    classify_english_text,
)
from tankarr.naming import final_library_path
from tankarr.service import ExternalImportConflict
from tests.test_assembled_upgrade import ReplacementReader
from tests.test_assembled_upgrade import archive as assembly_archive
from tests.test_assembly_provenance import assembled
from tests.test_deletion import chapter, make_service, manga
from tests.test_importer import PNG


@pytest.fixture
def context(tmp_path, monkeypatch):
    database, service, _reader = make_service(tmp_path)
    service.settings.ensure_directories()
    database.upsert_manga(manga(), "en", "all")
    database.update_manga("manga-1", {"series_unit_override": "chapters"})
    published = []

    async def publish(_manga, book, path, *_args):
        published.append(book)
        destination = (
            service.settings.library_dir
            / f"import-{book['volume'] or book['chapter']}.cbz"
        )
        shutil.copyfile(path, destination)
        return {"path": str(destination), "reused": False}

    monkeypatch.setattr(service, "publish_external_import", publish)
    monkeypatch.setattr(
        service, "reconcile_komga_library", AsyncMock(return_value={"ready": True})
    )
    calls = []

    def ocr(pages, **kwargs):
        calls.append(kwargs)
        return {
            "verdict": "review",
            "sampled_pages": len(pages),
            "reason": "Not enough dialogue",
        }

    monkeypatch.setattr("tankarr.importer.audit_english_pages", ocr)
    importer = LibraryImporter(service.settings, database, service)
    content = tmp_path / "pack"
    content.mkdir()
    job = {
        "manga_id": "manga-1",
        "language": "en",
        "source": "nyaa",
        "title": "Example [English]",
        "category": "Literature English-translated",
        "source_url": "https://example.test/release",
        "info_hash": "a" * 40,
    }
    return database, service, importer, content, job, published, calls


def archive(context, name, *, extra=b""):
    path = context[3] / name
    with zipfile.ZipFile(path, "w") as output:
        for number in range(3):
            output.writestr(f"{number:03}.png", PNG)
        if extra:
            output.writestr("edition.txt", extra)
    return path


def owned(context, number, *, provider="fixture", language="en", is_chapter=False):
    database, service, *_ = context
    identifier = f"{provider}-{language}-{number}"
    release = {
        **chapter(
            identifier, number if is_chapter else None, volume=number, language=language
        ),
        "provider": provider,
        "release_unit": "chapter" if is_chapter else "volume",
    }
    database.upsert_chapters("manga-1", [release])
    if not is_chapter:
        with database.connect() as connection:
            connection.execute(
                "UPDATE chapter_release SET release_unit='volume' WHERE id=?",
                (identifier,),
            )
    path = service.settings.library_dir / f"{identifier}.cbz"
    path.write_bytes(b"existing file")
    database.mark_chapter_downloaded(identifier, path)
    return path


async def test_pack_imports_missing_books_in_chapter_mode_and_records_skipped_files(
    context,
):
    for number in (1, 2, 3):
        archive(context, f"Example v{number:02}.cbz")
    archive(context, "Extra story.cbz")
    first = owned(context, "1")
    human_chapter = owned(context, "2", is_chapter=True)
    _database, _service, importer, content, job, published, calls = context
    result = await importer.import_torrent_download(
        job, content, automatic_decision=True, skip_unnumbered=True
    )
    assert result["books"] == 2 and {book["volume"] for book in published} == {"2", "3"}
    assert {item["reason"] for item in result["skip_decisions"]} == {
        "already_owned",
        "unnumbered",
    }
    assert set(result["skipped_paths"]) == {"Extra story.cbz", "Example v01.cbz"}
    assert first.read_bytes() == b"existing file" and human_chapter.exists()
    assert calls == [{"detect_scripts": True}, {"detect_scripts": True}]
    assert all(
        audit["verdict"] == "review" for audit in result["language_evidence"]["ocr"]
    )
    assert all(
        audit["automatic_decision"]["basis"] == "metadata_fallback"
        for audit in result["language_evidence"]["ocr"]
    )


async def test_real_books_can_replace_assembled_and_other_language_does_not_own_slot(
    context,
):
    archive(context, "Example v01.cbz")
    archive(context, "Example v02.cbz")
    assembled = owned(context, "1", provider="assembled")
    italian = owned(context, "2", language="it")
    _database, _service, importer, content, job, published, _calls = context
    result = await importer.import_torrent_download(
        job, content, automatic_decision=True
    )
    assert result["books"] == 2 and {book["volume"] for book in published} == {"1", "2"}
    assert (
        assembled.exists() and italian.exists()
    )  # Actual replacement is the service's responsibility.


@pytest.mark.parametrize("identical", [False, True])
async def test_duplicate_slot_does_not_block_other_useful_books(context, identical):
    first = archive(context, "Example v01 A.cbz")
    second = context[3] / "Example v01 B.cbz"
    if identical:
        shutil.copyfile(first, second)
    else:
        archive(context, second.name, extra=b"different edition")
    archive(context, "Example v02.cbz")
    _database, _service, importer, content, job, published, _calls = context
    result = await importer.import_torrent_download(
        job, content, automatic_decision=True
    )
    assert {book["volume"] for book in published} == (
        {"1", "2"} if identical else {"2"}
    )
    assert {item["reason"] for item in result["skip_decisions"]} == {
        "identical_copy" if identical else "ambiguous_editions"
    }
    if identical:
        assert result["skipped_paths"] == [second.name]


async def test_entirely_owned_pack_has_explicit_rejection_decisions(context):
    archive(context, "Example v01.cbz")
    owned(context, "1")
    _database, _service, importer, content, job, published, _calls = context
    with pytest.raises(TorrentImportAmbiguous) as rejected:
        await importer.import_torrent_download(job, content, automatic_decision=True)
    assert rejected.value.skip_decisions == [
        {"path": "Example v01.cbz", "reason": "already_owned"}
    ]
    assert published == []


async def test_automatic_mode_never_uses_manual_confirmation_to_bypass_ocr(
    context, monkeypatch
):
    archive(context, "Example v01.cbz")
    monkeypatch.setattr(
        "tankarr.importer.audit_english_pages",
        lambda pages, **kwargs: {
            "verdict": "refused",
            "foreign_script_detected": True,
            "sampled_pages": 3,
        },
    )
    _database, _service, importer, content, job, published, _calls = context
    with pytest.raises(LanguageReviewRequired) as rejected:
        await importer.import_torrent_download(
            job, content, automatic_decision=True, confirm_language=True
        )
    assert rejected.value.evidence["manual_confirmation"] is False and published == []
    assert (
        rejected.value.evidence["ocr"][0]["automatic_decision"]["basis"]
        == "foreign_script"
    )


async def test_manual_confirmation_never_overrides_a_foreign_release_tag(context):
    archive(context, "Example v01 [JP].cbz")
    _database, _service, importer, content, job, published, calls = context

    with pytest.raises(LanguageReviewRequired) as rejected:
        await importer.import_torrent_download(job, content, confirm_language=True)

    audit = rejected.value.evidence["ocr"][0]
    assert rejected.value.evidence["manual_confirmation"] is True
    assert audit["automatic_decision"]["basis"] == "filename_language"
    assert published == [] and calls == [{"detect_scripts": True}]


async def test_explicit_foreign_filename_prevents_metadata_fallback(context):
    archive(context, "Example v01 [FR].cbz")
    _database, _service, importer, content, job, published, _calls = context
    with pytest.raises(LanguageReviewRequired) as rejected:
        await importer.import_torrent_download(job, content, automatic_decision=True)
    assert (
        rejected.value.evidence["ocr"][0]["automatic_decision"]["basis"]
        == "filename_language"
    )
    assert published == []


async def test_encoded_foreign_tag_in_download_url_prevents_automatic_import(context):
    archive(context, "Example v01.cbz")
    _database, _service, importer, content, job, published, _calls = context
    job["torrent_url"] = "https://example.test/Example%20%28JP%29/v01.cbr"

    with pytest.raises(LanguageReviewRequired) as rejected:
        await importer.import_torrent_download(job, content, automatic_decision=True)

    assert (
        rejected.value.evidence["ocr"][0]["automatic_decision"]["basis"]
        == "filename_language"
    )
    assert published == []


async def test_default_caller_keeps_manual_language_review_behavior(context):
    archive(context, "Example v01.cbz")
    _database, _service, importer, content, job, published, calls = context
    with pytest.raises(LanguageReviewRequired):
        await importer.import_torrent_download(job, content)
    assert published == [] and calls == [{}]


@pytest.mark.parametrize("automatic", [False, True])
async def test_one_publication_conflict_only_skips_that_book_in_automatic_mode(
    context, monkeypatch, automatic
):
    archive(context, "Example v01.cbz")
    archive(context, "Example v02.cbz")
    _database, service, importer, content, job, published, _calls = context
    original_publish = service.publish_external_import

    async def publish(manga_id, book, *args):
        if book["volume"] == "1":
            raise ExternalImportConflict("A real book already owns this slot")
        return await original_publish(manga_id, book, *args)

    monkeypatch.setattr(service, "publish_external_import", publish)
    if not automatic:
        with pytest.raises(ExternalImportConflict):
            await importer.import_torrent_download(job, content, confirm_language=True)
        assert published == []
        return
    result = await importer.import_torrent_download(
        job, content, automatic_decision=True
    )
    assert result["books"] == 1 and [book["volume"] for book in published] == ["2"]
    assert result["already_owned_paths"] == ["Example v01.cbz"]
    assert result["skip_decisions"][0]["reason"] == "owned_conflict"


async def test_pack_with_only_late_conflicts_is_rejected_with_paths(
    context, monkeypatch
):
    archive(context, "Example v01.cbz")
    _database, service, importer, content, job, _published, _calls = context
    monkeypatch.setattr(
        service,
        "publish_external_import",
        AsyncMock(side_effect=ExternalImportConflict("Owned")),
    )
    with pytest.raises(TorrentImportAmbiguous) as rejected:
        await importer.import_torrent_download(job, content, automatic_decision=True)
    assert rejected.value.skip_decisions == [
        {"path": "Example v01.cbz", "reason": "owned_conflict", "detail": "Owned"}
    ]


def test_language_word_in_series_title_does_not_override_matching_release_tag():
    decision = automatic_english_decision(
        {"verdict": "review"},
        declared_language="en",
        expected_language="en",
        names=["Japanese Ghost Stories v01 [English]"],
    )
    assert (
        decision["verdict"] == "confirmed" and decision["basis"] == "metadata_fallback"
    )


@pytest.mark.parametrize("text", ["こんにちは世界", "Привет товарищи", "مرحبا بالعالم"])
def test_ocr_foreign_alphabets_are_not_english_metadata_fallback(text):
    evidence = classify_english_text([text])
    assert evidence["foreign_script_detected"]
    decision = automatic_english_decision(
        evidence,
        declared_language="en",
        expected_language="en",
        names=["Example English"],
    )
    assert decision["verdict"] == "refused"


def test_script_detection_catches_nonlatin_pages_even_when_english_ocr_returns_no_text(
    tmp_path, monkeypatch
):
    path = tmp_path / "page.png"
    Image.new("RGB", (500, 700), "white").save(path)
    monkeypatch.setattr(
        "tankarr.language_audit.shutil.which", lambda name: "/fixture/tesseract"
    )

    def run(args, **_kwargs):
        return SimpleNamespace(
            returncode=0,
            stdout="Script: Han\nScript confidence: 8.2\n" if "osd" in args else "",
        )

    monkeypatch.setattr("tankarr.language_audit.subprocess.run", run)
    result = audit_english_pages([path], detect_scripts=True)
    assert result["foreign_script_detected"] and result["verdict"] == "refused"
    assert result["script_samples"] == [{"script": "Han", "confidence": 8.2}]


def test_unavailable_ocr_is_reported_as_inconclusive_and_uses_matching_tag(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("tankarr.language_audit.shutil.which", lambda name: None)
    evidence = audit_english_pages([tmp_path / "page.png"], detect_scripts=True)
    assert evidence["verdict"] == "review"
    decision = automatic_english_decision(
        evidence,
        declared_language="en",
        expected_language="en",
        names=["Example [English]"],
    )
    assert (
        decision["verdict"] == "confirmed" and decision["basis"] == "metadata_fallback"
    )


async def test_automatic_pack_really_upgrades_assembly_and_retains_original_archive(
    context, tmp_path, monkeypatch
):
    database, service, importer, content, job, _published, _calls = context
    monkeypatch.setattr(
        service,
        "publish_external_import",
        type(service).publish_external_import.__get__(service, type(service)),
    )
    reader = ReplacementReader()
    monkeypatch.setattr(service, "komga", reader)
    original = {**assembled(), "pages": 1}
    staged, old_hash, old_content_hash = assembly_archive(
        tmp_path, original, "assembled-pages"
    )
    canonical = final_library_path(service.settings.library_dir, manga(), original)
    canonical.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(staged, canonical)
    database.publish_external_chapter(
        "manga-1", original, canonical, old_hash, old_content_hash
    )
    archive(context, "Example v01.cbz")

    result = await importer.import_torrent_download(
        job, content, automatic_decision=True, skip_unnumbered=True
    )

    assert result["books"] == 1 and result["already_owned"] == 0
    assert result["paths"] == [str(canonical)]
    replacement = database.get_chapter("assembled-1")
    assert replacement["provider"] == "nyaa" and replacement["assembled_from"] is None
    assert replacement["downloaded"] and replacement["release_unit"] == "volume"
    assert replacement["library_path"] == str(canonical)
    assert replacement["library_sha256"] == sha256(canonical) != old_hash
    assert replacement["local_import_sha256"] != old_content_hash
    retained = list(
        service.settings.library_dir.glob(".tankarr-delete-*/*.quarantined")
    )
    assert len(retained) == 1 and sha256(retained[0]) == old_hash
    assert reader.synced == [
        canonical.relative_to(service.settings.library_dir).as_posix()
    ]
    assert reader.purged == [] and database.list_deletion_operations() == []
