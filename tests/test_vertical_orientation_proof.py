from __future__ import annotations

import importlib.util
from pathlib import Path


def load(name):
    path = Path(__file__).resolve().parents[1] / "contrib" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ta = load("translate_archive")

LEXICONS = {
    "en": {"move", "end", "no", "on", "pain", "the", "story"},
    "fr": {"pain", "fin", "le"},
}
MARGIN = (28, 1015, 50, 250)
PAGE = (900, 1280)
ROLE = {
    "role": "end marker of the story",
    "basis": "left outer margin of the last page, outside every balloon",
}


def decide(engine_used, engine_turned, as_printed, turned, **changes):
    arguments = {
        "box": MARGIN,
        "page_size": PAGE,
        "engine_readings": {"used": engine_used, "turned": engine_turned},
        "reader_readings": {"as_printed": as_printed, "turned": turned},
        "source_language": "zh",
        "target_language": "en",
        "lexicons": LEXICONS,
        "role": ROLE,
    }
    arguments.update(changes)
    return ta.orientation_decision(**arguments)


def test_a_marker_read_the_same_way_by_every_reading_of_the_turned_crop_is_kept_as_printed():
    decision = decide(
        ("[ON3 3A0W]", 0.8),
        ("[MOVE END]", 0.95),
        ("[MOVE END]", 1.0),
        ("[MOVE END]", 1.0),
    )
    assert decision["accepted"] and decision["use"] == "keep_printed"
    assert decision["words"] == ["move", "end"]
    assert decision["role"] == ROLE


def test_imove_is_not_move_end_a_letter_is_never_dropped_as_a_guessed_bracket():
    # The very readings of the original MOVE c006 page 33 margin: the reader reads IMOVE one way and MOVE the other.
    decision = decide(
        ("[ON3 3A0W]", 0.8),
        ("[MOVE END]", 0.95),
        ("IMOVE END]", 1.0),
        ("[MOVE END]", 1.0),
    )
    assert not decision["accepted"]
    assert "word for word" in decision["why"]
    assert ta.whole_tokens("IMOVE END]") == ["imove", "end"]


def test_two_orientations_that_are_both_words_stay_refused():
    decision = decide(("ON", 0.99), ("NO", 0.99), ("NO", 1.0), ("NO", 1.0))
    assert not decision["accepted"]
    assert "both orientations" in decision["why"]


def test_a_word_shared_with_another_latin_language_is_not_proved_to_be_already_english():
    decision = decide(("NIAd", 0.7), ("PAIN", 0.99), ("PAIN", 1.0), ("PAIN", 1.0))
    assert not decision["accepted"]
    assert "fr" in decision["why"]


def test_in_a_french_book_the_corrected_word_is_french_source_text_for_the_ordinary_translation():
    decision = decide(
        ("NIAd", 0.7),
        ("PAIN", 0.99),
        ("PAIN", 1.0),
        ("PAIN", 1.0),
        source_language="fr",
    )
    assert decision["accepted"] and decision["use"] == "translate"
    assert decision["text"] == "PAIN"


def test_a_line_without_an_independent_reading_of_its_role_is_not_kept():
    for role in (None, {"role": "end marker"}, {"role": "", "basis": "margin"}):
        decision = decide(
            ("[ON3 3A0W]", 0.8),
            ("[MOVE END]", 0.95),
            ("[MOVE END]", 1.0),
            ("[MOVE END]", 1.0),
            role=role,
        )
        assert not decision["accepted"]
        assert "role" in decision["why"]


def test_a_horizontal_or_foreign_box_is_never_turned():
    readings = (
        ("[ON3 3A0W]", 0.8),
        ("[MOVE END]", 0.95),
        ("[MOVE END]", 1.0),
        ("[MOVE END]", 1.0),
    )
    assert "vertical" in decide(*readings, box=(28, 1015, 250, 50))["why"]
    for box in ((880, 1015, 50, 250), (28, 1100, 50, 250), (-1, 0, 50, 250), "margin"):
        assert "original page" in decide(*readings, box=box)["why"]


def test_a_reader_that_disagrees_or_is_unsure_refuses_the_turn():
    assert (
        "word for word"
        in decide(
            ("[ON3 3A0W]", 0.8),
            ("[MOVE END]", 0.95),
            ("[MOVE END]", 1.0),
            ("[MOVE AND]", 1.0),
        )["why"]
    )
    assert (
        "word for word"
        in decide(
            ("[ON3 3A0W]", 0.8),
            ("[MOVE END]", 0.95),
            ("[MOVE END THE]", 1.0),
            ("[MOVE END THE]", 1.0),
        )["why"]
    )
    assert (
        "not confident"
        in decide(
            ("[ON3 3A0W]", 0.8),
            ("[MOVE END]", 0.95),
            ("[MOVE END]", 0.6),
            ("[MOVE END]", 1.0),
        )["why"]
    )
    assert (
        "missing"
        in decide(("[ON3 3A0W]", 0.8), ("[MOVE END]", 0.95), ("[MOVE END]", 1.0), None)[
            "why"
        ]
    )


def test_cjk_lettering_and_a_line_already_read_rightly_are_left_to_the_ordinary_path():
    assert "Latin" in decide(("完", 0.8), ("完", 0.95), ("完", 1.0), ("完", 1.0))["why"]
    assert (
        "nothing to turn"
        in decide(
            ("[MOVE END]", 0.9),
            ("[MOVE END]", 0.95),
            ("[MOVE END]", 1.0),
            ("[MOVE END]", 1.0),
        )["why"]
    )


def test_words_of_no_language_are_refused_even_when_the_readings_agree():
    decision = decide(("XZQ", 0.8), ("QZX", 0.95), ("QZX", 1.0), ("QZX", 1.0))
    assert not decision["accepted"]


def test_nonfinite_and_invalid_confidence_never_proves_an_orientation():
    for confidence in (float("nan"), float("inf"), -1, 2, True):
        decision = decide(
            ("[ON3 3A0W]", 0.8),
            ("[MOVE END]", confidence),
            ("[MOVE END]", 1.0),
            ("[MOVE END]", 1.0),
        )
        assert not decision["accepted"]


def test_a_kept_line_must_be_delivered_as_the_printed_words_over_the_original_pixels():
    decision = decide(
        ("[ON3 3A0W]", 0.8),
        ("[MOVE END]", 0.95),
        ("[MOVE END]", 1.0),
        ("[MOVE END]", 1.0),
    )
    pixels = b"original crop"
    assert ta.printed_target_kept(decision, "[MOVE END]", pixels, pixels)
    assert not ta.printed_target_kept(decision, "THE MOVE END STORY", pixels, pixels)
    assert not ta.printed_target_kept(decision, "MOVE END", pixels, b"repainted crop")
    assert not ta.printed_target_kept({"accepted": False}, "[MOVE END]", pixels, pixels)
