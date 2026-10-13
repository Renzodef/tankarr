import pytest

from tankarr.language_audit import (
    automatic_english_decision,
    classify_english_text,
    corroborate_release_language,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("foreign_last", [False, True])
async def test_pack_import_shares_ocr_evidence_without_accepting_a_foreign_file(
    tmp_path, monkeypatch, foreign_last
):
    from tankarr.config import Settings
    from tankarr.database import Database
    from tankarr.importer import LanguageReviewRequired, LibraryImporter
    from tests.test_importer import make_volume_zip, provision_library_identity
    from tests.test_torrents import FakeService, release, seed

    settings = Settings(
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "library",
        monitor_enabled=False,
        metadata_enabled=False,
    )
    settings.ensure_directories()
    provision_library_identity(settings)
    database = Database(settings.database_path)
    database.initialize()
    seed(database)
    published = []

    class PublishingService(FakeService):
        async def publish_external_import(self, _manga_id, chapter, path, *_args):
            published.append(chapter)
            return {"path": str(path), "reused": False}

        async def reconcile_komga_library(self):
            return {"ready": True}

    audited = 0

    def audit(_pages, **kwargs):
        nonlocal audited
        audited += 1
        if audited < 5:
            return {
                "verdict": "confirmed",
                "successful_samples": 3,
                "foreign_letter_count": 0,
            }
        return {
            "verdict": "refused" if foreign_last else "review",
            "successful_samples": 3,
            "foreign_letter_count": 3 if foreign_last else 0,
            "foreign_script_detected": foreign_last,
            "poor_english_signal": not foreign_last,
        }

    monkeypatch.setattr("tankarr.importer.audit_english_pages", audit)
    content = tmp_path / "pack"
    content.mkdir()
    for number in range(1, 6):
        make_volume_zip(content / f"Fixture v{number:02d}.cbz")
    importer = LibraryImporter(settings, database, PublishingService(tmp_path))
    job = database.create_torrent_download(
        "kamui", {**release(), "title": "Fixture v01-05 [Digital]", "volume": "1-5"}
    )
    if foreign_last:
        with pytest.raises(LanguageReviewRequired):
            await importer.import_torrent_download(
                job, content, automatic_decision=True
            )
        assert published == []
    else:
        result = await importer.import_torrent_download(
            job, content, automatic_decision=True
        )
        assert result["books"] == 5
        assert len(published) == 5
        assert (
            result["language_evidence"]["ocr"][-1]["automatic_decision"]["basis"]
            == "release_ocr"
        )
    database.close()


def confirmed():
    return {
        "verdict": "confirmed",
        "successful_samples": 3,
        "automatic_decision": {"verdict": "confirmed", "basis": "ocr"},
    }


def uncertain():
    return {
        "verdict": "review",
        "successful_samples": 3,
        "foreign_letter_count": 0,
        "poor_english_signal": True,
        "automatic_decision": {"verdict": "review", "basis": "ocr_noise"},
    }


def test_strong_release_evidence_corroborates_an_inconclusive_file():
    audits = [confirmed() for _ in range(4)] + [uncertain()]
    result = corroborate_release_language(audits)
    assert result[-1]["automatic_decision"]["verdict"] == "confirmed"
    assert result[-1]["automatic_decision"]["basis"] == "release_ocr"
    assert audits[-1]["automatic_decision"]["verdict"] == "review"


@pytest.mark.parametrize(
    "kind",
    [
        "foreign_script",
        "foreign_letters",
        "failed_ocr",
        "filename",
        "weak_majority",
        "metadata_only",
    ],
)
def test_release_evidence_never_overrides_contradictions_or_weak_samples(kind):
    audits = [confirmed() for _ in range(4)] + [uncertain()]
    if kind == "foreign_script":
        audits[-1]["foreign_script_detected"] = True
    elif kind == "foreign_letters":
        audits[-1]["foreign_letter_count"] = 1
    elif kind == "failed_ocr":
        audits[-1]["successful_samples"] = 0
    elif kind == "filename":
        audits[-1]["automatic_decision"] = {
            "verdict": "refused",
            "basis": "filename_language",
        }
    elif kind == "weak_majority":
        audits.append(uncertain())
    else:
        for audit in audits[:-1]:
            audit["automatic_decision"]["basis"] = "metadata_fallback"
    assert (
        corroborate_release_language(audits)[-1]["automatic_decision"]["verdict"]
        != "confirmed"
    )


def test_short_common_fragments_do_not_confirm_english_dialogue():
    audit = classify_english_text(["a i the " * 30 + "longwords " * 30])
    assert audit["verdict"] == "review"
    assert audit["dialogue_word_count"] == 1


def test_foreign_filename_is_refused_even_when_ocr_is_noisy():
    decision = automatic_english_decision(
        uncertain(),
        declared_language="en",
        expected_language="en",
        names=["Fixture v01 [RAW]"],
    )
    assert decision["verdict"] == "refused"
    assert decision["basis"] == "filename_language"
