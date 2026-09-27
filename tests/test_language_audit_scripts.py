from __future__ import annotations

from types import SimpleNamespace

from PIL import Image

from tankarr import language_audit

ENGLISH = (
    "I don't know what you want from me. We were there when it happened, and "
    "you said it would be fine. Well, it was not fine, was it? Where do we go now?"
)


def _fake_run(osd_script: str, text: str):
    def run(command, **_kwargs):
        if "osd" in command:
            return SimpleNamespace(
                returncode=0,
                stdout=f"Script: {osd_script}\nScript confidence: 6.0\n",
                stderr="",
            )
        return SimpleNamespace(returncode=0, stdout=text, stderr="")

    return run


def _pages(tmp_path, count=3):
    pages = []
    for index in range(count):
        page = tmp_path / f"p{index}.png"
        Image.new("L", (40, 60), color=255).save(page)
        pages.append(page)
    return pages


def test_english_dialogue_outranks_a_japanese_script_guess_about_the_artwork(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        language_audit.shutil, "which", lambda _name: "/usr/bin/tesseract"
    )
    monkeypatch.setattr(
        language_audit.subprocess, "run", _fake_run("Japanese", ENGLISH)
    )

    evidence = language_audit.audit_english_pages(_pages(tmp_path), detect_scripts=True)

    assert evidence["verdict"] == "confirmed"
    assert evidence["foreign_letter_count"] == 0
    assert evidence.get("foreign_script_detected") is False
    assert "artwork" in evidence["script_note"]


def test_a_script_guess_still_refuses_when_the_text_gives_no_english_signal(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        language_audit.shutil, "which", lambda _name: "/usr/bin/tesseract"
    )
    monkeypatch.setattr(
        language_audit.subprocess, "run", _fake_run("Japanese", "ka ta na")
    )

    evidence = language_audit.audit_english_pages(_pages(tmp_path), detect_scripts=True)

    assert evidence["verdict"] == "refused"
    assert evidence["reason"] == "Sampled pages use a non-Latin alphabet"


def test_a_script_guess_on_pages_the_ocr_could_not_read_refuses_nothing(
    tmp_path, monkeypatch
):
    """Ghost in the Shell 2 is painted: tesseract failed on every sampled
    page and a 7%-confidence "Cyrillic" guess about the artwork refused the
    only English edition that exists. With no text read, a person decides."""

    def run(command, **_kwargs):
        if "osd" in command:
            return SimpleNamespace(
                returncode=0,
                stdout="Script: Cyrillic\nScript confidence: 7.45\n",
                stderr="",
            )
        return SimpleNamespace(returncode=1, stdout="", stderr="read error")

    monkeypatch.setattr(
        language_audit.shutil, "which", lambda _name: "/usr/bin/tesseract"
    )
    monkeypatch.setattr(language_audit.subprocess, "run", run)

    evidence = language_audit.audit_english_pages(_pages(tmp_path), detect_scripts=True)

    assert evidence["verdict"] == "review"
    assert evidence["reason"] == "OCR could not read any sampled page"
    assert evidence.get("foreign_script_detected") is False
    assert "could not read" in evidence["script_note"]


def test_japanese_ocr_fragments_cannot_confirm_english_by_metadata():
    # Representative output from Tesseract's English model on a Japanese
    # Demon Slayer page: short Latin fragments and incidental common words.
    noise = (
        "CT Wet MD A i ta Be NM aD whim x al a a ko I HT Fe SS d oa al Ne "
        "be ars RE wb ih hk held a BY hy zg be Ww i Ml VJ MI ial Xl bd Se "
        "Ser Wo AS xe LD WA eae oe AB ERS yuan A AN ne w wk We age WAN te t AN "
    ) * 3
    evidence = language_audit.classify_english_text([noise])

    assert evidence["word_count"] >= 100
    assert evidence["common_word_count"] >= 3
    assert evidence["poor_english_signal"] is True
    assert evidence["verdict"] == "review"
    decision = language_audit.automatic_english_decision(
        evidence,
        declared_language="en",
        expected_language="en",
        names=["Demon Slayer v17 [English]"],
    )
    assert decision["verdict"] == "review"
    assert decision["basis"] == "ocr_noise"
