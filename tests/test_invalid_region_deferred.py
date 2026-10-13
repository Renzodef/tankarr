"""A page whose regions stay untranslated or invalid is refused as a suspect page, put off and listed; it is never kept."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image


def load(name):
    path = Path(__file__).resolve().parents[1] / "contrib" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _region(source, target):
    return SimpleNamespace(text=source, translation=target)


GOOD = _region("Hola amigo", "Hello friend")
COPIED = _region("Ven aquí ahora mismo", "Ven aquí ahora mismo")  # returned as it came
MARKER = _region("Dónde está", "<|2|>Where")  # a request marker leaked into the text
BLANK = _region("Tenemos que salir", "")


def _job(monkeypatch, tmp_path, runner, pages, fallback_pages=None):
    """A Spanish book with one page per item of `pages` (the regions the engine keeps).

    While the fallback OCR is installed the engine returns `fallback_pages[n]` instead, when it has one."""
    source = tmp_path / "source.cbz"
    with zipfile.ZipFile(source, "w") as archive:
        for number in range(1, len(pages) + 1):
            image = io.BytesIO()
            Image.new("RGB", (32, 48), (number, 0, 0)).save(image, format="PNG")
            archive.writestr(f"{number}.png", image.getvalue())
    request = {
        "source_sha256": runner.digest(source),
        "source_language": "es",
        "target_language": "en",
        "ai": {
            "base_url": "https://ai.example.test/v1",
            "model": "m",
            "api_key": "sk-test-0000",
        },
    }
    state = {"fallback": False, "installed": 0, "calls": []}

    def context(number):
        def read(image):
            kept = pages[number - 1]
            if state["fallback"] and fallback_pages and number in fallback_pages:
                kept = fallback_pages[number]
            return SimpleNamespace(
                result=image,
                textlines=[SimpleNamespace(text=r.text) for r in kept],
                text_regions=list(kept),
            )

        return read

    class Engine:
        def __init__(self, _params):
            pass

        async def translate(self, image, _config):
            state["calls"].append((image.getpixel((0, 0))[0], state["fallback"]))
            return context(image.getpixel((0, 0))[0])(image)

    monkeypatch.setitem(
        sys.modules, "manga_translator", SimpleNamespace(MangaTranslator=Engine)
    )
    monkeypatch.setitem(
        sys.modules, "manga_translator.config", SimpleNamespace(Config=lambda **kw: kw)
    )
    monkeypatch.setitem(
        sys.modules,
        "manga_translator.args",
        SimpleNamespace(reparse=lambda _args: SimpleNamespace(kernel_size=3)),
    )
    monkeypatch.setitem(
        sys.modules,
        "py3langid",
        SimpleNamespace(classify=lambda text: ("es" if "hola" in text else "en", 1)),
    )
    monkeypatch.setattr(runner, "provider_unavailable_reason", lambda _request: None)

    @contextlib.contextmanager
    def install():
        state["fallback"] = True
        state["installed"] += 1
        try:
            yield
        finally:
            state["fallback"] = False

    if fallback_pages is not None:
        monkeypatch.setattr(runner, "FALLBACK_OCR", ("vision", install))
    return source, request, tmp_path / "result.cbz", tmp_path / "checkpoints", state


def _saved(checkpoints):
    return sorted(int(path.name[:5]) for path in checkpoints.glob("*.zip"))


@pytest.mark.asyncio
async def test_address_only_colophon_keeps_address_without_inflating_translation_count(
    tmp_path, monkeypatch
):
    runner = load("translate_archive")
    address = _region("https://example.org/", "https://example.org/")
    source, request, output, checkpoints, _ = _job(
        monkeypatch, tmp_path, runner, [[GOOD], [address]]
    )
    await runner.translate(source, output, request, checkpoints=checkpoints)
    assert _saved(checkpoints) == [1, 2]
    with zipfile.ZipFile(output) as archive:
        receipt = json.loads(archive.read("translation.json"))
    assert receipt["translated_regions"] == 1
    assert receipt["untranslated_regions"] == 0
    with zipfile.ZipFile(next(checkpoints.glob("00002-*.zip"))) as archive:
        page = json.loads(archive.read("page.json"))
    assert page["source_text"] == page["target_text"] == [address.text]
    assert page["translated"] == page["untranslated"] == 0


@pytest.mark.asyncio
async def test_addresses_alone_cannot_make_a_translated_book(tmp_path, monkeypatch):
    runner = load("translate_archive")
    address = _region("https://example.org/", "https://example.org/")
    source, request, output, checkpoints, _ = _job(
        monkeypatch, tmp_path, runner, [[address]]
    )
    with pytest.raises(ValueError, match="No translated text was detected"):
        await runner.translate(source, output, request, checkpoints=checkpoints)
    assert not output.exists()


@pytest.mark.asyncio
async def test_page_with_address_and_copied_dialogue_remains_rejected(
    tmp_path, monkeypatch
):
    runner = load("translate_archive")
    address = _region("https://example.org/", "https://example.org/")
    source, request, output, checkpoints, _ = _job(
        monkeypatch, tmp_path, runner, [[address, COPIED], [GOOD]]
    )
    with pytest.raises(runner.SuspectPage, match=r"1 page\(s\) need review \(1\)"):
        await runner.translate(source, output, request, checkpoints=checkpoints)
    assert _saved(checkpoints) == [2]
    assert not output.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [COPIED, MARKER, BLANK])
async def test_an_invalid_page_is_put_off_and_the_valid_page_after_it_is_saved(
    tmp_path, monkeypatch, bad
):
    runner = load("translate_archive")
    source, request, output, checkpoints, _ = _job(
        monkeypatch, tmp_path, runner, [[GOOD, bad], [GOOD]]
    )
    with pytest.raises(
        runner.SuspectPage,
        match=r"1 page\(s\) need review \(1\); every other page is saved",
    ) as stop:
        await runner.translate(source, output, request, checkpoints=checkpoints)
    assert "untranslated or invalid text regions" in str(stop.value)
    assert not isinstance(stop.value, ValueError)
    assert runner.transient_error(stop.value)  # retried, then blocked, like any refusal
    assert _saved(checkpoints) == [2]  # the invalid page has no checkpoint
    assert (
        not output.exists()
        and not output.with_suffix(".partial").exists()
        or (not output.exists())
    )
    kept = [
        json.loads(line)
        for line in (checkpoints / "rejected-pages.jsonl").read_text().splitlines()
    ]
    assert [entry["page"] for entry in kept] == [1]
    assert bad.text[:20] in kept[0]["lines"]
    assert "Hola amigo" not in kept[0]["lines"]  # only what was refused


@pytest.mark.asyncio
async def test_a_book_that_keeps_an_invalid_page_is_never_finished(
    tmp_path, monkeypatch
):
    """No receipt, no result archive: a rerun refuses the same page again instead of importing the book."""
    runner = load("translate_archive")
    source, request, output, checkpoints, _ = _job(
        monkeypatch, tmp_path, runner, [[COPIED], [GOOD], [GOOD]]
    )
    for _ in range(2):
        with pytest.raises(runner.SuspectPage, match=r"\(1\)"):
            await runner.translate(source, output, request, checkpoints=checkpoints)
        assert not output.exists()
    assert _saved(checkpoints) == [2, 3]


@pytest.mark.asyncio
async def test_the_invalid_pages_are_listed_and_the_sixth_stops_the_book(
    tmp_path, monkeypatch
):
    runner = load("translate_archive")
    source, request, output, checkpoints, _ = _job(
        monkeypatch, tmp_path, runner, [[COPIED]] * 6 + [[GOOD]] * 2
    )
    with pytest.raises(
        runner.SuspectPage, match=r"Stopped after 6 refused pages \(1, 2, 3, 4, 5, 6\)"
    ):
        await runner.translate(source, output, request, checkpoints=checkpoints)
    assert _saved(checkpoints) == []  # the limit is the same as for any suspect page


@pytest.mark.asyncio
async def test_the_fallback_ocr_reads_an_invalid_page_again(tmp_path, monkeypatch):
    runner = load("translate_archive")
    source, request, output, checkpoints, state = _job(
        monkeypatch,
        tmp_path,
        runner,
        [[GOOD, COPIED], [GOOD]],
        fallback_pages={1: [GOOD]},
    )
    await runner.translate(source, output, request, checkpoints=checkpoints)
    assert state["installed"] == 1 and state["calls"] == [
        (1, False),
        (1, True),
        (2, False),
    ]
    first = json.loads(
        zipfile.ZipFile(next(checkpoints.glob("00001-*.zip"))).read("page.json")
    )
    assert first["ocr_fallback"] == "vision" and first["untranslated"] == 0
    assert _saved(checkpoints) == [1, 2] and output.exists()


@pytest.mark.asyncio
async def test_an_invalid_page_the_fallback_does_not_fix_is_still_refused(
    tmp_path, monkeypatch
):
    runner = load("translate_archive")
    source, request, output, checkpoints, state = _job(
        monkeypatch, tmp_path, runner, [[COPIED], [GOOD]], fallback_pages={1: [COPIED]}
    )
    with pytest.raises(runner.SuspectPage, match="also after reading it with vision"):
        await runner.translate(source, output, request, checkpoints=checkpoints)
    assert state["installed"] == 1 and _saved(checkpoints) == [2]
    assert not output.exists()


@pytest.mark.asyncio
async def test_a_credential_in_the_page_stays_a_hard_error(tmp_path, monkeypatch):
    """The page is valid, so the check that follows still stops the job: not a suspect page, nothing put off."""
    runner = load("translate_archive")
    leaked = _region(
        "Hola amigo", "Hello sk-test-0000 friend"
    )  # the job's key in the answer
    source, request, output, checkpoints, _ = _job(
        monkeypatch, tmp_path, runner, [[leaked], [GOOD]]
    )
    with pytest.raises(ValueError, match="credential") as stop:
        await runner.translate(source, output, request, checkpoints=checkpoints)
    assert not isinstance(stop.value, runner.SuspectPage)
    assert _saved(checkpoints) == []


@pytest.mark.asyncio
async def test_a_provider_that_is_unavailable_stays_a_hard_error(tmp_path, monkeypatch):
    runner = load("translate_archive")
    source, request, output, checkpoints, _ = _job(
        monkeypatch, tmp_path, runner, [[COPIED], [GOOD]]
    )
    monkeypatch.setattr(runner, "provider_unavailable_reason", lambda _r: "no balance")
    with pytest.raises(runner.ProviderUnavailable, match="no balance") as stop:
        await runner.translate(source, output, request, checkpoints=checkpoints)
    assert not isinstance(stop.value, runner.SuspectPage)
    assert _saved(checkpoints) == []


def test_region_counts_still_refuses_names_and_copies():
    """No allowlist: a name returned unchanged is untranslated for the counter, as before."""
    runner = load("translate_archive")
    assert runner.region_counts([_region("Motorô Mase", "Motorô Mase")]) == (0, 1)
    assert runner.region_counts([GOOD, COPIED, MARKER, BLANK]) == (1, 3)
