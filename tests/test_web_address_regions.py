"""Literal addresses survive translation without exempting copied dialogue."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


def runner():
    path = Path(__file__).resolve().parents[1] / "contrib/translate_archive.py"
    spec = importlib.util.spec_from_file_location("web_address_runner", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "address",
    [
        "http://example.org/",
        "https://example.org/catalogue?volume=2&language=ja#credits",
        "https://EXAMPLE.org:8443/a%20b.pdf",
        "http://192.0.2.1/",
        "http://[2001:db8::1]:8080/",
        "https://nas.local/path",
    ],
)
def test_preserved_web_address_is_not_translation_or_untranslated_prose(address):
    assert runner().region_counts(
        [SimpleNamespace(text=address, translation=address)]
    ) == (0, 0)


@pytest.mark.parametrize(
    "target", [None, "", "https://example.org/other", "HTTP://EXAMPLE.ORG/"]
)
def test_missing_or_changed_web_address_still_fails(target):
    assert runner().region_counts(
        [SimpleNamespace(text="http://example.org/", translation=target)]
    ) == (0, 1)


@pytest.mark.parametrize(
    "text",
    [
        "Visita http://example.org/ per leggere il libro.",
        "続きはhttp://example.org/で読めます",
        "https://example.org/物語",
        "https://example.org/hello world",
        "https://",
        "http:///path",
        "https://bad_host.example/",
        "https://example.org:invalid/",
        "https://example.org:99999/",
        "http://[invalid]/",
        "http://999.999.999.999/",
        "https://@example.org/",
        "https://example.org/%GG",
        "https://example.org/\nありがとう",
        "https://example.org/\x00",
        "https://user:fixture-password@example.org/",
        "ftp://example.org/",
        "mailto:reader@example.org",
        "こんにちは",
        "JE NE VEUX PAS PARTIR.",
        "Yoshikazu Example",
    ],
)
def test_addresses_do_not_exempt_prose_malformed_urls_or_other_identifiers(text):
    assert runner().region_counts([SimpleNamespace(text=text, translation=text)]) == (
        0,
        1,
    )


def test_page_counts_only_effective_translation_and_keeps_copied_prose_rejected():
    regions = [
        SimpleNamespace(text="こんにちは", translation="Hello"),
        SimpleNamespace(
            text="https://example.org/", translation="https://example.org/"
        ),
        SimpleNamespace(text="VAI VIA!", translation="vai via"),
    ]
    assert runner().region_counts(regions) == (1, 1)
