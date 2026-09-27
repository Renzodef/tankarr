from __future__ import annotations

import asyncio
import io
import json
import shutil
import zipfile
from unittest.mock import AsyncMock

import httpx
import pytest
import respx
from PIL import Image, ImageDraw, ImageFont

from tankarr import local_translation as local
from tankarr.archive import sha256

ENGLISH = "The weather is beautiful today. We should go outside and enjoy the sunshine together."
ITALIAN = "Il tempo oggi è bellissimo. Dovremmo uscire e goderci insieme questa splendida giornata di sole."


def fixture(tmp_path, pages=2):
    image = Image.new("RGB", (1200, 800), "white")
    ImageDraw.Draw(image).multiline_text(
        (80, 100),
        "The weather is beautiful today.\nWe should go outside and enjoy\nthe sunshine together.",
        font=ImageFont.truetype("DejaVuSans.ttf", 32),
        fill="black",
        spacing=12,
    )
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    source = tmp_path / "source.cbz"
    with zipfile.ZipFile(source, "w") as archive:
        for i in range(pages):
            archive.writestr(f"{i + 1:05d}.png", buffer.getvalue())
    request = {
        "source_sha256": sha256(source),
        "source_language": "en",
        "target_language": "it",
        "ai": {
            "base_url": "https://provider.example/v1",
            "model": "test-model",
            "api_key": "private-provider-key",
        },
    }
    return source, tmp_path / "result.cbz", request


def response(request):
    texts = json.loads(json.loads(request.content)["messages"][1]["content"])
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": json.dumps([ITALIAN] * len(texts))}}]
        },
    )


@pytest.mark.asyncio
async def test_real_ocr_to_validated_archive_without_processor(tmp_path):
    if not shutil.which("tesseract"):
        pytest.skip("Tesseract is not installed on this test host")
    source, output, request = fixture(tmp_path)
    progress = []
    with respx.mock as mock:
        route = mock.post("https://provider.example/v1/chat/completions").mock(
            side_effect=response
        )
        await local.translate_archive(
            source, output, request, lambda *v: progress.append(v)
        )
        assert route.call_count == 2
    with zipfile.ZipFile(output) as archive:
        assert archive.testzip() is None
        receipt = json.loads(archive.read("translation.json"))
        assert receipt["detected_source_language"] == "en"
        assert receipt["detected_target_language"] == "it"
        assert receipt["page_count"] == 2
        assert receipt["translated_regions"] >= 2
        with Image.open(io.BytesIO(archive.read("00001.png"))) as image:
            assert image.size == (1200, 800)
        with zipfile.ZipFile(source) as original:
            assert archive.read("00001.png") != original.read("00001.png")
    assert progress == [(0, 2), (1, 2), (2, 2)]
    assert not output.with_suffix(".partial").exists()
    for path in tmp_path.rglob("*.zip"):
        with zipfile.ZipFile(path) as archive:
            assert b"private-provider-key" not in archive.read("page.json")


@pytest.mark.asyncio
async def test_completed_pages_resume_after_provider_failure(tmp_path, monkeypatch):
    source, output, request = fixture(tmp_path)
    monkeypatch.setattr(
        local,
        "ocr",
        AsyncMock(return_value=[{"text": ENGLISH, "box": [50, 50, 1100, 300]}]),
    )
    with respx.mock as mock:
        route = mock.post("https://provider.example/v1/chat/completions")
        route.mock(
            side_effect=[
                response(
                    httpx.Request(
                        "POST",
                        "https://provider.example",
                        json={"messages": [{}, {"content": '["dialogue"]'}]},
                    )
                ),
                httpx.Response(503),
            ]
        )
        with pytest.raises(httpx.HTTPStatusError):
            await local.translate_archive(source, output, request, lambda *_: None)
        assert not output.exists()
        assert not output.with_suffix(".partial").exists()
        assert len(list(tmp_path.rglob("*.zip"))) == 1
        route.mock(side_effect=response)
        await local.translate_archive(source, output, request, lambda *_: None)
        assert route.call_count == 3  # first completed page reused
        assert local.ocr.await_count == 3
        await local.translate_archive(source, output, request, lambda *_: None)
        assert route.call_count == 3
        request["ai"]["model"] = "changed-model"
        await local.translate_archive(source, output, request, lambda *_: None)
        assert route.call_count == 5


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answer",
    ["[]", '[""]', json.dumps([ENGLISH]), '["private-provider-key"]', "not json"],
)
async def test_invalid_ai_output_never_becomes_a_page_checkpoint(
    tmp_path, monkeypatch, answer
):
    source, output, request = fixture(tmp_path, pages=1)
    monkeypatch.setattr(
        local,
        "ocr",
        AsyncMock(return_value=[{"text": ENGLISH, "box": [50, 50, 1100, 300]}]),
    )
    with respx.mock as mock:
        mock.post("https://provider.example/v1/chat/completions").respond(
            200, json={"choices": [{"message": {"content": answer}}]}
        )
        with pytest.raises(local.LocalTranslationError):
            await local.translate_archive(source, output, request, lambda *_: None)
    assert not output.exists()
    assert not list(tmp_path.rglob("*.zip"))
    assert not list(tmp_path.rglob("*.partial"))


@pytest.mark.asyncio
async def test_cancel_kills_owned_ocr_process(monkeypatch):
    started = asyncio.Event()

    class Process:
        returncode = None
        killed = False

        async def communicate(self, _data):
            started.set()
            await asyncio.Event().wait()

        def kill(self):
            self.killed = True
            self.returncode = -9

        async def wait(self):
            return self.returncode

    process = Process()
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", AsyncMock(return_value=process)
    )
    task = asyncio.create_task(local.ocr(Image.new("RGB", (32, 32)), "en"))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert process.killed


def test_missing_local_prerequisite_is_actionable(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda _: None)
    with pytest.raises(local.LocalTranslationError, match="Install Tesseract"):
        local.local_requirements()


def test_checkpoint_corruption_is_not_reused(tmp_path):
    source, output, request = fixture(tmp_path, pages=1)
    path = tmp_path / "checkpoint.zip"
    with zipfile.ZipFile(source) as archive:
        data = archive.read("00001.png")
    identity = local.identity_for(request)
    meta = {"identity": identity, "index": 1, "sha256": "wrong"}
    local.write_checkpoint(path, data, meta)
    assert local.read_checkpoint(path, identity, 1, (1200, 800)) is None
