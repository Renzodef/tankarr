"""region_counts treats a sentence sent back unchanged as untranslated, whatever hyphens, spacing or dots surround it."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


def load(name):
    path = Path(__file__).resolve().parents[1] / "contrib" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def regions(*pairs):
    return [
        SimpleNamespace(text=source, translation=target) for source, target in pairs
    ]


# (id, source as the OCR read it, what the model returned, expected (translated, untranslated))
CASES = [
    (
        "italian-copied-with-line-break-hyphen",
        "NON PUOI ANDARE LON- TANO DA QUI!",
        "NON PUOI ANDARE LONTANO DA QUI!",
        (0, 1),
    ),
    (
        "french-copied-with-line-break-hyphen",
        "JE NE VEUX PAS PAR- TIR MAINTENANT.",
        "Je ne veux pas partir maintenant.",
        (0, 1),
    ),
    ("copy-with-different-dots", "Dove stai andando..", "Dove stai andando...", (0, 1)),
    ("copy-without-the-space-before-a-mark", "ASPETTA ?!", "ASPETTA?!", (0, 1)),
    ("copy-with-double-space", "VIENI  QUI SUBITO", "VIENI QUI SUBITO", (0, 1)),
    ("exact-copy-in-other-case", "VIENI QUI SUBITO", "vieni qui subito", (0, 1)),
    (
        "genuine-english-translation",
        "NON PUOI ANDARE LON- TANO DA QUI!",
        "You can't go far from here!",
        (1, 0),
    ),
    (
        "partial-translation-still-counts-as-translated",
        "Dove stai andando con quella faccia?",
        "Where are you going con quella faccia?",
        (1, 0),
    ),
    ("numeric-only-region-is-ignored", "1234", "1234", (0, 0)),
    ("number-and-letters-copy", "3 PM", "3PM", (0, 1)),
    ("number-with-a-translated-word", "3 VOLTE", "3 times", (1, 0)),
    ("english-text-left-unchanged", "Okay then", "okay then", (0, 1)),
    ("name-returned-with-other-punctuation", "GARRETT?", "GARRETT", (0, 1)),
    ("name-returned-in-other-case", "GARRETT", "Garrett", (0, 1)),
    ("empty-translation", "CIAO", "", (0, 1)),
    ("provider-marker-left-in-translation", "CIAO A TUTTI", "<|12|>", (0, 1)),
    ("japanese-copy", "こんにちは", "こんにちは", (0, 1)),
    ("japanese-translated", "こんにちは", "Hello", (1, 0)),
]


@pytest.mark.parametrize("case", CASES, ids=[case[0] for case in CASES])
def test_region_counts(case):
    runner = load("translate_archive")
    _, source, target, expected = case
    assert runner.region_counts(regions((source, target))) == expected


def test_page_mixing_copies_and_translations_counts_each_region():
    runner = load("translate_archive")
    page = regions(
        ("VAI VIA, SUBI- TO!", "VAI VIA, SUBITO!"),
        ("Non lo so.", "I don't know."),
        ("42", "42"),
        ("CIAO ?!", "CIAO?!"),
    )
    assert runner.region_counts(page) == (1, 2)


def test_region_identity_is_the_one_the_rest_of_the_engine_uses():
    runner = load("translate_archive")
    assert runner.normalised("LON- TANO!") == runner.normalised("lontano")
    assert runner.normalised("a..b") == "ab"
