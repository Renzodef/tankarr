import io
import json
import sys
import zipfile

import httpx
import pytest
from PIL import Image, PngImagePlugin
from test_translation_processor import _archive, _fake_engine, load


@pytest.mark.asyncio
async def test_cached_archive_skips_engine_and_source_decode(tmp_path, monkeypatch):
    runner = load("translate_archive")
    source, request = _archive(tmp_path, runner, 3)
    _fake_engine(monkeypatch, [])
    engine = sys.modules["manga_translator"].MangaTranslator

    def forbidden(*args):
        raise AssertionError("Cached pages must not initialize the engine")

    monkeypatch.setattr(engine, "__init__", forbidden)
    config = {
        "translator": {
            "translator": "chatgpt",
            "target_lang": "ENG",
            "no_text_lang_skip": True,
            "translator_chain": None,
            "selective_translation": None,
            "skip_lang": None,
        },
        "ocr": {"ocr": "48px", "use_mocr_merge": False},
        "render": {"renderer": "manga2eng", "rtl": True},
        "upscale": {"upscale_ratio": None},
        "detector": {"detection_size": 1024},
    }
    identity = runner.checkpoint_identity(request, config)
    checkpoints = tmp_path / "checkpoints"
    payload = io.BytesIO()
    Image.new("RGB", (32, 48), "white").save(payload, format="PNG")
    for index in range(1, 4):
        runner.save_checkpoint(
            checkpoints,
            index,
            payload.getvalue(),
            {
                "identity": identity,
                "index": index,
                "translated": 1,
                "untranslated": 0,
                "source_text": ["日本語"],
                "target_text": ["Where is the bridge?"],
            },
        )
    original_load = PngImagePlugin.PngImageFile.load

    def check_load(image, *args, **kwargs):
        assert not isinstance(getattr(image, "fp", None), zipfile.ZipExtFile)
        return original_load(image, *args, **kwargs)

    monkeypatch.setattr(PngImagePlugin.PngImageFile, "load", check_load)
    output = tmp_path / "result.cbz"
    await runner.translate(source, output, request, checkpoints=checkpoints)
    with zipfile.ZipFile(output) as archive:
        assert archive.testzip() is None
        receipt = json.loads(archive.read("translation.json"))
        assert receipt["performance"]["cached_pages"] == 3
        assert receipt["performance"]["engine_pages"] == 0
        assert receipt["translated_regions"] == 3
        assert all(
            archive.read(f"{i:05d}.png") == payload.getvalue() for i in range(1, 4)
        )


def test_catalog_preserves_integrity_checks(tmp_path):
    runner = load("translate_archive")
    payload = io.BytesIO()
    Image.new("RGB", (32, 48)).save(payload, format="PNG")
    meta = {
        "identity": "test",
        "index": 1,
        "translated": 1,
        "untranslated": 0,
        "source_text": ["hola"],
        "target_text": ["hello"],
    }
    runner.save_checkpoint(tmp_path, 1, payload.getvalue(), meta)
    catalog = runner.checkpoint_catalog(tmp_path)
    assert runner.load_checkpoint(tmp_path, 1, "test", (32, 48), catalog=catalog)
    assert (
        runner.load_checkpoint(tmp_path, 1, "changed", (32, 48), catalog=catalog)
        is None
    )
    assert (
        runner.load_checkpoint(tmp_path, 1, "test", (33, 48), catalog=catalog) is None
    )
    catalog[1][0].write_bytes(b"corrupted")
    assert (
        runner.load_checkpoint(tmp_path, 1, "test", (32, 48), catalog=catalog) is None
    )


@pytest.mark.asyncio
async def test_connection_reuse_does_not_reuse_authentication(monkeypatch):
    runner = load("translate_archive")
    seen = []

    def answer(req):
        seen.append(req)
        return httpx.Response(200)

    monkeypatch.setattr(runner, "PROVIDER_TRANSPORT", httpx.MockTransport(answer))
    async with runner.provider_session() as session:
        async with runner.provider_client() as one:
            await one.get(
                "https://one.example.test",
                headers={"Authorization": "Bearer fixture-one"},
            )
        async with runner.provider_client() as two:
            await two.get("https://two.example.test")
        assert one is two is session
        assert not session.is_closed
    assert session.is_closed
    assert runner._PROVIDER_SESSION.get() is None
    assert seen[0].headers["authorization"] == "Bearer fixture-one"
    assert "authorization" not in seen[1].headers


@pytest.mark.asyncio
async def test_session_cleanup_on_failure():
    runner = load("translate_archive")
    with pytest.raises(RuntimeError):
        async with runner.provider_session() as session:
            raise RuntimeError("fixture failure")
    assert session.is_closed
    assert runner._PROVIDER_SESSION.get() is None
