from __future__ import annotations

import concurrent.futures
import copy
import hashlib
import importlib.util
import json
import threading
import time
from pathlib import Path

import httpx
import pytest


def load():
    path = Path(__file__).resolve().parents[1] / "contrib/dispatch_translation.py"
    spec = importlib.util.spec_from_file_location("dispatcher", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def lab(tmp_path):
    module = load()
    secret = tmp_path / "worker.token"
    secret.write_text("fixture-worker-token")
    source = tmp_path / "source.cbz"
    source.write_bytes(b"fixture archive")
    registry = tmp_path / "workers.json"
    workers = [
        {
            "id": f"node-{n}",
            "url": f"https://node-{n}.example.test",
            "token_file": str(secret),
            "max_jobs": 1,
        }
        for n in range(6)
    ]
    registry.write_text(json.dumps({"workers": workers}))
    request = {
        "protocol_version": 1,
        "event": "translation.requested",
        "job_id": "a" * 32,
        "attempt": 1,
        "source_path": str(source),
        "result_path": str(tmp_path / "result.cbz"),
        "checkpoint_directory": str(tmp_path / "checkpoints"),
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "source_language": "ja",
        "target_language": "en",
        "ai": {
            "base_url": "https://provider.example.test/v1",
            "model": "test-model",
            "api_key": "fixture-provider-key",
        },
    }
    health = {
        "protocol_version": 1,
        "accepting": True,
        "capacity": 1,
        "active_jobs": 0,
        "capabilities": ["translation"],
        "resources": {"disk_free_bytes": 1000000, "on_battery": False},
    }
    return module, registry, workers, request, health


def factory(answer):
    return lambda **kwargs: httpx.Client(
        transport=httpx.MockTransport(answer), **kwargs
    )


def complete(health, calls):
    submitted = set()

    def answer(request):
        calls.append((request.method, request.url.host, request.url.path))
        assert request.headers["Authorization"] == "Bearer fixture-worker-token"
        if request.url.path == "/health":
            return httpx.Response(200, json=health)
        if request.method == "PUT":
            return httpx.Response(200, json={"status": "uploaded"})
        if request.method == "POST":
            submitted.add(str(request.url))
            return httpx.Response(202, json={"status": "completed"})
        if request.url.path.endswith("/result"):
            return httpx.Response(200, content=b"translated fixture")
        if str(request.url) in submitted:
            return httpx.Response(200, json={"status": "completed"})
        return httpx.Response(404)

    return answer


def test_six_enrolled_workers_are_used_concurrently(lab, tmp_path):
    module, registry, workers, request, health = lab
    barrier = threading.Barrier(6)
    calls, placements = [], []
    inner = complete(health, calls)

    def answer(r):
        if r.method == "POST":
            placements.append(r.url.host)
            barrier.wait(timeout=10)
        return inner(r)

    def run(index):
        item = {
            **request,
            "job_id": f"{index:032x}",
            "checkpoint_directory": str(tmp_path / f"cp-{index}"),
            "result_path": str(tmp_path / f"result-{index}.cbz"),
        }
        return module.Dispatcher(
            registry, tmp_path / "state", client_factory=factory(answer), review=False
        ).dispatch(item)

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(run, range(1, 7)))
    assert results == [0] * 6
    assert len(set(placements)) == 6
    assert set(placements) == {f"node-{n}.example.test" for n in range(6)}


def test_registry_reload_removes_and_adds_worker(lab, tmp_path):
    module, registry, workers, request, health = lab
    calls = []
    dispatcher = module.Dispatcher(
        registry,
        tmp_path / "state",
        client_factory=factory(complete(health, calls)),
        review=False,
    )
    registry.write_text(json.dumps({"workers": [workers[0]]}))
    assert dispatcher.dispatch(request) == 0
    registry.write_text(
        json.dumps(
            {
                "workers": [
                    {
                        **workers[0],
                        "id": "new-machine",
                        "url": "https://new-machine.example.test",
                    }
                ]
            }
        )
    )
    item = {
        **request,
        "job_id": "b" * 32,
        "checkpoint_directory": str(tmp_path / "cp2"),
    }
    calls.clear()
    assert dispatcher.dispatch(item) == 0
    assert all(host == "new-machine.example.test" for _, host, _ in calls)


def test_ambiguous_post_is_pinned_and_never_resubmitted_elsewhere(lab, tmp_path):
    module, registry, workers, request, health = lab
    calls = []

    def answer(r):
        calls.append((r.method, r.url.host, r.url.path))
        if r.url.path == "/health":
            return httpx.Response(200, json=health)
        if r.method == "GET":
            return httpx.Response(404)
        if r.method == "PUT":
            return httpx.Response(200)
        raise httpx.ReadTimeout("fixture timeout", request=r)

    dispatcher = module.Dispatcher(
        registry, tmp_path / "state", client_factory=factory(answer)
    )
    assert dispatcher.dispatch(request) == 75
    chosen = next(host for method, host, _ in calls if method == "POST")
    calls.clear()
    assert dispatcher.dispatch(request) == 75
    assert {host for _, host, _ in calls} == {chosen}
    registry.write_text(
        json.dumps({"workers": [w for w in workers if w["url"] != "https://" + chosen]})
    )
    calls.clear()
    assert dispatcher.dispatch(request) == 75
    assert not calls


def test_review_bound_to_hash_and_cached_result_avoids_remote_work(lab, tmp_path):
    module, registry, workers, request, health = lab
    calls = []
    dispatcher = module.Dispatcher(
        registry, tmp_path / "state", client_factory=factory(complete(health, calls))
    )
    assert dispatcher.dispatch(request) == 75
    assert not Path(request["result_path"]).exists()
    receipt = tmp_path / "state" / ("a" * 32 + "-1.review.json")
    approval = receipt.with_name(receipt.name.replace(".review.", ".approved."))
    invalid = json.loads(receipt.read_text())
    invalid["attempt"] = 2
    approval.write_text(json.dumps(invalid))
    calls.clear()
    assert dispatcher.dispatch(request) == 75
    assert not calls
    approval.write_text(receipt.read_text())
    assert dispatcher.dispatch(request) == 0
    assert not calls
    assert Path(request["result_path"]).read_bytes() == b"translated fixture"
    assert all(
        "fixture-provider-key" not in p.read_text()
        for p in (tmp_path / "state").glob("*.json")
    )


@pytest.mark.parametrize(
    "change",
    [
        {"accepting": False},
        {"active_jobs": 1},
        {"protocol_version": 2},
        {"capabilities": ["storage"]},
        {"resources": {"disk_free_bytes": 1}},
        {"resources": {"disk_free_bytes": 1000000, "on_battery": True}},
        {"source_languages": ["zh"]},
        {"target_languages": ["it"]},
    ],
)
def test_capacity_and_capability_gate(lab, tmp_path, change):
    module, registry, workers, request, health = lab
    calls = []
    dispatcher = module.Dispatcher(
        registry,
        tmp_path / "state",
        client_factory=factory(complete({**health, **change}, calls)),
    )
    assert dispatcher.dispatch(request) == 75
    assert all(path == "/health" for _, _, path in calls)


def test_offline_and_auth_failed_workers_are_not_selected(lab, tmp_path):
    module, registry, workers, request, health = lab
    calls = []
    inner = complete(health, calls)

    def answer(r):
        if r.url.host == "node-0.example.test":
            raise httpx.ConnectTimeout("fixture timeout", request=r)
        if r.url.host == "node-1.example.test":
            return httpx.Response(401)
        return inner(r)

    dispatcher = module.Dispatcher(
        registry, tmp_path / "state", client_factory=factory(answer), review=False
    )
    assert dispatcher.dispatch(request) == 0
    assert (
        next(h for method, h, _ in calls if method == "POST") == "node-2.example.test"
    )


def test_estimate_uses_throughput_transfer_and_occupancy(lab):
    module, registry, workers, request, health = lab
    slow = {**workers[0], "pages_per_second": 0.1}
    fast = {**workers[1], "pages_per_second": 2}
    assert module.estimate(fast, health, request, 1000000) < module.estimate(
        slow, health, request, 1000000
    )
    distant = {**fast, "transfer_bytes_per_second": 1}
    assert module.estimate(slow, health, request, 1000000) < module.estimate(
        distant, health, request, 1000000
    )


def test_duplicate_same_attempt_does_not_send_requests(lab, tmp_path):
    module, registry, workers, request, health = lab
    calls = []
    dispatcher = module.Dispatcher(
        registry, tmp_path / "state", client_factory=factory(complete(health, calls))
    )
    with module.locked(tmp_path / "state" / ("a" * 32 + "-1.lock")) as owns:
        assert owns
        assert dispatcher.dispatch(request) == 75
    assert not calls


def test_result_limit_leaves_no_partial_or_publishable_output(lab, tmp_path):
    module, registry, workers, request, health = lab
    dispatcher = module.Dispatcher(
        registry,
        tmp_path / "state",
        client_factory=factory(complete(health, [])),
        review=False,
        max_result_bytes=3,
    )
    with pytest.raises(ValueError, match="exceeds"):
        dispatcher.dispatch(request)
    assert not Path(request["result_path"]).exists()
    assert not list(Path(request["checkpoint_directory"]).glob("*.part"))


def test_same_id_changed_request_refused(lab, tmp_path):
    module, registry, workers, request, health = lab
    dispatcher = module.Dispatcher(
        registry, tmp_path / "state", client_factory=factory(complete(health, []))
    )
    assert dispatcher.dispatch(request) == 75
    changed = copy.deepcopy(request)
    changed["ai"]["model"] = "other-model"
    with pytest.raises(ValueError, match="identity"):
        dispatcher.dispatch(changed)


def test_health_probes_are_parallel(lab, tmp_path):
    module, registry, workers, request, health = lab
    barrier = threading.Barrier(6)

    def answer(r):
        assert r.url.path == "/health"
        barrier.wait(timeout=5)
        time.sleep(0.01)
        return httpx.Response(200, json={**health, "accepting": False})

    dispatcher = module.Dispatcher(
        registry, tmp_path / "state", client_factory=factory(answer)
    )
    assert dispatcher.dispatch(request) == 75


def test_footer_pipeline_uses_only_matching_workers(lab, tmp_path):
    module, registry, workers, request, health = lab
    del request["ai"]
    request.update(event="lettering.prepare", pipeline="footer-reuse")
    health["pipeline_fingerprint"] = "f" * 64
    calls, payloads = [], []
    inner = complete(health, calls)

    def answer(r):
        if r.url.path == "/health":
            return httpx.Response(
                200,
                json={
                    **health,
                    "capabilities": ["footer-reuse"]
                    if r.url.host == "node-3.example.test"
                    else ["translation"],
                },
            )
        if r.method == "POST":
            payloads.append(json.loads(r.content))
        return inner(r)

    dispatcher = module.Dispatcher(
        registry, tmp_path / "state", client_factory=factory(answer), review=False
    )
    assert dispatcher.dispatch(request) == 0
    assert {h for _, h, _ in calls} == {"node-3.example.test"}
    assert payloads[0]["pipeline"] == "footer-reuse"
    assert "ai" not in payloads[0] and "options" not in payloads[0]
    request["options"] = {"workers": 2}
    with pytest.raises(ValueError, match="configuration"):
        dispatcher.dispatch(request)


def test_cancellation_intent_survives_unavailable_worker_and_retries_delete(
    lab, tmp_path
):
    module, registry, workers, request, health = lab
    calls = []
    dispatcher = None
    available = False

    def answer(r):
        calls.append((r.method, r.url.path))
        if r.url.path == "/health":
            return httpx.Response(200, json=health)
        if not available:
            dispatcher.cancelled.set()
            raise httpx.ConnectError("fixture connection loss", request=r)
        if r.method == "DELETE":
            return httpx.Response(202, json={"status": "cancelled"})
        return httpx.Response(200, json={"status": "cancelled"})

    dispatcher = module.Dispatcher(
        registry, tmp_path / "state", client_factory=factory(answer), review=False
    )
    assert dispatcher.dispatch(request) == 75
    pin = tmp_path / "state" / ("a" * 32 + "-1.json")
    assert json.loads(pin.read_text())["cancel_requested"]
    available = True
    dispatcher.cancelled.clear()
    calls.clear()
    assert dispatcher.dispatch(request) == 1
    assert calls[0][0] == "DELETE"
    assert not Path(request["result_path"]).exists()


@pytest.mark.parametrize(
    "change",
    [
        {"capacity": 0},
        {"capacity": True},
        {"active_jobs": "0"},
        {"active_jobs": -1},
        {"capabilities": None},
        {"capabilities": "translation"},
        {"capabilities": [{}]},
        {"resources": None},
        {"resources": {"disk_free_bytes": "1000000"}},
        {"resources": {"disk_free_bytes": None}},
        {"performance": None},
        {"source_languages": "ja"},
    ],
)
def test_malformed_health_does_not_prevent_other_workers(lab, tmp_path, change):
    module, registry, workers, request, health = lab
    calls = []
    inner = complete(health, calls)

    def answer(r):
        if r.url.path == "/health" and r.url.host == "node-0.example.test":
            return httpx.Response(200, json={**health, **change})
        return inner(r)

    d = module.Dispatcher(
        registry, tmp_path / "state", client_factory=factory(answer), review=False
    )
    assert d.dispatch(request) == 0
    assert next(h for m, h, _ in calls if m == "POST") == "node-1.example.test"


@pytest.mark.parametrize("memory", [None, "large", True, -1])
def test_unknown_or_invalid_required_memory_is_ineligible(lab, tmp_path, memory):
    module, registry, workers, request, health = lab
    for worker in workers:
        worker["minimum_memory_bytes"] = 100
    registry.write_text(json.dumps({"workers": workers}))
    health["resources"]["memory_available_bytes"] = memory
    d = module.Dispatcher(
        registry, tmp_path / "state", client_factory=factory(complete(health, []))
    )
    assert d.dispatch(request) == 75


def test_same_size_source_change_blocks_cached_handoff(lab, tmp_path):
    module, registry, workers, request, health = lab
    d = module.Dispatcher(
        registry, tmp_path / "state", client_factory=factory(complete(health, []))
    )
    assert d.dispatch(request) == 75
    receipt = tmp_path / "state" / ("a" * 32 + "-1.review.json")
    receipt.with_name(receipt.name.replace(".review.", ".approved.")).write_text(
        receipt.read_text()
    )
    source = Path(request["source_path"])
    source.write_bytes(b"X" * source.stat().st_size)
    with pytest.raises(ValueError, match="changed"):
        d.dispatch(request)
    assert not Path(request["result_path"]).exists()


def test_unchanged_source_is_not_rehashed_on_resume(lab, tmp_path, monkeypatch):
    module, registry, workers, request, health = lab
    d = module.Dispatcher(
        registry, tmp_path / "state", client_factory=factory(complete(health, []))
    )
    assert d.dispatch(request) == 75
    real_digest = module.digest
    calls = []

    def counted(path):
        calls.append(Path(path))
        return real_digest(path)

    monkeypatch.setattr(module, "digest", counted)
    assert d.dispatch(request) == 75
    assert Path(request["source_path"]) not in calls


def test_cancel_between_download_and_rename_withholds_result(
    lab, tmp_path, monkeypatch
):
    module, registry, workers, request, health = lab
    d = module.Dispatcher(
        registry,
        tmp_path / "state",
        client_factory=factory(complete(health, [])),
        review=False,
    )

    def approving(*args):
        d.cancelled.set()
        return True

    monkeypatch.setattr(d, "approved", approving)
    assert d.dispatch(request) == 75
    pin = tmp_path / "state" / ("a" * 32 + "-1.json")
    assert json.loads(pin.read_text())["cancel_requested"]
    assert not Path(request["result_path"]).exists()
    assert not Path(request["result_path"]).with_suffix(".part").exists()


def test_resumed_completion_does_not_train_full_job_throughput(lab, tmp_path):
    module, registry, workers, request, health = lab
    submitted = False
    finished = False

    def answer(r):
        nonlocal submitted
        if r.url.path == "/health":
            return httpx.Response(200, json=health)
        if r.url.path.endswith("/result"):
            return httpx.Response(200, content=b"fixture result")
        if r.method == "POST":
            submitted = True
        if r.method == "PUT":
            return httpx.Response(200)
        if not submitted:
            return httpx.Response(404)
        return httpx.Response(
            200, json={"status": "completed" if finished else "running"}
        )

    d = module.Dispatcher(
        registry,
        tmp_path / "state",
        client_factory=factory(answer),
        review=False,
        timeout=0,
    )
    assert d.dispatch(request) == 75
    finished = True
    assert d.dispatch(request) == 0
    assert not (tmp_path / "state" / "performance.json").exists()


def test_footer_runtime_change_blocks_cached_result(lab, tmp_path):
    module, registry, workers, request, health = lab
    request.pop("ai")
    request.update(event="lettering.prepare", pipeline="footer-reuse")
    health["capabilities"] = ["footer-reuse"]
    health["pipeline_fingerprint"] = "f" * 64
    d = module.Dispatcher(
        registry, tmp_path / "state", client_factory=factory(complete(health, []))
    )
    assert d.dispatch(request) == 75
    health["pipeline_fingerprint"] = "a" * 64
    with pytest.raises(ValueError, match="runtime changed"):
        d.dispatch(request)


def test_pending_review_reuses_unchanged_result_without_full_rehash(
    lab, tmp_path, monkeypatch
):
    module, registry, workers, request, health = lab
    d = module.Dispatcher(
        registry, tmp_path / "state", client_factory=factory(complete(health, []))
    )
    assert d.dispatch(request) == 75

    def forbidden(path):
        raise AssertionError("Unchanged review must not re-read the archive")

    monkeypatch.setattr(module, "digest", forbidden)
    assert d.dispatch(request) == 75


def test_cached_result_edit_cannot_reuse_approval(lab, tmp_path):
    module, registry, workers, request, health = lab
    d = module.Dispatcher(
        registry, tmp_path / "state", client_factory=factory(complete(health, []))
    )
    assert d.dispatch(request) == 75
    receipt = tmp_path / "state" / ("a" * 32 + "-1.review.json")
    receipt.with_name(receipt.name.replace(".review.", ".approved.")).write_text(
        receipt.read_text()
    )
    cached = Path(request["checkpoint_directory"]) / "dispatch-result.cbz"
    cached.write_bytes(b"X" * cached.stat().st_size)
    with pytest.raises(ValueError, match="changed"):
        d.dispatch(request)
    assert not Path(request["result_path"]).exists()


def test_mit_runtime_fingerprint_change_blocks_local_cached_handoff(lab, tmp_path):
    module, registry, workers, request, health = lab
    health["pipeline_fingerprint"] = "f" * 64
    d = module.Dispatcher(
        registry, tmp_path / "state", client_factory=factory(complete(health, []))
    )
    assert d.dispatch(request) == 75
    receipt = tmp_path / "state" / ("a" * 32 + "-1.review.json")
    receipt.with_name(receipt.name.replace(".review.", ".approved.")).write_text(
        receipt.read_text()
    )
    health["pipeline_fingerprint"] = "e" * 64
    with pytest.raises(ValueError, match="runtime changed"):
        d.dispatch(request)
    assert not Path(request["result_path"]).exists()


@pytest.mark.parametrize("field", ["ai", "vision", "options"])
@pytest.mark.parametrize("value", [{}, None, ""])
def test_footer_forbidden_fields_refused_even_when_empty(lab, tmp_path, field, value):
    module, registry, workers, request, health = lab
    request.pop("ai")
    request.update(event="lettering.prepare", pipeline="footer-reuse")
    request[field] = value
    calls = []
    d = module.Dispatcher(
        registry, tmp_path / "state", client_factory=factory(complete(health, calls))
    )
    with pytest.raises(ValueError, match="configuration"):
        d.dispatch(request)
    assert not calls
