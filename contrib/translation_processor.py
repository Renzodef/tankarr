#!/usr/bin/env python3
"""Reference Tankarr translation processor; standard library HTTP/control plane.

The engine runs in its own Python environment. Deployment-specific dispatchers
can supply an execute(directory, request) callback instead of running locally.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import shutil
import signal
import subprocess
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

try:
    from .worker_resources import (
        ResourceProbe,
        admission_reasons,
        thread_environment,
        threads_per_worker,
    )
except ImportError:
    try:
        from contrib.worker_resources import (
            ResourceProbe,
            admission_reasons,
            thread_environment,
            threads_per_worker,
        )
    except ImportError:
        from worker_resources import (
            ResourceProbe,
            admission_reasons,
            thread_environment,
            threads_per_worker,
        )

JOB = re.compile(r"^[a-f0-9]{32}-\d{1,6}$")
LANGUAGE_TAG = re.compile(r"[a-z]{2,3}(?:-[a-z]{4})?(?:-(?:[a-z]{2}|[0-9]{3}))?")
MAX_ARCHIVE_BYTES = 16 * 1024**3


def digest(path):
    with Path(path).open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def write_json(path, value):
    partial = path.with_name(path.name + "." + uuid.uuid4().hex)
    with partial.open("w") as handle:
        partial.chmod(0o600)
        handle.write(json.dumps(value))
        handle.flush()
        os.fsync(handle.fileno())
    partial.replace(path)
    if os.name == "posix":
        with suppress(OSError):
            descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)


def stop_process(process):
    if process.poll() is not None:
        return
    with suppress(ProcessLookupError):
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
            process.wait()


def runtime_fingerprint(
    engine_root, engine_python, runner, config=None, *, threads=None, cpu=False
):
    """Identify executable code/config without saving secrets or hashing model weights.

    Engine weights and Python packages must remain pinned by the administrator.
    Untracked engine files are outside the tracked checkout identity.
    """
    value = hashlib.sha256(b"tankarr-mit-runtime-v1\0")
    value.update(f"threads:{threads}\0".encode())
    value.update(f"cpu:{bool(cpu)}\0".encode())
    runner = Path(runner)
    for label, path in (
        ("runner", runner),
        ("vision", runner.with_name("vision_balloon.py")),
        ("config", Path(config) if config is not None else None),
    ):
        value.update(label.encode() + b"\0")
        if path is not None:
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    value.update(block)
        value.update(b"\0")
    for arguments in (
        ("rev-parse", "HEAD"),
        ("diff", "--no-ext-diff", "--no-textconv", "--binary", "HEAD", "--"),
    ):
        # Local checkout inspection only. Never fetch or contact its remote.
        completed = subprocess.run(
            ["git", "-C", str(engine_root), *arguments],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=3,
            check=True,
        )
        if len(completed.stdout) > 16 * 1024**2:
            raise ValueError("Engine patch exceeds runtime identity limit")
        value.update(completed.stdout)
        value.update(b"\0")
    interpreter = Path(shutil.which(str(engine_python)) or engine_python).resolve()
    info = interpreter.stat()
    value.update(str(interpreter).encode())
    value.update(f"\0{info.st_size}:{info.st_mtime_ns}".encode())
    return value.hexdigest()


def checkpoint_directory(root, job_id):
    """Keep concurrent attempts apart; retries of one attempt reuse its pages.

    Pre-upgrade base-ID checkpoints remain on disk for operator retention, but
    are not adopted implicitly because their owning attempt cannot be proven.
    """
    if not JOB.fullmatch(job_id):
        raise ValueError("Invalid job ID")
    return Path(root) / "checkpoints" / job_id


class BusyError(Exception):
    """Temporary admission refusal, safe to retry using the same job identity."""


class Processor:
    def __init__(
        self,
        root,
        execute,
        *,
        workers=1,
        cancel=None,
        max_queue=0,
        capabilities=("translation",),
        resource_probe=None,
        admission=None,
        pipeline_fingerprint=None,
    ):
        if (
            type(workers) is not int
            or workers < 1
            or type(max_queue) is not int
            or max_queue < 0
        ):
            raise ValueError("Worker and queue capacities must be nonnegative integers")
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.execute = execute
        self.cancel_callback = cancel
        self.executor = ThreadPoolExecutor(max_workers=workers)
        self.lock = threading.RLock()
        self.workers = workers
        self.max_queue = max_queue
        self.capabilities = list(capabilities)
        self.pipeline_fingerprint = pipeline_fingerprint
        if "footer-reuse" in self.capabilities and not re.fullmatch(
            r"[a-zA-Z0-9_.:-]{1,128}", pipeline_fingerprint or ""
        ):
            raise ValueError("Footer workers require a versioned pipeline fingerprint")
        self.resource_probe = resource_probe or ResourceProbe(self.root)
        self.admission = dict(admission or {})
        self.source_hashes = {}
        self.running = set()
        self.active = set()
        self.cancelling = set()
        for marker in self.root.glob("*/cancel.json"):
            self.cancel(marker.parent.name)

    def health(self):
        resources = self.resource_probe()
        reasons = admission_reasons(resources, self.admission)
        with self.lock:
            if len(self.active) >= self.workers + self.max_queue:
                reasons.append("capacity")
            return {
                "protocol_version": 1,
                "accepting": not reasons,
                "capacity": self.workers,
                "active_jobs": len(self.active),
                "running_jobs": len(self.running),
                "queued_jobs": len(self.active - self.running),
                "queue_limit": self.max_queue,
                "capabilities": self.capabilities,
                "pipeline_fingerprint": self.pipeline_fingerprint,
                "resources": resources,
                "admission_reasons": reasons,
            }

    @staticmethod
    def source_signature(path):
        stat = path.stat()
        return (
            stat.st_dev,
            stat.st_ino,
            stat.st_size,
            stat.st_mtime_ns,
            stat.st_ctime_ns,
        )

    def source_digest(self, path):
        """Reuse a verified immutable upload; invalidate even same-size rewrites."""
        signature = self.source_signature(path)
        with self.lock:
            cached = self.source_hashes.get(str(path))
        if cached and cached[0] == signature:
            return cached[1]
        value = digest(path)
        if self.source_signature(path) != signature:
            raise ValueError("Source changed during verification")
        with self.lock:
            self._remember_source_hash(path, signature, value)
        return value

    def _remember_source_hash(self, path, signature, value):
        self.source_hashes.pop(str(path), None)
        self.source_hashes[str(path)] = (signature, value)
        while len(self.source_hashes) > 256:
            self.source_hashes.pop(next(iter(self.source_hashes)))

    def directory(self, job_id):
        if not JOB.fullmatch(job_id):
            raise ValueError("Invalid job ID")
        return self.root / job_id

    def status(self, job_id):
        path = self.directory(job_id) / "status.json"
        if not path.exists():
            return None
        state = json.loads(path.read_text())
        if (path.parent / "cancel.json").exists() and state["status"] not in {
            "cancelled",
            "cancelling",
        }:
            return {"status": "cancelled"}
        identity_file = path.parent / "request.json"
        if self.pipeline_fingerprint and identity_file.is_file():
            identity = json.loads(identity_file.read_text())
            if identity.get("pipeline_fingerprint") != self.pipeline_fingerprint:
                return {
                    "status": "failed",
                    "failure": {"code": "validation_failed", "retryable": False},
                }
        if state["status"] in {"queued", "running"} and job_id not in self.active:
            # After a restart, ask Tankarr to resubmit the credential. It is
            # deliberately absent from all durable processor records.
            return None
        return state

    def submit(self, job_id, request):
        if not isinstance(request, dict):
            raise ValueError("Request must be an object")
        pipeline = request.get("pipeline", "translation")
        if (
            pipeline not in {"translation", "footer-reuse"}
            or pipeline not in self.capabilities
        ):
            raise ValueError("Unsupported pipeline")
        if pipeline == "footer-reuse" and any(
            key in request for key in ("options", "ai", "vision")
        ):
            raise ValueError("Footer runtime options belong to worker configuration")
        directory = self.directory(job_id)
        source = directory / "source.cbz"
        if not source.exists() or self.source_digest(source) != request.get(
            "source_sha256"
        ):
            raise ValueError("Source archive digest mismatch")
        for key in ("source_language", "target_language"):
            if not LANGUAGE_TAG.fullmatch(str(request.get(key, ""))):
                raise ValueError("Invalid language")
        identity = {
            key: request[key]
            for key in ("source_sha256", "source_language", "target_language")
        }
        if pipeline != "translation" or "pipeline" in request:
            identity["pipeline"] = pipeline
        if self.pipeline_fingerprint:
            identity["pipeline_fingerprint"] = self.pipeline_fingerprint
        if pipeline == "translation":
            ai = request.get("ai", {})
            if not isinstance(ai, dict):
                raise ValueError("Invalid provider settings")
            parsed = urlsplit(ai.get("base_url", ""))
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or not ai.get("api_key")
                or not ai.get("model")
            ):
                raise ValueError("AI endpoint, model and credential are required")
            identity["ai"] = {key: ai[key] for key in ("base_url", "model")}
            if "vision" in request:
                vision = request["vision"]
                if not isinstance(vision, dict):
                    raise ValueError("Invalid vision provider settings")
                parsed = urlsplit(str(vision.get("base_url") or ""))
                if (
                    parsed.scheme not in {"http", "https"}
                    or not parsed.hostname
                    or parsed.username
                    or parsed.password
                    or parsed.query
                    or parsed.fragment
                    or not vision.get("model")
                ):
                    raise ValueError("Vision endpoint and model are required")
                identity["vision"] = {key: vision[key] for key in ("base_url", "model")}

        with self.lock:
            if (directory / "cancel.json").exists():
                return {"status": "cancelled"}
            identity_file = directory / "request.json"
            if (
                identity_file.exists()
                and json.loads(identity_file.read_text()) != identity
            ):
                raise ValueError("Job ID already belongs to another request")
            previous = self.status(job_id)
            if previous:
                return previous
            if not self.health()["accepting"]:
                raise BusyError("Processor cannot admit new work")
            write_json(identity_file, identity)
            self.active.add(job_id)
            write_json(directory / "status.json", {"status": "queued"})
            self.executor.submit(self._run, job_id, request)
            return {"status": "queued"}

    def cancel(self, job_id):
        directory = self.directory(job_id)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.lock:
            write_json(directory / "cancel.json", {"requested": True})
            write_json(directory / "status.json", {"status": "cancelling"})
            if job_id not in self.cancelling:
                self.cancelling.add(job_id)
                threading.Thread(
                    target=self._cancel, args=(job_id,), daemon=True
                ).start()
        return {"status": "cancelling"}

    def _cancel(self, job_id):
        try:
            if self.cancel_callback is not None:
                self.cancel_callback(self.directory(job_id))
            with self.lock:
                if job_id not in self.active:
                    write_json(
                        self.directory(job_id) / "status.json", {"status": "cancelled"}
                    )
        finally:
            with self.lock:
                self.cancelling.discard(job_id)

    def _run(self, job_id, request):
        directory = self.directory(job_id)
        state = "failed"
        try:
            with self.lock:
                if (directory / "cancel.json").exists():
                    return
                self.running.add(job_id)
                (directory / "result.failure.json").unlink(missing_ok=True)
                write_json(directory / "status.json", {"status": "running"})
            self.execute(directory, request)
            if not (directory / "result.cbz").is_file():
                raise ValueError("Processor produced no result")
            state = "completed"
        except Exception:  # noqa: BLE001 - engine errors and credentials never reach the HTTP response
            pass
        finally:
            request.get("ai", {}).pop("api_key", None)
            request.get("vision", {}).pop("api_key", None)
            with self.lock:
                if (directory / "cancel.json").exists():
                    state = "cancelled"
                status = {"status": state}
                if state == "failed":
                    try:
                        receipt = directory / "result.failure.json"
                        with receipt.open("r") as stream:
                            failure = json.loads(stream.read(1024))
                        if (
                            failure.get("code")
                            in {
                                "provider_unavailable",
                                "page_review_required",
                                "translation_failed",
                                "provider_interrupted",
                                "validation_failed",
                            }
                            and type(failure.get("retryable")) is bool
                        ):
                            status["failure"] = {
                                "code": failure["code"],
                                "retryable": failure["retryable"],
                            }
                    except (OSError, ValueError, TypeError, AttributeError):
                        pass  # An absent or malformed receipt must not hide the failure.
                write_json(directory / "status.json", status)
                self.active.discard(job_id)
                self.running.discard(job_id)


def handler_for(processor, token):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args):
            pass

        def reply(self, status, body):
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(data)
            self.close_connection = True

        def authorized(self):
            if token and not hmac.compare_digest(
                self.headers.get("Authorization", ""), "Bearer " + token
            ):
                self.reply(401, {"error": "Unauthorized"})
                return False
            return True

        def route(self):
            if not self.authorized():
                return None
            parts = urlsplit(self.path).path.strip("/").split("/")
            if (
                len(parts) not in {2, 3}
                or parts[0] != "jobs"
                or not JOB.fullmatch(parts[1])
            ):
                self.reply(404, {"error": "Not found"})
                return None
            return parts[1], parts[2] if len(parts) == 3 else ""

        def do_GET(self):
            if urlsplit(self.path).path == "/health":
                if self.authorized():
                    self.reply(200, processor.health())
                return
            route = self.route()
            if route is None:
                return
            job_id, action = route
            state = processor.status(job_id)
            if state is None:
                self.reply(404, {"error": "Not submitted"})
                return
            if action == "":
                self.reply(200, state)
            elif action == "result" and state["status"] == "completed":
                with (processor.directory(job_id) / "result.cbz").open("rb") as handle:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/zip")
                    self.send_header(
                        "Content-Length", str(os.fstat(handle.fileno()).st_size)
                    )
                    self.end_headers()
                    shutil.copyfileobj(handle, self.wfile, 1024 * 1024)
            else:
                self.reply(409, {"error": "Result not ready"})

        def do_PUT(self):
            route = self.route()
            if route is None:
                return
            job_id, action = route
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self.reply(400, {"error": "Invalid content length"})
                return
            if action != "source" or not 0 < length <= MAX_ARCHIVE_BYTES:
                self.reply(400, {"error": "A bounded source archive is required"})
                return
            directory = processor.directory(job_id)
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            if (
                not processor.health()["accepting"]
                and not (directory / "source.cbz").exists()
            ):
                self.reply(503, {"error": "Processor busy"})
                return
            partial = directory / ("upload-" + uuid.uuid4().hex)
            uploaded_hash = hashlib.sha256()
            try:
                with partial.open("wb") as handle:
                    while length:
                        data = self.rfile.read(min(length, 1024 * 1024))
                        if not data:
                            raise ValueError("Incomplete upload")
                        handle.write(data)
                        uploaded_hash.update(data)
                        length -= len(data)
                with processor.lock:
                    source = directory / "source.cbz"
                    if source.exists():
                        if processor.source_digest(source) != uploaded_hash.hexdigest():
                            self.reply(409, {"error": "Source is immutable"})
                            return
                    else:
                        partial.replace(source)
                        processor._remember_source_hash(
                            source,
                            processor.source_signature(source),
                            uploaded_hash.hexdigest(),
                        )
                self.reply(200, {"status": "uploaded"})
            except ValueError:
                self.reply(400, {"error": "Incomplete upload"})
            finally:
                partial.unlink(missing_ok=True)

        def do_POST(self):
            route = self.route()
            if route is None:
                return
            job_id, action = route
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self.reply(400, {"error": "Invalid content length"})
                return
            if action or not 0 < length <= 64 * 1024:
                self.reply(400, {"error": "Invalid request"})
                return
            try:
                request = json.loads(self.rfile.read(length))
                response = processor.submit(job_id, request)
            except BusyError:
                self.reply(503, {"error": "Processor busy"})
                return
            except (ValueError, KeyError, TypeError):
                self.reply(400, {"error": "Invalid translation request"})
                return
            self.reply(202, response)

        def do_DELETE(self):
            route = self.route()
            if route is None:
                return
            job_id, action = route
            if action:
                self.reply(404, {"error": "Not found"})
                return
            self.reply(202, processor.cancel(job_id))

    return Handler


def serve(processor, host, port, token):
    if host not in {"127.0.0.1", "::1", "localhost"} and not token:
        raise ValueError(
            "Set TRANSLATION_PROCESSOR_TOKEN before exposing the processor"
        )
    server = ThreadingHTTPServer((host, port), handler_for(processor, token))
    server.serve_forever()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8095)
    parser.add_argument(
        "--state", type=Path, default=Path("data/translation-processor")
    )
    parser.add_argument("--engine-python", required=True)
    parser.add_argument("--engine-root", type=Path, required=True)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--cpu", action="store_true")
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument(
        "--threads-per-worker",
        type=int,
        help="Engine CPU threads per job; default: available CPUs divided by workers",
    )
    parser.add_argument("--max-queue", type=int, default=0)
    parser.add_argument("--min-memory-mib", type=int, default=512)
    parser.add_argument("--min-disk-mib", type=int, default=1024)
    parser.add_argument("--max-load-per-cpu", type=float, default=1.5)
    parser.add_argument("--allow-battery", action="store_true")
    parser.add_argument("--min-battery-percent", type=int, default=30)
    args = parser.parse_args()
    if (
        args.workers < 1
        or (args.threads_per_worker is not None and args.threads_per_worker < 1)
        or args.max_queue < 0
        or args.min_memory_mib < 0
        or args.min_disk_mib < 0
        or args.max_load_per_cpu < 0
        or not 0 <= args.min_battery_percent <= 100
    ):
        parser.error(
            "Worker capacities and resource thresholds must be valid nonnegative values"
        )
    runner = Path(__file__).with_name("translate_archive.py").resolve()
    thread_budget = threads_per_worker(args.workers, args.threads_per_worker)
    processes = {}
    process_lock = threading.Lock()

    def execute(directory, request):
        command = [
            args.engine_python,
            str(runner),
            "--source",
            str((directory / "source.cbz").resolve()),
            "--output",
            str((directory / "result.cbz").resolve()),
            "--checkpoint-directory",
            str(checkpoint_directory(args.state, directory.name).resolve()),
            "--cpu-threads",
            str(thread_budget),
        ]
        if args.config:
            command += ["--config", str(args.config.resolve())]
        if args.cpu:
            command.append("--cpu")
        # Engine imports resolve from the selected checkout even when the runner
        # lives in this repository; the engine environment supplies dependencies.
        env = thread_environment(
            {**os.environ, "PYTHONPATH": str(args.engine_root.resolve())}, thread_budget
        )
        with process_lock:
            if (directory / "cancel.json").exists():
                return
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                cwd=args.engine_root,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=os.name == "posix",
            )
            processes[directory.name] = process
        try:
            process.communicate(json.dumps(request).encode(), timeout=6 * 3600)
            if process.returncode:
                raise ValueError("Engine failed")
        finally:
            stop_process(process)
            with process_lock:
                processes.pop(directory.name, None)

    def cancel(directory):
        with process_lock:
            process = processes.get(directory.name)
        if process is not None:
            stop_process(process)

    serve(
        Processor(
            args.state,
            execute,
            cancel=cancel,
            workers=args.workers,
            max_queue=args.max_queue,
            capabilities=("translation", "mit"),
            pipeline_fingerprint=runtime_fingerprint(
                args.engine_root,
                args.engine_python,
                runner,
                args.config,
                threads=thread_budget,
                cpu=args.cpu,
            ),
            admission={
                "min_memory_bytes": args.min_memory_mib * 1024**2,
                "min_disk_bytes": args.min_disk_mib * 1024**2,
                "max_load_per_cpu": args.max_load_per_cpu,
                "allow_battery": args.allow_battery,
                "min_battery_percent": args.min_battery_percent,
            },
        ),
        args.host,
        args.port,
        os.environ.get("TRANSLATION_PROCESSOR_TOKEN", ""),
    )


if __name__ == "__main__":
    main()
