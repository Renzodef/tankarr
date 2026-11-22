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


JOB = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb-0"


def run_failed_engine(tmp_path, returncode, summary, receipt=None):
    module = load("translation_processor")

    def execute(directory, request):
        if receipt is not None:
            (directory / "result.failure.json").write_text(json.dumps(receipt))
        raise module.EngineFailed(returncode, summary)

    processor = module.Processor(tmp_path, execute)
    directory = processor.directory(JOB)
    directory.mkdir()
    processor.active.add(JOB)
    try:
        processor._run(JOB, {"ai": {"api_key": "fixture-secret"}})
        return processor.status(JOB), directory
    finally:
        processor.executor.shutdown(wait=True)


def test_retry_exit_without_receipt_is_retryable(tmp_path):
    status, directory = run_failed_engine(
        tmp_path, 75, "Temporary provider interruption"
    )
    assert status == {
        "status": "failed",
        "failure": {"code": "provider_interrupted", "retryable": True},
        "exit_code": 75,
    }
    diagnostic = directory / "engine-failure.json"
    assert json.loads(diagnostic.read_text()) == {
        "exit_code": 75,
        "stderr_summary": "Temporary provider interruption",
    }
    assert stat.S_IMODE(diagnostic.stat().st_mode) == 0o600


def test_permanent_exit_stays_failed_without_retry_hint(tmp_path):
    status, _ = run_failed_engine(tmp_path, 1, "Translation or validation failed")
    assert status == {"status": "failed", "exit_code": 1}


def test_runner_receipt_wins_over_exit_status(tmp_path):
    status, _ = run_failed_engine(
        tmp_path, 75, "x", {"code": "page_review_required", "retryable": False}
    )
    assert status["failure"] == {"code": "page_review_required", "retryable": False}
    assert status["exit_code"] == 75


def test_stale_engine_diagnostic_is_removed_on_success(tmp_path):
    module = load("translation_processor")

    def execute(directory, request):
        (directory / "result.cbz").write_bytes(b"fixture-result")

    processor = module.Processor(tmp_path, execute)
    directory = processor.directory(JOB)
    directory.mkdir()
    (directory / "engine-failure.json").write_text('{"exit_code": 75}')
    processor.active.add(JOB)
    try:
        processor._run(JOB, {})
        assert processor.status(JOB) == {"status": "completed"}
        assert not (directory / "engine-failure.json").exists()
    finally:
        processor.executor.shutdown(wait=True)


@pytest.mark.parametrize(
    "line",
    [
        "Provider unavailable: Bearer fixture-secret refused",
        "Provider unavailable: api_key=fixture-secret",
        "Provider unavailable: sk-fixturesecret123 refused",
        "Provider unavailable: https://user:fixture-secret@example.invalid/v1",
        "Provider unavailable: literal fixture-secret",
    ],
)
def test_stderr_summary_redacts_credentials(line):
    module = load("translation_processor")
    summary = module.stderr_summary(
        ("noise\n" + line + "\n").encode(), ["fixture-secret"]
    )
    assert summary.startswith("Provider unavailable:")
    assert "fixture-secret" not in summary and "fixturesecret" not in summary
    assert len(summary) <= 200


def test_stderr_summary_is_bounded():
    module = load("translation_processor")
    assert module.stderr_summary(b"x" * 10000) == "x" * 200
    assert module.stderr_summary(b"") == ""
