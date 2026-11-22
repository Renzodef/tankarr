"""Admission, source integrity and cancellation under actual worker concurrency."""

from __future__ import annotations

import copy
import hashlib
import http.client
import json
import sys
import threading
from http.server import ThreadingHTTPServer

import pytest

from contrib import translation_processor as module
from contrib import worker_resources


def resources(**overrides):
    return {
        "memory_available_bytes": 4 * 1024**3,
        "disk_free_bytes": 20 * 1024**3,
        "cpu_count": 8,
        "load_per_cpu": 0.3,
        "on_battery": False,
        "battery_percent": None,
        **overrides,
    }


def request(processor, digit, source=b"source"):
    job = digit * 32 + "-0"
    folder = processor.directory(job)
    folder.mkdir(exist_ok=True)
    (folder / "source.cbz").write_bytes(source)
    return job, {
        "source_sha256": hashlib.sha256(source).hexdigest(),
        "source_language": "ja",
        "target_language": "en",
        "ai": {
            "base_url": "https://translate.example/v1",
            "model": "fixture-model",
            "api_key": "fixture-secret",
        },
    }


def test_capacity_backpressure_and_queued_cancel_never_execute(tmp_path):
    started, release = threading.Event(), threading.Event()
    calls = []

    def execute(directory, _request):
        calls.append(directory.name)
        started.set()
        assert release.wait(5)
        (directory / "result.cbz").write_bytes(b"done")

    p = module.Processor(tmp_path, execute, max_queue=1, resource_probe=resources)
    first, one = request(p, "a")
    second, two = request(p, "b")
    third, three = request(p, "c")
    try:
        p.submit(first, copy.deepcopy(one))
        assert started.wait(2)
        p.submit(second, copy.deepcopy(two))
        health = p.health()
        assert health["active_jobs"] == 2
        assert health["running_jobs"] == health["queued_jobs"] == 1
        assert health["accepting"] is False
        with pytest.raises(module.BusyError):
            p.submit(third, copy.deepcopy(three))
        assert not (p.directory(third) / "request.json").exists()
        # Reconnection to an existing request works even when the machine is full.
        assert p.submit(first, copy.deepcopy(one))["status"] == "running"
        p.cancel(second)
    finally:
        release.set()
        p.executor.shutdown(wait=True)
    assert calls == [first]
    assert p.status(second) == {"status": "cancelled"}
    assert p.health()["active_jobs"] == p.health()["running_jobs"] == 0


def test_duplicate_submissions_reuse_hash_but_detect_same_size_source_change(
    tmp_path, monkeypatch
):
    release = threading.Event()
    p = module.Processor(tmp_path, lambda *_: release.wait(5), resource_probe=resources)
    job, payload = request(p, "a", b"first")
    original = module.digest
    reads = []

    def count(path):
        reads.append(path)
        return original(path)

    monkeypatch.setattr(module, "digest", count)
    try:
        p.submit(job, copy.deepcopy(payload))
        for _ in range(20):
            p.submit(job, copy.deepcopy(payload))
        assert len(reads) == 1
        (p.directory(job) / "source.cbz").write_bytes(b"other")
        with pytest.raises(ValueError, match="digest mismatch"):
            p.submit(job, copy.deepcopy(payload))
        assert len(reads) == 2
    finally:
        release.set()
        p.executor.shutdown(wait=True)


def test_changed_during_hash_not_cached(tmp_path, monkeypatch):
    p = module.Processor(tmp_path, lambda *_: None, resource_probe=resources)
    job, payload = request(p, "a")
    original = module.digest

    def race(path):
        result = original(path)
        path.write_bytes(b"changed")
        return result

    monkeypatch.setattr(module, "digest", race)
    try:
        with pytest.raises(ValueError, match="changed during verification"):
            p.submit(job, payload)
        assert not p.source_hashes
    finally:
        p.executor.shutdown(wait=True)


@pytest.mark.parametrize(
    ("observation", "policy", "expected"),
    [
        ({"memory_available_bytes": None}, {"min_memory_bytes": 1}, "memory"),
        ({"memory_available_bytes": 0}, {"min_memory_bytes": 1}, "memory"),
        ({"disk_free_bytes": 12}, {"min_disk_bytes": 13}, "disk"),
        ({"load_per_cpu": 2}, {"max_load_per_cpu": 1.5}, "cpu_load"),
        ({"on_battery": True, "battery_percent": 90}, {}, "battery"),
        (
            {"on_battery": True, "battery_percent": 10},
            {"allow_battery": True},
            "battery",
        ),
    ],
)
def test_resource_admission(tmp_path, observation, policy, expected):
    p = module.Processor(
        tmp_path,
        lambda *_: None,
        resource_probe=lambda: resources(**observation),
        admission=policy,
    )
    job, payload = request(p, "a")
    try:
        assert expected in p.health()["admission_reasons"]
        with pytest.raises(module.BusyError):
            p.submit(job, payload)
        assert p.active == set()
    finally:
        p.executor.shutdown(wait=True)


def test_resource_sampling_ttl(monkeypatch, tmp_path):
    calls = []
    now = [10.0]
    monkeypatch.setattr(worker_resources.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(
        worker_resources, "snapshot", lambda _: calls.append(1) or resources()
    )
    probe = worker_resources.ResourceProbe(tmp_path, ttl=2)
    for _ in range(20):
        assert probe()["disk_free_bytes"] > 0
    assert len(calls) == 1
    now[0] += 3
    probe()
    assert len(calls) == 2


def test_health_authenticated_and_streaming_upload_hash_reused(tmp_path, monkeypatch):
    p = module.Processor(
        tmp_path,
        lambda *_: None,
        resource_probe=resources,
        capabilities=("translation", "mit"),
    )
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), module.handler_for(p, "fixture-token")
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
    headers = {"Authorization": "Bearer fixture-token"}
    try:
        conn.request("GET", "/health")
        response = conn.getresponse()
        assert response.status == 401
        response.read()
        conn.request("GET", "/health", headers=headers)
        response = conn.getresponse()
        body = json.loads(response.read())
        assert body["protocol_version"] == 1
        assert body["capabilities"] == ["translation", "mit"]
        assert body["accepting"] is True
        assert "fixture-token" not in json.dumps(body)
        job = "a" * 32 + "-0"
        conn.request("PUT", f"/jobs/{job}/source", body=b"source", headers=headers)
        response = conn.getresponse()
        assert response.status == 200
        response.read()
        # Successful upload computed SHA while streaming; no second full read.
        monkeypatch.setattr(
            module, "digest", lambda _: pytest.fail("unexpected disk hash")
        )
        assert (
            p.source_digest(p.directory(job) / "source.cbz")
            == hashlib.sha256(b"source").hexdigest()
        )
        conn.request("PUT", f"/jobs/{job}/source", body=b"source", headers=headers)
        response = conn.getresponse()
        assert response.status == 200
        response.read()
        conn.request("PUT", f"/jobs/{job}/source", body=b"different", headers=headers)
        response = conn.getresponse()
        assert response.status == 409
        response.read()
    finally:
        conn.close()
        server.shutdown()
        server.server_close()
        p.executor.shutdown(wait=True)


def test_footer_pipeline_is_opt_in_fixed_configuration_without_ai(tmp_path):
    calls = []

    def execute(directory, payload):
        calls.append(payload)
        (directory / "result.cbz").write_bytes(b"evidence-only")

    p = module.Processor(
        tmp_path,
        execute,
        resource_probe=resources,
        capabilities=("footer-reuse",),
        pipeline_fingerprint="sha256:fixture-runtime-v1",
    )
    job, payload = request(p, "a")
    payload.pop("ai")
    payload["pipeline"] = "footer-reuse"
    try:
        for invalid in (
            {**payload, "options": {}},
            {**payload, "pipeline": "translation"},
        ):
            with pytest.raises(ValueError):
                p.submit(job, invalid)
        p.submit(job, payload)
    finally:
        p.executor.shutdown(wait=True)
    assert p.status(job)["status"] == "completed"
    identity = json.loads((p.directory(job) / "request.json").read_text())
    assert identity["pipeline"] == "footer-reuse"
    assert identity["pipeline_fingerprint"] == "sha256:fixture-runtime-v1"
    assert "ai" not in identity
    p.pipeline_fingerprint = "sha256:fixture-runtime-v2"
    assert p.status(job) == {
        "status": "failed",
        "failure": {"code": "validation_failed", "retryable": False},
    }
    with pytest.raises(ValueError, match="another request"):
        p.submit(job, payload)
    assert len(calls) == 1


def test_invalid_json_shapes_do_not_reach_engine(tmp_path):
    p = module.Processor(
        tmp_path, lambda *_: pytest.fail("must not execute"), resource_probe=resources
    )
    job, payload = request(p, "a")
    try:
        for invalid in ([], None, {**payload, "ai": []}):
            with pytest.raises(ValueError):
                p.submit(job, invalid)
    finally:
        p.executor.shutdown(wait=True)


def test_retry_checkpoint_is_attempt_scoped_and_does_not_adopt_legacy(tmp_path):
    job = "a" * 32
    legacy = tmp_path / "checkpoints" / job
    legacy.mkdir(parents=True)
    (legacy / "00001-old.zip").write_bytes(b"old checkpoint")
    first = module.checkpoint_directory(tmp_path, job + "-0")
    second = module.checkpoint_directory(tmp_path, job + "-1")
    assert first != second != legacy
    assert module.checkpoint_directory(tmp_path, job + "-0") == first
    assert (legacy / "00001-old.zip").read_bytes() == b"old checkpoint"
    with pytest.raises(ValueError):
        module.checkpoint_directory(tmp_path, "../escape")


def test_competing_uploads_cannot_change_immutable_source(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    p = module.Processor(tmp_path, lambda *_: None, resource_probe=resources)
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), module.handler_for(p, "fixture-token")
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    job = "d" * 32 + "-0"
    barrier = threading.Barrier(2)

    def upload(payload):
        conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        try:
            barrier.wait(timeout=2)
            conn.request(
                "PUT",
                f"/jobs/{job}/source",
                body=payload,
                headers={"Authorization": "Bearer fixture-token"},
            )
            response = conn.getresponse()
            status = response.status
            response.read()
            return status, payload
        finally:
            conn.close()

    try:
        with ThreadPoolExecutor(2) as pool:
            results = list(
                pool.map(upload, (b"first source" * 1000, b"other source" * 1000))
            )
        assert sorted(status for status, _ in results) == [200, 409]
        accepted = next(data for status, data in results if status == 200)
        source = p.directory(job) / "source.cbz"
        assert source.read_bytes() == accepted
        assert p.source_digest(source) == hashlib.sha256(accepted).hexdigest()
        assert not list(p.directory(job).glob("upload-*"))
    finally:
        server.shutdown()
        server.server_close()
        p.executor.shutdown(wait=True)


def test_mit_runtime_identity_tracks_code_and_private_config_without_leaking(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace

    runner = tmp_path / "translate_archive.py"
    runner.write_text("runner version one")
    vision = tmp_path / "vision_balloon.py"
    vision.write_text("vision version one")
    interpreter = tmp_path / "python"
    interpreter.write_bytes(b"fixture interpreter")
    config = tmp_path / "config.json"
    config.write_text('{"private_value": "fixture-secret", "detection_size": 1024}')
    commands = []
    dirty = [b""]

    def git(command, **options):
        commands.append(command)
        assert options["timeout"] == 3 and options["check"] is True
        assert command[:3] == ["git", "-C", str(tmp_path)]
        return SimpleNamespace(
            stdout=b"a" * 40 if command[3] == "rev-parse" else dirty[0]
        )

    monkeypatch.setattr(module.subprocess, "run", git)
    arguments = (tmp_path, interpreter, runner, config)
    initial = module.runtime_fingerprint(*arguments)
    assert initial == module.runtime_fingerprint(*arguments)
    config.write_text('{"private_value": "fixture-secret", "detection_size": 2048}')
    changed_config = module.runtime_fingerprint(*arguments)
    runner.write_text("runner version two")
    changed_runner = module.runtime_fingerprint(*arguments)
    dirty[0] = b"tracked local engine patch"
    changed_engine = module.runtime_fingerprint(*arguments)
    assert len({initial, changed_config, changed_runner, changed_engine}) == 4
    assert changed_engine != module.runtime_fingerprint(*arguments, cpu=True)
    assert changed_engine != module.runtime_fingerprint(*arguments, threads=2)
    assert all(
        len(value) == 64 and "fixture-secret" not in value
        for value in (initial, changed_config, changed_runner, changed_engine)
    )
    assert not any("fetch" in command for command in commands)


def test_effective_cpus_respects_affinity_and_fractional_container_quota(monkeypatch):
    monkeypatch.setattr(worker_resources.os, "cpu_count", lambda: 64)
    monkeypatch.setattr(
        worker_resources.os, "sched_getaffinity", lambda _: set(range(8)), raising=False
    )
    monkeypatch.setattr(worker_resources.platform, "system", lambda: "Linux")
    monkeypatch.setattr(worker_resources.Path, "read_text", lambda _: "250000 100000")
    assert worker_resources.effective_cpu_count() == 2.5
    assert worker_resources.threads_per_worker(2) == 1
    assert worker_resources.threads_per_worker(2, 3) == 3
    with pytest.raises(ValueError):
        worker_resources.threads_per_worker(2, 0)


def test_processor_cli_divides_threads_and_overrides_inherited_library_limits(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace

    monkeypatch.setattr(worker_resources, "effective_cpu_count", lambda: 12)
    monkeypatch.setenv("OMP_NUM_THREADS", "64")
    monkeypatch.setenv("OPENBLAS_NUM_THREADS", "64")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "translation_processor.py",
            "--engine-python",
            sys.executable,
            "--engine-root",
            str(tmp_path),
            "--state",
            str(tmp_path / "state"),
            "--workers",
            "3",
            "--cpu",
        ],
    )
    launches = []
    fingerprints = []

    def fingerprint(*_args, **options):
        fingerprints.append(options)
        return "a" * 64

    def launch(command, **options):
        launches.append((command, options))
        return SimpleNamespace(
            returncode=0,
            communicate=lambda *_args, **_kwargs: (None, b""),
            poll=lambda: 0,
        )

    def serve(processor, *_args):
        try:
            folder = processor.directory("a" * 32 + "-0")
            folder.mkdir()
            processor.execute(folder, {})
        finally:
            processor.executor.shutdown(wait=True)

    monkeypatch.setattr(module, "runtime_fingerprint", fingerprint)
    monkeypatch.setattr(module.subprocess, "Popen", launch)
    monkeypatch.setattr(module, "serve", serve)
    module.main()
    command, options = launches[0]
    budget = int(command[command.index("--cpu-threads") + 1])
    assert budget == 4 and budget * 3 <= 12
    assert fingerprints == [{"threads": 4, "cpu": True}]
    for name in (
        "OMP_NUM_THREADS",
        "OMP_THREAD_LIMIT",
        "MKL_NUM_THREADS",
        "OPENBLAS_NUM_THREADS",
        "NUMEXPR_NUM_THREADS",
        "NUMEXPR_MAX_THREADS",
        "VECLIB_MAXIMUM_THREADS",
    ):
        assert options["env"][name] == "4"
    assert options["env"]["OMP_MAX_ACTIVE_LEVELS"] == "1"
    assert module.os.environ["OMP_NUM_THREADS"] == "64"
    assert module.os.environ["OPENBLAS_NUM_THREADS"] == "64"


def test_runner_limits_torch_and_opencv_before_engine_work(monkeypatch):
    from types import SimpleNamespace

    from contrib import translate_archive as runner

    calls = []
    monkeypatch.setitem(
        sys.modules,
        "torch",
        SimpleNamespace(
            set_num_threads=lambda n: calls.append(("torch", n)),
            set_num_interop_threads=lambda n: calls.append(("interop", n)),
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "cv2",
        SimpleNamespace(setNumThreads=lambda n: calls.append(("opencv", n))),
    )
    runner.configure_engine_threads(4)
    assert calls == [("torch", 4), ("interop", 1), ("opencv", 4)]
    with pytest.raises(ValueError):
        runner.configure_engine_threads(0)
