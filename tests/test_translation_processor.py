from __future__ import annotations

import contextlib
import copy
import importlib.util
import io
import json
import logging
import os
import re
import sys
import threading
import urllib.error
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


def test_source_language_validation_accepts_original_language_backmatter():
    module = load("translate_archive")
    pages = [["HOLA AMIGO, VAMOS A CASA. " * 20] for _ in range(8)]
    pages += [["日本語のインタビュー" * 200] for _ in range(2)]

    def classify(sample):
        latin = len(module.LATIN_LETTERS.findall(sample))
        cjk = len(module.JAPANESE_OR_HAN.findall(sample))
        return ("ja" if cjk > latin else "es", 0.0)

    assert module.detect_source_language(pages, "es", classify) == (
        "es",
        "main_story_with_cjk_backmatter",
    )


def test_source_language_validation_rejects_wrong_language_story():
    module = load("translate_archive")
    pages = [["日本語の漫画" * 40] for _ in range(8)]
    pages += [["HOLA AMIGO" * 40] for _ in range(2)]

    def classify(sample):
        latin = len(module.LATIN_LETTERS.findall(sample))
        cjk = len(module.JAPANESE_OR_HAN.findall(sample))
        return ("ja" if cjk > latin else "es", 0.0)

    assert module.detect_source_language(pages, "es", classify) == (
        "ja",
        "whole_archive",
    )


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
    assert runner.transient_error(runner.TranslationFailed("provider failed"))
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


def test_provider_probe_reports_a_deepseek_account_without_balance(monkeypatch):
    runner = load("translate_archive")
    request = {
        "ai": {
            "base_url": "https://api.deepseek.com/v1",
            "model": "m",
            "api_key": "secret",
        }
    }

    class Answer:
        def __init__(self, body):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def read(self, _limit):
            return self.body

    seen = []

    def refuse(probe, timeout):
        seen.append((probe.full_url, probe.get_header("Authorization")))
        return Answer(b'{"is_available": false}')

    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    assert (
        runner.provider_unavailable_reason(request)
        == "provider account has no available balance"
    )
    assert seen == [("https://api.deepseek.com/user/balance", "Bearer secret")]
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda probe, timeout: Answer(b'{"is_available": true}'),
    )
    assert runner.provider_unavailable_reason(request) is None

    def http_402(probe, timeout):
        raise urllib.error.HTTPError(
            probe.full_url, 402, "Insufficient Balance", {}, None
        )

    monkeypatch.setattr(urllib.request, "urlopen", http_402)
    assert "402" in runner.provider_unavailable_reason(request)

    def offline(probe, timeout):
        raise OSError("network down")

    monkeypatch.setattr(urllib.request, "urlopen", offline)
    assert (
        runner.provider_unavailable_reason(request) is None
    )  # not evidence of a refusal
    other = {
        "ai": {"base_url": "https://ai.example.test/v1", "model": "m", "api_key": "s"}
    }
    assert runner.provider_unavailable_reason(other) is None


def test_checkpoint_that_hid_detected_text_is_not_reused(tmp_path):
    runner = load("translate_archive")
    image = io.BytesIO()
    Image.new("RGB", (32, 48)).save(image, format="PNG")
    page = {
        "identity": "same",
        "index": 1,
        "translated": 0,
        "untranslated": 0,
        "source_text": [],
        "target_text": [],
    }
    directory = tmp_path / "checkpoints"
    runner.save_checkpoint(
        directory, 1, image.getvalue(), {**page, "detected_lines": 5}
    )
    assert runner.load_checkpoint(directory, 1, "same", (32, 48)) is None
    runner.save_checkpoint(directory, 2, image.getvalue(), {**page, "index": 2})
    assert (
        runner.load_checkpoint(directory, 2, "same", (32, 48)) is not None
    )  # a blank page
    # lines already in the target script (poster words, credits) hid nothing
    runner.save_checkpoint(
        directory,
        3,
        image.getvalue(),
        {**page, "index": 3, "detected_lines": 5, "detected_source_lines": 1},
    )
    assert runner.load_checkpoint(directory, 3, "same", (32, 48)) is not None
    runner.save_checkpoint(
        directory,
        4,
        image.getvalue(),
        {**page, "index": 4, "detected_lines": 5, "detected_source_lines": 4},
    )
    assert runner.load_checkpoint(directory, 4, "same", (32, 48)) is None
    # sound effects the model read and left blank keep their proof, and a resumed job keeps the page
    proof = {"requests": 1, "lines": ["GUAU", "GRRR"]}
    runner.save_checkpoint(
        directory,
        5,
        image.getvalue(),
        {
            **page,
            "index": 5,
            "detected_lines": 8,
            "detected_source_lines": 8,
            "empty_proof": proof,
        },
    )
    assert runner.load_checkpoint(directory, 5, "same", (32, 48)) is not None
    # the weaker marker of an earlier runner (an HTTP 200 and nothing else) is not a proof
    runner.save_checkpoint(
        directory,
        6,
        image.getvalue(),
        {
            **page,
            "index": 6,
            "detected_lines": 8,
            "detected_source_lines": 8,
            "empty_verified": True,
        },
    )
    assert runner.load_checkpoint(directory, 6, "same", (32, 48)) is None


def _provider(kind="blank", status=200, error=None):
    """A stub provider for the fake engine: what the model answers for the numbered regions it is sent.

    blank: every region numbered, no text. empty: nothing at all. cut: stopped at the token limit.
    filtered: stopped by the content filter. prose: a refusal without numbering. short: one region missing.
    translated: every region translated. echo: every region returned as it came."""

    def numbered(numbers, words=""):
        if words is None:  # every region unchanged, as the fake engine numbered them
            unchanged = {"1": "uno", "2": "dos", "3": "tres"}
            return "\n".join(f"<|{number}|>{unchanged[number]}" for number in numbers)
        return "\n".join(f"<|{number}|>{words}" for number in numbers)

    def answer(request):
        if error is not None:
            raise error
        if status != 200:
            return httpx.Response(status, json={})
        prompt = json.loads(request.content)["messages"][-1]["content"]
        asked = re.findall(r"(?m)^<\|(\d+)\|>", prompt)
        content, finish = {
            "blank": (numbered(asked), "stop"),
            "empty": ("", "stop"),
            "cut": (numbered(asked), "length"),
            "filtered": (numbered(asked), "content_filter"),
            "prose": ("I am sorry, I cannot help with that.", "stop"),
            "short": (numbered(asked[:-1]), "stop"),
            "translated": (numbered(asked, "Hello"), "stop"),
            "echo": (numbered(asked, None), "stop"),
        }[kind]
        message = {"role": "assistant", "content": content}
        return httpx.Response(
            200, json={"choices": [{"finish_reason": finish, "message": message}]}
        )

    return answer


def _fake_engine(
    monkeypatch, contexts, provider=None, requests=None, on_translate=None
):
    """requests: {page: [[region text, ...], ...]}, one request per list; by default every page sends uno, dos, tres."""

    class Engine:
        def __init__(self, _params):
            pass

        async def translate(self, image, _config):
            if on_translate is not None:
                on_translate()
            if provider is not None:
                # What the real engine does: number the regions of the page, ask the provider, swallow a failure.
                page = image.getpixel((0, 0))[0]
                for regions in (requests or {}).get(page, [["uno", "dos", "tres"]]):
                    prompt = "Translate into English:\n" + "\n".join(
                        f"<|{number}|>{region}"
                        for number, region in enumerate(regions, 1)
                    )
                    messages = [
                        {"role": "system", "content": "translate manga"},
                        {"role": "user", "content": prompt},
                    ]
                    try:
                        async with httpx.AsyncClient(
                            transport=httpx.MockTransport(provider)
                        ) as client:
                            await client.post(
                                "https://ai.example.test/v1/chat/completions",
                                json={"messages": messages},
                            )
                    except httpx.HTTPError:
                        pass
            return contexts[image.getpixel((0, 0))[0] - 1](image)

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


def _archive(tmp_path, runner, pages, language="ja"):
    source = tmp_path / "source.cbz"
    with zipfile.ZipFile(source, "w") as archive:
        for number in range(1, pages + 1):
            image = io.BytesIO()
            Image.new("RGB", (32, 48), (number, 0, 0)).save(image, format="PNG")
            archive.writestr(f"{number}.png", image.getvalue())
    request = {
        "source_sha256": runner.digest(source),
        "source_language": language,
        "target_language": "en",
        "ai": {
            "base_url": "https://ai.example.test/v1",
            "model": "m",
            "api_key": "secret",
        },
    }
    return source, request


BARKS = [
    SimpleNamespace(text=word)
    for word in ("¡GUAU,", "GUAU,", "GUAU!") * 2 + ("¡GRRR!", "¡GRR!", "15")
]
SENTENCES = [
    SimpleNamespace(text=line)
    for line in (
        "Ven aquí ahora mismo y explícame todo",
        "No puedo creer lo que estás diciendo",
        "Tenemos que salir de esta casa",
        "¿Dónde está mi madre?",
    )
]


def _spanish_job(monkeypatch, tmp_path, runner, page_one, provider):
    """Two pages of a Spanish book: the first is the page under test, the second is ordinary dialogue."""
    source, request = _archive(tmp_path, runner, 2, language="es")
    _fake_engine(
        monkeypatch,
        [
            lambda image: SimpleNamespace(
                result=image, textlines=page_one, text_regions=[]
            ),
            lambda image: SimpleNamespace(
                result=image,
                textlines=[SimpleNamespace(text="Hola amigo")],
                text_regions=[
                    SimpleNamespace(text="Hola amigo", translation="Hello friend")
                ],
            ),
        ],
        provider,
    )
    monkeypatch.setitem(
        sys.modules,
        "py3langid",
        SimpleNamespace(classify=lambda text: ("es" if "hola" in text else "en", 1)),
    )
    monkeypatch.setattr(runner, "provider_unavailable_reason", lambda _request: None)
    return source, request, tmp_path / "result.cbz", tmp_path / "checkpoints"


def _saved_pages(checkpoints):
    return sorted(int(path.name[:5]) for path in checkpoints.glob("*.zip"))


@pytest.mark.asyncio
async def test_runner_stops_when_the_provider_failed_on_a_page_with_text(
    tmp_path, monkeypatch
):
    runner = load("translate_archive")
    source, request = _archive(tmp_path, runner, 1)
    output, checkpoints = tmp_path / "result.cbz", tmp_path / "checkpoints"
    lost = lambda image: SimpleNamespace(  # noqa: E731
        result=image,
        textlines=[SimpleNamespace(text="日本語のせりふ")] * 4,
        text_regions=[],
    )
    monkeypatch.setattr(runner, "provider_unavailable_reason", lambda _request: None)
    for provider in (
        _provider(status=402),
        _provider(error=httpx.ConnectError("offline")),
    ):
        _fake_engine(monkeypatch, [lost], provider)
        with pytest.raises(
            runner.TranslationFailed, match="provider request"
        ) as failure:
            await runner.translate(source, output, request, checkpoints=checkpoints)
        assert runner.transient_error(
            failure.value
        )  # retried later, not blocked for good
        assert not list(checkpoints.glob("*.zip")) and not output.exists()
    monkeypatch.setattr(
        runner,
        "provider_unavailable_reason",
        lambda _request: "provider account has no available balance",
    )
    with pytest.raises(runner.ProviderUnavailable) as refused:
        await runner.translate(source, output, request, checkpoints=checkpoints)
    assert runner.transient_error(refused.value)
    assert not list(checkpoints.glob("*.zip"))


@pytest.mark.asyncio
async def test_runner_keeps_a_page_of_sound_effects_the_model_read_and_left_blank(
    tmp_path, monkeypatch
):
    """A page of barks and a page number, no dialogue: the model numbered every region and answered blank."""

    runner = load("translate_archive")
    source, request, output, checkpoints = _spanish_job(
        monkeypatch, tmp_path, runner, BARKS, _provider("blank")
    )
    await runner.translate(source, output, request, checkpoints=checkpoints)
    with zipfile.ZipFile(output) as archive:
        receipt = json.loads(archive.read("translation.json"))
    assert receipt["page_count"] == 2 and receipt["translated_regions"] == 1
    page = json.loads(
        zipfile.ZipFile(next(checkpoints.glob("00001-*.zip"))).read("page.json")
    )
    assert page["translated"] == 0 and page["detected_source_lines"] == len(BARKS)
    assert page["empty_proof"]["requests"] == 1
    assert "¡GRRR!" in page["empty_proof"]["lines"]
    assert "empty_verified" not in page and "provider_failures" not in page
    # a resumed job keeps that page instead of asking the engine again
    assert (
        runner.load_checkpoint(checkpoints, 1, page["identity"], (32, 48)) is not None
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind,why",
    [
        ("empty", "the answer was empty"),
        ("cut", "stopped early \\(length\\)"),
        ("filtered", "stopped early \\(content_filter\\)"),
        ("prose", "outside the numbered format"),
        ("short", "does not cover every numbered region"),
    ],
)
async def test_an_http_200_is_not_proof_that_a_page_had_no_dialogue(
    tmp_path, monkeypatch, kind, why
):
    """A refused, empty, truncated or unnumbered answer can hide a real sentence: the page must not be kept."""

    runner = load("translate_archive")
    source, request, output, checkpoints = _spanish_job(
        monkeypatch, tmp_path, runner, BARKS, _provider(kind)
    )
    with pytest.raises(runner.TranslationFailed, match=why) as failure:
        await runner.translate(source, output, request, checkpoints=checkpoints)
    assert runner.transient_error(failure.value)
    # the refused page is put off; the one after it is read and kept
    assert _saved_pages(checkpoints) == [2] and not output.exists()
    # the refusal leaves a record of the page: why, how many requests, what the engine read
    kept = [
        json.loads(line)
        for line in (checkpoints / "rejected-pages.jsonl").read_text().splitlines()
    ]
    assert kept[-1]["page"] == 1 and re.search(why, kept[-1]["problem"])
    assert kept[-1]["requests"] == 1 and "¡GUAU," in kept[-1]["lines"]


@pytest.mark.asyncio
async def test_an_empty_page_needs_a_request_that_reached_the_provider(
    tmp_path, monkeypatch
):
    runner = load("translate_archive")
    source, request, output, checkpoints = _spanish_job(
        monkeypatch, tmp_path, runner, BARKS, None
    )
    with pytest.raises(runner.TranslationFailed, match="no request reached"):
        await runner.translate(source, output, request, checkpoints=checkpoints)
    assert _saved_pages(checkpoints) == [2]


@pytest.mark.asyncio
async def test_an_empty_page_that_reads_as_dialogue_is_not_kept_even_when_answered(
    tmp_path, monkeypatch
):
    runner = load("translate_archive")
    source, request, output, checkpoints = _spanish_job(
        monkeypatch, tmp_path, runner, SENTENCES, _provider("blank")
    )
    with pytest.raises(runner.TranslationFailed, match="reads as dialogue"):
        await runner.translate(source, output, request, checkpoints=checkpoints)
    assert _saved_pages(checkpoints) == [2]


def test_a_rejection_outlives_the_job_directory_on_a_worker(tmp_path):
    runner = load("translate_archive")
    lines = [SimpleNamespace(text="¡GUAU,"), SimpleNamespace(text="GRRR")]
    job = "8687255aee0063a3f8cd23e3cd6f98e1-2"
    checkpoints = tmp_path / "workspace" / job / "checkpoints"
    runner.record_rejection(checkpoints, 156, "the answer was empty", 8, 1, lines)
    for kept in (
        checkpoints / "rejected-pages.jsonl",
        tmp_path / "workspace" / "rejected-pages.jsonl",
    ):
        entry = json.loads(kept.read_text().splitlines()[-1])
        assert entry["job"] == job and entry["page"] == 156
        assert entry["lines"] == ["¡GUAU,", "GRRR"] and entry["requests"] == 1
    # outside a worker's job directory (a plain checkpoint folder) nothing is written next to it
    plain = tmp_path / "somewhere" / "checkpoints"
    runner.record_rejection(plain, 1, "x", 3, 0, lines)
    assert (plain / "rejected-pages.jsonl").exists()
    assert not (tmp_path / "rejected-pages.jsonl").exists()


def _fallback(runner, monkeypatch, name="vision"):
    """A fallback OCR the fake engine can see: while it is installed, state['fallback'] is True."""
    state = {"fallback": False, "installed": 0}

    @contextlib.contextmanager
    def install():
        state["fallback"] = True
        state["installed"] += 1
        try:
            yield
        finally:
            state["fallback"] = False

    monkeypatch.setattr(runner, "FALLBACK_OCR", (name, install))
    return state


def _page_lost_by_the_standard_ocr(state, joined):
    """Page one: the standard OCR reads fragments; the fallback reads the balloon whole (or, if joined is False, the same fragments)."""

    def read(image):
        if state["fallback"] and joined:
            return SimpleNamespace(
                result=image,
                textlines=[SimpleNamespace(text="¡SOCORRO...!")],
                text_regions=[
                    SimpleNamespace(text="¡SOCORRO...!", translation="Help...!")
                ],
            )
        return SimpleNamespace(result=image, textlines=FRAGMENTS, text_regions=[])

    return read


def _fallback_job(monkeypatch, tmp_path, runner, state, provider, joined=True):
    source, request = _archive(tmp_path, runner, 2, language="es")
    _fake_engine(
        monkeypatch,
        [
            _page_lost_by_the_standard_ocr(state, joined),
            lambda image: SimpleNamespace(
                result=image,
                textlines=[SimpleNamespace(text="Hola amigo")],
                text_regions=[
                    SimpleNamespace(text="Hola amigo", translation="Hello friend")
                ],
            ),
        ],
        provider,
    )
    monkeypatch.setitem(
        sys.modules,
        "py3langid",
        SimpleNamespace(classify=lambda text: ("es" if "hola" in text else "en", 1)),
    )
    monkeypatch.setattr(runner, "provider_unavailable_reason", lambda _request: None)
    return source, request, tmp_path / "result.cbz", tmp_path / "checkpoints"


def test_the_fallback_is_inert_by_default():
    assert load("translate_archive").FALLBACK_OCR is None


@pytest.mark.asyncio
async def test_a_page_the_standard_ocr_lost_is_read_again_with_the_fallback(
    tmp_path, monkeypatch
):
    runner = load("translate_archive")
    state = _fallback(runner, monkeypatch)
    source, request, output, checkpoints = _fallback_job(
        monkeypatch, tmp_path, runner, state, _provider("blank")
    )
    await runner.translate(source, output, request, checkpoints=checkpoints)
    first, second = (
        json.loads(
            zipfile.ZipFile(next(checkpoints.glob(f"0000{n}-*.zip"))).read("page.json")
        )
        for n in (1, 2)
    )
    assert first["translated"] == 1 and first["ocr_fallback"] == "vision"
    assert "ocr_fallback" not in second
    assert first["identity"] == second["identity"]  # the job's identity did not change
    assert state["installed"] == 1  # only the suspect page was read the other way
    kept = json.loads(
        (checkpoints / "rejected-pages.jsonl").read_text().splitlines()[0]
    )
    assert (
        kept["page"] == 1 and "jSOCO-" in kept["lines"]
    )  # what the standard OCR had read


@pytest.mark.asyncio
async def test_the_fallback_is_tried_once_and_its_failure_stops_the_job(
    tmp_path, monkeypatch
):
    runner = load("translate_archive")
    state = _fallback(runner, monkeypatch)
    source, request, output, checkpoints = _fallback_job(
        monkeypatch, tmp_path, runner, state, _provider("blank"), joined=False
    )
    with pytest.raises(runner.SuspectPage, match="also after reading it with vision"):
        await runner.translate(source, output, request, checkpoints=checkpoints)
    assert state["installed"] == 1 and _saved_pages(checkpoints) == [2]


@pytest.mark.asyncio
async def test_the_fallback_is_not_used_when_the_provider_failed(tmp_path, monkeypatch):
    runner = load("translate_archive")
    state = _fallback(runner, monkeypatch)
    source, request, output, checkpoints = _fallback_job(
        monkeypatch, tmp_path, runner, state, _provider(status=402)
    )
    with pytest.raises(runner.TranslationFailed, match="provider request") as failure:
        await runner.translate(source, output, request, checkpoints=checkpoints)
    assert not isinstance(failure.value, runner.SuspectPage)
    assert state["installed"] == 0


def test_sound_effects_are_told_from_dialogue():
    runner = load("translate_archive")

    def lines(*texts):
        return [SimpleNamespace(text=text) for text in texts]

    assert not runner.looks_like_dialogue(BARKS)
    assert not runner.looks_like_dialogue(lines("ドドド", "ゴゴゴゴ", "ドン", "バキッ"))
    assert not runner.looks_like_dialogue(lines("BOOM", "CRASH", "BANG"))
    assert not runner.looks_like_dialogue(lines("...", "!?", "12"))
    assert not runner.looks_like_dialogue(lines("AAAAAAAAAAH", "GUAU", "GUAU"))
    assert runner.looks_like_dialogue(SENTENCES)
    # a question is a sentence, however short and however the words are split
    assert runner.looks_like_dialogue(lines("¿CÓMO", "ESTÁ", "TU", "MADRE,", "CHICO?"))
    assert runner.looks_like_dialogue(lines("¿Qué?"))
    assert runner.looks_like_dialogue(lines("何だと？"))
    # Japanese has no spaces: three short phrases are three lost lines, not lettering
    assert runner.looks_like_dialogue(lines("これは", "ありがとう", "おやすみ"))
    assert runner.looks_like_dialogue(lines("そんなことより早く逃げろ", "ドン", "ゴゴ"))
    assert runner.looks_like_dialogue(lines("안녕하세요"))
    # katakana passes only when it is short or repeated
    assert not runner.looks_like_dialogue(lines("ドドド", "ガシャガシャ", "ズドドドド"))
    assert runner.looks_like_dialogue(lines("コンニチハ"))


def test_names_titles_and_credits_are_not_dialogue():
    """A cover or a page of credits comes back from the model unchanged and needs no translation."""

    runner = load("translate_archive")

    def lines(*texts):
        return [SimpleNamespace(text=text) for text in texts]

    assert not runner.looks_like_dialogue(
        lines("dēmokratía", "KAZÉ SEINEN_", "MotorôMase"), "fr"
    )
    assert not runner.looks_like_dialogue(
        lines("LA FILLE DE LA FORÊT", "Éditions Kana", "Tome 1"), "fr"
    )
    assert not runner.looks_like_dialogue(
        lines(
            "Texto: Ana Ruiz",
            "Dibujo: Luis Gómez",
            "Edición: Norma Editorial",
            "Traducción: Marta Díez",
        ),
        "es",
    )


def test_two_lines_that_end_like_sentences_are_short_balloons_in_a_latin_book():
    """Demokuratia v3 page 154: two small balloons and the names of the people in a chat, kept as an empty page."""

    runner = load("translate_archive")

    def lines(*texts):
        return [SimpleNamespace(text=text) for text in texts]

    balloons = lines(
        "JE",
        "PAS.",
        "twin tail",
        "PAOPAO",
        "Ice&mind",
        "PAOPAO",
        "D'ACCORD...",
        "BAT ASYOU",
        "RECOMMENCERAI",
    )
    assert runner.looks_like_dialogue(balloons, "fr")
    assert runner.looks_like_dialogue(lines("D'ACCORD", "L'HOMME", "ECLAIR"), "fr")
    assert runner.looks_like_dialogue(lines("DON'T", "I'M", "BOOM"), "en")
    # one such line is not enough: a name with an apostrophe, a title that ends with a period
    assert not runner.looks_like_dialogue(
        lines("D'ARTAGNAN", "PAOPAO", "twin tail"), "fr"
    )
    assert not runner.looks_like_dialogue(lines("FIN.", "PAOPAO", "twin tail"), "fr")
    # a chant picked up from an ellipsis is not the end of a sentence
    chant = lines("...YGNAIIH...", "...YOG-SOTHOTH...", "...YAH...")
    assert not runner.looks_like_dialogue(chant, "es")
    # only in a book in a Latin-script language: Russian and an unknown language read as before
    assert not runner.looks_like_dialogue(balloons, "ru")
    assert not runner.looks_like_dialogue(balloons)


def test_running_text_is_dialogue_even_when_it_is_short_or_split():
    runner = load("translate_archive")

    def lines(*texts):
        return [SimpleNamespace(text=text) for text in texts]

    # a word broken across lines means the balloon was read in fragments
    assert runner.looks_like_dialogue(lines("jPA-", "DRE!"), "es")
    assert runner.looks_like_dialogue(lines("...YOG-SO-", "THOTH..."), "es")
    assert runner.looks_like_dialogue(lines("BRAN-", "CAR-", "DIERS!"), "fr")
    assert not runner.looks_like_dialogue(lines("YOG-SOTHOTH...", "DING-DONG!"), "es")
    # fourteen different words or more on the page, in lines of four: prose read as broken lines
    prose = lines(
        "uno dos tres cuatro",
        "cinco seis siete ocho",
        "nueve diez once doce",
        "trece catorce quince dieciseis",
    )
    assert runner.looks_like_dialogue(prose, "es")
    assert not runner.looks_like_dialogue(prose[:3], "es")
    # a chant is one word said again and again, however long the line
    assert not runner.looks_like_dialogue(
        lines("KILL KILL KILL KILL KILL KILL KILL KILL", "GGH"), "fr"
    )
    # laughter is one word said again and again, however many lines it takes
    assert not runner.looks_like_dialogue(
        lines(*["HA", "MA", "HA", "M", "HA"] * 5), "fr"
    )


FRAGMENTS = [
    SimpleNamespace(text=text)
    for text in ("P.", "jPA-", "ね..!", "DRE!", "RR....!", "jSOCO-", "162")
]
COVER = [
    SimpleNamespace(text=text) for text in ("dēmokratía", "KAZÉ SEINEN_", "MotorôMase")
]


def test_a_stray_japanese_character_in_a_spanish_page_is_noise_not_dialogue():
    runner = load("translate_archive")
    barks = [*BARKS, SimpleNamespace(text="ね..!")]
    assert not runner.looks_like_dialogue(barks, "es")
    assert runner.looks_like_dialogue(barks, "ja")  # in a Japanese book it is hiragana
    assert runner.looks_like_dialogue(barks)  # unknown language: strict
    assert not runner.looks_like_dialogue([SimpleNamespace(text="ドドド")] * 4, "fr")


def test_hyphen_split_dialogue_reads_as_dialogue_without_the_stray_hiragana():
    """Balloons broken into fragments (PA- / DRE!, SOCO- / RR...!) are lost dialogue, whatever the OCR added."""

    runner = load("translate_archive")
    assert runner.looks_like_dialogue(FRAGMENTS, "es")
    clean = [line for line in FRAGMENTS if "ね" not in line.text]
    assert runner.looks_like_dialogue(clean, "es")


@pytest.mark.asyncio
async def test_a_cover_of_names_is_kept_when_the_model_read_it_and_returned_it(
    tmp_path, monkeypatch
):
    """The first page of a volume: title, publisher, author. Every run of a French book stopped here."""

    runner = load("translate_archive")
    source, request, output, checkpoints = _spanish_job(
        monkeypatch, tmp_path, runner, COVER, _provider("echo")
    )
    await runner.translate(source, output, request, checkpoints=checkpoints)
    page = json.loads(
        zipfile.ZipFile(next(checkpoints.glob("00001-*.zip"))).read("page.json")
    )
    assert page["translated"] == 0 and page["empty_proof"]["requests"] == 1
    assert "MotorôMase" in page["empty_proof"]["lines"]


@pytest.mark.asyncio
async def test_a_page_of_hyphen_split_balloons_stops_the_job_even_when_the_model_answered(
    tmp_path, monkeypatch
):
    runner = load("translate_archive")
    source, request, output, checkpoints = _spanish_job(
        monkeypatch, tmp_path, runner, FRAGMENTS, _provider("blank")
    )
    with pytest.raises(runner.TranslationFailed, match="reads as dialogue"):
        await runner.translate(source, output, request, checkpoints=checkpoints)
    assert _saved_pages(checkpoints) == [2]
    kept = json.loads(
        (checkpoints / "rejected-pages.jsonl").read_text().splitlines()[-1]
    )
    assert "jSOCO-" in kept["lines"] and kept["requests"] == 1


@pytest.mark.asyncio
async def test_runner_does_not_accept_an_empty_page_when_it_cannot_see_the_provider(
    tmp_path, monkeypatch
):
    runner = load("translate_archive")
    source, request = _archive(tmp_path, runner, 2)
    # Translated text without a single request seen means the traffic watch is blind.
    _fake_engine(
        monkeypatch,
        [
            lambda image: SimpleNamespace(
                result=image,
                textlines=[SimpleNamespace(text="日本語")],
                text_regions=[SimpleNamespace(text="日本語", translation="Hello")],
            ),
            lambda image: SimpleNamespace(
                result=image,
                textlines=[SimpleNamespace(text="ドドド")] * 4,
                text_regions=[],
            ),
        ],
    )
    monkeypatch.setattr(runner, "provider_unavailable_reason", lambda _request: None)
    with pytest.raises(runner.TranslationFailed, match="cannot be seen"):
        await runner.translate(
            source,
            tmp_path / "result.cbz",
            request,
            checkpoints=tmp_path / "checkpoints",
        )


@pytest.mark.asyncio
async def test_provider_traffic_watch_ignores_other_hosts_and_is_removed():
    runner = load("translate_archive")
    original = (httpx.Client.send, httpx.AsyncClient.send)
    trace = runner.ProviderTrace("https://api.deepseek.com/v1")
    restore = trace.install()
    assert httpx.AsyncClient.send is not original[1]

    def answer(request):
        return httpx.Response(500 if request.url.host == "api.deepseek.com" else 404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as client:
        await client.get("https://api.deepseek.com/v1/models")
        await client.get("https://other.example.test/x")
    with httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200))
    ) as client:
        client.get("https://api.deepseek.com/v1/models")
    assert (trace.requests, trace.failures, trace.total) == (2, 1, 2)
    assert trace.problem() == "1 provider request(s) failed"
    assert trace.unproven == ["the request could not be read"]
    trace.page()
    assert (trace.requests, trace.failures, trace.total) == (0, 0, 2)
    assert trace.unproven == []
    restore()
    assert (httpx.Client.send, httpx.AsyncClient.send) == original


@pytest.mark.asyncio
async def test_runner_probes_the_provider_before_paying_for_a_page(
    tmp_path, monkeypatch
):
    runner = load("translate_archive")
    source, request = _archive(tmp_path, runner, 2)
    calls = []
    _fake_engine(
        monkeypatch,
        [
            lambda image: (
                calls.append(1) or SimpleNamespace(result=image, text_regions=[])
            )
        ]
        * 2,
    )
    monkeypatch.setattr(
        runner, "provider_unavailable_reason", lambda _request: "no available balance"
    )
    with pytest.raises(runner.ProviderUnavailable):
        await runner.translate(
            source,
            tmp_path / "result.cbz",
            request,
            checkpoints=tmp_path / "checkpoints",
        )
    assert calls == []


@pytest.mark.asyncio
async def test_runner_keeps_blank_pages_and_records_detected_lines(
    tmp_path, monkeypatch
):
    runner = load("translate_archive")
    source, request = _archive(tmp_path, runner, 2)
    output, checkpoints = tmp_path / "result.cbz", tmp_path / "checkpoints"
    _fake_engine(
        monkeypatch,
        [
            lambda image: SimpleNamespace(
                result=image,
                textlines=[1, 2],
                text_regions=[SimpleNamespace(text="日本語", translation="Hello")],
            ),
            lambda image: SimpleNamespace(result=image, textlines=[], text_regions=[]),
        ],
    )
    monkeypatch.setattr(runner, "provider_unavailable_reason", lambda _request: None)
    await runner.translate(source, output, request, checkpoints=checkpoints)
    with zipfile.ZipFile(output) as archive:
        receipt = json.loads(archive.read("translation.json"))
    assert receipt["page_count"] == 2 and receipt["translated_regions"] == 1
    page = json.loads(
        zipfile.ZipFile(next(checkpoints.glob("00001-*.zip"))).read("page.json")
    )
    assert page["detected_lines"] == 2 and page["detected_source_lines"] == 0


def test_only_lines_in_the_source_script_count_as_dropped_text():
    runner = load("translate_archive")
    lines = [
        SimpleNamespace(text=text)
        for text in ("LOST CAT", "SALE TODAY", "描凶记", "こんにちは", "")
    ]
    assert runner.source_line_count(lines, "zh") == 2
    assert runner.source_line_count(lines, "zh-hk") == 2
    assert runner.source_line_count(lines, "ja") == 2
    assert (
        runner.source_line_count([SimpleNamespace(text="안녕하세요"), *lines], "ko")
        == 2  # Hangul and Han, not kana
    )
    assert runner.source_line_count([SimpleNamespace(text="Привет"), *lines], "ru") == 1
    assert (
        runner.source_line_count(lines, "fr") == 5
    )  # a Latin-script source: every line counts
    assert runner.source_line_count([1, 2, 3], "ja") == 0  # no text attribute


@pytest.mark.asyncio
async def test_runner_accepts_a_page_of_english_posters_and_a_stylised_title(
    tmp_path, monkeypatch
):
    """A full-page illustration with English poster words and one stylised Chinese title is not a failure."""

    runner = load("translate_archive")
    source, request = _archive(tmp_path, runner, 2)
    output, checkpoints = tmp_path / "result.cbz", tmp_path / "checkpoints"
    posters = [
        SimpleNamespace(text=text)
        for text in ("LOST CAT", "SALE TODAY", "OPEN LATE", "描凶记")
    ]
    _fake_engine(
        monkeypatch,
        [
            lambda image: SimpleNamespace(
                result=image, textlines=posters, text_regions=[]
            ),
            lambda image: SimpleNamespace(
                result=image,
                textlines=[SimpleNamespace(text="日本語")],
                text_regions=[SimpleNamespace(text="日本語", translation="Hello")],
            ),
        ],
    )
    monkeypatch.setattr(runner, "provider_unavailable_reason", lambda _request: None)
    await runner.translate(source, output, request, checkpoints=checkpoints)
    page = json.loads(
        zipfile.ZipFile(next(checkpoints.glob("00001-*.zip"))).read("page.json")
    )
    assert page["translated"] == 0 and page["detected_lines"] == 4
    assert page["detected_source_lines"] == 1


def test_a_single_japanese_character_stuck_to_english_is_noise():
    runner = load("translate_archive")
    strip = runner.without_stray_japanese
    assert strip("GOTTA ADMIT \u30eb") == "GOTTA ADMIT"
    assert strip("I MUST \u30eb SAY THIS") == "I MUST SAY THIS"
    assert strip("WHAT \u30eb!") == "WHAT!"
    assert strip("ADMIT\u30ebTED") == "ADMITTED"
    assert strip("TWO \u30eb AND \u4e00 STRAYS") == "TWO AND STRAYS"
    assert strip("\u3072 NO, \u3072 YES") == "NO, YES"


def test_japanese_text_and_regions_without_letters_are_left_for_the_caption_writer():
    runner = load("translate_archive")
    strip = runner.without_stray_japanese
    assert strip("THANKS, \u5148\u751f!") is None  # two in a row: text, not noise
    assert (
        strip("HELLO \u30eb AND \u5148\u751f") is None
    )  # one word of real Japanese spoils the region
    assert strip("\u30eb!") is None  # nothing but the noise: no caption to keep
    assert strip("\u30eb\u3001\u30eb") is None
    assert strip("PLAIN ENGLISH") is None
    assert strip("") is None
    assert (
        strip("WAIT\u30fbWHAT") is None
    )  # the caption writer turns the middle dot into a dot


def _noise_job(monkeypatch, tmp_path, runner, language, regions, marker):
    """One page of a book in `language` whose engine returned these (source, English) regions."""
    source, request = _archive(tmp_path, runner, 1, language=language)
    _fake_engine(
        monkeypatch,
        [
            lambda image: SimpleNamespace(
                result=image,
                textlines=[SimpleNamespace(text=text) for text, _ in regions],
                text_regions=[
                    SimpleNamespace(text=text, translation=english)
                    for text, english in regions
                ],
            )
        ],
    )
    monkeypatch.setitem(
        sys.modules,
        "py3langid",
        SimpleNamespace(
            classify=lambda text: (
                language.split("-")[0] if marker in text else "en",
                1,
            )
        ),
    )
    monkeypatch.setattr(runner, "provider_unavailable_reason", lambda _request: None)
    return source, request, tmp_path / "result.cbz", tmp_path / "checkpoints"


def _checkpoint_page(checkpoints):
    return json.loads(
        zipfile.ZipFile(next(checkpoints.glob("00001-*.zip"))).read("page.json")
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["fr", "es", "pt-br"])
async def test_the_stray_character_is_taken_out_of_the_english_and_noted(
    tmp_path, monkeypatch, language
):
    runner = load("translate_archive")
    regions = [
        ("hola, il faut l'admettre \u30eb", "GOTTA ADMIT IT \u30eb"),
        ("hola, on y va", "LET'S GO"),
    ]
    source, request, output, checkpoints = _noise_job(
        monkeypatch, tmp_path, runner, language, regions, "hola"
    )
    await runner.translate(source, output, request, checkpoints=checkpoints)
    page = _checkpoint_page(checkpoints)
    assert page["target_text"] == ["GOTTA ADMIT IT", "LET'S GO"]
    assert page["stripped_noise"] == [{"region": 0, "removed": "\u30eb"}]
    assert (
        page["source_text"][0] == regions[0][0]
    )  # what the OCR read is evidence: never touched
    assert page["translated"] == 2 and page["untranslated"] == 0


@pytest.mark.asyncio
async def test_a_page_without_noise_gets_no_note(tmp_path, monkeypatch):
    runner = load("translate_archive")
    source, request, output, checkpoints = _noise_job(
        monkeypatch, tmp_path, runner, "es", [("hola amigo", "Hello friend")], "hola"
    )
    await runner.translate(source, output, request, checkpoints=checkpoints)
    page = _checkpoint_page(checkpoints)
    assert "stripped_noise" not in page and page["target_text"] == ["Hello friend"]


@pytest.mark.asyncio
async def test_japanese_the_model_left_in_the_english_of_a_latin_book_still_reaches_the_caption_writer(
    tmp_path, monkeypatch
):
    """Two characters in a row are a word nobody translated: the page is kept as it came, not tidied away."""
    runner = load("translate_archive")
    regions = [("hola, gracias", "THANKS, \u5148\u751f!")]
    source, request, output, checkpoints = _noise_job(
        monkeypatch, tmp_path, runner, "es", regions, "hola"
    )
    await runner.translate(source, output, request, checkpoints=checkpoints)
    page = _checkpoint_page(checkpoints)
    assert (
        page["target_text"] == ["THANKS, \u5148\u751f!"]
        and "stripped_noise" not in page
    )


@pytest.mark.asyncio
async def test_a_book_in_japanese_keeps_every_japanese_character_in_the_english(
    tmp_path, monkeypatch
):
    """There an untranslated character is a translation that failed: the caption writer must still refuse it."""
    runner = load("translate_archive")
    regions = [("\u65e5\u672c\u8a9e\u3067\u3059", "Hello \u30eb")]
    source, request, output, checkpoints = _noise_job(
        monkeypatch, tmp_path, runner, "ja", regions, "\u65e5\u672c\u8a9e"
    )
    await runner.translate(source, output, request, checkpoints=checkpoints)
    page = _checkpoint_page(checkpoints)
    assert page["target_text"] == ["Hello \u30eb"] and "stripped_noise" not in page


def _book(monkeypatch, tmp_path, runner, lost):
    """A Spanish book with one page per item of `lost`: True is a page whose dialogue the engine dropped."""
    source, request = _archive(tmp_path, runner, len(lost), language="es")
    ordinary = lambda image: SimpleNamespace(  # noqa: E731
        result=image,
        textlines=[SimpleNamespace(text="Hola amigo")],
        text_regions=[SimpleNamespace(text="Hola amigo", translation="Hello friend")],
    )
    dropped = lambda image: SimpleNamespace(  # noqa: E731
        result=image, textlines=SENTENCES, text_regions=[]
    )
    _fake_engine(
        monkeypatch,
        [dropped if page_lost else ordinary for page_lost in lost],
        _provider("blank"),
    )
    monkeypatch.setitem(
        sys.modules,
        "py3langid",
        SimpleNamespace(classify=lambda text: ("es" if "hola" in text else "en", 1)),
    )
    monkeypatch.setattr(runner, "provider_unavailable_reason", lambda _request: None)
    return source, request, tmp_path / "result.cbz", tmp_path / "checkpoints"


@pytest.mark.asyncio
async def test_refused_pages_are_put_off_and_the_job_stops_at_the_end_listing_all_of_them(
    tmp_path, monkeypatch
):
    runner = load("translate_archive")
    source, request, output, checkpoints = _book(
        monkeypatch, tmp_path, runner, [True, False, True, False]
    )
    with pytest.raises(
        runner.SuspectPage,
        match=r"2 page\(s\) need review \(1, 3\); every other page is saved",
    ) as stop:
        await runner.translate(source, output, request, checkpoints=checkpoints)
    assert runner.transient_error(
        stop.value
    )  # retried, then blocked, as a refusal always was
    assert "First: Page 1 has" in str(stop.value)
    assert _saved_pages(checkpoints) == [2, 4] and not output.exists()
    kept = [
        json.loads(line)
        for line in (checkpoints / "rejected-pages.jsonl").read_text().splitlines()
    ]
    assert [entry["page"] for entry in kept] == [1, 3]


@pytest.mark.asyncio
async def test_a_book_may_have_five_refused_pages_but_not_a_sixth(
    tmp_path, monkeypatch
):
    runner = load("translate_archive")
    source, request, output, checkpoints = _book(
        monkeypatch, tmp_path, runner, [True] * 5 + [False] * 3
    )
    with pytest.raises(runner.SuspectPage, match=r"5 page\(s\) need review"):
        await runner.translate(source, output, request, checkpoints=checkpoints)
    assert _saved_pages(checkpoints) == [6, 7, 8]
    other = tmp_path / "other"
    other.mkdir()
    source, request, output, checkpoints = _book(
        monkeypatch, other, runner, [True] * 6 + [False] * 2
    )
    with pytest.raises(
        runner.SuspectPage,
        match=r"Stopped after 6 refused pages \(1, 2, 3, 4, 5, 6\).*not only those pages",
    ):
        await runner.translate(source, output, request, checkpoints=checkpoints)
    assert _saved_pages(checkpoints) == []  # it did not go on to read pages 7 and 8


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["text", "empty"])
async def test_a_page_reviewed_by_hand_lets_the_job_finish(tmp_path, monkeypatch, kind):
    """What the operator's review tool writes: a checkpoint with the job's identity and a note of who looked at the page."""

    runner = load("translate_archive")
    source, request, output, checkpoints = _book(
        monkeypatch, tmp_path, runner, [True, False]
    )
    with pytest.raises(runner.SuspectPage):
        await runner.translate(source, output, request, checkpoints=checkpoints)
    identity = json.loads(
        zipfile.ZipFile(next(checkpoints.glob("00002-*.zip"))).read("page.json")
    )["identity"]
    image = io.BytesIO()
    Image.new("RGB", (32, 48), (1, 0, 0)).save(image, format="PNG")
    reviewed = {"by": "operator", "note": "looked at the page"}
    page = {
        "identity": identity,
        "index": 1,
        "translated": 1 if kind == "text" else 0,
        "untranslated": 0,
        "detected_lines": 4,
        "detected_source_lines": 4,
        "source_text": ["¿Dónde está mi madre?"] if kind == "text" else [],
        "target_text": ["Where is my mother?"] if kind == "text" else [],
    }
    if kind == "text":
        page["reviewed"] = reviewed
    else:
        page["empty_proof"] = {"requests": 0, "lines": ["SFX"], "reviewed": reviewed}
    runner.save_checkpoint(checkpoints, 1, image.getvalue(), page)
    await runner.translate(source, output, request, checkpoints=checkpoints)
    with zipfile.ZipFile(output) as archive:
        receipt = json.loads(archive.read("translation.json"))
    assert receipt["page_count"] == 2
    assert receipt["translated_regions"] == (2 if kind == "text" else 1)


def _answering(answers, failing=(), flaky=0, seen=None):
    """A provider that answers each region by its text (no entry: blank), fails a request that carries a text of
    `failing`, and fails the first `flaky` requests whatever they carry."""

    calls = {"count": 0}

    def answer(request):
        calls["count"] += 1
        body = json.loads(request.content)
        if seen is not None:
            seen.append(body)
        prompt = body["messages"][-1]["content"]
        asked = re.findall(r"(?m)^<\|(\d+)\|>(.*)$", prompt)
        single = not asked  # the runner's own request for one lost line carries the line, not numbered regions
        if single:
            asked = [("1", prompt)]
        if calls["count"] <= flaky or any(
            region.strip() in failing for _, region in asked
        ):
            return httpx.Response(500, json={})
        if single:
            content = answers.get(prompt.strip(), "")
        else:
            content = "\n".join(
                f"<|{number}|>{answers.get(region.strip(), '')}"
                for number, region in asked
            )
        message = {"role": "assistant", "content": content}
        return httpx.Response(
            200, json={"choices": [{"finish_reason": "stop", "message": message}]}
        )

    return answer


def _region_job(
    monkeypatch,
    tmp_path,
    runner,
    kept,
    requests,
    provider,
    lines=None,
    on_translate=None,
):
    """One page of a Spanish book: the engine keeps the regions of `kept` (source, English) and sends `requests`."""
    source, request = _archive(tmp_path, runner, 1, language="es")
    texts = lines if lines is not None else [region for region, _ in kept]
    _fake_engine(
        monkeypatch,
        [
            lambda image: SimpleNamespace(
                result=image,
                textlines=[SimpleNamespace(text=line) for line in texts],
                text_regions=[
                    SimpleNamespace(text=region, translation=english)
                    for region, english in kept
                ],
            )
        ],
        provider,
        requests={1: requests},
        on_translate=on_translate,
    )
    monkeypatch.setitem(
        sys.modules,
        "py3langid",
        SimpleNamespace(classify=lambda text: ("es" if "hola" in text else "en", 1)),
    )
    monkeypatch.setattr(runner, "provider_unavailable_reason", lambda _request: None)
    if (
        provider is not None
    ):  # the runner's own request for a lost region goes to the same stub
        monkeypatch.setattr(runner, "PROVIDER_TRANSPORT", httpx.MockTransport(provider))
    return source, request, tmp_path / "result.cbz", tmp_path / "checkpoints"


HELLO = [("Hola amigo", "Hello friend")]


def test_one_region_is_a_balloon_or_lettering():
    runner = load("translate_archive")
    for balloon in (
        "BRAN- CAR- DIERS!",
        "BON- JOUR!",
        "SA- LUT!",
        "D'ACCORD...",
        "Je sais.",
        "¿Dónde está?",
    ):
        assert runner.region_reads_as_dialogue(balloon, "fr"), balloon
    for lettering in ("BOUM!", "GRRR...", "HÉ!", "OUI.", "ZAS", "162", "Motorô Mase"):
        assert not runner.region_reads_as_dialogue(lettering, "fr"), lettering
    # Russian and an unknown language read as before: only the page reading applies
    assert not runner.region_reads_as_dialogue("BON- JOUR!", "ru")
    assert not runner.region_reads_as_dialogue("BON- JOUR!")


@pytest.mark.asyncio
async def test_a_balloon_sent_to_the_model_and_missing_from_the_page_stops_the_page(
    tmp_path, monkeypatch
):
    """The model answered blank for a balloon read in fragments; the engine dropped it and the receipt would say nothing."""

    runner = load("translate_archive")
    source, request, output, checkpoints = _region_job(
        monkeypatch,
        tmp_path,
        runner,
        HELLO,
        [["Hola amigo", "SOCO- RR...!", "BOUM"]],
        _answering({"Hola amigo": "Hello friend"}),
    )
    with pytest.raises(
        runner.SuspectPage, match=r"SOCO- RR\.\.\.!.*answered blank"
    ) as stop:
        await runner.translate(source, output, request, checkpoints=checkpoints)
    assert "BOUM" not in str(stop.value)  # a sound the model left blank is not a loss
    kept = json.loads(
        (checkpoints / "rejected-pages.jsonl").read_text().splitlines()[-1]
    )
    assert kept["page"] == 1 and kept["lines"] == ["SOCO- RR...!"]


@pytest.mark.asyncio
async def test_sound_effects_the_model_left_blank_do_not_stop_a_page_with_dialogue(
    tmp_path, monkeypatch
):
    runner = load("translate_archive")
    source, request, output, checkpoints = _region_job(
        monkeypatch,
        tmp_path,
        runner,
        HELLO,
        [["Hola amigo", "BOUM", "GRRR...", "ZAS!"]],
        _answering({"Hola amigo": "Hello friend"}),
    )
    await runner.translate(source, output, request, checkpoints=checkpoints)
    page = json.loads(
        zipfile.ZipFile(next(checkpoints.glob("00001-*.zip"))).read("page.json")
    )
    assert (page["regions_sent"], page["regions_kept"]) == (4, 1)
    assert "region_check" not in page and "provider_failures" not in page
    assert page["dropped_regions"] == [
        "BOUM (blank)",
        "GRRR... (blank)",
        "ZAS! (blank)",
    ]


@pytest.mark.asyncio
async def test_a_region_lost_to_a_failed_request_stops_the_job_even_when_the_page_has_others(
    tmp_path, monkeypatch
):
    """The engine keeps the text of a failed request and drops it as unchanged: what is left of the page looks fine."""

    runner = load("translate_archive")
    source, request, output, checkpoints = _region_job(
        monkeypatch,
        tmp_path,
        runner,
        HELLO,
        [["Hola amigo", "SOCORRO"], ["Hola amigo"]],
        _answering({"Hola amigo": "Hello friend"}, failing={"SOCORRO"}),
    )
    with pytest.raises(
        runner.TranslationFailed, match=r"SOCORRO.*its request failed"
    ) as stop:
        await runner.translate(source, output, request, checkpoints=checkpoints)
    assert not isinstance(
        stop.value, runner.SuspectPage
    )  # the provider failed: the job stops now and is retried
    assert runner.transient_error(stop.value)
    assert not list(checkpoints.glob("*.zip"))


@pytest.mark.asyncio
async def test_a_request_that_failed_and_was_retried_in_full_costs_the_page_nothing(
    tmp_path, monkeypatch
):
    runner = load("translate_archive")
    kept = [("Hola amigo", "Hello friend"), ("SOCORRO", "HELP")]
    source, request, output, checkpoints = _region_job(
        monkeypatch,
        tmp_path,
        runner,
        kept,
        [["Hola amigo", "SOCORRO"], ["Hola amigo", "SOCORRO"]],
        _answering({"Hola amigo": "Hello friend", "SOCORRO": "HELP"}, flaky=1),
    )
    await runner.translate(source, output, request, checkpoints=checkpoints)
    page = json.loads(
        zipfile.ZipFile(next(checkpoints.glob("00001-*.zip"))).read("page.json")
    )
    assert page["provider_failures"] == 1 and (
        page["regions_sent"],
        page["regions_kept"],
    ) == (2, 2)
    with zipfile.ZipFile(output) as archive:
        assert json.loads(archive.read("translation.json"))["provider_failures"] == 1


@pytest.mark.asyncio
async def test_a_tiny_page_whose_request_failed_is_not_kept_as_empty(
    tmp_path, monkeypatch
):
    """Two lines and no region used to pass as an empty page whatever the provider did."""

    runner = load("translate_archive")
    source, request, output, checkpoints = _region_job(
        monkeypatch,
        tmp_path,
        runner,
        [],
        [["SOCORRO", "PADRE"]],
        _answering({}, failing={"SOCORRO"}),
        lines=["SOCORRO", "PADRE"],
    )
    with pytest.raises(runner.TranslationFailed, match="its request failed"):
        await runner.translate(source, output, request, checkpoints=checkpoints)


@pytest.mark.asyncio
async def test_the_region_check_does_not_run_when_the_engine_sends_other_text_than_it_keeps(
    tmp_path, monkeypatch
):
    """If no region kept matches a region sent, the texts cannot be compared: say so, never guess."""

    runner = load("translate_archive")
    source, request, output, checkpoints = _region_job(
        monkeypatch,
        tmp_path,
        runner,
        HELLO,
        [["¿ALGO DISTINTO?", "OTRA COSA"]],
        _answering({}),
    )
    await runner.translate(source, output, request, checkpoints=checkpoints)
    page = json.loads(
        zipfile.ZipFile(next(checkpoints.glob("00001-*.zip"))).read("page.json")
    )
    assert page["region_check"].startswith("skipped")


@pytest.mark.asyncio
async def test_detected_dialogue_that_no_region_covers_is_noted_on_the_page(
    tmp_path, monkeypatch
):
    runner = load("translate_archive")
    source, request, output, checkpoints = _region_job(
        monkeypatch,
        tmp_path,
        runner,
        HELLO,
        [["Hola amigo"]],
        _answering({"Hola amigo": "Hello friend"}),
        lines=["Hola amigo", "¿Dónde está mi madre?", "162"],
    )
    await runner.translate(source, output, request, checkpoints=checkpoints)
    page = json.loads(
        zipfile.ZipFile(next(checkpoints.glob("00001-*.zip"))).read("page.json")
    )
    assert page["uncovered_dialogue"] == ["¿Dónde está mi madre?"]  # noted, not refused


def test_a_word_broken_across_two_lines_is_put_back_together():
    runner = load("translate_archive")
    for broken, whole in (
        ("PAR- DON...", "PARDON..."),
        ("BON- JOUR!", "BONJOUR!"),
        ("BRAN- CAR- DIERS!", "BRANCARDIERS!"),
        ("LA FILLE DE M. KAMI- TORI !", "LA FILLE DE M. KAMITORI !"),
        ("D'AC- CORD...", "D'ACCORD..."),
    ):
        assert runner.joined_words(broken) == whole
    # a hyphen inside a word, a dash with spaces on both sides and one with no letter after it are not breaks
    for kept in ("peut-être", "oui - non", "EH -", "- NON", "A-B", "12- 34"):
        assert runner.joined_words(kept) == kept


class _GPT:
    """What the engine's prompt builder looks like to the runner: it yields the prompt and its size."""

    def _assemble_prompts(self, from_lang, to_lang, queries):
        yield (
            "\n".join(f"<|{n}|>{query}" for n, query in enumerate(queries, 1)),
            len(queries),
        )


@pytest.mark.asyncio
async def test_the_model_is_sent_whole_words_in_a_latin_book_and_the_prompt_builder_is_restored(
    tmp_path, monkeypatch
):
    runner = load("translate_archive")
    monkeypatch.setitem(
        sys.modules,
        "manga_translator.translators.common_gpt",
        SimpleNamespace(CommonGPTTranslator=_GPT),
    )
    original, seen = _GPT._assemble_prompts, []
    source, request, output, checkpoints = _region_job(
        monkeypatch,
        tmp_path,
        runner,
        HELLO,
        [["Hola amigo"]],
        _answering({"Hola amigo": "Hello friend"}),
        on_translate=lambda: seen.extend(
            _GPT()._assemble_prompts("es", "en", ["PAR- DON...", "peut-être"])
        ),
    )
    await runner.translate(source, output, request, checkpoints=checkpoints)
    prompt = seen[0][0]
    assert "<|1|>PARDON..." in prompt and "PAR- DON" not in prompt
    assert "peut-être" in prompt
    assert _GPT._assemble_prompts is original  # taken out again when the job ends


@pytest.mark.asyncio
async def test_a_book_in_japanese_is_sent_as_it_was_read(tmp_path, monkeypatch):
    runner = load("translate_archive")
    monkeypatch.setitem(
        sys.modules,
        "manga_translator.translators.common_gpt",
        SimpleNamespace(CommonGPTTranslator=_GPT),
    )
    source, request = _archive(tmp_path, runner, 1)
    seen = []
    _fake_engine(
        monkeypatch,
        [
            lambda image: SimpleNamespace(
                result=image,
                textlines=[SimpleNamespace(text="日本語")],
                text_regions=[SimpleNamespace(text="日本語", translation="Hello")],
            )
        ],
        on_translate=lambda: seen.extend(
            _GPT()._assemble_prompts("ja", "en", ["PAR- DON..."])
        ),
    )
    monkeypatch.setattr(runner, "provider_unavailable_reason", lambda _request: None)
    await runner.translate(
        source, tmp_path / "result.cbz", request, checkpoints=tmp_path / "checkpoints"
    )
    assert "PAR- DON..." in seen[0][0]


def _page_json(checkpoints):
    return json.loads(
        zipfile.ZipFile(next(checkpoints.glob("00001-*.zip"))).read("page.json")
    )


@pytest.mark.asyncio
async def test_a_balloon_the_engine_lost_is_asked_for_directly_and_put_back_in_its_place(
    tmp_path, monkeypatch
):
    runner = load("translate_archive")
    kept = [("Hola amigo", "Hello friend"), ("Adiós", "Goodbye")]
    source, request, output, checkpoints = _region_job(
        monkeypatch,
        tmp_path,
        runner,
        kept,
        [["Hola amigo", "SOCO- RR...!", "Adiós"]],
        _answering(
            {"Hola amigo": "Hello friend", "Adiós": "Goodbye", "SOCORR...!": "HELP...!"}
        ),
    )
    await runner.translate(source, output, request, checkpoints=checkpoints)
    page = _page_json(checkpoints)
    assert page["target_text"] == [
        "Hello friend",
        "HELP...!",
        "Goodbye",
    ]  # in the order the engine numbered them
    assert page["source_text"][1] == "SOCO- RR...!" and page["translated"] == 3
    assert page["recovered_regions"] == [
        {"source": "SOCO- RR...!", "english": "HELP...!"}
    ]
    assert (
        page["regions_sent"] == 3
    )  # the runner's own request is not counted as the engine's


@pytest.mark.asyncio
async def test_a_name_the_model_will_not_change_does_not_stop_the_page(
    tmp_path, monkeypatch
):
    runner = load("translate_archive")
    source, request, output, checkpoints = _region_job(
        monkeypatch,
        tmp_path,
        runner,
        HELLO,
        [["Hola amigo", "MAE- ZAWA..."]],
        _answering(
            {
                "Hola amigo": "Hello friend",
                "MAE- ZAWA...": "MAE- ZAWA...",
                "MAEZAWA...": "MAEZAWA...",
            }
        ),
    )
    await runner.translate(source, output, request, checkpoints=checkpoints)
    page = _page_json(checkpoints)
    assert (
        page["unchanged_regions"] == ["MAE- ZAWA..."]
        and "recovered_regions" not in page
    )
    assert page["target_text"] == ["Hello friend"]


@pytest.mark.asyncio
async def test_a_region_lost_to_a_failed_request_is_recovered_when_the_provider_answers_again(
    tmp_path, monkeypatch
):
    runner = load("translate_archive")
    source, request, output, checkpoints = _region_job(
        monkeypatch,
        tmp_path,
        runner,
        HELLO,
        [["Hola amigo", "SOCORRO"], ["Hola amigo"]],
        _answering({"Hola amigo": "Hello friend", "SOCORRO": "HELP"}, flaky=1),
    )
    await runner.translate(source, output, request, checkpoints=checkpoints)
    page = _page_json(checkpoints)
    assert page["provider_failures"] == 1
    assert page["target_text"] == ["Hello friend", "HELP"]
    assert page["recovered_regions"] == [{"source": "SOCORRO", "english": "HELP"}]


@pytest.mark.asyncio
async def test_a_page_with_no_region_left_is_filled_by_the_recovery(
    tmp_path, monkeypatch
):
    runner = load("translate_archive")
    source, request, output, checkpoints = _region_job(
        monkeypatch,
        tmp_path,
        runner,
        [],
        [["HOLA, SOCORRO", "HOLA, PADRE"]],
        _answering(
            {"HOLA, SOCORRO": "HELLO, HELP", "HOLA, PADRE": "HELLO, FATHER"}, flaky=1
        ),
        lines=["HOLA, SOCORRO", "HOLA, PADRE"],
    )
    await runner.translate(source, output, request, checkpoints=checkpoints)
    page = _page_json(checkpoints)
    assert page["translated"] == 2 and page["target_text"] == [
        "HELLO, HELP",
        "HELLO, FATHER",
    ]


NOTE = "\u3010The text appears corrupted/gibberish and cannot be translated.\u3011"


def test_a_note_about_unreadable_text_is_not_a_translation():
    runner = load("translate_archive")
    for note in (
        NOTE,
        "The text appears to be gibberish.",
        "I cannot translate this text.",
        "Unable to translate: the line is garbled.",
        "\u3010unreadable\u3011",
    ):
        assert runner.is_meta_answer(note), note
    for line in (
        "That's gibberish!",
        "TEXT ME LATER!",
        "HELP ME TRANSLATE THIS",
        "I'M SORRY!",
        "SORRY... ABOUT THAT",
        "AS AN AI, I HAVE NO CHOICE",
        "IT CANNOT BE TRANSLATED!! AHA IA!!",
        "\u3010FIRST PUBLISHED IN\u3011ICE ROAD",
        "\u3010TEXT: HIROSHI TANAKA\u3011",
        "I CAN'T TRANSLATE FOR YOU",
        "The text was on the door",
        "",
    ):
        assert not runner.is_meta_answer(line), line


@pytest.mark.asyncio
async def test_a_page_the_ocr_could_not_read_is_refused_when_the_model_says_so(
    tmp_path, monkeypatch
):
    """Dunwich v1 page 33: cursive read as noise, four regions, four notes kept as the translation."""

    runner = load("translate_archive")
    noise = "Ate uonte de ea, neyiauu ceutnual de ?Yaaaaccwuuwetta, dejauudaa"
    source, request, output, checkpoints = _region_job(
        monkeypatch,
        tmp_path,
        runner,
        [(noise, NOTE), ("hola amigo", "Hello friend")],
        [[noise, "hola amigo"]],
        _answering({"hola amigo": "Hello friend"}),
    )
    with pytest.raises(
        runner.SuspectPage, match="could not read.*the OCR did not read this page"
    ):
        await runner.translate(source, output, request, checkpoints=checkpoints)
    assert not list(checkpoints.glob("*.zip"))


@pytest.mark.asyncio
async def test_a_short_noise_region_the_model_could_not_translate_is_left_out(
    tmp_path, monkeypatch
):
    runner = load("translate_archive")
    source, request, output, checkpoints = _region_job(
        monkeypatch,
        tmp_path,
        runner,
        [("Hola amigo", "Hello friend"), ("kdl", NOTE)],
        [["Hola amigo", "kdl"]],
        _answering({"Hola amigo": "Hello friend"}),
    )
    await runner.translate(source, output, request, checkpoints=checkpoints)
    page = _page_json(checkpoints)
    assert page["target_text"] == ["Hello friend"] and page["unreadable_regions"] == [
        "kdl"
    ]


def test_a_stored_page_that_kept_such_a_note_is_read_again(tmp_path):
    runner = load("translate_archive")
    image = io.BytesIO()
    Image.new("RGB", (32, 48), (1, 0, 0)).save(image, format="PNG")
    good = {
        "identity": "x",
        "index": 1,
        "translated": 1,
        "untranslated": 0,
        "detected_lines": 1,
        "detected_source_lines": 1,
        "source_text": ["hola amigo"],
        "target_text": ["Hello friend"],
    }
    directory = tmp_path / "checkpoints"
    runner.save_checkpoint(directory, 1, image.getvalue(), good)
    assert runner.load_checkpoint(directory, 1, "x", (32, 48)) is not None
    bad = dict(good, target_text=[NOTE])
    other = tmp_path / "other"
    runner.save_checkpoint(other, 1, image.getvalue(), bad)
    assert runner.load_checkpoint(other, 1, "x", (32, 48)) is None


@pytest.mark.asyncio
async def test_a_direct_answer_that_is_a_note_recovers_nothing(tmp_path, monkeypatch):
    runner = load("translate_archive")
    source, request, output, checkpoints = _region_job(
        monkeypatch,
        tmp_path,
        runner,
        HELLO,
        [["Hola amigo", "SOCO- RR...!"]],
        _answering(
            {
                "Hola amigo": "Hello friend",
                "SOCORR...!": "I cannot translate this text.",
            }
        ),
    )
    with pytest.raises(runner.SuspectPage, match="could not be recovered"):
        await runner.translate(source, output, request, checkpoints=checkpoints)


INVENTED = "ZORK BLAN G TRUDAR EL!! MIKES G OM.."


@pytest.mark.asyncio
async def test_a_line_the_model_says_is_not_speech_stays_unread_and_the_page_is_read(
    tmp_path, monkeypatch
):
    """Dunwich v1 p163: a balloon in an invented language sat in the same page as a plain balloon the engine lost."""

    runner = load("translate_archive")
    source, request, output, checkpoints = _region_job(
        monkeypatch,
        tmp_path,
        runner,
        HELLO,
        [["Hola amigo", INVENTED, "iVEN AQUi WLBUR."]],
        _answering(
            {
                "Hola amigo": "Hello friend",
                "iVEN AQUi WLBUR.": "Come here, Wilbur.",
                INVENTED: "NOT SPEECH",
            }
        ),
    )
    await runner.translate(source, output, request, checkpoints=checkpoints)
    page = _page_json(checkpoints)
    assert page["target_text"] == ["Hello friend", "Come here, Wilbur."]
    assert page["unreadable_regions"] == [INVENTED]  # kept to be looked at, no caption
    assert page["recovered_regions"] == [
        {"source": "iVEN AQUi WLBUR.", "english": "Come here, Wilbur."}
    ]


@pytest.mark.asyncio
async def test_a_longer_or_a_third_not_speech_line_still_stops_the_page(
    tmp_path, monkeypatch
):
    runner = load("translate_archive")
    long_line = "ZORK BLAN G TRUDAR EL!! MIKES G OM.. " * 3
    source, request, output, checkpoints = _region_job(
        monkeypatch,
        tmp_path,
        runner,
        HELLO,
        [["Hola amigo", long_line]],
        _answering({"Hola amigo": "Hello friend", long_line.strip(): "NOT SPEECH"}),
    )
    with pytest.raises(runner.SuspectPage, match="could not be recovered"):
        await runner.translate(source, output, request, checkpoints=checkpoints)

    many = [
        f"ZORK BLAN G TRUDAR EL!! MIKES G OM.. {n}"
        for n in range(runner.UNREADABLE_PER_PAGE + 1)
    ]
    (tmp_path / "many").mkdir()
    source, request, output, checkpoints = _region_job(
        monkeypatch,
        tmp_path / "many",
        runner,
        HELLO,
        [["Hola amigo", *many]],
        _answering(
            {"Hola amigo": "Hello friend", **{line: "NOT SPEECH" for line in many}}
        ),
    )
    with pytest.raises(runner.SuspectPage, match="could not be recovered"):
        await runner.translate(source, output, request, checkpoints=checkpoints)


@pytest.mark.asyncio
async def test_each_lost_line_has_its_own_request_and_a_failed_one_takes_no_other_with_it():
    runner = load("translate_archive")
    seen = []
    provider = _answering(
        {"HOLA": "HELLO", "ADIOS": "GOODBYE"}, failing=("ROTO",), seen=seen
    )
    runner.PROVIDER_TRANSPORT = httpx.MockTransport(provider)
    try:
        request = {
            "source_language": "es",
            "ai": {
                "base_url": "https://api.deepseek.com",
                "model": "m",
                "api_key": "k",
            },
        }
        answers = await runner.translate_lost(
            request, ["HOLA", "ROTO", "ADIOS", "GRRR ZZZ"]
        )
    finally:
        runner.PROVIDER_TRANSPORT = None
    assert answers == ["HELLO", None, "GOODBYE", None]  # a blank answer is no answer
    assert len(seen) == 4 and all(len(body["messages"]) == 2 for body in seen)
    assert {body["messages"][1]["content"] for body in seen} == {
        "HOLA",
        "ROTO",
        "ADIOS",
        "GRRR ZZZ",
    }
    # the reasoning of the provider is off for these requests and their answer is short
    assert all(
        body["thinking"] == {"type": "disabled"} and body["max_tokens"] == 400
        for body in seen
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("language", "source_text"), [("ja", "ありがとう"), ("fr", "Merci")]
)
async def test_lost_region_recovery_preserves_english_in_a_foreign_language_book(
    language,
    source_text,
):
    runner = load("translate_archive")
    seen = []
    english = "PB=Paperback HC=Hardcover ED=Edition"
    provider = _answering(
        {english: english, source_text: "Thank you", "ZZ XQ": "NOT SPEECH"}, seen=seen
    )
    runner.PROVIDER_TRANSPORT = httpx.MockTransport(provider)
    request = {
        "source_language": language,
        "ai": {
            "base_url": "https://api.example.com/v1",
            "model": "fixture-model",
            "api_key": "fixture-key",
        },
    }
    answers = await runner.translate_lost(request, [english, source_text, "ZZ XQ"])
    assert answers == [english, "Thank you", runner.NOT_SPEECH]
    # Recovery must not ask the provider to reject readable target-language
    # text solely because it differs from the book's declared source language.
    for body in seen:
        instruction = body["messages"][0]["content"]
        assert "text already in English unchanged" in instruction
        assert "Do not classify readable English as NOT SPEECH" in instruction


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("language", "greeting", "lost_dialogue"),
    [
        ("ja", "こんにちは", "助けてくれてありがとう"),
        ("fr", "Bonjour", "Merci beaucoup pour votre aide."),
    ],
)
@pytest.mark.parametrize("refused", [False, True])
async def test_long_english_recovery_records_unchanged_or_refuses_not_speech(
    tmp_path, monkeypatch, language, greeting, lost_dialogue, refused
):
    """Exercise page gates with provider fixtures, not real-model language accuracy."""
    runner = load("translate_archive")
    legend = (
        "PB=Paperback HC=Hardcover ED=Edition TR=Translator IL=Illustrator "
        "PR=Printer YR=Year VOL=Volume"
    )
    assert len(legend) > runner.UNREADABLE_LINE_CHARS
    seen = []
    source, request, output, checkpoints = _region_job(
        monkeypatch,
        tmp_path,
        runner,
        [(greeting, "Hello")],
        [[greeting, legend, lost_dialogue]],
        _answering(
            {
                greeting: "Hello",
                legend: "NOT SPEECH" if refused else legend,
                lost_dialogue: "Thank you for helping me",
            },
            seen=seen,
        ),
    )
    request["source_language"] = language
    # _region_job defaults to a Spanish language-detector fixture; adapt its
    # source classification for these books while still classifying English
    # output separately. The real page and archive validators run unchanged.
    monkeypatch.setitem(
        sys.modules,
        "py3langid",
        SimpleNamespace(
            classify=lambda text: (
                language
                if any(
                    s.casefold() in text.casefold() for s in (greeting, lost_dialogue)
                )
                else "en",
                1,
            )
        ),
    )
    if refused:
        with pytest.raises(runner.SuspectPage, match="could not be recovered"):
            await runner.translate(source, output, request, checkpoints=checkpoints)
        assert not output.exists()
        assert not list(checkpoints.glob("00001-*.zip"))
        rejection = json.loads(
            (checkpoints / "rejected-pages.jsonl").read_text().splitlines()[-1]
        )
        assert rejection["page"] == 1
        assert any(line.startswith("PB=Paperback") for line in rejection["lines"])
    else:
        await runner.translate(source, output, request, checkpoints=checkpoints)
        page = _page_json(checkpoints)
        assert output.is_file()
        assert page["unchanged_regions"] == [legend[:60]]
        assert page["target_text"] == ["Hello", "Thank you for helping me"]
        assert page["recovered_regions"] == [
            {"source": lost_dialogue, "english": "Thank you for helping me"}
        ]
        assert page["translated"] == 2 and page["untranslated"] == 0
        assert "unreadable_regions" not in page
    # Both lost lines reach the direct recovery request, including the foreign
    # dialogue: preserving English must not silently drop its neighbour.
    direct_requests = [body["messages"][-1]["content"] for body in seen]
    assert legend in direct_requests and lost_dialogue in direct_requests


@pytest.mark.asyncio
@pytest.mark.parametrize("retained", [False, True])
@pytest.mark.parametrize(
    "english",
    [
        "PB=Paperback HC=Hardcover ED=Edition TR=Translator IL=Illustrator PR=Printer YR=Year VOL=Volume",
        "Please stay here until the others return from the station.",
    ],
)
async def test_copied_english_requires_separate_language_evidence(
    tmp_path, monkeypatch, retained, english
):
    runner = load("translate_archive")
    checked = []
    original = _answering({"Hola amigo": "Hello friend", english: english})

    def provider(request):
        body = json.loads(request.content)
        if body["messages"][0]["content"].startswith("Identify the language"):
            checked.append(body["messages"][1]["content"])
            content = json.dumps({"language": "en", "readable": True, "text": english})
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {"finish_reason": "stop", "message": {"content": content}}
                    ]
                },
            )
        return original(request)

    source, request, output, checkpoints = _region_job(
        monkeypatch,
        tmp_path,
        runner,
        HELLO + ([(english, english)] if retained else []),
        [["Hola amigo", english]],
        provider,
    )
    await runner.translate(source, output, request, checkpoints=checkpoints)
    page = _page_json(checkpoints)
    assert [json.loads(text)["text"] for text in checked] == [english]
    expected_context = ["Hola amigo"] + ([english] if retained else [])
    assert json.loads(checked[0])["page_context"] == expected_context
    assert page["preserved_english_context"] == expected_context
    assert page["preserved_english_evidence"] == [
        {
            "text": english,
            "language": "en",
            "method": "separate_provider_language_check_exact_echo",
        }
    ]
    assert page["unchanged_regions"] == [english[:60]]
    assert page["target_text"] == ["Hello friend"]
    assert page["untranslated"] == 0
    assert page["translated"] == 1  # preservation is not counted as translation
    assert output.is_file()
    # Without the separate evidence, the original strict gate still refuses.
    assert runner.region_counts(
        [SimpleNamespace(text=english, translation=english)]
    ) == (0, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "verdict",
    [
        {"language": "fr", "readable": True},
        {"language": "en", "readable": False},
        {"language": "en", "readable": "true"},
        {"language": "en", "readable": True, "text": "only part of the line"},
        {},
        "not JSON",
    ],
)
async def test_uncertain_language_evidence_cannot_release_a_copied_region(
    tmp_path, monkeypatch, verdict
):
    runner = load("translate_archive")
    text = "Cette phrase reste dans sa langue originale."
    original = _answering({"Hola amigo": "Hello friend", text: text})

    def provider(request):
        body = json.loads(request.content)
        if body["messages"][0]["content"].startswith("Identify the language"):
            content = (
                json.dumps({"text": text, **verdict})
                if isinstance(verdict, dict)
                else verdict
            )
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {"finish_reason": "stop", "message": {"content": content}}
                    ]
                },
            )
        return original(request)

    source, request, output, checkpoints = _region_job(
        monkeypatch,
        tmp_path,
        runner,
        HELLO + [(text, text)],
        [["Hola amigo", text]],
        provider,
    )
    with pytest.raises(runner.SuspectPage, match="untranslated or invalid"):
        await runner.translate(source, output, request, checkpoints=checkpoints)
    assert not output.exists()
    assert not list(checkpoints.glob("00001-*.zip"))


@pytest.mark.asyncio
async def test_english_verification_never_accepts_source_script_or_failed_answers(
    monkeypatch,
):
    runner = load("translate_archive")
    calls = []

    def provider(request):
        calls.append(json.loads(request.content))
        return httpx.Response(503, json={"error": {"type": "unavailable"}})

    monkeypatch.setattr(runner, "PROVIDER_TRANSPORT", httpx.MockTransport(provider))
    request = {
        "ai": {
            "base_url": "https://api.example.test",
            "model": "fixture",
            "api_key": "fixture",
        }
    }
    assert (
        await runner.verify_existing_english(
            request,
            ["Hello こんにちは", "Спасибо", "English sentence returned unchanged."],
        )
        == []
    )
    assert len(calls) == 1  # no provider verdict may exempt source-script text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code", ["RX.7", "B1.C2", "No.27", "No.I", "Fig. IV", "Fig.A3", "Ch.2b"]
)
async def test_copied_technical_code_requires_independent_original_context_evidence(
    tmp_path, monkeypatch, code
):
    runner = load("translate_archive")
    checked = []
    original = _answering({"Hola amigo": "Hello friend", code: code})

    def provider(request):
        body = json.loads(request.content)
        system = body["messages"][0]["content"]
        if system.startswith("Identify the language"):
            answer = {"language": "und", "readable": False, "text": code}
        elif system.startswith("Identify a non-linguistic"):
            supplied = json.loads(body["messages"][1]["content"])
            checked.append(supplied)
            answer = {"classification": "identifier", "certain": True, "text": code}
        else:
            return original(request)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": json.dumps(answer)},
                    }
                ]
            },
        )

    source, request, output, checkpoints = _region_job(
        monkeypatch,
        tmp_path,
        runner,
        HELLO + [(code, code)],
        [["Hola amigo", code]],
        provider,
    )
    await runner.translate(source, output, request, checkpoints=checkpoints)
    page = _page_json(checkpoints)
    assert checked == [{"text": code, "page_context": ["Hola amigo", code]}]
    assert page["preserved_identifier_evidence"] == [
        {
            "text": code,
            "classification": "identifier",
            "method": "separate_provider_source_context_check_exact_echo",
        }
    ]
    assert "preserved_english_evidence" not in page
    assert page["preserved_identifier_context"] == ["Hola amigo", code]
    assert page["unchanged_regions"] == [code]
    assert page["target_text"] == ["Hello friend"]
    assert page["translated"] == 1 and page["untranslated"] == 0
    assert output.is_file()
    assert runner.region_counts([SimpleNamespace(text=code, translation=code)]) == (
        0,
        1,
    )


def test_verification_context_retains_original_neighbours_of_late_table_code():
    runner = load("translate_archive")
    originals = [f"Original equipment row {i}" for i in range(90)]
    originals[70] = "RX.7"
    lines = [SimpleNamespace(text=text) for text in originals]
    context = runner.english_verification_context(lines, focus_texts=["RX.7"])
    assert context[:3] == originals[:3]
    assert all(text in context for text in originals[67:74])
    assert len(context) <= 48 and sum(map(len, context)) <= 8192
    assert all(text in originals for text in context)


def test_verification_context_never_injects_a_missing_generated_candidate():
    runner = load("translate_archive")
    lines = [SimpleNamespace(text=f"Original line {i}") for i in range(90)]
    ordinary = runner.english_verification_context(lines)
    assert runner.english_verification_context(lines, focus_texts=["RX.7"]) == ordinary
    assert "RX.7" not in ordinary


def test_verification_context_with_late_candidate_keeps_original_size_limit():
    runner = load("translate_archive")
    lines = [
        SimpleNamespace(text="Original line " + str(i) + "x" * 300) for i in range(90)
    ]
    lines[70] = SimpleNamespace(text="RX.7")
    context = runner.english_verification_context(lines, focus_texts=["RX.7"])
    assert "RX.7" in context
    assert len(context) <= 48 and sum(map(len, context)) <= 8192
    assert all(len(text) <= 256 for text in context)


def test_small_original_context_preserves_order_with_copied_text():
    runner = load("translate_archive")
    originals = ["Original heading", "RX.7", "Other original line"]
    lines = [SimpleNamespace(text=text) for text in originals]
    assert runner.english_verification_context(lines, focus_texts=["RX.7"]) == originals


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("verdict", "status", "finish"),
    [
        ({"classification": "prose", "certain": True, "text": "RX.7"}, 200, "stop"),
        ({"classification": "name", "certain": True, "text": "RX.7"}, 200, "stop"),
        (
            {"classification": "identifier", "certain": False, "text": "RX.7"},
            200,
            "stop",
        ),
        (
            {"classification": "identifier", "certain": "true", "text": "RX.7"},
            200,
            "stop",
        ),
        ({"classification": "identifier", "certain": True, "text": "RX"}, 200, "stop"),
        (
            {"classification": "identifier", "certain": True, "text": "RX.7"},
            503,
            "stop",
        ),
        (
            {"classification": "identifier", "certain": True, "text": "RX.7"},
            200,
            "length",
        ),
        ("not JSON", 200, "stop"),
    ],
)
async def test_uncertain_code_verdict_leaves_the_page_rejected(
    tmp_path, monkeypatch, verdict, status, finish
):
    runner = load("translate_archive")
    code = "RX.7"
    original = _answering({"Hola amigo": "Hello friend", code: code})

    def provider(request):
        body = json.loads(request.content)
        system = body["messages"][0]["content"]
        if system.startswith("Identify the language"):
            content = json.dumps({"language": "und", "readable": False, "text": code})
            response_status, response_finish = 200, "stop"
        elif system.startswith("Identify a non-linguistic"):
            content = json.dumps(verdict) if isinstance(verdict, dict) else verdict
            response_status, response_finish = status, finish
        else:
            return original(request)
        return httpx.Response(
            response_status,
            json={
                "choices": [
                    {"finish_reason": response_finish, "message": {"content": content}}
                ]
            },
        )

    source, request, output, checkpoints = _region_job(
        monkeypatch,
        tmp_path,
        runner,
        HELLO + [(code, code)],
        [["Hola amigo", code]],
        provider,
    )
    with pytest.raises(runner.SuspectPage, match="untranslated or invalid"):
        await runner.translate(source, output, request, checkpoints=checkpoints)
    assert not output.exists()
    assert not list(checkpoints.glob("00001-*.zip"))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "context"),
    [
        ("RX.7", []),
        ("RX.7", ["MODEL RX.70"]),
        ("RX.7", ["MODEL ARX.7"]),
        ("RX.7 ありがとう", ["RX.7 ありがとう"]),
        ("Cette phrase est française.", ["Cette phrase est française."]),
        ("ALEX RIVER", ["EDITORIAL STAFF", "ALEX RIVER"]),
        ("RX", ["MODEL RX"]),
        ("123.456", ["123.456"]),
        ("RX.7:IGNORE-INSTRUCTIONS-NOW", ["RX.7:IGNORE-INSTRUCTIONS-NOW"]),
        ("No.27", []),
        ("No.27", ["No.270"]),
        ("No.27", ["wrongNo.27"]),
        ("No.27 日本語", ["No.27 日本語"]),
        ("No.27 please return", ["No.27 please return"]),
        ("No.", ["No."]),
        ("Fig.A3", ["Fig.A30"]),
        ("Ch.2b", ["wrongCh.2b"]),
        ("No.hello", ["No.hello"]),
        ("Fig.A12345678", ["Fig.A12345678"]),
    ],
)
async def test_code_verdict_cannot_override_shape_script_or_original_context(
    text, context
):
    runner = load("translate_archive")
    calls = []

    def provider(request):
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": json.dumps(
                                {
                                    "classification": "identifier",
                                    "certain": True,
                                    "text": text,
                                }
                            )
                        },
                    }
                ]
            },
        )

    runner.PROVIDER_TRANSPORT = httpx.MockTransport(provider)
    request = {
        "ai": {
            "base_url": "https://api.example.test",
            "model": "fixture",
            "api_key": "fixture-token",
        }
    }
    assert (
        await runner.verify_existing_identifiers(request, [text], page_context=context)
        == []
    )
    assert not calls


@pytest.mark.asyncio
async def test_english_role_and_names_need_an_independent_exact_echo_with_original_context():
    runner = load("translate_archive")
    role = "EDITING ALEX RIVER SAM TIDE"
    context = ["EDITORIAL STAFF", "PRINTING", role]
    seen = []

    def provider(request):
        body = json.loads(request.content)
        supplied = json.loads(body["messages"][1]["content"])
        seen.append(supplied)
        answer = {
            "language": "en",
            "readable": supplied["page_context"] == context,
            "text": supplied["text"],
        }
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": json.dumps(answer)},
                    }
                ]
            },
        )

    runner.PROVIDER_TRANSPORT = httpx.MockTransport(provider)
    request = {
        "ai": {
            "base_url": "https://api.example.test",
            "model": "fixture",
            "api_key": "fixture-token",
        }
    }
    assert await runner.verify_existing_english(
        request, [role], page_context=context
    ) == [role]
    assert seen == [{"text": role, "page_context": context}]
    assert runner.region_counts([SimpleNamespace(text=role, translation=role)]) == (
        0,
        1,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "verdict",
    [
        {"language": "ja", "readable": True, "text": "EDITING ALEX RIVER"},
        {"language": "en", "readable": False, "text": "EDITING ALEX RIVER"},
        {"language": "en", "readable": "true", "text": "EDITING ALEX RIVER"},
        {"language": "en", "readable": True, "text": "EDITING"},
    ],
)
async def test_original_english_context_does_not_replace_an_exact_readable_language_verdict(
    verdict,
):
    runner = load("translate_archive")

    def provider(request):
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": json.dumps(verdict)},
                    }
                ]
            },
        )

    runner.PROVIDER_TRANSPORT = httpx.MockTransport(provider)
    request = {
        "ai": {
            "base_url": "https://api.example.test",
            "model": "fixture",
            "api_key": "fixture-token",
        }
    }
    assert (
        await runner.verify_existing_english(
            request, ["EDITING ALEX RIVER"], page_context=["EDITORIAL STAFF"]
        )
        == []
    )


@pytest.mark.asyncio
async def test_english_page_context_cannot_exempt_a_source_script_region_or_a_failed_request():
    runner = load("translate_archive")
    calls = []

    def provider(request):
        calls.append(json.loads(request.content))
        return httpx.Response(503, json={"error": {"type": "unavailable"}})

    runner.PROVIDER_TRANSPORT = httpx.MockTransport(provider)
    request = {
        "ai": {
            "base_url": "https://api.example.test",
            "model": "fixture",
            "api_key": "fixture-token",
        }
    }
    assert (
        await runner.verify_existing_english(
            request,
            ["Hello こんにちは", "Спасибо", "English label"],
            page_context=["EDITORIAL STAFF"],
        )
        == []
    )
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_original_ocr_context_preserves_confirmed_english_credits_without_counting_translation(
    tmp_path, monkeypatch
):
    runner = load("translate_archive")
    role, heading = "EDITING ALEX RIVER SAM TIDE", "EDITORIAL STAFF"
    context = ["Hola amigo", heading, role]
    original = _answering({"Hola amigo": "Hello friend", heading: heading, role: role})
    checked = []

    def provider(request):
        body = json.loads(request.content)
        if body["messages"][0]["content"].startswith("Identify the language"):
            supplied = json.loads(body["messages"][1]["content"])
            checked.append(supplied)
            verdict = {
                "language": "en",
                "readable": supplied["page_context"] == context,
                "text": supplied["text"],
            }
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {"content": json.dumps(verdict)},
                        }
                    ]
                },
            )
        return original(request)

    source, request, output, checkpoints = _region_job(
        monkeypatch,
        tmp_path,
        runner,
        HELLO + [(heading, heading), (role, role)],
        [["Hola amigo", heading, role]],
        provider,
    )
    await runner.translate(source, output, request, checkpoints=checkpoints)
    page = _page_json(checkpoints)
    assert output.is_file()
    assert [item["text"] for item in checked] == [heading, role]
    assert all(item["page_context"] == context for item in checked)
    assert page["preserved_english_context"] == context
    assert page["translated"] == 1 and page["untranslated"] == 0
    assert page["target_text"] == ["Hello friend"]
    assert page["unchanged_regions"] == [heading, role]


@pytest.mark.asyncio
async def test_the_reasoning_switch_goes_only_to_the_provider_that_has_it_and_answers_are_cleaned():
    runner = load("translate_archive")
    seen = []

    def provider(request):
        body = json.loads(request.content)
        seen.append(body)
        content = {
            "UNO": '"<|1|>One"',
            "DOS": "not speech.",
            "TRES": "  Three\n  more  ",
        }[body["messages"][1]["content"]]
        message = {"role": "assistant", "content": content}
        return httpx.Response(
            200, json={"choices": [{"finish_reason": "stop", "message": message}]}
        )

    runner.PROVIDER_TRANSPORT = httpx.MockTransport(provider)
    try:
        request = {
            "source_language": "es",
            "ai": {
                "base_url": "https://api.example.com/v1",
                "model": "m",
                "api_key": "k",
            },
        }
        answers = await runner.translate_lost(request, ["UNO", "DOS", "TRES"])
    finally:
        runner.PROVIDER_TRANSPORT = None
    assert "thinking" not in seen[0]
    assert answers[1] is runner.NOT_SPEECH
    assert answers[2] == "Three more"
    assert (
        answers[0] == "One"
    )  # quotes that wrap the whole answer and a numbering marker inside them are taken off


# Printed Latin names in a book of another script: independent image reading plus a separate function verdict.

LABEL = "UN Gamaschane Host AF LeLe-ist-Hotos.Rei Rei Sha"
LABEL_LINES = ["UN Gamaschane Host AF", "LeLe-ist-Hotos.Rei Rei Sha"]
JAPANESE = ("日本語のせりふです", "These are Japanese lines.")


def _verdict_provider(label, verdict, status=200, finish="stop", calls=None):
    original = _answering({JAPANESE[0]: JAPANESE[1], label: label})

    def provider(request):
        body = json.loads(request.content)
        system = body["messages"][0]["content"]
        if system.startswith("Identify the language"):
            content, code, end = (
                json.dumps({"language": "und", "readable": False, "text": label}),
                200,
                "stop",
            )
        elif system.startswith("Identify a non-linguistic"):
            content, code, end = (
                json.dumps({"classification": "name", "certain": True, "text": label}),
                200,
                "stop",
            )
        elif system.startswith("Classify the function"):
            if calls is not None:
                calls.append(json.loads(body["messages"][1]["content"]))
            content = json.dumps(verdict) if isinstance(verdict, dict) else verdict
            code, end = status, finish
        else:
            return original(request)
        return httpx.Response(
            code,
            json={"choices": [{"finish_reason": end, "message": {"content": content}}]},
        )

    return provider


def _name_job(
    monkeypatch, tmp_path, runner, label, lines, provider, reading, readings=None
):
    """One page of a Japanese book whose engine kept a Japanese balloon and a copied Latin label."""
    source, request = _archive(tmp_path, runner, 1, language="ja")
    _fake_engine(
        monkeypatch,
        [
            lambda image: SimpleNamespace(
                result=image,
                textlines=[SimpleNamespace(text=t) for t in [JAPANESE[0], *lines]],
                text_regions=[
                    SimpleNamespace(text=JAPANESE[0], translation=JAPANESE[1]),
                    SimpleNamespace(text=label, translation=label),
                ],
            )
        ],
        provider,
        requests={1: [[JAPANESE[0], label]]},
    )
    monkeypatch.setitem(
        sys.modules,
        "py3langid",
        SimpleNamespace(classify=lambda text: ("ja" if "日本語" in text else "en", 1)),
    )
    monkeypatch.setattr(runner, "provider_unavailable_reason", lambda _request: None)
    monkeypatch.setattr(runner, "PROVIDER_TRANSPORT", httpx.MockTransport(provider))

    async def second_reading(_image, source_lines, _device="cpu"):
        if readings is not None:
            readings.append([line.text for line in source_lines])
        return reading

    monkeypatch.setattr(runner, "independent_latin_reading", second_reading)
    return source, request, tmp_path / "result.cbz", tmp_path / "checkpoints"


@pytest.mark.asyncio
@pytest.mark.parametrize("function", ["name_label", "credit_list"])
async def test_printed_latin_name_label_is_kept_with_image_reading_and_function_evidence(
    tmp_path, monkeypatch, function
):
    runner = load("translate_archive")
    calls, readings = [], []
    verdict = {"function": function, "certain": True, "text": LABEL}
    source, request, output, checkpoints = _name_job(
        monkeypatch,
        tmp_path,
        runner,
        LABEL,
        LABEL_LINES,
        _verdict_provider(LABEL, verdict, calls=calls),
        'UN Gamaschane Host AF "LeLe-ist-Hotos" Rei Rei Sha',
        readings,
    )
    await runner.translate(source, output, request, checkpoints=checkpoints)
    page = _page_json(checkpoints)
    assert readings == [LABEL_LINES]
    assert calls == [{"text": LABEL, "page_context": [JAPANESE[0], *LABEL_LINES]}]
    assert page["preserved_original_name_evidence"] == [
        {
            "text": LABEL,
            "second_reading": 'UN Gamaschane Host AF "LeLe-ist-Hotos" Rei Rei Sha',
            "second_reading_method": "page",
            "same_caption_original": [],
            "function": function,
            "method": "independent_latin_ocr_reread_and_separate_provider_function_check_exact_echo",
        }
    ]
    assert "preserved_english_evidence" not in page
    assert "preserved_identifier_evidence" not in page
    assert page["unchanged_regions"] == [LABEL]
    assert page["target_text"] == [JAPANESE[1]]
    assert page["translated"] == 1 and page["untranslated"] == 0
    assert output.is_file()


@pytest.mark.asyncio
async def test_a_clipped_credit_line_end_is_corrected_only_by_the_second_reading(
    tmp_path, monkeypatch
):
    runner = load("translate_archive")
    credit = "YOSHIYUKITABATA CHIEKO YASUM"
    verdict = {"function": "credit_list", "certain": True, "text": credit}
    source, request, output, checkpoints = _name_job(
        monkeypatch,
        tmp_path,
        runner,
        credit,
        ["YOSHIYUKITABATA", "CHIEKO YASUM"],
        _verdict_provider(credit, verdict),
        "YOSHIYUKI TABATA CHIEKO YASUMI",
    )
    await runner.translate(source, output, request, checkpoints=checkpoints)
    evidence = _page_json(checkpoints)["preserved_original_name_evidence"]
    assert evidence[0]["text"] == credit
    assert evidence[0]["second_reading"] == "YOSHIYUKI TABATA CHIEKO YASUMI"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("verdict", "status", "finish", "reading"),
    [
        ({"function": "dialogue", "certain": True, "text": LABEL}, 200, "stop", LABEL),
        ({"function": "prose", "certain": True, "text": LABEL}, 200, "stop", LABEL),
        ({"function": "other", "certain": True, "text": LABEL}, 200, "stop", LABEL),
        (
            {"function": "uncertain", "certain": False, "text": LABEL},
            200,
            "stop",
            LABEL,
        ),
        (
            {"function": "name_label", "certain": False, "text": LABEL},
            200,
            "stop",
            LABEL,
        ),
        (
            {"function": "name_label", "certain": "true", "text": LABEL},
            200,
            "stop",
            LABEL,
        ),
        (
            {"function": "name_label", "certain": True, "text": "UN Gamaschane"},
            200,
            "stop",
            LABEL,
        ),
        (
            {"function": "name_label", "certain": True, "text": LABEL},
            503,
            "stop",
            LABEL,
        ),
        (
            {"function": "name_label", "certain": True, "text": LABEL},
            200,
            "length",
            LABEL,
        ),
        ("not JSON", 200, "stop", LABEL),
        ({"function": "name_label", "certain": True, "text": LABEL}, 200, "stop", None),
        (
            {"function": "name_label", "certain": True, "text": LABEL},
            200,
            "stop",
            "UN Gamaschane Host AF Lele Rei",
        ),
        (
            {"function": "name_label", "certain": True, "text": LABEL},
            200,
            "stop",
            LABEL + " Extra words",
        ),
    ],
)
async def test_uncertain_function_or_disagreeing_reading_leaves_the_name_page_rejected(
    tmp_path, monkeypatch, verdict, status, finish, reading
):
    runner = load("translate_archive")
    source, request, output, checkpoints = _name_job(
        monkeypatch,
        tmp_path,
        runner,
        LABEL,
        LABEL_LINES,
        _verdict_provider(LABEL, verdict, status, finish),
        reading,
    )
    with pytest.raises(runner.SuspectPage, match="untranslated or invalid"):
        await runner.translate(source, output, request, checkpoints=checkpoints)
    assert not output.exists()
    assert not list(checkpoints.glob("00001-*.zip"))


@pytest.mark.asyncio
async def test_a_label_the_original_lines_do_not_make_up_is_never_read_again(
    tmp_path, monkeypatch
):
    runner = load("translate_archive")
    readings = []
    verdict = {"function": "name_label", "certain": True, "text": LABEL}
    source, request, output, checkpoints = _name_job(
        monkeypatch,
        tmp_path,
        runner,
        LABEL,
        ["UN Gamaschane Host AF"],  # the rest of the label is not in the original OCR
        _verdict_provider(LABEL, verdict),
        LABEL,
        readings,
    )
    with pytest.raises(runner.SuspectPage, match="untranslated or invalid"):
        await runner.translate(source, output, request, checkpoints=checkpoints)
    assert readings == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "language"),
    [
        ("Watashi wa Rei desu", "ja"),
        ("Kimi no koto ga suki da yo", "ja"),
        ("Arigatou gozaimasu", "ja"),
        ("Ikimasu", "ja"),
        ("I will protect you.", "ja"),
        ("Where are you going?", "ja"),
        ("Help me!", "ja"),
        ("Zura", "ja"),
        ("Rei 令令謝", "ja"),
        ("Nao Lindo Reithle", "es"),
        ("Nao Lindo Reithle", "fr"),
        ("Nao Lindo Reithle", None),
    ],
)
async def test_romanized_speech_prose_single_words_and_latin_books_never_reach_the_name_check(
    monkeypatch, text, language
):
    runner = load("translate_archive")
    calls, readings = [], []

    def provider(request):
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": json.dumps(
                                {
                                    "function": "name_label",
                                    "certain": True,
                                    "text": text,
                                }
                            )
                        },
                    }
                ]
            },
        )

    async def second_reading(_image, lines, _device="cpu"):
        readings.append(lines)
        return text

    monkeypatch.setattr(runner, "PROVIDER_TRANSPORT", httpx.MockTransport(provider))
    monkeypatch.setattr(runner, "independent_latin_reading", second_reading)
    request = {
        "source_language": language,
        "ai": {
            "base_url": "https://api.example.test",
            "model": "fixture",
            "api_key": "fixture-token",
        },
    }
    assert (
        await runner.verify_original_names(
            request,
            [text],
            lines=[SimpleNamespace(text=text)],
            image=None,
            page_context=[text],
        )
        == []
    )
    assert not calls and not readings


def test_original_lines_must_tile_the_region_exactly():
    runner = load("translate_archive")
    lines = [
        SimpleNamespace(text="MITSUHIRO SATO (ANIMO CARAMEL)"),
        SimpleNamespace(text="THANKS"),
        SimpleNamespace(text="KENJIMURASHITA"),
    ]
    found = runner.name_source_lines(
        lines, "KENJIMURASHITA MITSUHIRO SATO ANIMO CARAMEL"
    )
    assert [line.text for line in found] == [
        "KENJIMURASHITA",
        "MITSUHIRO SATO (ANIMO CARAMEL)",
    ]
    assert runner.name_source_lines(lines, "KENJIMURASHITA MITSUHIRO") == []
    assert runner.name_source_lines(lines, "THANKS THANKS") == []
    assert runner.name_source_lines([], "THANKS") == []


def test_a_second_reading_confirms_but_never_rewrites_a_name():
    runner = load("translate_archive")
    assert (
        runner.confirmed_reading("KENJI MURASHITA", "KENJIMURASHITA")
        == "KENJIMURASHITA"
    )
    assert (
        runner.confirmed_reading("YOSHIYUKITABATA YASUM", "YOSHIYUKI TABATA YASUMI")
        == "YOSHIYUKI TABATA YASUMI"
    )
    assert (
        runner.confirmed_reading("SHORT", "SHORTER") is None
    )  # too short to call a clipped end
    assert (
        runner.confirmed_reading("YOSHIYUKITABATA YASUM", "YOSHIYUKI TABATA YASUMIKO")
        is None
    )
    assert (
        runner.confirmed_reading("YOSHIYUKITABATA YASUM", "YOSHIYUKI TABATA YASUN")
        is None
    )
    assert runner.confirmed_reading("ABC DEF", None) is None
    assert runner.confirmed_reading("ABC DEF", "") is None


@pytest.mark.asyncio
async def test_an_engine_without_the_second_ocr_model_proves_nothing(monkeypatch):
    runner = load("translate_archive")
    calls = []
    monkeypatch.setattr(
        runner,
        "PROVIDER_TRANSPORT",
        httpx.MockTransport(
            lambda request: calls.append(request) or httpx.Response(500)
        ),
    )
    monkeypatch.delitem(sys.modules, "manga_translator", raising=False)
    monkeypatch.setitem(sys.modules, "manga_translator.ocr", None)
    request = {
        "source_language": "ja",
        "ai": {
            "base_url": "https://api.example.test",
            "model": "fixture",
            "api_key": "fixture-token",
        },
    }
    assert (
        await runner.verify_original_names(
            request,
            [LABEL],
            lines=[SimpleNamespace(text=t) for t in LABEL_LINES],
            image=Image.new("RGB", (8, 8)),
            page_context=LABEL_LINES,
        )
        == []
    )
    assert not calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("verdict", "reading", "lines", "step", "detail"),
    [
        (
            {"function": "uncertain", "certain": False, "text": LABEL},
            LABEL,
            LABEL_LINES,
            "function",
            "uncertain certain=False echo=True",
        ),
        (
            {"function": "name_label", "certain": True, "text": LABEL},
            "UN Gamaschane Host",
            LABEL_LINES,
            "second_reading",
            "page=UN Gamaschane Host",
        ),
        (
            {"function": "name_label", "certain": True, "text": LABEL},
            None,
            LABEL_LINES,
            "second_reading",
            "page=none",
        ),
        (
            {"function": "name_label", "certain": True, "text": LABEL},
            LABEL,
            ["UN Gamaschane Host AF"],
            "original_lines",
            "",
        ),
    ],
)
async def test_the_rejection_record_says_which_name_check_refused_the_label(
    tmp_path, monkeypatch, verdict, reading, lines, step, detail
):
    runner = load("translate_archive")
    source, request, output, checkpoints = _name_job(
        monkeypatch,
        tmp_path,
        runner,
        LABEL,
        lines,
        _verdict_provider(LABEL, verdict),
        reading,
    )
    with pytest.raises(runner.SuspectPage):
        await runner.translate(source, output, request, checkpoints=checkpoints)
    records = [
        json.loads(line)
        for line in (checkpoints / "rejected-pages.jsonl").read_text().splitlines()
    ]
    checks = [c for r in records for c in r.get("original_name_checks", [])]
    assert {
        "text": runner.redacted_excerpt(LABEL),
        "step": step,
        "detail": detail,
    } in checks
    assert all(
        "fixture" not in json.dumps(r) and "secret" not in json.dumps(r)
        for r in records
    )


class _Quad:
    """The engine's text line box as far as the second reading uses it: corner points, text and probability."""

    def __init__(self, pts, text, prob):
        self.pts, self.text, self.prob = [list(map(float, p)) for p in pts], text, prob


def _band_page(light_on_dark):
    page = Image.new("RGB", (200, 120), (255, 255, 255))
    band = (60, 60, 60) if light_on_dark else (250, 250, 250)
    page.paste(Image.new("RGB", (120, 40), band), (40, 50))
    return page


BAND_LINES = [_Quad([[50, 55], [150, 55], [150, 70], [50, 70]], "x", 0.9)]


def test_reading_crops_take_original_pixels_inverted_only_on_a_dark_band():
    runner = load("translate_archive")
    dark = runner.reading_crops(_band_page(True), BAND_LINES, enlarge=False)
    assert [name for name, _, _ in dark] == ["crop", "crop_inverted"]
    name, picture, boxes = dark[0]
    assert picture.size == (113, 28)  # box 50..150 x 55..70 with a 6 pixel margin
    assert boxes[0].pts == [[6.0, 6.0], [106.0, 6.0], [106.0, 21.0], [6.0, 21.0]]
    assert (
        boxes[0].text == "" and BAND_LINES[0].text == "x"
    )  # the original box is never changed
    light = runner.reading_crops(_band_page(False), BAND_LINES, enlarge=False)
    assert [name for name, _, _ in light] == ["crop"]
    enlarged = runner.reading_crops(_band_page(True), BAND_LINES, enlarge=True)
    assert [name for name, _, _ in enlarged] == [
        "crop",
        "crop_inverted",
        "crop_enlarged",
        "crop_inverted_enlarged",
    ]
    assert enlarged[2][1].size == (226, 56)
    assert enlarged[2][2][0].pts[2] == [212.0, 42.0]
    assert (
        runner.reading_crops(
            _band_page(True), [SimpleNamespace(text="x")], enlarge=True
        )
        == []
    )


def _scripted_reader(monkeypatch, runner, answers, seen):
    answers = list(answers)

    async def reader(picture, boxes, _device="cpu"):
        seen.append(picture.size)
        return answers.pop(0) if answers else None

    monkeypatch.setattr(runner, "independent_latin_reading", reader)


@pytest.mark.asyncio
async def test_a_dark_band_label_is_confirmed_by_the_inverted_crop_without_enlargement(
    monkeypatch,
):
    runner = load("translate_archive")
    seen, attempts = [], []
    _scripted_reader(monkeypatch, runner, [None, None, "UN HOST AF REI SHA"], seen)
    method, reading = await runner.second_reading(
        "UN HOST AF REI SHA", _band_page(True), BAND_LINES, attempts=attempts
    )
    assert (method, reading) == ("crop_inverted", "UN HOST AF REI SHA")
    assert seen == [(200, 120), (113, 28), (113, 28)]
    assert attempts == [
        ("page", None),
        ("crop", None),
        ("crop_inverted", "UN HOST AF REI SHA"),
    ]


@pytest.mark.asyncio
async def test_enlargement_is_tried_only_after_a_reading_that_disagreed(monkeypatch):
    runner = load("translate_archive")
    text = "KENJIMURASHITA MITSUHIRO SATO"
    seen, attempts = [], []
    _scripted_reader(
        monkeypatch,
        runner,
        ["KENJLMUBASHTA MITSUHIRO SATO", None, "KENJI MURASHITA MITSUHIRO SATO"],
        seen,
    )
    method, reading = await runner.second_reading(
        text, _band_page(False), BAND_LINES, attempts=attempts
    )
    assert (method, reading) == ("crop_enlarged", "KENJI MURASHITA MITSUHIRO SATO")
    assert seen == [(200, 120), (113, 28), (226, 56)]

    seen.clear()
    _scripted_reader(monkeypatch, runner, [None, None], seen)
    assert await runner.second_reading(text, _band_page(False), BAND_LINES) == (
        None,
        None,
    )
    assert seen == [(200, 120), (113, 28)]  # nothing disagreed: no enlarged reading


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "enlarged",
    [
        "KENJLMUBASHTA MITSUHIRO SATO",
        "KENJIMURASHITA MITSUHIRO SATOXYZ",
        "KENJIMURASHITA MITSUHIRO",
        "",
    ],
)
async def test_every_variant_must_agree_under_the_same_rule(monkeypatch, enlarged):
    runner = load("translate_archive")
    text = "KENJIMURASHITA MITSUHIRO SATO"
    seen, attempts = [], []
    _scripted_reader(
        monkeypatch,
        runner,
        [
            "KENJLMUBASHTA MITSUHIRO SATO",
            "KENJLMUBASHTA MITSUHIRO SATO",
            enlarged or None,
        ],
        seen,
    )
    assert await runner.second_reading(
        text, _band_page(False), BAND_LINES, attempts=attempts
    ) == (None, None)
    assert [name for name, _ in attempts] == ["page", "crop", "crop_enlarged"]


# An optional local text reader: another independent reading of the original crops, never a verdict.


def _reader_script(tmp_path, body):
    script = tmp_path / "reader"
    script.write_text("#!/bin/sh\n" + body + "\n")
    script.chmod(0o755)
    return str(script)


def test_reader_output_is_read_top_to_bottom_and_refused_when_unsure_or_malformed():
    runner = load("translate_archive")
    two = "0.1\t0.2\t0.5\t0.1\t0.99\tMITSUHIRO SATO\n0.1\t0.6\t0.5\t0.1\t1.0\tKENJI MURASHITA\n"
    assert runner.reader_text(two) == "KENJI MURASHITA MITSUHIRO SATO"
    assert runner.reader_text(two.replace("0.99", "0.3")) is None
    assert runner.reader_text("0.1\t0.2\t0.5\t0.1\tKENJI\n") is None
    assert runner.reader_text("0.1\t0.2\t0.5\t0.1\tnan\tKENJI\n") is None
    assert runner.reader_text("0.1\t0.2\t0.5\t0.1\t1.0\t \n") is None
    assert runner.reader_text("") is None


def test_only_an_executable_file_is_a_reader(tmp_path):
    runner = load("translate_archive")
    plain = tmp_path / "plain"
    plain.write_text("x")
    assert runner.usable_reader(str(plain)) is None
    assert runner.usable_reader(str(tmp_path / "missing")) is None
    assert runner.usable_reader(None) is None and runner.usable_reader(7) is None
    script = _reader_script(tmp_path, "exit 0")
    assert runner.usable_reader(script) == script


@pytest.mark.asyncio
async def test_the_reader_process_answers_or_proves_nothing(tmp_path, monkeypatch):
    runner = load("translate_archive")
    picture = Image.new("RGB", (20, 10))
    good = _reader_script(
        tmp_path, "printf '0.1\\t0.5\\t0.8\\t0.2\\t1.0\\tKENJI MURASHITA\\n'"
    )
    assert await runner.external_latin_reading(good, picture) == "KENJI MURASHITA"
    failing = _reader_script(tmp_path, "echo boom; exit 3")
    assert await runner.external_latin_reading(failing, picture) is None
    slow = _reader_script(tmp_path, "sleep 5")
    monkeypatch.setattr(runner, "READER_TIMEOUT", 0.2)
    assert await runner.external_latin_reading(slow, picture) is None


@pytest.mark.asyncio
async def test_the_reader_is_asked_only_after_the_engine_model_and_under_the_same_rule(
    monkeypatch, tmp_path
):
    runner = load("translate_archive")
    text = "KENJIMURASHITA MITSUHIRO SATO"
    seen, asked, attempts = [], [], []
    _scripted_reader(monkeypatch, runner, ["KENJLMUBASHTA MITSUHIRO SATO"] * 4, seen)
    answers = ["KENJIMUASHITA MITSUHIRO SATO", "KENJI MURASHITA MITSUHIRO SATO"]

    async def external(reader, picture):
        asked.append((reader, picture.size))
        return answers.pop(0)

    monkeypatch.setattr(runner, "external_latin_reading", external)
    method, reading = await runner.second_reading(
        text, _band_page(False), BAND_LINES, attempts=attempts, reader="/tool"
    )
    assert (method, reading) == (
        "reader_crop_inverted",
        "KENJI MURASHITA MITSUHIRO SATO",
    )
    assert [name for name, _ in attempts] == [
        "page",
        "crop",
        "crop_enlarged",
        "reader_crop",
        "reader_crop_inverted",
    ]
    assert asked == [("/tool", (113, 28)), ("/tool", (113, 28))]

    seen.clear()
    asked.clear()
    attempts.clear()
    _scripted_reader(monkeypatch, runner, [None, None], seen)
    answers[:] = ["KENJLMUBASHTA MITSUHIRO SATO"] * 4
    assert await runner.second_reading(
        text, _band_page(False), BAND_LINES, attempts=attempts, reader="/tool"
    ) == (None, None)
    assert [name for name, _ in attempts][2:] == [
        "reader_crop",
        "reader_crop_inverted",
        "reader_crop_enlarged",
        "reader_crop_inverted_enlarged",
    ]
    assert await runner.second_reading(text, _band_page(False), BAND_LINES) == (
        None,
        None,
    )


@pytest.mark.asyncio
async def test_a_configured_reader_never_changes_the_engine_configuration_or_checkpoint_identity(
    tmp_path, monkeypatch
):
    runner = load("translate_archive")
    reader = _reader_script(tmp_path, "exit 1")
    identities, configs = [], []
    for name, config in (
        ("run-plain", {}),
        ("run-reader", {"latin_text_reader": reader}),
    ):
        directory = tmp_path / name
        directory.mkdir()
        source, request, output, checkpoints = _name_job(
            monkeypatch,
            directory,
            runner,
            LABEL,
            LABEL_LINES,
            _verdict_provider(
                LABEL, {"function": "name_label", "certain": True, "text": LABEL}
            ),
            LABEL,
        )
        monkeypatch.setitem(
            sys.modules,
            "manga_translator.config",
            SimpleNamespace(Config=lambda **kw: configs.append(kw) or kw),
        )
        await runner.translate(
            source, output, request, config=dict(config), checkpoints=checkpoints
        )
        identities.append(_page_json(checkpoints)["identity"])
    assert identities[0] == identities[1]
    assert all("latin_text_reader" not in kw for kw in configs)


@pytest.mark.asyncio
async def test_the_function_question_covers_composite_caption_labels_and_still_refuses_narrative(
    tmp_path, monkeypatch
):
    runner = load("translate_archive")
    systems = {}
    inner = _verdict_provider(
        LABEL, {"function": "name_label", "certain": True, "text": LABEL}
    )

    def provider(request):
        system = json.loads(request.content)["messages"][0]["content"]
        systems.setdefault(system[:40], system)
        return inner(request)

    source, request, output, checkpoints = _name_job(
        monkeypatch, tmp_path, runner, LABEL, LABEL_LINES, provider, LABEL
    )
    await runner.translate(source, output, request, checkpoints=checkpoints)
    question = next(
        s for s in systems.values() if s.startswith("Classify the function")
    )
    for wording in (
        "non-narrative caption label",
        "organisation, role, rank, title or class designation",
        "prose for sentences, narration or descriptions",
        "romanized or English speech",
        "certain to true only for an unambiguous name_label",
    ):
        assert wording in question
    english = next(s for s in systems.values() if s.startswith("Identify the language"))
    assert "caption label" not in english and "rank" not in english
    assert (
        _page_json(checkpoints)["preserved_original_name_evidence"][0]["function"]
        == "name_label"
    )


# A character who says some writing cannot be read: its faithful translation reads like the model's note.
SPEECH_SOURCE = "だっだめデスッ うちのマスター ムズカシイ字 読めませんっ!!"
SPEECH_ENGLISH = "No good! My master's difficult characters can't be read!!"


def test_a_cannot_read_line_is_speech_only_with_evidence_in_the_original_region():
    runner = load("translate_archive")
    assert runner.is_meta_answer(SPEECH_ENGLISH)  # no source: never excused
    assert runner.is_meta_answer(
        SPEECH_ENGLISH, "ムズカシイ字"
    )  # the original does not say it
    assert runner.is_meta_answer(SPEECH_ENGLISH, "ﾑｽﾞｶｼｲ")
    for source in (
        SPEECH_SOURCE,
        "この字は読めない",
        "这些字我看不懂",
        "이 글자는 읽을 수 없어",
        "I can't read this!",
    ):
        assert not runner.is_meta_answer(SPEECH_ENGLISH, source), source
    # The model's own notes stay notes even beside a line that says it cannot read.
    for note in (
        NOTE,
        "The text appears to be gibberish.",
        "I cannot translate this text.",
        "The original characters cannot be read (OCR error).",
        "Unable to translate: the line is garbled.",
        "The source text cannot be read.",
        "\u3010unreadable\u3011",
    ):
        assert runner.is_meta_answer(note, SPEECH_SOURCE), note
    assert not runner.is_meta_answer("Good morning", SPEECH_SOURCE)


def _speech_job(monkeypatch, tmp_path, runner, source_text, english):
    source, request = _archive(tmp_path, runner, 1, language="ja")
    _fake_engine(
        monkeypatch,
        [
            lambda image: SimpleNamespace(
                result=image,
                textlines=[
                    SimpleNamespace(text=JAPANESE[0]),
                    SimpleNamespace(text=source_text),
                ],
                text_regions=[
                    SimpleNamespace(text=JAPANESE[0], translation=JAPANESE[1]),
                    SimpleNamespace(text=source_text, translation=english),
                ],
            )
        ],
    )
    monkeypatch.setitem(
        sys.modules,
        "py3langid",
        SimpleNamespace(classify=lambda text: ("ja" if "日本語" in text else "en", 1)),
    )
    monkeypatch.setattr(runner, "provider_unavailable_reason", lambda _request: None)
    return source, request, tmp_path / "result.cbz", tmp_path / "checkpoints"


@pytest.mark.asyncio
async def test_speech_about_unreadable_writing_is_kept_and_its_checkpoint_reused(
    tmp_path, monkeypatch
):
    runner = load("translate_archive")
    source, request, output, checkpoints = _speech_job(
        monkeypatch, tmp_path, runner, SPEECH_SOURCE, SPEECH_ENGLISH
    )
    await runner.translate(source, output, request, checkpoints=checkpoints)
    page = _page_json(checkpoints)
    assert (
        SPEECH_ENGLISH in page["target_text"] and SPEECH_SOURCE in page["source_text"]
    )
    assert page["untranslated"] == 0
    identity = page["identity"]
    with Image.open(io.BytesIO(zipfile.ZipFile(source).read("1.png"))) as image:
        image.load()
        assert runner.load_checkpoint(checkpoints, 1, identity, image.size) is not None


@pytest.mark.asyncio
async def test_the_same_english_without_evidence_in_the_original_still_stops_the_page(
    tmp_path, monkeypatch
):
    runner = load("translate_archive")
    noise = "ﾑｽﾞｶｼｲ字ﾏｽﾀｰ" * 4
    source, request, output, checkpoints = _speech_job(
        monkeypatch, tmp_path, runner, noise, SPEECH_ENGLISH
    )
    with pytest.raises(runner.SuspectPage, match="could not read"):
        await runner.translate(source, output, request, checkpoints=checkpoints)
    assert not list(checkpoints.glob("00001-*.zip"))


# Original text of the same caption, read from the whole page in the source language, for the function question.


def test_only_confident_reader_lines_are_original_caption_evidence():
    runner = load("translate_archive")
    output = (
        "0.1\t0.6\t0.3\t0.02\t0.5\tAF レレイスホト（令令謝）\n"
        "0.1\t0.5\t0.3\t0.02\t0.49\tウソのグロス\n"
        "0.1\t0.4\t0.3\t0.02\t0.3\t低信頼\n"
    )
    observed = runner.reader_observations(output, 1000, 1000)
    assert [o["text"] for o in observed] == ["AF レレイスホト（令令謝）"]
    assert observed[0]["box"] == pytest.approx((100, 380, 400, 400))
    assert runner.reader_observations("0.1\t0.6\t0.3\tnope\t1\tX\n", 10, 10) is None
    assert runner.reader_observations("0.1\t0.6\tX\n", 10, 10) is None


def test_same_caption_lines_are_the_original_text_next_to_the_region_only():
    runner = load("translate_archive")
    lines = [_Quad([[100, 100], [500, 100], [500, 140], [100, 140]], "", 1.0)]
    observations = [
        {"text": "UN HOST AF", "box": (100, 100, 500, 140)},  # the region itself
        {
            "text": "AF レレイスホト",
            "box": (100, 145, 300, 175),
        },  # printed just under it
        {"text": "遠くの台詞", "box": (100, 400, 300, 430)},  # beyond one block height
        {"text": "横の台詞", "box": (600, 145, 800, 175)},  # no horizontal overlap
    ]
    assert runner.same_caption_lines(observations, lines, "UN HOST AF") == [
        "AF レレイスホト"
    ]
    assert (
        runner.same_caption_lines(
            observations, [SimpleNamespace(text="x")], "UN HOST AF"
        )
        == []
    )


@pytest.mark.asyncio
async def test_page_reading_asks_for_the_source_language_and_proves_nothing_without_it(
    tmp_path,
):
    runner = load("translate_archive")
    seen = tmp_path / "args"
    reader = _reader_script(
        tmp_path,
        f'echo "$2" > {seen}; printf "0.1\\t0.6\\t0.3\\t0.02\\t0.5\\tAF レレイスホト\\n"',
    )
    page = Image.new("RGB", (100, 100))
    lines = await runner.page_source_reading(reader, page, "ja")
    assert [o["text"] for o in lines] == ["AF レレイスホト"]
    assert seen.read_text().strip() == "ja-JP,en-US"
    assert await runner.page_source_reading(reader, page, "es") == []
    assert await runner.page_source_reading(None, page, "ja") == []
    failing = _reader_script(tmp_path, "exit 2")
    assert await runner.page_source_reading(failing, page, "ja") == []


def _caption_job(monkeypatch, tmp_path, runner, reader_output, verdict, calls):
    reader = _reader_script(tmp_path, "cat <<'OUT'\n" + reader_output + "OUT")
    lines = [
        _Quad([[100, 100], [500, 100], [500, 140], [100, 140]], LABEL_LINES[0], 1.0),
        _Quad([[100, 142], [520, 142], [520, 180], [100, 180]], LABEL_LINES[1], 1.0),
    ]
    source, request = _archive(tmp_path, runner, 1, language="ja")
    _fake_engine(
        monkeypatch,
        [
            lambda image: SimpleNamespace(
                result=image,
                textlines=[SimpleNamespace(text=JAPANESE[0]), *lines],
                text_regions=[
                    SimpleNamespace(text=JAPANESE[0], translation=JAPANESE[1]),
                    SimpleNamespace(text=LABEL, translation=LABEL),
                ],
            )
        ],
        _verdict_provider(LABEL, verdict, calls=calls),
        requests={1: [[JAPANESE[0], LABEL]]},
    )
    monkeypatch.setitem(
        sys.modules,
        "py3langid",
        SimpleNamespace(classify=lambda text: ("ja" if "日本語" in text else "en", 1)),
    )
    monkeypatch.setattr(runner, "provider_unavailable_reason", lambda _request: None)
    monkeypatch.setattr(
        runner,
        "PROVIDER_TRANSPORT",
        httpx.MockTransport(_verdict_provider(LABEL, verdict, calls=calls)),
    )

    async def agreeing(_image, _lines, _device="cpu"):
        return LABEL

    monkeypatch.setattr(runner, "independent_latin_reading", agreeing)
    return (
        source,
        request,
        tmp_path / "result.cbz",
        tmp_path / "checkpoints",
        {"latin_text_reader": reader},
    )


def _band(y_top, y_bottom, text, confidence, height=48):
    # the fake page is 32x48 pixels: express pixel rows as Vision fractions from the bottom
    return f"0.0\t{1 - y_bottom / height:.6f}\t1.0\t{(y_bottom - y_top) / height:.6f}\t{confidence}\t{text}\n"


@pytest.mark.asyncio
async def test_a_confident_same_caption_gloss_reaches_the_function_question_and_certainty_still_decides(
    tmp_path, monkeypatch
):
    runner = load("translate_archive")
    calls = []
    verdict = {"function": "name_label", "certain": True, "text": LABEL}
    # the label boxes above are in engine pixels of the page; scale the fake page boxes the same way
    source, request, output, checkpoints, config = _caption_job(
        monkeypatch,
        tmp_path,
        runner,
        "0.0\t0.0\t1.0\t1.0\t0.5\tAF レレイスホト（令令謝）\n",
        verdict,
        calls,
    )
    monkeypatch.setattr(
        runner,
        "reader_observations",
        lambda output, w, h: (
            [{"text": "AF レレイスホト（令令謝）", "box": (100, 182, 400, 210)}]
            if "0.5" in output
            else []
        ),
    )
    await runner.translate(
        source, output, request, config=config, checkpoints=checkpoints
    )
    assert calls[0]["same_caption_original"] == ["AF レレイスホト（令令謝）"]
    evidence = _page_json(checkpoints)["preserved_original_name_evidence"][0]
    assert evidence["same_caption_original"] == ["AF レレイスホト（令令謝）"]


@pytest.mark.asyncio
@pytest.mark.parametrize("confidence", ["0.49", "0.3"])
async def test_a_gloss_below_the_reader_confidence_never_reaches_the_question(
    tmp_path, monkeypatch, confidence
):
    runner = load("translate_archive")
    calls = []
    verdict = {"function": "name_label", "certain": False, "text": LABEL}
    source, request, output, checkpoints, config = _caption_job(
        monkeypatch,
        tmp_path,
        runner,
        f"0.0\t0.0\t1.0\t1.0\t{confidence}\tAF レレイスホト（令令謝）\n",
        verdict,
        calls,
    )
    with pytest.raises(runner.SuspectPage, match="untranslated or invalid"):
        await runner.translate(
            source, output, request, config=config, checkpoints=checkpoints
        )
    assert calls and all("same_caption_original" not in call for call in calls)


# The language check of a book whose OCR lost its diacritics: an independent reading of every original page.

VI_MARKS = set("ăâđêôơưạảấầẩẫậắằẳẵặẹẻẽếềểễệỉịọỏốồổỗộớờởỡợụủứừửữựỳỵỷỹáàãéèíìóòõúùý")
VI_OCR = [
    ["Khong co gi dau", "Cam on nha chang long bong"],
    ["Toi rat lay lam hanh phuc"],
    ["Dia than nay hiem lam"],
]
VI_READ = [
    ["Không có gì đâu", "Cảm ơn nha chàng lông bông"],
    ["Tôi rất lấy làm hạnh phúc"],
    ["Đĩa than này hiếm lắm"],
]
VI_ENGLISH = {
    "Khong co gi dau": "It is nothing",
    "Cam on nha chang long bong": "Thanks, you drifter",
    "Toi rat lay lam hanh phuc": "I am very happy",
    "Dia than nay hiem lam": "This vinyl is very rare",
}


def _vi_classify(text):
    lines = [line for line in text.split("\n") if line.strip()] or [text]
    marked = sum(1 for line in lines if VI_MARKS & set(line.casefold()))
    if marked * 2 > len(lines):
        return ("vi", 1)
    return ("es" if "hola" in text else "en", 1)


def _vi_job(
    monkeypatch,
    tmp_path,
    runner,
    reading,
    ocr=VI_OCR,
    english=VI_ENGLISH,
    reader=True,
    calls=None,
):
    """A Vietnamese book whose engine OCR has no diacritics; the reader reads each page as given in the reading."""
    source, request = _archive(tmp_path, runner, len(ocr), language="vi")
    translated = []
    _fake_engine(
        monkeypatch,
        [
            (
                lambda page: (
                    lambda image: SimpleNamespace(
                        result=image,
                        textlines=[SimpleNamespace(text=t) for t in page],
                        text_regions=[
                            SimpleNamespace(text=t, translation=english[t])
                            for t in page
                        ],
                    )
                )
            )(page)
            for page in ocr
        ],
        _answering(english),
        requests={n: [page] for n, page in enumerate(ocr, 1)},
        on_translate=lambda: translated.append(1),
    )
    monkeypatch.setitem(
        sys.modules, "py3langid", SimpleNamespace(classify=_vi_classify)
    )
    monkeypatch.setattr(runner, "provider_unavailable_reason", lambda _request: None)
    monkeypatch.setattr(
        runner, "PROVIDER_TRANSPORT", httpx.MockTransport(_answering(english))
    )

    async def run_reader(_reader, picture, languages=None):
        if calls is not None:
            calls.append(languages)
        page = reading[picture.getpixel((0, 0))[0] - 1]
        if page is None:
            return None
        return "".join(f"0.1\t0.5\t0.5\t0.05\t1.0\t{line}\n" for line in page)

    monkeypatch.setattr(runner, "run_reader", run_reader)
    config = (
        {"latin_text_reader": str(_reader_script(tmp_path, "exit 0"))} if reader else {}
    )
    return (
        source,
        request,
        tmp_path / "result.cbz",
        tmp_path / "checkpoints",
        config,
        translated,
    )


@pytest.mark.asyncio
async def test_a_book_whose_ocr_lost_its_diacritics_is_proven_by_reading_every_original_page(
    tmp_path, monkeypatch
):
    runner = load("translate_archive")
    calls = []
    ocr = [["Co sao ma ban than da tro nen nhu.the anay", *VI_OCR[0]], *VI_OCR[1:]]
    reading = [["Cớ sao mà bản thân đã trở nên như thế này", *VI_READ[0]], *VI_READ[1:]]
    english = {**VI_ENGLISH, ocr[0][0]: "How did I end up like this"}
    source, request, output, checkpoints, config, _ = _vi_job(
        monkeypatch, tmp_path, runner, reading, ocr=ocr, english=english, calls=calls
    )
    await runner.translate(
        source, output, request, config=config, checkpoints=checkpoints
    )
    receipt = json.loads(zipfile.ZipFile(output).read("translation.json"))
    assert receipt["detected_source_language"] == "vi"
    assert (
        receipt["source_language_validation_scope"]
        == "independent_reader_whole_archive"
    )
    # the reader proves the language only: every region read without its diacritics is listed for correction
    reader_check = receipt["source_language_reader"]
    assert len(reader_check["regions"]) == 5 and not any(
        r["confirmed"] for r in reader_check["regions"]
    )
    assert {k: reader_check[k] for k in ("pages", "unconfirmed_regions")} == {
        "pages": 3,
        "unconfirmed_regions": [
            {"page": 1, "region": 0},
            {"page": 1, "region": 1},
            {"page": 1, "region": 2},
            {"page": 2, "region": 0},
            {"page": 3, "region": 0},
        ],
    }
    assert calls == [["vi-VN"]] * 3


@pytest.mark.asyncio
async def test_the_independent_reading_reuses_every_checkpoint_and_changes_no_english(
    tmp_path, monkeypatch
):
    runner = load("translate_archive")
    # One source archive for both runs, byte for byte: only the reading changes between them.
    reading = [VI_READ[0], None, VI_READ[2]]
    source, request, output, checkpoints, config, translated = _vi_job(
        monkeypatch, tmp_path, runner, reading
    )
    source_digest = runner.digest(source)
    with pytest.raises(ValueError, match="does not match the requested languages"):
        await runner.translate(
            source, output, request, config=config, checkpoints=checkpoints
        )
    saved = sorted(p.name for p in checkpoints.glob("*.zip"))
    assert len(saved) == 3 and len(translated) == 3
    reading[1] = VI_READ[1]  # the reader now reads page 2
    translated.clear()
    assert runner.digest(source) == source_digest == request["source_sha256"]
    await runner.translate(
        source, output, request, config=config, checkpoints=checkpoints
    )
    assert (
        translated == [] and sorted(p.name for p in checkpoints.glob("*.zip")) == saved
    )
    with zipfile.ZipFile(output) as result:
        receipt = json.loads(result.read("translation.json"))
    assert receipt["source_language_reader"]["unconfirmed_regions"] == [
        {"page": 1, "region": 0},
        {"page": 1, "region": 1},
        {"page": 2, "region": 0},
        {"page": 3, "region": 0},
    ]


@pytest.mark.asyncio
async def test_the_reader_is_not_asked_when_the_ordinary_check_already_passes(
    tmp_path, monkeypatch
):
    runner = load("translate_archive")
    calls = []
    english = {
        line: VI_ENGLISH[ocr]
        for page_ocr, page_read in zip(VI_OCR, VI_READ)
        for ocr, line in zip(page_ocr, page_read)
    }
    source, request, output, checkpoints, config, _ = _vi_job(
        monkeypatch,
        tmp_path,
        runner,
        VI_READ,
        ocr=VI_READ,
        english=english,
        calls=calls,
    )
    await runner.translate(
        source, output, request, config=config, checkpoints=checkpoints
    )
    receipt = json.loads(zipfile.ZipFile(output).read("translation.json"))
    assert (
        receipt["source_language_validation_scope"] == "whole_archive"
        and "source_language_reader" not in receipt
    )
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reading", "english", "reader"),
    [
        (
            [
                ["Hola, no es nada", "Gracias, vago"],
                ["Hola, estoy feliz"],
                ["Hola, este disco"],
            ],
            VI_ENGLISH,
            True,
        ),
        (
            [VI_READ[0], ["Hola, estoy feliz"], ["Hola, este disco es raro"]],
            VI_ENGLISH,
            True,
        ),
        (
            [["Mưa rơi trên phố"], ["Mùa đông lạnh giá"], ["Ngày mai trời nắng"]],
            VI_ENGLISH,
            True,
        ),
        ([VI_READ[0], None, VI_READ[2]], VI_ENGLISH, True),
        ([VI_READ[1], VI_READ[0], VI_READ[2]], VI_ENGLISH, True),
        (VI_READ, {k: "Hola, " + v for k, v in VI_ENGLISH.items()}, True),
        (VI_READ, VI_ENGLISH, False),
    ],
    ids=[
        "reader_reads_another_language",
        "mostly_another_language",
        "reading_of_another_book",
        "reader_fails_on_a_page",
        "readings_of_swapped_pages",
        "english_is_not_english",
        "no_reader_configured",
    ],
)
async def test_the_independent_reading_never_certifies_a_wrong_book_or_a_wrong_translation(
    tmp_path, monkeypatch, reading, english, reader
):
    runner = load("translate_archive")
    source, request, output, checkpoints, config, _ = _vi_job(
        monkeypatch, tmp_path, runner, reading, english=english, reader=reader
    )
    with pytest.raises(ValueError, match="does not match the requested languages"):
        await runner.translate(
            source, output, request, config=config, checkpoints=checkpoints
        )
    assert not output.exists()


@pytest.mark.parametrize(
    ("region", "reading", "confirmed"),
    [
        ("art", ["Il faut partir"], False),  # a word inside a longer word
        ("salut", ["salutations à tous"], False),
        ("ô ng", ["ông"], False),  # letters split differently
        (
            "Co sao ma ban than",
            ["Cớ sao", "mà bản", "thân"],
            False,
        ),  # the same letters without diacritics
        ("bản thân", ["Cớ sao", "mà bản", "thân"], False),  # part of a line
        ("Không có gì", ["Không có gì đâu"], False),
        (
            "Cớ sao mà bản thân",
            ["Cớ sao", "mà bản", "thân"],
            True,
        ),  # whole consecutive lines
        ("Salut !", ["Salut !"], True),
    ],
)
def test_a_region_is_confirmed_only_by_whole_lines_of_its_page_reading(
    region, reading, confirmed
):
    runner = load("translate_archive")
    other_page = ["art partir salut ông Cớ sao mà bản thân"]
    found = runner.region_confirmations([[region], ["x y z"]], [reading, other_page])
    assert found[0]["confirmed"] is confirmed
    assert [f["page"] for f in found] == [
        1,
        2,
    ]  # every region with letters has its own entry


# A name called with a romanized Japanese honorific in a Latin-script book becomes English only with evidence.


def _vocative_job(monkeypatch, tmp_path, runner, page_one, vocative, answer):
    """Two pages of a Spanish book: page one is ordinary dialogue, page two a balloon the model gave back unchanged."""
    source, request = _archive(tmp_path, runner, 2, language="es")
    regions_one = [(page_one, "Hello friend " + page_one.split()[-1])]
    regions_two = [("Hola amigo", "Hello friend"), (vocative, vocative)]
    _fake_engine(
        monkeypatch,
        [
            lambda image: SimpleNamespace(
                result=image,
                textlines=[SimpleNamespace(text=t) for t, _ in regions_one],
                text_regions=[
                    SimpleNamespace(text=t, translation=e) for t, e in regions_one
                ],
            ),
            lambda image: SimpleNamespace(
                result=image,
                textlines=[SimpleNamespace(text=t) for t, _ in regions_two],
                text_regions=[
                    SimpleNamespace(text=t, translation=e) for t, e in regions_two
                ],
            ),
        ],
        _answering(
            {
                page_one: regions_one[0][1],
                "Hola amigo": "Hello friend",
                vocative: answer,
            }
        ),
        requests={1: [[page_one]], 2: [["Hola amigo", vocative]]},
    )
    monkeypatch.setitem(
        sys.modules,
        "py3langid",
        SimpleNamespace(classify=lambda text: ("es" if "hola" in text else "en", 1)),
    )
    monkeypatch.setattr(runner, "provider_unavailable_reason", lambda _request: None)
    monkeypatch.setattr(
        runner,
        "PROVIDER_TRANSPORT",
        httpx.MockTransport(
            _answering(
                {
                    page_one: regions_one[0][1],
                    "Hola amigo": "Hello friend",
                    vocative: answer,
                }
            )
        ),
    )
    return source, request, tmp_path / "result.cbz", tmp_path / "checkpoints"


def _page_two(checkpoints):
    return json.loads(
        zipfile.ZipFile(next(checkpoints.glob("00002-*.zip"))).read("page.json")
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("page_one", "vocative", "answer"),
    [
        (
            "Hola amigo Kenta",
            "HIRONII...",
            "Hiro-nii...",
        ),  # no name of the book proves the stem
        (
            "Hola Akiko Akineesha",
            "AKINEECHAN!",
            "Akinee-chan!",
        ),  # two splits proven: ambiguous
        (
            "Hola amigo Susanne",
            "SUSAN...",
            "Susan...",
        ),  # a stem too short to be a proven name
        (
            "Hola amigo Hiroto",
            "HIRONLI...",
            "Hironli...",
        ),  # a false reading carries no honorific
        ("Hola amigo Hiroto", "VAMOS.", "VAMOS."),  # dialogue given back unchanged
        (
            "Hola amigo Hiroto",
            "HELLO THERE!",
            "HELLO THERE!",
        ),  # English nothing verified
        # a common word that merely begins with the letters before an honorific proves no name
        ("Hola amigo article", "ARTISAN.", "Artisan."),
        ("Hola amigo payer", "PAYSAN!", "Paysan!"),
        ("Hola amigo piensa", "PIENSAN...", "Piensan..."),
        ("Hola amigo hast", "HASSAN.", "Hassan."),
        # a longer name sharing the first letters is not, alone, evidence of the shorter called name
        ("Hola amigo Hiroto", "HIRONII...", "Hiro-nii..."),
    ],
)
async def test_unproven_or_unchanged_text_still_stops_the_page(
    tmp_path, monkeypatch, page_one, vocative, answer
):
    runner = load("translate_archive")
    source, request, output, checkpoints = _vocative_job(
        monkeypatch, tmp_path, runner, page_one, vocative, answer
    )
    with pytest.raises(runner.SuspectPage, match="kept untranslated or invalid"):
        await runner.translate(source, output, request, checkpoints=checkpoints)
    assert not list(checkpoints.glob("00002-*.zip")) and not output.exists()


# A called name becomes English only with an agreeing reading, a certain verdict quoting the job's own original
# text, and the translator's English written with that context.

SHORT_FORM = {
    "vocative": True,
    "called_name": "HIRO",
    "honorific": "NII",
    "refers_to": "Hiroto",
    "evidence_line": "Hola, Hiroto vino a casa",
    "relation": "short_form",
    "certain": True,
}


def _semantic_vocative_job(
    monkeypatch,
    tmp_path,
    runner,
    pages,
    vocative,
    verdict,
    english,
    reading=None,
    calls=None,
):
    """A Spanish book: the pages are lists of (original, English); the model gives the vocative back unchanged."""
    source, request = _archive(tmp_path, runner, len(pages), language="es")
    answers = {t: e for page in pages for t, e in page}
    answers[vocative] = vocative.title()
    regular = _answering(answers)

    def provider(request_):
        body = json.loads(request_.content)
        system = body["messages"][0]["content"]
        if system.startswith("Decide whether the supplied text"):
            question = json.loads(body["messages"][1]["content"])
            if calls is not None:
                calls.append(question)
            answer = verdict(question) if callable(verdict) else verdict
            content = json.dumps(answer)
        elif system.startswith(
            "You translate one line of dialogue from a comic book into English. The line calls"
        ):
            content = english
        else:
            return regular(request_)
        return httpx.Response(
            200,
            json={
                "choices": [{"finish_reason": "stop", "message": {"content": content}}]
            },
        )

    _fake_engine(
        monkeypatch,
        [
            (
                lambda regions: (
                    lambda image: SimpleNamespace(
                        result=image,
                        textlines=[SimpleNamespace(text=t) for t, _ in regions],
                        text_regions=[
                            SimpleNamespace(text=t, translation=e) for t, e in regions
                        ],
                    )
                )
            )(page)
            for page in pages
        ],
        provider,
        requests={n: [[t for t, _ in page]] for n, page in enumerate(pages, 1)},
    )
    monkeypatch.setitem(
        sys.modules,
        "py3langid",
        SimpleNamespace(classify=lambda text: ("es" if "hola" in text else "en", 1)),
    )
    monkeypatch.setattr(runner, "provider_unavailable_reason", lambda _request: None)
    monkeypatch.setattr(runner, "PROVIDER_TRANSPORT", httpx.MockTransport(provider))

    async def second_reading(_image, source_lines, _device="cpu"):
        return (
            reading
            if reading is not None
            else " ".join(line.text for line in source_lines)
        )

    monkeypatch.setattr(runner, "independent_latin_reading", second_reading)
    return source, request, tmp_path / "result.cbz", tmp_path / "checkpoints"


def _page_of(checkpoints, number):
    return json.loads(
        zipfile.ZipFile(next(checkpoints.glob(f"{number:05d}-*.zip"))).read("page.json")
    )


@pytest.mark.asyncio
async def test_a_short_form_called_with_an_honorific_is_translated_with_quoted_evidence(
    tmp_path, monkeypatch
):
    runner = load("translate_archive")
    calls = []
    pages = [
        [("Hola, Hiroto vino a casa", "Hello, Hiroto came home")],
        [("Hola amigo", "Hello friend"), ("HIRONII...", "HIRONII...")],
    ]
    source, request, output, checkpoints = _semantic_vocative_job(
        monkeypatch,
        tmp_path,
        runner,
        pages,
        "HIRONII...",
        SHORT_FORM,
        "Hiro...",
        calls=calls,
    )
    await runner.translate(source, output, request, checkpoints=checkpoints)
    page = _page_of(checkpoints, 2)
    assert (
        page["target_text"] == ["Hello friend", "Hiro..."] and page["untranslated"] == 0
    )
    evidence = page["vocative_evidence"][0]
    assert evidence["english"] == "Hiro..." and evidence["relation"] == "short_form"
    assert evidence["evidence"] == {"line": "Hola, Hiroto vino a casa", "page": 1}
    assert "HIRONII..." not in calls[0]["evidence"]  # the region never proves itself


@pytest.mark.asyncio
async def test_a_later_page_proves_the_called_name_through_a_cited_checkpoint_on_the_ordinary_retry(
    tmp_path, monkeypatch
):
    runner = load("translate_archive")
    pages = [
        [("Hola amigo", "Hello friend"), ("TOMOCHAN!", "TOMOCHAN!")],
        [("Hola, Tomo llegó tarde", "Hello, Tomo came late")],
    ]
    proven = {
        "vocative": True,
        "called_name": "TOMO",
        "honorific": "CHAN",
        "refers_to": "Tomo",
        "evidence_line": "Hola, Tomo llegó tarde",
        "relation": "same_name",
        "certain": True,
    }

    def verdict(question):
        return (
            proven
            if proven["evidence_line"] in question["evidence"]
            else {"vocative": True, "certain": False}
        )

    source, request, output, checkpoints = _semantic_vocative_job(
        monkeypatch, tmp_path, runner, pages, "TOMOCHAN!", verdict, "Tomo!"
    )
    with pytest.raises(runner.SuspectPage, match="kept untranslated or invalid"):
        await runner.translate(source, output, request, checkpoints=checkpoints)
    later = next(checkpoints.glob("00002-*.zip"))
    await runner.translate(source, output, request, checkpoints=checkpoints)
    evidence = _page_of(checkpoints, 1)["vocative_evidence"][0]["evidence"]
    assert evidence == {
        "line": "Hola, Tomo llegó tarde",
        "page": 2,
        "checkpoint_sha256": later.stem.split("-", 1)[1],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("first", "vocative", "verdict", "english", "reading"),
    [
        (
            "Hola, Hiroto vino a casa",
            "HIRONII...",
            {**SHORT_FORM, "certain": False},
            "Hiro...",
            None,
        ),
        (
            "Hola, Hiroto vino a casa",
            "HIRONII...",
            {**SHORT_FORM, "called_name": "HIROT"},
            "Hiro...",
            None,
        ),
        (
            "Hola, Hiroto vino a casa",
            "HIRONII...",
            {**SHORT_FORM, "evidence_line": "Hiroto es su primo"},
            "Hiro...",
            None,
        ),
        (
            "Hola, Hiroto vino a casa",
            "HIRONII...",
            {**SHORT_FORM, "refers_to": "Hiro", "relation": "same_name"},
            "Hiro...",
            None,
        ),
        (
            "Hola, Hiroto vino a casa",
            "HIRONII...",
            {**SHORT_FORM, "relation": "same_name"},
            "Hiro...",
            None,
        ),
        ("Hola, Hiroto vino a casa", "HIRONII...", SHORT_FORM, "Hiro...", "HIRONLI..."),
        ("Hola, Hiroto vino a casa", "HIRONII...", SHORT_FORM, "Hiro-nii...", None),
        (
            "Hola, Hiroto vino a casa",
            "HIRONII...",
            SHORT_FORM,
            "Big brother Hiro...",
            None,
        ),
        ("Hola, Hiroto vino a casa", "HIRONII...", SHORT_FORM, "HIRONII...", None),
        (
            "Hola, Hassan vino a casa",
            "HASSAN.",
            {
                **SHORT_FORM,
                "called_name": "HAS",
                "honorific": "SAN",
                "refers_to": "Hassan",
                "evidence_line": "Hola, Hassan vino a casa",
            },
            "Has.",
            None,
        ),
        (
            "Hola, el article dice",
            "ARTISAN.",
            {
                **SHORT_FORM,
                "called_name": "ARTI",
                "honorific": "SAN",
                "refers_to": "article",
                "evidence_line": "Hola, el article dice",
            },
            "Arti.",
            None,
        ),
    ],
    ids=[
        "uncertain",
        "echo_not_the_letters",
        "quote_not_in_the_book",
        "person_not_a_word_of_the_quote",
        "relation_inconsistent",
        "independent_reading_disagrees",
        "english_keeps_the_honorific",
        "english_adds_a_relation",
        "english_is_the_source",
        "real_name_ending_like_an_honorific",
        "common_word_not_written_as_a_name",
    ],
)
async def test_a_called_name_without_every_proof_still_stops_the_page(
    tmp_path, monkeypatch, first, vocative, verdict, english, reading
):
    runner = load("translate_archive")
    pages = [[(first, "Hello")], [("Hola amigo", "Hello friend"), (vocative, vocative)]]
    source, request, output, checkpoints = _semantic_vocative_job(
        monkeypatch,
        tmp_path,
        runner,
        pages,
        vocative,
        verdict,
        english,
        reading=reading,
    )
    with pytest.raises(runner.SuspectPage, match="kept untranslated or invalid"):
        await runner.translate(source, output, request, checkpoints=checkpoints)
    assert not list(checkpoints.glob("00002-*.zip")) and not output.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["ar", "he", "en", "ja", "ru", None])
async def test_a_source_not_explicitly_latin_never_reaches_the_vocative_check(
    monkeypatch, language
):
    runner = load("translate_archive")
    calls = []
    monkeypatch.setattr(
        runner,
        "PROVIDER_TRANSPORT",
        httpx.MockTransport(lambda r: calls.append(r) or httpx.Response(500)),
    )
    request = {
        "source_language": language,
        "ai": {"base_url": "https://x.test", "model": "m", "api_key": "k"},
    }
    found = await runner.verify_romanized_vocatives(
        request,
        ["HIRONII..."],
        lines=[SimpleNamespace(text="HIRONII...")],
        image=None,
        evidence=[{"line": "Hiroto", "page": 1}],
    )
    assert found == [] and not calls


# A copied Latin region of a book in another script that no independent reading confirms is never set aside:
# short speech, a sign the story needs and drawn lettering all stop the page, whatever their length.


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("label", "reading"),
    [
        ("HELP!", "HFLP!"),  # short speech in a balloon
        ("HELP", "HEIP"),
        ("STOP RUN", "STDP RUN"),
        ("NO ENTRY AFTER DARK", "NO ENTRY AFTER OARK"),  # a sign the story turns on
        ("DEST.AIN", "DESVANN"),  # drawn lettering read differently each time
    ],
)
async def test_unconfirmed_latin_text_in_a_book_in_another_script_stops_the_page(
    tmp_path, monkeypatch, label, reading
):
    runner = load("translate_archive")
    calls = []
    verdict = {"function": "other", "certain": True, "text": label}
    source, request, output, checkpoints = _name_job(
        monkeypatch,
        tmp_path,
        runner,
        label,
        [label],
        _verdict_provider(label, verdict, calls=calls),
        reading,
    )
    with pytest.raises(runner.SuspectPage, match="kept untranslated or invalid"):
        await runner.translate(source, output, request, checkpoints=checkpoints)
    assert not list(checkpoints.glob("00001-*.zip"))
    assert not output.exists()
