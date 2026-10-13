from __future__ import annotations

from types import SimpleNamespace

from PIL import Image

from tankarr import language_audit

ENGLISH = (
    "I don't know what you want from me. We were there when it happened, and "
    "you said it would be fine. Well, it was not fine, was it? Where do we go now?"
)


def _fake_run(osd_script: str, text: str, *, confidence: float = 6.0):
    def run(command, **_kwargs):
        if "osd" in command:
            return SimpleNamespace(
                returncode=0,
                stdout=f"Script: {osd_script}\nScript confidence: {confidence}\n",
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


def test_weak_script_guess_with_noisy_all_caps_english_requires_review(
    tmp_path, monkeypatch
):
    dialogue = (
        "WE WERE OUT THERE AND NOW WE CAN SEE THE WAY HOME. "
        "YOU KNOW WHAT THEY WANT AND WHERE THEY WENT. "
    )
    noisy_artwork = " x y z " * 100

    def run(command, **_kwargs):
        if "osd" in command:
            return SimpleNamespace(
                returncode=0,
                stdout="Script: Cyrillic\nScript confidence: 3.33\n",
                stderr="",
            )
        page = int(command[1].rsplit("sample-", 1)[1].split(".", 1)[0])
        text = dialogue + noisy_artwork if page < 3 else noisy_artwork
        return SimpleNamespace(returncode=0, stdout=text, stderr="")

    monkeypatch.setattr(language_audit.shutil, "which", lambda _name: "tesseract")
    monkeypatch.setattr(language_audit.subprocess, "run", run)

    evidence = language_audit.audit_english_pages(_pages(tmp_path), detect_scripts=True)
    decision = language_audit.automatic_english_decision(
        evidence,
        declared_language="en",
        expected_language="en",
        names=["Example [English]"],
    )

    assert evidence["poor_english_signal"] is True
    assert evidence["foreign_letter_count"] == 0
    assert evidence["foreign_script_detected"] is False
    assert evidence["verdict"] == "review"
    assert "weak script guess" in evidence["script_note"]
    assert decision["verdict"] == "review"


def test_confident_script_guess_without_foreign_letters_stays_manual_review(
    tmp_path, monkeypatch
):
    # Stylized English lettering can produce a confident but wrong OSD script
    # guess. OCR fragments remain inconclusive and must still require review.
    noise = (
        "CT Wet MD A i ta Be NM aD whim x al a a ko I HT Fe SS d oa al Ne "
        "be ars RE wb ih hk held a BY hy zg be Ww i Ml VJ MI ial Xl bd Se "
        "Ser Wo AS xe LD WA ERS yuan A AN ne w wk We age WAN te t AN "
    ) * 3
    monkeypatch.setattr(language_audit.shutil, "which", lambda _name: "tesseract")
    monkeypatch.setattr(
        language_audit.subprocess,
        "run",
        _fake_run("Cyrillic", noise, confidence=6.41),
    )

    evidence = language_audit.audit_english_pages(_pages(tmp_path), detect_scripts=True)
    decision = language_audit.automatic_english_decision(
        evidence,
        declared_language="en",
        expected_language="en",
        names=["MangaDex English scanlation"],
    )

    assert evidence["foreign_letter_count"] == 0
    assert evidence["poor_english_signal"] is True
    assert evidence["foreign_script_detected"] is False
    assert evidence["verdict"] == "review"
    assert "manual review required" in evidence["script_note"]
    assert decision["verdict"] == "review"
    assert decision["basis"] == "ocr_noise"


def test_weak_script_guess_still_refuses_detected_foreign_letters(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(language_audit.shutil, "which", lambda _name: "tesseract")
    monkeypatch.setattr(
        language_audit.subprocess,
        "run",
        _fake_run(
            "Cyrillic", "WE WERE THERE AND WE CAN SEE. Привет мир", confidence=3.33
        ),
    )

    evidence = language_audit.audit_english_pages(_pages(tmp_path), detect_scripts=True)

    assert evidence["foreign_letter_count"] >= 3
    assert evidence["foreign_script_detected"] is True
    assert evidence["verdict"] == "refused"


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


NOISE = (
    "CT Wet MD A i ta Be NM aD whim x al a a ko I HT Fe SS d oa al Ne "
    "be ars RE wb ih hk held a BY hy zg be Ww i Ml VJ MI ial Xl bd Se "
    "Ser Wo AS xe LD WA eae oe AB ERS yuan A AN ne w wk We age WAN te t AN "
)


def _guessing_run(guesses: list[tuple[str, float]], text: str):
    """One script guess per sampled page, in page order."""
    remaining = list(guesses)

    def run(command, **_kwargs):
        if "osd" in command:
            script, confidence = remaining.pop(0)
            return SimpleNamespace(
                returncode=0,
                stdout=f"Script: {script}\nScript confidence: {confidence}\n",
                stderr="",
            )
        return SimpleNamespace(returncode=0, stdout=text, stderr="")

    return run


def test_one_page_guessing_a_script_does_not_refuse_the_book(tmp_path, monkeypatch):
    """Official English chapters came back "Arabic" 5.0, "Cyrillic" 15.0 and
    "Greek" 2.2 on one sampled page each, with no non-Latin letter read and
    the other pages disagreeing: Japanese book spines drawn into the art."""
    monkeypatch.setattr(
        language_audit.shutil, "which", lambda _name: "/usr/bin/tesseract"
    )
    guesses = [("Japanese", 0.67), ("Arabic", 5.0), ("Japanese", 0.29)]
    monkeypatch.setattr(
        language_audit.subprocess, "run", _guessing_run(guesses, NOISE * 3)
    )

    evidence = language_audit.audit_english_pages(_pages(tmp_path), detect_scripts=True)

    assert evidence["foreign_letter_count"] == 0
    assert evidence["poor_english_signal"] is True
    assert evidence["verdict"] == "review"
    assert evidence.get("foreign_script_detected") is False
    assert "single page" in evidence["script_note"]
    decision = language_audit.automatic_english_decision(
        evidence,
        declared_language="en",
        expected_language="en",
        names=["Series 168 (2024) (Digital)"],
    )
    assert decision["verdict"] == "review"
    assert decision["basis"] == "ocr_noise"


def test_the_same_script_on_two_pages_still_refuses_the_book(tmp_path, monkeypatch):
    monkeypatch.setattr(
        language_audit.shutil, "which", lambda _name: "/usr/bin/tesseract"
    )
    # No English dialogue: noisy English fragments remain reviewable under
    # the existing audit rules, covered by the dedicated test above.
    guesses = [("Japanese", 4.2), ("Arabic", 0.5), ("Japanese", 2.5)]
    monkeypatch.setattr(
        language_audit.subprocess, "run", _guessing_run(guesses, "ka ta na")
    )

    evidence = language_audit.audit_english_pages(_pages(tmp_path), detect_scripts=True)

    assert evidence["verdict"] == "refused"
    assert evidence["foreign_script_detected"] is True
    assert evidence["reason"] == "Sampled pages use a non-Latin alphabet"
    decision = language_audit.automatic_english_decision(
        evidence,
        declared_language="en",
        expected_language="en",
        names=["Series v01 (raw)"],
    )
    assert decision["verdict"] == "refused"
    assert decision["basis"] == "foreign_script"


def test_uncorroborated_script_with_sparse_ocr_requires_manual_confirmation(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(language_audit.shutil, "which", lambda _name: "tesseract")
    monkeypatch.setattr(
        language_audit.subprocess,
        "run",
        _guessing_run(
            [("Japanese", 6.0), ("Latin", 3.0), ("Unknown", 0.0)], "ka ta na"
        ),
    )

    evidence = language_audit.audit_english_pages(_pages(tmp_path), detect_scripts=True)
    assert evidence["verdict"] == "review"
    assert not evidence["poor_english_signal"]
    decision = language_audit.automatic_english_decision(
        evidence,
        declared_language="en",
        expected_language="en",
        names=["Series v01 [English]"],
    )
    assert decision["verdict"] == "review"
    assert decision["basis"] == "script_uncertain"
