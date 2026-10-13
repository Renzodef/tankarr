"""Regression pins for the page gate on English lines of a non-Latin-script book.

An engine answer that equals the source text counts as untranslated, and a dropped region that reads as dialogue is a
lost region. A credit line already in English (AND ALL. READERS) is refused by that gate, but no rule that accepts it
was found that also keeps refusing upper-case English dialogue (PLEASE COME BACK): the string alone does not tell them
apart and the page position is not evidence. These tests pin the refusal so any future acceptance has to change them
on purpose, with a positive and text-independent signal that the region is a credit.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


def load():
    path = Path(__file__).resolve().parents[1] / "contrib" / "translate_archive.py"
    spec = importlib.util.spec_from_file_location("translate_archive_gate", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


engine = load()


def region(text, translation):
    return SimpleNamespace(text=text, translation=translation)


def trace_with(sent):
    trace = engine.ProviderTrace("https://api.example.test")
    for text, outcome in sent:
        trace.sent[engine.normalised(text)] = {"text": text, "outcome": outcome}
    return trace


ENGLISH_DIALOGUE = [
    "PLEASE COME BACK",
    "YOU ARE DEAD",
    "THEY KILLED MY BROTHER",
    "THANK YOU FOR NOTHING",
    "WE WILL WIN TOGETHER",
    "GOOD MORNING EVERYONE",
    "I will never forgive you.",
    "Thank you for reading, everyone",
    "WHY DID YOU DO THAT?",
    "RUN!",
    "WAIT...",
    "HELLO, SHINOBU!",
]

CREDIT_LIKE = [
    "AND ALL. READERS",
    "SHINOBU ABEBE HIROTAKA OHKUMA NAOKI HYODOH NOBUAK TAKANO RYO",
    "SPECIAL THANKS TO ALL STAFF",
]


@pytest.mark.parametrize("text", ENGLISH_DIALOGUE)
def test_region_counts_refuses_an_english_line_returned_unchanged(text):
    # region_counts takes no source language: this checks the unchanged-answer rule itself, not any book context.
    assert engine.region_counts([region(text, text)]) == (0, 1)


@pytest.mark.parametrize("text", CREDIT_LIKE)
def test_credit_like_english_lines_returned_unchanged_are_also_refused(text):
    # Deliberately pinned, not endorsed: these lines are already English and need no translation, yet nothing in the
    # text proves they are credits and not dialogue, so the gate keeps refusing them and the page goes to review.
    assert engine.region_counts([region(text, text)]) == (0, 1)


def test_an_answer_equal_to_the_source_apart_from_punctuation_or_case_is_still_untranslated():
    assert engine.region_counts([region("AND ALL. READERS", "AND ALL READERS")]) == (
        0,
        1,
    )
    assert engine.region_counts([region("PLEASE COME BACK", "Please come back")]) == (
        0,
        1,
    )


def test_a_real_translation_counts_and_an_empty_answer_is_refused():
    assert engine.region_counts([region("谢谢你", "Thank you")]) == (1, 0)
    assert engine.region_counts([region("AND ALL. READERS", "")]) == (0, 1)


@pytest.mark.parametrize("language", ["zh", "ja"])
@pytest.mark.parametrize("text", ENGLISH_DIALOGUE[:6])
def test_short_english_lines_dropped_by_the_engine_are_not_lost_regions_on_a_non_latin_book(
    text, language
):
    # Existing design, pinned: only source-script text counts as lost on these books ("poster words, credits and signs
    # already in the target script need no translation"). The gap with the kept-and-unchanged path, which refuses the
    # same lines, is the asymmetry the review found; it is not changed here.
    trace = trace_with([(text, "same")])
    assert trace.lost([], language) == []


def test_a_long_dropped_line_of_many_different_words_is_lost_even_on_a_non_latin_book():
    names = CREDIT_LIKE[1]
    trace = trace_with([(names, "same")])
    assert [entry["text"] for entry in trace.lost([], "zh")] == [names]


def test_a_dropped_question_or_source_script_line_is_lost_on_a_non_latin_book():
    trace = trace_with([("WHY DID YOU DO THAT?", "same"), ("谢谢大家", "same")])
    assert {entry["text"] for entry in trace.lost([], "zh")} == {
        "WHY DID YOU DO THAT?",
        "谢谢大家",
    }


def test_a_failed_request_is_lost_whatever_it_says():
    trace = trace_with([("AND ALL. READERS", "failed")])
    assert [entry["why"] for entry in trace.lost([], "zh")] == ["its request failed"]
