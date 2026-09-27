from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import unicodedata
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from PIL import Image, ImageOps, UnidentifiedImageError

COMMON_ENGLISH_WORDS = frozenset(
    {
        "a",
        "about",
        "after",
        "all",
        "and",
        "are",
        "as",
        "at",
        "be",
        "because",
        "but",
        "can",
        "come",
        "could",
        "did",
        "do",
        "for",
        "from",
        "get",
        "go",
        "good",
        "had",
        "has",
        "have",
        "he",
        "her",
        "here",
        "him",
        "his",
        "how",
        "i",
        "if",
        "in",
        "is",
        "it",
        "just",
        "know",
        "like",
        "me",
        "my",
        "no",
        "not",
        "now",
        "of",
        "oh",
        "on",
        "one",
        "or",
        "our",
        "out",
        "right",
        "said",
        "see",
        "she",
        "so",
        "some",
        "that",
        "the",
        "their",
        "them",
        "then",
        "there",
        "they",
        "this",
        "time",
        "to",
        "up",
        "want",
        "was",
        "we",
        "well",
        "were",
        "what",
        "when",
        "where",
        "who",
        "why",
        "will",
        "with",
        "would",
        "yes",
        "you",
        "your",
    }
)


def classify_english_text(texts: list[str]) -> dict[str, Any]:
    foreign_letters = sum(
        character.isalpha() and "LATIN" not in unicodedata.name(character, "")
        for text in texts
        for character in text
    )
    words = [
        word.casefold()
        for text in texts
        for word in re.findall(r"[A-Za-z]+(?:'[A-Za-z]+)?", text)
    ]
    common = [word for word in words if word in COMMON_ENGLISH_WORDS]
    long_words = [word for word in words if len(word) >= 3]
    long_word_ratio = len(long_words) / len(words) if words else 0.0
    # OCR of Japanese panels can hallucinate hundreds of isolated Latin
    # syllables, including common one-letter English words. Those fragments
    # must not become proof of English dialogue or a metadata fallback.
    poor_english_signal = len(words) >= 50 and long_word_ratio < 0.30
    confirmed = (
        len(words) >= 15
        and len(common) >= 3
        and len(long_words) >= 6
        and not poor_english_signal
    )
    return {
        "verdict": "refused"
        if foreign_letters >= 3
        else "confirmed"
        if confirmed
        else "review",
        "foreign_script_detected": foreign_letters >= 3,
        "foreign_letter_count": foreign_letters,
        "word_count": len(words),
        "common_word_count": len(common),
        "long_word_count": len(long_words),
        "long_word_ratio": round(long_word_ratio, 3),
        "poor_english_signal": poor_english_signal,
        "reason": (
            "OCR found text in a non-Latin alphabet"
            if foreign_letters >= 3
            else "OCR found repeated English-language dialogue signals"
            if confirmed
            else "OCR found mostly short fragments, not English dialogue"
            if poor_english_signal
            else "OCR did not find enough English-language dialogue evidence"
        ),
    }


def _sample_indexes(page_count: int) -> list[int]:
    if page_count <= 0:
        return []
    candidates = {
        min(page_count - 1, max(0, round((page_count - 1) * ratio)))
        for ratio in (0.18, 0.5, 0.82)
    }
    return sorted(candidates)


def automatic_english_decision(
    audit: dict[str, Any],
    *,
    declared_language: str,
    expected_language: str,
    names: list[str],
) -> dict[str, Any]:
    """Keep inconclusive OCR distinct from an allowed metadata fallback."""
    contradiction = re.compile(
        r"\b(?:French|Spanish|Italian|Japanese|Chinese|Korean|Russian|German|Portuguese|Polish|Arabic|Hebrew|Thai|Vietnamese|Indonesian)\b"
        r"|[\[(]\s*(?:raw|fr|fra|es|spa|it|ita|ja|jpn|jp|zh|chi|cn|ko|kor|ru|rus|de|ger|pt|por|pl|ar|he|th|vi|id)\s*[\])]",
        re.IGNORECASE,
    )
    if audit.get("foreign_script_detected") or audit.get("verdict") == "refused":
        return {
            "verdict": "refused",
            "basis": "foreign_script",
            "reason": "Sampled pages contain another alphabet",
        }
    if audit.get("poor_english_signal"):
        return {
            "verdict": "review",
            "basis": "ocr_noise",
            "reason": "OCR found mostly short fragments; confirm the page language manually",
        }
    if declared_language != expected_language or expected_language != "en":
        return {
            "verdict": "refused",
            "basis": "release_language",
            "reason": "Release language does not match the series language",
        }
    # A series title may legitimately contain "Japanese" or "Italian". Only
    # explicit release tags make a language claim that contradicts metadata.
    tags = [
        tag
        for name in names
        for tag in re.findall(r"[\[(][^\])]{1,120}[\])]", unquote(name))
    ]
    if any(contradiction.search(tag) for tag in tags):
        return {
            "verdict": "refused",
            "basis": "filename_language",
            "reason": "Release or file name declares another language",
        }
    if audit.get("verdict") == "confirmed":
        return {
            "verdict": "confirmed",
            "basis": "ocr",
            "reason": "OCR confirmed English dialogue",
        }
    return {
        "verdict": "confirmed",
        "basis": "metadata_fallback",
        "reason": "English release tag matches the series; OCR is inconclusive and names do not contradict it",
    }


def audit_english_pages(
    pages: list[Path], *, detect_scripts: bool = False
) -> dict[str, Any]:
    """OCR three representative pages and retain only aggregate evidence."""

    executable = shutil.which("tesseract")
    indexes = _sample_indexes(len(pages))
    if executable is None:
        return {
            "verdict": "review",
            "sampled_pages": len(indexes),
            "word_count": 0,
            "common_word_count": 0,
            "long_word_count": 0,
            "reason": "Tesseract is unavailable; OCR cannot confirm the page language",
        }
    texts: list[str] = []
    failures = 0
    scripts: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="tankarr-ocr-") as temporary:
        temporary_root = Path(temporary)
        for sample_number, index in enumerate(indexes, start=1):
            prepared = temporary_root / f"sample-{sample_number}.png"
            try:
                with Image.open(pages[index]) as image:
                    image = ImageOps.exif_transpose(image).convert("L")
                    image.thumbnail((1800, 1800))
                    image = ImageOps.autocontrast(image)
                    image.save(prepared, format="PNG", optimize=True)
                if detect_scripts:
                    try:
                        orientation = subprocess.run(
                            [
                                executable,
                                str(prepared),
                                "stdout",
                                "-l",
                                "osd",
                                "--psm",
                                "0",
                            ],
                            capture_output=True,
                            text=True,
                            timeout=15,
                            check=False,
                        )
                        script = re.search(
                            r"^Script:\s*(\w+)", orientation.stdout, re.MULTILINE
                        )
                        confidence = re.search(
                            r"^Script confidence:\s*([\d.]+)",
                            orientation.stdout,
                            re.MULTILINE,
                        )
                        if orientation.returncode == 0 and script and confidence:
                            scripts.append(
                                {
                                    "script": script[1],
                                    "confidence": float(confidence[1]),
                                }
                            )
                    except (OSError, subprocess.TimeoutExpired, ValueError):
                        pass
                result = subprocess.run(
                    [
                        executable,
                        str(prepared),
                        "stdout",
                        "-l",
                        "eng",
                        "--psm",
                        "11",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=45,
                    check=False,
                )
                if result.returncode == 0:
                    texts.append(result.stdout[:200_000])
                else:
                    failures += 1
            except (
                OSError,
                subprocess.TimeoutExpired,
                UnidentifiedImageError,
                # A page whose format is recognised but whose contents are
                # damaged raises SyntaxError/ValueError from the decoder. One
                # unreadable page is evidence this sample failed, never a
                # reason to abandon the whole import.
                SyntaxError,
                ValueError,
            ):
                failures += 1
    evidence = classify_english_text(texts)
    evidence.update(
        {
            "sampled_pages": len(indexes),
            "successful_samples": len(texts),
            "failed_samples": failures,
            "engine": "tesseract-eng",
        }
    )
    if not texts:
        evidence["verdict"] = "review"
        evidence["reason"] = "OCR could not read any sampled page"
    if detect_scripts:
        evidence["script_samples"] = scripts
        foreign_script = any(
            sample["script"] not in {"Latin", "Common", "Unknown"}
            and sample["confidence"] >= 2
            for sample in scripts
        )
        # Script detection reads the whole page, and an official English
        # digital edition keeps the Japanese sound effects drawn into the
        # art: Mars v12 (XRA-Empire) came back "Japanese" with 180 words of
        # OCR'd English dialogue and not one non-Latin letter. Dialogue the
        # OCR actually read outranks a script guess about the artwork; the
        # guess only decides when the text itself gives no English signal.
        # A page the OCR could not read says nothing about its alphabet
        # either. Ghost in the Shell 2 is painted throughout: tesseract
        # failed on all three sampled pages and a 7%-confidence "Cyrillic"
        # guess about the artwork refused the only English edition there is.
        # With no text read at all the guess decides nothing; a person does.
        if foreign_script and not texts:
            evidence["script_note"] = (
                "a script guess on pages the OCR could not read is not evidence"
            )
        elif foreign_script and not (
            evidence.get("verdict") == "confirmed"
            and int(evidence.get("foreign_letter_count") or 0) < 3
        ):
            evidence["verdict"] = "refused"
            evidence["foreign_script_detected"] = True
            evidence["reason"] = "Sampled pages use a non-Latin alphabet"
        elif foreign_script:
            evidence["script_note"] = (
                "non-Latin script seen in the artwork, dialogue is English"
            )
    return evidence
