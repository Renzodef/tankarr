from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


def load():
    path = Path(__file__).resolve().parents[1] / "contrib" / "translate_archive.py"
    spec = importlib.util.spec_from_file_location("translate_archive", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


engine = load()


def region(text, translation):
    return SimpleNamespace(text=text, translation=translation)


@pytest.mark.parametrize(
    "text",
    [
        "example.com",
        "Example.Com",
        "www.example.org",
        "sub.example.co.jp",
        "example.se",
        "example-site.net",
        "example.com/path/page-1.html",
        "example.com:8080/a?b=c#d",
        "https://example.com/a",
        "http://203.0.113.5/a",
    ],
)
def test_standalone_literal_addresses_are_recognised(text):
    assert engine.is_web_address(text)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "Hello.World",
        "e.g.",
        "i.e",
        "No.1",
        "Mr.Smith",
        "example",
        "example.invalidsuffixzz",
        "example.com is here",
        "visit example.com",
        "example.com.",
        ".example.com",
        "exa mple.com",
        "user@example.com",
        "user:pass@example.com",
        "example.com:99999",
        "example.com:abc",
        "ftp://example.com",
        "javascript:alert(1).com",
        "example..com",
        "-example.com",
        "example-.com",
        "192.0.2.5",
        "1.2.3.com",
        "例え.com",
        "example.com/%zz",
        "example.com/日本語",
        "その他.jp",
        "ある日。example.com",
    ],
)
def test_prose_and_malformed_strings_are_not_addresses(text):
    assert not engine.is_web_address(text)


def test_unchanged_bare_address_is_neither_translated_nor_untranslated():
    assert engine.region_counts([region("Example.Com", "Example.Com")]) == (0, 0)
    assert engine.region_counts([region("www.example.org/a", "www.example.org/a")]) == (
        0,
        0,
    )


def test_changed_or_missing_address_still_fails():
    assert engine.region_counts([region("example.com", "")]) == (0, 1)
    assert engine.region_counts([region("example.com", "example.net")]) == (0, 1)
    assert engine.region_counts([region("example.com", "Example.com")]) == (0, 1)


def test_narrative_text_keeps_the_original_gate():
    # Unchanged narrative, English-looking or not, is still untranslated prose.
    assert engine.region_counts([region("Hello.World", "Hello.World")]) == (0, 1)
    assert engine.region_counts([region("ありがとう", "ありがとう")]) == (0, 1)
    assert engine.region_counts([region("谢谢你", "谢谢你")]) == (0, 1)
    assert engine.region_counts([region("شكرا", "شكرا")]) == (0, 1)
    assert engine.region_counts([region("ありがとう", "Thank you")]) == (1, 0)
    assert engine.region_counts([region("visit example.com", "visit example.com")]) == (
        0,
        1,
    )


def test_mixed_batch_counts():
    regions = [
        region("RawLazy.Com", "RawLazy.Com"),
        region("ありがとう", "Thank you"),
        region("12", "12"),
        region("こんにちは", "こんにちは"),
    ]
    assert engine.region_counts(regions) == (1, 1)


@pytest.mark.parametrize(
    "raw",
    [
        "https://user:pw@example.com/a",
        "user:pw@example.com",
        "example.com/a?key=abc",
        "example.com/a?k=1&t=x",
        "example.com/a#frag1",
        "https://example.com/a?token=short#part",
        "example.com/a;sid=ab12",
        "example.com:8080/a?x=y",
        "prefix example.com?q=pw tail",
        "A" * 40 + ".example.com",
        "example.com/" + "b" * 30,
    ],
)
def test_redacted_excerpt_drops_every_secret_carrying_part(raw):
    cleaned = engine.redacted_excerpt(raw)
    for secret in (
        "pw",
        "abc",
        "key=",
        "token",
        "frag1",
        "ab12",
        "x=y",
        "t=x",
        "short",
    ):
        assert secret not in cleaned
    assert "A" * 24 not in cleaned and "b" * 24 not in cleaned


def test_redacted_excerpt_keeps_a_plain_address_and_cuts_length():
    assert engine.redacted_excerpt("Example.Com") == "Example.Com"
    assert engine.redacted_excerpt("www.example.org/path/page.html") == (
        "www.example.org/path/page.html"
    )
    assert len(engine.redacted_excerpt("a b " * 100)) == 60
    assert engine.redacted_excerpt(None) == ""


def test_rejection_record_keeps_the_returned_target_without_secrets(tmp_path):
    directory = tmp_path / "0123456789abcdef0123456789abcdef-1" / "checkpoints"
    rejected = [
        region("example.com", "Example.Com"),
        region("example.org", "https://user:pw@example.org/a?token=short#frag1"),
        region("example.net?k=pw2", "example.net"),
    ]
    engine.record_rejection(
        directory,
        3,
        "problem",
        2,
        1,
        rejected,
        {engine.redacted_excerpt("example.net?k=pw2"): "unchanged"},
    )
    entry = json.loads((directory / "rejected-pages.jsonl").read_text())
    assert entry["targets"][0] == "Example.Com"
    text = json.dumps(entry)
    for secret in ("pw", "token", "short", "frag1", "k=pw2"):
        assert secret not in text
    assert entry["provider_outcomes"] == {"example.net[redacted]": "unchanged"}


def test_rejection_record_without_translations_is_unchanged(tmp_path):
    directory = tmp_path / "0123456789abcdef0123456789abcdef-1" / "checkpoints"
    engine.record_rejection(
        directory, 1, "problem", 0, 0, [SimpleNamespace(text="x.com")]
    )
    entry = json.loads((directory / "rejected-pages.jsonl").read_text())
    assert "targets" not in entry and "provider_outcomes" not in entry


def run(coroutine):
    import asyncio

    return asyncio.run(coroutine)


def test_addresses_are_kept_out_of_the_provider_call_with_order_and_repeats():
    sent = []

    async def dispatch(delegated):
        sent.append(list(delegated))
        return [f"T:{text}" for text in delegated]

    texts = [
        "こんにちは",
        "Example.Com",
        "ありがとう",
        "example.com",
        "こんにちは",
        " www.example.org/a ",
    ]
    answers = run(engine.translate_around_addresses(texts, dispatch))
    assert sent == [["こんにちは", "ありがとう", "こんにちは"]]
    assert answers == [
        "T:こんにちは",
        "Example.Com",
        "T:ありがとう",
        "example.com",
        "T:こんにちは",
        " www.example.org/a ",
    ]


def test_a_page_of_addresses_only_makes_no_provider_call():
    async def dispatch(delegated):
        raise AssertionError("the provider must not be called")

    texts = ["Example.Com", "example.org", "Example.Com"]
    assert run(engine.translate_around_addresses(texts, dispatch)) == texts
    assert run(engine.translate_around_addresses([], dispatch)) == []


@pytest.mark.parametrize(
    "answers",
    [[], ["only one"], ["a", "b", "c"], "ab", None, ["a", 3], ["a", None]],
)
def test_an_incomplete_or_long_answer_is_a_failed_translation(answers):
    async def dispatch(delegated):
        return answers

    with pytest.raises(engine.TranslationFailed):
        run(
            engine.translate_around_addresses(
                ["こんにちは", "Example.Com", "ありがとう"], dispatch
            )
        )


def test_a_provider_error_is_not_swallowed():
    async def dispatch(delegated):
        raise engine.ProviderUnavailable("no balance")

    with pytest.raises(engine.ProviderUnavailable):
        run(engine.translate_around_addresses(["こんにちは", "Example.Com"], dispatch))


def test_blind_check_only_skips_pages_without_text_to_translate():
    assert not engine.needs_provider([region("Example.Com", "Example.Com")])
    assert not engine.needs_provider([])
    assert engine.needs_provider(
        [region("Example.Com", "Example.Com"), region("こんにちは", "")]
    )
    assert engine.needs_provider([region("visit example.com", "")])


def test_protection_does_not_relax_the_page_gate():
    # an address the engine altered after the fact still fails, as do copies, prose and real script
    assert engine.region_counts([region("Example.Com", "Example.Net")]) == (0, 1)
    assert engine.region_counts([region("Example.Com", "")]) == (0, 1)
    assert engine.region_counts([region("Example.Com", "Example.Com")]) == (0, 0)
    assert engine.region_counts([region("Thank you all", "Thank you all")]) == (0, 1)
    assert engine.region_counts([region("ありがとう", "ありがとう")]) == (0, 1)
    assert engine.region_counts([region("visit example.com", "visit example.com")]) == (
        0,
        1,
    )
    assert engine.region_counts([region("ありがとう", "<|1|>x")]) == (0, 1)


def line(text):
    return SimpleNamespace(text=text)


def test_uncovered_lines_are_always_recorded_and_dialogue_keeps_its_key():
    note = engine.uncovered_notes([line("Title"), line("Hey")], "en")
    assert note["uncovered_lines"] == ["Title", "Hey"]
    assert engine.uncovered_notes([], "en") == {}
    assert "uncovered_dialogue" not in engine.uncovered_notes([line("Title")], "en")
    long = engine.uncovered_notes([line("What are you doing here today?")], "en")
    assert long["uncovered_dialogue"] == ["What are you doing here today?"]
    assert long["uncovered_lines"] == ["What are you doing here today?"]


def test_uncovered_lines_are_bounded_and_redacted():
    many = [line(f"line {n}") for n in range(30)]
    assert len(engine.uncovered_notes(many, "en")["uncovered_lines"]) == 12
    note = engine.uncovered_notes(
        [line("see example.com?token=abc#x"), line("a" * 100)], "en"
    )
    assert "abc" not in str(note) and len(note["uncovered_lines"][1]) <= 40
