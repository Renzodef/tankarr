from __future__ import annotations

import json
import math

import pytest

from tankarr import fastjson

SAMPLES = [
    {"title": "Città — 進撃の巨人", "chapters": [{"n": "1.5", "pages": 20}]},
    [1, 2.5, 1e21, True, None, 'a/b"c\\d\n'],
    {"nested": {"deep": [[], {}, [{"k": "v"}]]}},
    {},
    [],
    "plain string",
    0.1,
]


@pytest.mark.parametrize("value", SAMPLES)
def test_output_matches_the_standard_encoder_byte_for_byte(value):
    expected = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
    assert fastjson.dumps(value) == expected


def test_non_string_keys_are_coerced_like_the_standard_encoder():
    value = {1: "one", 2.5: "two and a half", None: "none"}
    expected = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
    assert fastjson.dumps(value) == expected


def test_the_result_is_valid_json_that_round_trips():
    value = {"ratio": 0.3333333333333333, "big": 2**53, "list": list(range(50))}
    assert json.loads(fastjson.dumps(value)) == value


def test_an_unencodable_value_still_raises():
    with pytest.raises(TypeError):
        fastjson.dumps({"nan": object()})


def test_nan_is_not_emitted_as_invalid_json_when_orjson_is_present():
    if fastjson.orjson is None:
        pytest.skip("standard library only")
    assert fastjson.dumps({"v": math.nan}) == b'{"v":null}'
