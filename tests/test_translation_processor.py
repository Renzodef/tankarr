from __future__ import annotations

import copy
import importlib.util
import io
import json
import logging
import os
import sys
import threading
import urllib.request
import zipfile
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image


def load(name):
    path = Path(__file__).resolve().parents[1] / "contrib" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_latin_source_language_sample_ignores_retained_japanese_headings():
    module = load("translate_archive")
    dialogue = ["POR FAVOR PERDOE O AMIGO E VOLTE PARA CASA. " * 12]
    mixed = ["プレゼントフロム", *dialogue, "とうちゃんの目"]

    sample = module.source_language_sample(mixed, "pt-br")
    assert "por favor" in sample
    assert "プレゼント" not in sample
    assert module.source_language_sample(mixed, "ja") == "\n".join(mixed).casefold()

    mostly_japanese = ["日本語" * 100, *dialogue]
    assert "日本語" in module.source_language_sample(mostly_japanese, "pt-br")


def test_processor_is_idempotent_and_keeps_secrets_out_of_disk(tmp_path):
    module = load("translation_processor")
    release = threading.Event()
    calls = []

    def execute(directory, request):
        calls.append(request["ai"]["api_key"])
        assert release.wait(5)
        (directory / "result.cbz").write_bytes(b"result")

    processor = module.Processor(tmp_path, execute)
    job_id = "a" * 32 + "-0"
    directory = processor.directory(job_id)
    directory.mkdir()
    (directory / "source.cbz").write_bytes(b"source")
    request = {
        "source_sha256": module.digest(directory / "source.cbz"),
        "source_language": "ja",
        "target_language": "en",
        "ai": {
            "base_url": "https://ai.example.test/v1",
            "model": "model",
            "api_key": "private-key",
        },
    }
    try:
        assert processor.submit(job_id, copy.deepcopy(request))["status"] == "queued"
        assert processor.submit(job_id, copy.deepcopy(request))["status"] in {
            "queued",
            "running",
        }
        assert "private-key" not in (directory / "request.json").read_text()
        assert "private-key" not in (directory / "status.json").read_text()
        with pytest.raises(ValueError, match="another request"):
            processor.submit(job_id, {**request, "target_language": "it"})
    finally:
        release.set()
        processor.executor.shutdown(wait=True)
    assert processor.status(job_id)["status"] == "completed"
    assert calls == ["private-key"]


@pytest.mark.parametrize("source_language", ["zh-hant", "zh-hant-hk", "zh-hk"])
def test_processor_accepts_chinese_script_and_region_tags(tmp_path, source_language):
    module = load("translation_processor")

    def execute(directory, _request):
        (directory / "result.cbz").write_bytes(b"result")

    processor = module.Processor(tmp_path, execute)
    job_id = "d" * 32 + "-0"
    directory = processor.directory(job_id)
    directory.mkdir()
    (directory / "source.cbz").write_bytes(b"source")
    request = {
        "source_sha256": module.digest(directory / "source.cbz"),
        "source_language": source_language,
        "target_language": "en",
        "ai": {
            "base_url": "https://ai.example.test/v1",
            "model": "model",
            "api_key": "private-key",
        },
    }

    try:
        assert processor.submit(job_id, request)["status"] == "queued"
    finally:
        processor.executor.shutdown(wait=True)
    assert processor.status(job_id)["status"] == "completed"


def test_processor_restart_requests_credentials_again(tmp_path):
    module = load("translation_processor")
    processor = module.Processor(tmp_path, lambda *_args: None)
    job_id = "b" * 32 + "-0"
    directory = processor.directory(job_id)
    directory.mkdir()
    (directory / "status.json").write_text(json.dumps({"status": "running"}))
    assert processor.status(job_id) is None
    processor.executor.shutdown()


def test_http_cancel_stops_work_and_prevents_late_completion(tmp_path):
    module = load("translation_processor")
    started, released = threading.Event(), threading.Event()
    calls = []

    def execute(directory, _request):
        started.set()
        assert released.wait(5)
        (directory / "result.cbz").write_bytes(b"late result")

    def cancel(directory):
        calls.append(directory.name)
        released.set()

    processor = module.Processor(tmp_path, execute, cancel=cancel)
    job = "c" * 32 + "-0"
    directory = processor.directory(job)
    directory.mkdir()
    (directory / "source.cbz").write_bytes(b"source")
    request = {
        "source_sha256": module.digest(directory / "source.cbz"),
        "source_language": "ja",
        "target_language": "en",
        "ai": {
            "base_url": "https://ai.example.test/v1",
            "model": "model",
            "api_key": "private-key",
        },
    }
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), module.handler_for(processor, "token")
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        processor.submit(job, copy.deepcopy(request))
        assert started.wait(2)
        with urllib.request.urlopen(
            urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/jobs/{job}",
                method="DELETE",
                headers={"Authorization": "Bearer token"},
            ),
            timeout=3,
        ) as response:
            assert response.status == 202
        processor.executor.shutdown(wait=True)
        assert calls == [job]
        assert processor.status(job)["status"] == "cancelled"
        assert processor.submit(job, copy.deepcopy(request))["status"] == "cancelled"
        assert "private-key" not in (directory / "cancel.json").read_text()
    finally:
        released.set()
        server.shutdown()
        server.server_close()
        thread.join()
        processor.executor.shutdown(wait=True)


def test_pending_cancellation_resumes_without_ai_credentials(tmp_path):
    module = load("translation_processor")
    directory = tmp_path / ("d" * 32 + "-0")
    directory.mkdir()
    (directory / "cancel.json").write_text('{"requested":true}')
    called = threading.Event()
    processor = module.Processor(
        tmp_path, lambda *_args: None, cancel=lambda path: called.set()
    )
    assert called.wait(2)
    processor.executor.shutdown()


@pytest.mark.parametrize(
    "job_id", ["../../elsewhere", "/tmp/file", "a" * 32 + "/1", "a" * 32 + "-$(false)"]
)
def test_processor_rejects_unsafe_job_names(tmp_path, job_id):
    module = load("translation_processor")
    processor = module.Processor(tmp_path, lambda *_args: None)
    with pytest.raises(ValueError):
        processor.directory(job_id)
    processor.executor.shutdown()


def test_ocr_receipt_rejects_empty_echo_and_marker_output():
    runner = load("translate_archive")
    regions = [
        SimpleNamespace(text="こんにちは", translation=value)
        for value in ("", "こんにちは", "<|12|>", "Hello")
    ]
    assert runner.region_counts(regions) == (1, 3)


def test_transport_errors_retry_but_authentication_and_quality_errors_do_not():
    runner = load("translate_archive")
    assert runner.transient_error(httpx.ReadTimeout("provider timed out"))
    assert runner.transient_error(httpx.ConnectError("connection dropped"))
    for status in (429, 503, 401, 400):
        response = httpx.Response(
            status, request=httpx.Request("POST", "https://ai.example.test")
        )
        error = httpx.HTTPStatusError(
            "provider rejected", request=response.request, response=response
        )
        assert runner.transient_error(error) is (status in {429, 503})
    wrapper = RuntimeError("engine wrapper")
    wrapper.__cause__ = TimeoutError()
    assert runner.transient_error(wrapper)
    assert not runner.transient_error(ValueError("Untranslated regions"))


def test_cancellation_marker_wins_over_a_late_recovery_result(tmp_path):
    module = load("translation_processor")
    processor = module.Processor(tmp_path, lambda *_args: None)
    job = "c" * 32 + "-0"
    directory = processor.directory(job)
    directory.mkdir()
    module.write_json(directory / "cancel.json", {"requested": True})
    module.write_json(directory / "status.json", {"status": "completed"})
    assert processor.status(job) == {"status": "cancelled"}
    processor.executor.shutdown()


@pytest.mark.asyncio
async def test_runner_resumes_completed_pages_and_invalidates_changed_model(
    tmp_path, monkeypatch
):
    runner = load("translate_archive")
    source, output, checkpoints = (
        tmp_path / "source.cbz",
        tmp_path / "result.cbz",
        tmp_path / "checkpoints",
    )
    with zipfile.ZipFile(source, "w") as archive:
        for number in range(1, 5):
            image = io.BytesIO()
            Image.new("RGB", (32, 48), (number, 0, 0)).save(image, format="PNG")
            archive.writestr(f"{number}.png", image.getvalue())
    request = {
        "source_sha256": runner.digest(source),
        "source_language": "ja",
        "target_language": "en",
        "ai": {
            "base_url": "https://ai.example.test/v1",
            "model": "model-a",
            "api_key": "secret-one",
        },
    }
    calls = []
    interrupt = True

    class Engine:
        def __init__(self, _params):
            pass

        async def translate(self, image, _config):
            nonlocal interrupt
            number = image.getpixel((0, 0))[0]
            calls.append(number)
            if number == 3 and interrupt:
                interrupt = False
                raise InterruptedError("Simulated worker interruption")
            return SimpleNamespace(
                result=image,
                text_regions=[SimpleNamespace(text="日本語", translation="Hello")],
            )

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
        SimpleNamespace(classify=lambda text: ("ja" if "日本語" in text else "en", 1)),
    )
    with pytest.raises(InterruptedError):
        await runner.translate(source, output, request, checkpoints=checkpoints)
    assert len(list(checkpoints.glob("*.zip"))) == 2
    assert not output.exists()
    assert not output.with_suffix(".partial").exists()
    request["ai"]["api_key"] = (
        "secret-two"  # Key rotation does not invalidate completed pages.
    )
    await runner.translate(source, output, request, checkpoints=checkpoints)
    assert calls == [1, 2, 3, 3, 4]
    with zipfile.ZipFile(output) as archive:
        receipt = json.loads(archive.read("translation.json"))
        assert receipt["page_count"] == receipt["translated_regions"] == 4
        assert receipt["untranslated_regions"] == 0
    for checkpoint in checkpoints.glob("*.zip"):
        assert "secret-" not in checkpoint.read_bytes().decode("latin1")
    # A corrupt saved page is regenerated; the other three are retained.
    next(checkpoints.glob("00002-*.zip")).write_bytes(b"broken")
    await runner.translate(source, output, request, checkpoints=checkpoints)
    assert calls[-1] == 2 and len(calls) == 6
    request["ai"]["model"] = "model-b"
    await runner.translate(source, output, request, checkpoints=checkpoints)
    assert calls[-4:] == [1, 2, 3, 4]
    assert len(calls) == 10


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter,gpu", [("chatgpt", False), ("deepseek", True)])
@pytest.mark.parametrize("engine_failure", [False, True])
async def test_runner_uses_job_credentials_and_preserves_engine_adapter(
    tmp_path, monkeypatch, caplog, adapter, gpu, engine_failure
):
    runner = load("translate_archive")
    source, output = tmp_path / "source.cbz", tmp_path / "result.cbz"
    buffer = io.BytesIO()
    Image.new("RGB", (32, 48), "white").save(buffer, format="PNG")
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("page.png", buffer.getvalue())
    request = {
        "source_sha256": runner.digest(source),
        "source_language": "ja",
        "target_language": "it",
        "ai": {
            "base_url": "https://ai.example.test/v1",
            "model": "configured-model",
            "api_key": "private-job-key",
        },
    }
    config = {
        "ocr": {"ocr": "48px", "use_mocr_merge": False},
        "render": {"renderer": "default", "font_size": 22},
        "translator": {
            "translator": adapter,
            "target_lang": "FRA",
            "translator_chain": "google:JPN;sugoi:ENG",
            "selective_translation": "google:ENG",
            "skip_lang": "JPN",
        },
    }
    original_config = copy.deepcopy(config)
    for prefix in ("OPENAI", "DEEPSEEK"):
        for suffix in ("API_KEY", "API_BASE", "MODEL"):
            monkeypatch.setenv(f"{prefix}_{suffix}", "previous")
    previous_factory = logging.getLogRecordFactory()
    calls = []

    class Engine:
        def __init__(self, params):
            assert params["kernel_size"] == 3
            assert params["batch_size"] == 1
            assert params["use_gpu"] is gpu
            assert params["ignore_errors"] is False
            for prefix in ("OPENAI", "DEEPSEEK"):
                assert os.environ[f"{prefix}_API_KEY"] == "private-job-key"
                assert os.environ[f"{prefix}_API_BASE"] == request["ai"]["base_url"]
                assert os.environ[f"{prefix}_MODEL"] == "configured-model"
            logging.warning("Provider credential: private-job-key")
            if engine_failure:
                raise RuntimeError("Engine unavailable")

        async def translate(self, image, parsed):
            calls.append(parsed)
            return SimpleNamespace(
                result=image,
                text_regions=[
                    SimpleNamespace(text="こんにちは", translation="Buongiorno")
                ],
            )

    monkeypatch.setitem(
        sys.modules, "manga_translator", SimpleNamespace(MangaTranslator=Engine)
    )
    monkeypatch.setitem(
        sys.modules, "manga_translator.config", SimpleNamespace(Config=lambda **kw: kw)
    )
    monkeypatch.setitem(
        sys.modules,
        "manga_translator.args",
        SimpleNamespace(
            reparse=lambda args: SimpleNamespace(
                kernel_size=3, batch_size=1, use_gpu=False
            )
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "py3langid",
        SimpleNamespace(
            classify=lambda text: (
                "ja"
                if text == "こんにちは"
                else "it"
                if text == "buongiorno"
                else "bad",
                1,
            )
        ),
    )
    if engine_failure:
        with pytest.raises(RuntimeError, match="Engine unavailable"):
            await runner.translate(source, output, request, gpu=gpu, config=config)
        assert not output.exists()
    else:
        await runner.translate(source, output, request, gpu=gpu, config=config)
        assert calls[0]["translator"]["translator"] == adapter
        assert calls[0]["translator"]["target_lang"] == "ITA"
        assert calls[0]["translator"]["translator_chain"] is None
        assert calls[0]["translator"]["selective_translation"] is None
        assert calls[0]["translator"]["skip_lang"] is None
        assert calls[0]["ocr"] == config["ocr"]
        assert calls[0]["render"] == config["render"]
        with zipfile.ZipFile(output) as archive:
            receipt = json.loads(archive.read("translation.json"))
            assert receipt["page_count"] == 1
            assert receipt["translated_regions"] == 1
            assert receipt["detected_target_language"] == "it"
            assert receipt["model"] == "configured-model"
            with Image.open(io.BytesIO(archive.read("00001.png"))) as rendered:
                assert rendered.size == (32, 48)
    assert config == original_config
    assert "private-job-key" not in caplog.text
    assert "[redacted]" in caplog.text
    assert logging.getLogRecordFactory() is previous_factory
    for prefix in ("OPENAI", "DEEPSEEK"):
        for suffix in ("API_KEY", "API_BASE", "MODEL"):
            assert os.environ[f"{prefix}_{suffix}"] == "previous"
