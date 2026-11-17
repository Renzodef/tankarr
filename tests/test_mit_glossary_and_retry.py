from __future__ import annotations

import importlib.util
import json
import socket
import zipfile
from pathlib import Path

import httpx


def load(name):
    path = Path(__file__).resolve().parents[1] / "contrib" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def archive(path, series):
    with zipfile.ZipFile(path, "w") as target:
        target.writestr("ComicInfo.xml", f"<ComicInfo><Series>{series}</Series></ComicInfo>")
        target.writestr("001.png", b"")
    return path


def test_series_glossary_is_found_by_comicinfo_series(tmp_path, monkeypatch):
    module = load("translate_archive")
    folder = tmp_path / "glossaries"
    folder.mkdir()
    slug = module.glossary_slug("Scorpione Rosso")
    (folder / f"{slug}.json").write_text(
        json.dumps({"terms": [{"source": "韋德", "target": "Wade", "variants": ["章德"]}]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(module, "GLOSSARY_DIR", folder)
    glossary = module.series_glossary(archive(tmp_path / "a.cbz", "Scorpione Rosso"))
    assert glossary["terms"][0]["target"] == "Wade"
    assert len(glossary["_digest"]) == 64
    assert module.normalise_variants("章德來了", glossary) == "韋德來了"
    assert "Wade" in module.glossary_note(glossary)
    assert module.series_glossary(archive(tmp_path / "b.cbz", "Other series")) is None


def test_unreachable_vision_server_is_retryable(monkeypatch):
    module = load("translate_archive")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    monkeypatch.setattr("time.sleep", lambda _: None)
    request = {"vision": {"base_url": f"http://127.0.0.1:{port}/v1"}}
    assert module.vision_reachable(request) is False
    assert module.vision_reachable({}) is True
    assert module.transient_error(httpx.RemoteProtocolError("Server disconnected"))


def test_long_narrow_strip_is_laid_out_along_the_strip():
    module = load("vision_balloon")
    assert module.fit_strip("West of the city. Inside a luxurious villa...", 352, 30)


def test_shape_with_artwork_inside_is_not_taken_for_a_balloon():
    import numpy as np
    from types import SimpleNamespace

    module = load("vision_balloon")
    page = np.full((300, 300, 3), 255, np.uint8)
    mask = np.ones((300, 300), np.uint8)
    region = SimpleNamespace(xyxy=[120, 40, 160, 120])
    page[60:100, 130:150] = 0  # the lettering
    assert module.foreign_ink(page, (0, 0, 300, 300), mask, [region]) < module.FOREIGN_INK_LIMIT
    for row in range(150, 290, 6):  # a face drawn in the same white area
        page[row : row + 2, 40:260] = 0
    assert module.foreign_ink(page, (0, 0, 300, 300), mask, [region]) > module.FOREIGN_INK_LIMIT
    caption = np.full((100, 100, 3), (30, 40, 120), np.uint8)
    caption[40:60, 20:80] = 255  # white text on a dark caption is its own lettering
    lettering = SimpleNamespace(xyxy=[20, 40, 80, 60])
    assert module.foreign_ink(caption, (0, 0, 100, 100), np.ones((100, 100), np.uint8), [lettering]) == 0


def test_japanese_columns_are_shown_to_the_model_with_their_breaks():
    from types import SimpleNamespace

    module = load("translate_archive")
    region = SimpleNamespace(texts=["たのんだ", "オートバイ", "ノワさんに"], text="たのんだオートバイノワさんに")
    restore = module.install_japanese_prompt("ja")
    try:
        from manga_translator.translators.common_gpt import CommonGPTTranslator  # noqa: F401
    except ImportError:
        restore()
        module.JAPANESE_ACTIVE = True
        module.japanese_columns([region])
        assert module.COLUMN_TEXT[region.text] == "たのんだ\u3000オートバイ\u3000ノワさんに"
        module.JAPANESE_ACTIVE = False
        return
    try:
        module.japanese_columns([region])
        assert module.COLUMN_TEXT[region.text] == "たのんだ\u3000オートバイ\u3000ノワさんに"
        assert "Japanese source" in module.JAPANESE_NOTE
        phrase = SimpleNamespace(texts=["素直じゃ", "ねーか"], text="素直じゃねーか")
        module.japanese_columns([phrase])
        assert phrase.text not in module.COLUMN_TEXT
    finally:
        restore()
    assert not module.COLUMN_TEXT
    assert module.install_japanese_prompt("zh")() is None


def test_sound_effects_are_lettered_in_english_over_the_artwork():
    import numpy as np
    from types import SimpleNamespace
    from PIL import Image

    module = load("vision_balloon")
    canvas = Image.fromarray(np.full((200, 200, 3), 255, np.uint8))
    effect = SimpleNamespace(xyxy=[40, 40, 160, 100], translation="thud")
    untranslated = SimpleNamespace(xyxy=[40, 120, 160, 180], translation="ドスン")
    module.draw_sound_effects(canvas, [effect, untranslated])
    pixels = np.asarray(canvas)
    assert (pixels[40:100, 40:160] < 128).any()
    assert (pixels[120:180, 40:160] == 255).all()
    assert module.SFX_TEXT.fullmatch("ドスン") and not module.SFX_TEXT.fullmatch("ell")
