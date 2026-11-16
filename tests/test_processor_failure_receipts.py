from __future__ import annotations

import importlib.util
import io
import json
import stat
import sys
from pathlib import Path

import pytest


def load(name):
    path = Path(__file__).resolve().parents[1] / "contrib" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("error_type", "code", "retryable", "exit_code"),
    [
        ("SuspectPage", "page_review_required", False, 75),
        ("SuspectGeometry", "page_review_required", False, 75),
        ("ProviderUnavailable", "provider_unavailable", True, 75),
        ("TranslationFailed", "translation_failed", True, 75),
        ("ValueError", "validation_failed", False, 1),
        ("TimeoutError", "provider_interrupted", True, 75),
    ],
)
def test_cli_records_safe_failure_category(
    tmp_path, monkeypatch, error_type, code, retryable, exit_code
):
    runner = load("translate_archive")
    output = tmp_path / "result.cbz"
    secret = "fixture-provider-secret"
    error_class = {"ValueError": ValueError, "TimeoutError": TimeoutError}.get(
        error_type
    ) or getattr(runner, error_type)

    async def fail(*args, **kwargs):
        raise error_class(f"Provider body contains {secret} and source dialogue")

    monkeypatch.setattr(runner, "translate", fail)
    monkeypatch.setattr(
        sys,
        "argv",
        ["runner", "--source", str(tmp_path / "source.cbz"), "--output", str(output)],
    )
    monkeypatch.setattr(
        sys,
        "stdin",
        io.TextIOWrapper(io.BytesIO(json.dumps({"ai": {"api_key": secret}}).encode())),
    )
    with pytest.raises(SystemExit) as raised:
        runner.main()
    assert raised.value.code == exit_code
    receipt = output.with_suffix(".failure.json")
    assert json.loads(receipt.read_text()) == {"code": code, "retryable": retryable}
    assert secret not in receipt.read_text()
    assert stat.S_IMODE(receipt.stat().st_mode) == 0o600
    assert not output.exists()


def test_success_removes_previous_failure_receipt(tmp_path, monkeypatch):
    runner = load("translate_archive")
    output = tmp_path / "result.cbz"
    receipt = output.with_suffix(".failure.json")
    receipt.write_text('{"code":"page_review_required","retryable":false}')

    async def succeed(*args, **kwargs):
        output.write_bytes(b"fixture-result")

    monkeypatch.setattr(runner, "translate", succeed)
    monkeypatch.setattr(
        sys,
        "argv",
        ["runner", "--source", str(tmp_path / "source.cbz"), "--output", str(output)],
    )
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"{}")))
    runner.main()
    assert output.exists()
    assert not receipt.exists()


@pytest.mark.parametrize(
    "receipt",
    [
        {
            "code": "page_review_required",
            "retryable": False,
            "provider_body": "fixture-secret",
        },
        {"code": "fixture-secret", "retryable": True},
        {"code": "page_review_required", "retryable": "fixture-secret"},
        {"code": [], "retryable": False},
        ["fixture-secret"],
        "not-json",
        "x" * 2048,
        None,
    ],
)
def test_processor_exposes_only_validated_failure_fields(tmp_path, receipt):
    module = load("translation_processor")

    def execute(directory, request):
        if receipt is not None:
            data = receipt if isinstance(receipt, str) else json.dumps(receipt)
            (directory / "result.failure.json").write_text(data)
        raise RuntimeError("fixture-secret must not reach status")

    processor = module.Processor(tmp_path, execute)
    directory = processor.directory("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-0")
    directory.mkdir()
    (directory / "result.failure.json").write_text(
        '{"code":"provider_unavailable","retryable":true}'
    )
    processor.active.add("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-0")
    try:
        processor._run("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-0", {})
        status = processor.status("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-0")
        assert status["status"] == "failed"
        if (
            isinstance(receipt, dict)
            and receipt.get("code") == "page_review_required"
            and receipt.get("retryable") is False
        ):
            assert status["failure"] == {
                "code": "page_review_required",
                "retryable": False,
            }
        else:
            assert "failure" not in status
        assert "fixture-secret" not in json.dumps(status)
        assert "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-0" not in processor.active
    finally:
        processor.executor.shutdown(wait=True)
