#!/usr/bin/env python3
"""Optional capability-aware hook for administrator-enrolled HTTP processors.

Run all hook invocations against the same local state directory. No network
scanning, remote installation or library publication is performed here.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import re
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

import httpx

RETRY = 75
JOB = re.compile(r"^[a-f0-9]{32}$")
WORKER = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")
MAX_ARCHIVE_BYTES = 16 * 1024**3


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def source_signature(path):
    stat = path.stat()
    return [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns]


def verify_source(path, expected, previous=None):
    before = source_signature(path)
    if before == previous:
        return before
    if digest(path) != expected or source_signature(path) != before:
        raise ValueError("Source archive changed or digest mismatch")
    return before


def atomic_json(path, value):
    partial = path.with_suffix(".part")
    with open(partial, "w", opener=lambda p, f: os.open(p, f, 0o600)) as stream:
        json.dump(value, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    partial.replace(path)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@contextmanager
def locked(path):
    with open(path, "a", opener=lambda p, f: os.open(p, f, 0o600)) as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def positive(value, default):
    if isinstance(value, bool):
        return default
    try:
        value = float(value)
    except (ValueError, TypeError):
        return default
    return value if math.isfinite(value) and value > 0 else default


def endpoint(value):
    parts = urlsplit(value)
    if (
        parts.scheme not in {"https", "http"}
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
    ):
        raise ValueError("Invalid worker endpoint")
    return value.rstrip("/")


def registry(path):
    """Reload enrolment every invocation; only explicit endpoints are contacted."""
    content = json.loads(path.read_text())
    workers = content.get("workers")
    if not isinstance(workers, list) or len(workers) > 256:
        raise ValueError("Invalid worker registry")
    seen = set()
    result = []
    for item in workers:
        item = dict(item)
        name = item["id"]
        if not WORKER.fullmatch(name) or name in seen:
            raise ValueError("Duplicate or invalid worker ID")
        seen.add(name)
        item["url"] = endpoint(item["url"])
        slots = item.get("max_jobs", 1)
        if type(slots) is not int or not 1 <= slots <= 64:
            raise ValueError("Invalid worker capacity")
        item["max_jobs"] = slots
        token = Path(item["token_file"])
        item["token_file"] = token if token.is_absolute() else path.parent / token
        result.append(item)
    return result


def request_identity(request):
    public = {
        k: request[k] for k in ("source_sha256", "source_language", "target_language")
    }
    public["pipeline"] = request.get("pipeline", "translation")
    public["options"] = request.get("options", {})
    for kind in ("ai", "vision"):
        if request.get(kind):
            public[kind] = {k: request[kind][k] for k in ("base_url", "model")}
    return hashlib.sha256(json.dumps(public, sort_keys=True).encode()).hexdigest()


def admissible(worker, health, request, size):
    if health.get("protocol_version") != 1 or health.get("accepting") is not True:
        return False
    capacity, active = health.get("capacity"), health.get("active_jobs")
    if (
        type(capacity) is not int
        or not 1 <= capacity <= 1024
        or type(active) is not int
        or not 0 <= active <= 1048576
    ):
        return False
    if active >= min(worker["max_jobs"], capacity):
        return False
    required = {request.get("pipeline", "translation")} | set(
        worker.get("required_capabilities", [])
    )
    capabilities = health.get("capabilities")
    if not isinstance(capabilities, list) or not all(
        isinstance(v, str) for v in capabilities
    ):
        return False
    if not isinstance(health.get("performance", {}), dict):
        return False
    if request.get("pipeline") == "footer-reuse" and not re.fullmatch(
        "[a-f0-9]{64}", str(health.get("pipeline_fingerprint", ""))
    ):
        return False
    if not required.issubset(set(capabilities)):
        return False
    for field, language in (
        ("source_languages", "source_language"),
        ("target_languages", "target_language"),
    ):
        if health.get(field) is not None:
            if (
                not isinstance(health[field], list)
                or request[language] not in health[field]
            ):
                return False
    resources = health.get("resources", {})
    if not isinstance(resources, dict):
        return False
    memory = worker.get("minimum_memory_bytes", 0)
    disk = max(worker.get("minimum_disk_bytes", 0), size * 3)
    available_memory = resources.get("memory_available_bytes")
    if memory and (type(available_memory) is not int or available_memory < memory):
        return False
    free_disk = resources.get("disk_free_bytes")
    if type(free_disk) is not int or free_disk < disk:
        return False
    if resources.get("on_battery") not in (True, False, None):
        return False
    if resources.get("on_battery") is True and not worker.get("allow_battery", False):
        return False
    return worker.get("enabled", True) is True


def estimate(worker, health, request, size, measured=None):
    """Compare total expected processing and I/O time, never device names."""
    performance = health.get("performance", {})
    pages = positive(request.get("page_count"), max(1, size / 1024**2))
    key = worker["id"] + ":" + request.get("pipeline", "translation")
    observed = (measured or {}).get(key, {})
    rate = positive(
        performance.get("pages_per_second"),
        positive(worker.get("pages_per_second"), 0.1),
    )
    if observed.get("seconds_per_byte"):
        work = observed["seconds_per_byte"] * size
    else:
        work = pages / rate
    transfer = positive(
        performance.get("transfer_bytes_per_second"),
        positive(worker.get("transfer_bytes_per_second"), 10 * 1024**2),
    )
    slots = min(worker["max_jobs"], health["capacity"])
    occupancy = health["active_jobs"] / slots
    return work * (1 + occupancy) + 2 * size / transfer


class Dispatcher:
    def __init__(
        self,
        registry_path,
        state,
        *,
        client_factory=httpx.Client,
        review=True,
        poll_interval=3,
        timeout=6 * 3600,
        max_result_bytes=MAX_ARCHIVE_BYTES,
    ):
        self.registry_path = Path(registry_path)
        self.state = Path(state)
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.client_factory = client_factory
        self.review = review
        self.poll_interval = poll_interval
        self.timeout = timeout
        self.max_result_bytes = max_result_bytes
        self.cancelled = threading.Event()

    def client(self, worker):
        token = worker["token_file"].read_text().strip()
        if not token:
            raise ValueError("Worker credential is missing")
        return self.client_factory(
            headers={"Authorization": "Bearer " + token},
            timeout=httpx.Timeout(30, connect=3),
            follow_redirects=False,
            trust_env=False,
        )

    def probe(self, worker):
        if worker.get("enabled", True) is not True:
            return None
        try:
            with self.client(worker) as client:
                result = client.get(worker["url"] + "/health", timeout=3)
                result.raise_for_status()
                health = result.json()
                return (worker, health) if isinstance(health, dict) else None
        except (OSError, ValueError, httpx.HTTPError):
            return None

    def dispatch(self, request):
        if (
            request.get("protocol_version") != 1
            or (request.get("event"), request.get("pipeline", "translation"))
            not in {
                ("translation.requested", "translation"),
                ("lettering.prepare", "footer-reuse"),
            }
            or not JOB.fullmatch(str(request.get("job_id", "")))
            or type(request.get("attempt")) is not int
            or not 0 <= request["attempt"] < 1_000_000
        ):
            raise ValueError("Invalid hook request")
        if request.get("pipeline") == "footer-reuse" and any(
            k in request for k in ("ai", "vision", "options")
        ):
            raise ValueError(
                "Footer worker configuration belongs to the enrolled worker"
            )
        job = f"{request['job_id']}-{request['attempt']}"
        source = Path(request["source_path"])
        if (
            source.is_symlink()
            or not source.is_file()
            or not 0 < source.stat().st_size <= MAX_ARCHIVE_BYTES
        ):
            raise ValueError("Invalid source archive")
        identity = request_identity(request)
        workers = registry(self.registry_path)
        with locked(self.state / f"{job}.lock") as owns:
            if not owns:
                return RETRY
            pin_file = self.state / f"{job}.json"
            if pin_file.exists():
                pin = json.loads(pin_file.read_text())
                if pin["identity"] != identity:
                    raise ValueError("Attempt identity changed")
                worker = next((w for w in workers if w["id"] == pin["worker"]), None)
                if (
                    worker is None
                    or hashlib.sha256(worker["url"].encode()).hexdigest()
                    != pin["endpoint"]
                ):
                    # A removed or repointed worker may still own the job.
                    return RETRY
                pin["source_signature"] = verify_source(
                    source, request["source_sha256"], pin.get("source_signature")
                )
                if pin.get("pipeline_fingerprint"):
                    observation = self.probe(worker)
                    if observation is None:
                        return RETRY
                    if observation[1].get("pipeline_fingerprint") != pin.get(
                        "pipeline_fingerprint"
                    ):
                        raise ValueError("Pinned worker runtime changed")
                with locked(
                    self.state / f"worker-{worker['id']}-{pin['slot']}.lock"
                ) as owns_slot:
                    if not owns_slot:
                        return RETRY
                    return self.run(worker, request, pin_file, pin)
            signature = verify_source(source, request["source_sha256"])
            with ThreadPoolExecutor(max_workers=min(8, max(1, len(workers)))) as pool:
                observations = [v for v in pool.map(self.probe, workers) if v]
            size = source.stat().st_size
            candidates = []
            for worker, health in observations:
                try:
                    if admissible(worker, health, request, size):
                        candidates.append((worker, health))
                except (TypeError, ValueError, AttributeError):
                    continue  # A malformed worker advertisement cannot block peers.
            measured = self.measurements()
            candidates.sort(
                key=lambda row: (estimate(*row, request, size, measured), row[0]["id"])
            )
            for worker, health in candidates:
                for slot in range(min(worker["max_jobs"], health["capacity"])):
                    with locked(
                        self.state / f"worker-{worker['id']}-{slot}.lock"
                    ) as owns_slot:
                        if not owns_slot:
                            continue
                        pin = {
                            "worker": worker["id"],
                            "endpoint": hashlib.sha256(
                                worker["url"].encode()
                            ).hexdigest(),
                            "identity": identity,
                            "job": job,
                            "slot": slot,
                            "source_signature": signature,
                            "pipeline_fingerprint": health.get("pipeline_fingerprint"),
                        }
                        atomic_json(
                            pin_file, pin
                        )  # durable before the first mutating HTTP request
                        return self.run(worker, request, pin_file, pin, fresh=True)
            return RETRY

    def measurements(self):
        try:
            return json.loads((self.state / "performance.json").read_text())
        except (OSError, ValueError):
            return {}

    def record(self, worker, request, source_bytes, elapsed):
        with locked(self.state / "performance.lock") as owns:
            if not owns:
                return
            data = self.measurements()
            key = worker["id"] + ":" + request.get("pipeline", "translation")
            old = data.get(key, {}).get("seconds_per_byte")
            rate = elapsed / max(1, source_bytes)
            data[key] = {
                "seconds_per_byte": rate if old is None else old * 0.75 + rate * 0.25
            }
            atomic_json(self.state / "performance.json", data)

    def approved(self, request, result_hash):
        expected = {
            "job_id": request["job_id"],
            "attempt": request["attempt"],
            "source_sha256": request["source_sha256"],
            "result_sha256": result_hash,
        }
        name = f"{request['job_id']}-{request['attempt']}"
        atomic_json(self.state / f"{name}.review.json", expected)
        if not self.review:
            return True
        try:
            return (
                json.loads((self.state / f"{name}.approved.json").read_text())
                == expected
            )
        except (OSError, ValueError):
            return False

    def finish(self, request, cached, pin_file, pin):
        result_hash = pin["result_sha256"]
        if not self.approved(request, result_hash):
            return RETRY
        result = Path(request["result_path"])
        result.parent.mkdir(parents=True, exist_ok=True)
        partial = result.with_suffix(".part")
        try:
            copied_hash = hashlib.sha256()
            with cached.open("rb") as src, partial.open("wb") as target:
                for block in iter(lambda: src.read(1024 * 1024), b""):
                    copied_hash.update(block)
                    target.write(block)
                target.flush()
                os.fsync(target.fileno())
            if copied_hash.hexdigest() != result_hash:
                raise ValueError("Reviewed result changed during handoff")
            verify_source(
                Path(request["source_path"]),
                request["source_sha256"],
                pin.get("source_signature"),
            )
            if self.cancelled.is_set():
                pin["cancel_requested"] = True
                atomic_json(pin_file, pin)
                return RETRY
            partial.replace(result)
        finally:
            partial.unlink(missing_ok=True)
        return 0

    def run(self, worker, request, pin_file, pin, *, fresh=False):
        checkpoint = Path(request["checkpoint_directory"])
        checkpoint.mkdir(parents=True, exist_ok=True, mode=0o700)
        cached = checkpoint / "dispatch-result.cbz"
        if (
            not pin.get("cancel_requested")
            and not self.cancelled.is_set()
            and pin.get("result_sha256")
            and cached.is_file()
        ):
            verify_source(cached, pin["result_sha256"], pin.get("result_signature"))
            return self.finish(request, cached, pin_file, pin)
        start = time.monotonic()
        url = worker["url"] + "/jobs/" + pin["job"]
        source = Path(request["source_path"])
        try:
            with self.client(worker) as client:
                if pin.get("cancel_requested") or self.cancelled.is_set():
                    return self.cancel(client, url, pin_file, pin)
                response = client.get(url)
                if response.status_code == 404:
                    if self.cancelled.is_set():
                        return self.cancel(client, url, pin_file, pin)
                    # Same pinned worker only, including after an ambiguous upload or POST.
                    with source.open("rb") as stream:
                        response = client.put(
                            url + "/source",
                            content=iter(lambda: stream.read(1024 * 1024), b""),
                            headers={
                                "Content-Type": "application/zip",
                                "Content-Length": str(source.stat().st_size),
                            },
                        )
                    response.raise_for_status()
                    payload = {
                        k: request[k]
                        for k in (
                            "source_sha256",
                            "source_language",
                            "target_language",
                            "ai",
                            "vision",
                            "pipeline",
                            "options",
                        )
                        if k in request
                    }
                    if self.cancelled.is_set():
                        return self.cancel(client, url, pin_file, pin)
                    response = client.post(url, json=payload)
                while True:
                    if self.cancelled.is_set():
                        return self.cancel(client, url, pin_file, pin)
                    response.raise_for_status()
                    status = response.json()["status"]
                    if status == "completed":
                        break
                    if status in {"failed", "cancelled", "cancelling"}:
                        return 1
                    if status not in {"queued", "running"}:
                        raise ValueError("Invalid processor status")
                    if time.monotonic() - start >= self.timeout:
                        return RETRY
                    self.cancelled.wait(self.poll_interval)
                    response = client.get(url)
                partial = cached.with_suffix(".part")
                try:
                    with client.stream("GET", url + "/result") as response:
                        response.raise_for_status()
                        with partial.open("wb") as output:
                            total = 0
                            result_digest = hashlib.sha256()
                            for block in response.iter_bytes(1024 * 1024):
                                if self.cancelled.is_set():
                                    return self.cancel(client, url, pin_file, pin)
                                total += len(block)
                                if total > self.max_result_bytes:
                                    raise ValueError("Result archive exceeds limit")
                                result_digest.update(block)
                                output.write(block)
                            if not total:
                                raise ValueError("Empty processor result")
                            output.flush()
                            os.fsync(output.fileno())
                    partial.replace(cached)
                finally:
                    partial.unlink(missing_ok=True)
                pin["result_sha256"] = result_digest.hexdigest()
                pin["result_signature"] = source_signature(cached)
                atomic_json(pin_file, pin)
                if fresh:
                    self.record(
                        worker, request, source.stat().st_size, time.monotonic() - start
                    )
                return self.finish(request, cached, pin_file, pin)
        except httpx.HTTPStatusError as error:
            return (
                RETRY
                if error.response.status_code in {408, 429, 503}
                or error.response.status_code >= 500
                else 1
            )
        except httpx.TransportError:
            if self.cancelled.is_set():
                pin["cancel_requested"] = True
                atomic_json(pin_file, pin)
            return RETRY

    def cancel(self, client, url, pin_file, pin):
        pin["cancel_requested"] = True
        atomic_json(pin_file, pin)
        try:
            client.delete(url, timeout=3).raise_for_status()
            response = client.get(url, timeout=3)
            if response.is_success and response.json().get("status") == "cancelled":
                return 1
        except httpx.HTTPError:
            pass  # Pin remains; never allow reassignment after uncertain cancellation.
        return RETRY


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument(
        "--no-review",
        action="store_true",
        help="Allow automatic handoff to Tankarr receipt validation",
    )
    args = parser.parse_args()
    dispatcher = Dispatcher(args.registry, args.state, review=not args.no_review)
    signal.signal(signal.SIGTERM, lambda *_: dispatcher.cancelled.set())
    signal.signal(signal.SIGINT, lambda *_: dispatcher.cancelled.set())
    try:
        request = json.loads(sys.stdin.buffer.read(64 * 1024 + 1))
        return dispatcher.dispatch(request)
    except (OSError, ValueError, KeyError, TypeError):
        # Neither provider text, credentials nor private endpoints reach logs.
        print(
            "Translation dispatcher configuration or result is invalid", file=sys.stderr
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
