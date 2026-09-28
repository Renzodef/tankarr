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


class Processor:
    def __init__(self, root, execute, *, workers=1, cancel=None):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.execute = execute
        self.cancel_callback = cancel
        self.executor = ThreadPoolExecutor(max_workers=workers)
        self.lock = threading.RLock()
        self.active = set()
        self.cancelling = set()
        for marker in self.root.glob("*/cancel.json"):
            self.cancel(marker.parent.name)

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
        if state["status"] in {"queued", "running"} and job_id not in self.active:
            # After a restart, ask Tankarr to resubmit the credential. It is
            # deliberately absent from all durable processor records.
            return None
        return state

    def submit(self, job_id, request):
        directory = self.directory(job_id)
        source = directory / "source.cbz"
        if not source.exists() or digest(source) != request.get("source_sha256"):
            raise ValueError("Source archive digest mismatch")
        for key in ("source_language", "target_language"):
            if not LANGUAGE_TAG.fullmatch(str(request.get(key, ""))):
                raise ValueError("Invalid language")
        ai = request.get("ai", {})
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
        identity = {
            key: request[key]
            for key in ("source_sha256", "source_language", "target_language")
        }
        identity["ai"] = {key: ai[key] for key in ("base_url", "model")}
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
            write_json(directory / "status.json", {"status": "running"})
            self.execute(directory, request)
            if not (directory / "result.cbz").is_file():
                raise ValueError("Processor produced no result")
            state = "completed"
        except Exception:  # noqa: BLE001 - engine errors and credentials never reach the HTTP response
            pass
        finally:
            request.get("ai", {}).pop("api_key", None)
            with self.lock:
                if (directory / "cancel.json").exists():
                    state = "cancelled"
                write_json(directory / "status.json", {"status": state})
                self.active.discard(job_id)


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

        def route(self):
            if token and not hmac.compare_digest(
                self.headers.get("Authorization", ""), "Bearer " + token
            ):
                self.reply(401, {"error": "Unauthorized"})
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
            length = int(self.headers.get("Content-Length", "0"))
            if action != "source" or not 0 < length <= MAX_ARCHIVE_BYTES:
                self.reply(400, {"error": "A bounded source archive is required"})
                return
            directory = processor.directory(job_id)
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            partial = directory / ("upload-" + uuid.uuid4().hex)
            try:
                with partial.open("wb") as handle:
                    while length:
                        data = self.rfile.read(min(length, 1024 * 1024))
                        if not data:
                            raise ValueError("Incomplete upload")
                        handle.write(data)
                        length -= len(data)
                with processor.lock:
                    source = directory / "source.cbz"
                    if source.exists():
                        if digest(source) != digest(partial):
                            self.reply(409, {"error": "Source is immutable"})
                            return
                    else:
                        partial.replace(source)
                self.reply(200, {"status": "uploaded"})
            finally:
                partial.unlink(missing_ok=True)

        def do_POST(self):
            route = self.route()
            if route is None:
                return
            job_id, action = route
            length = int(self.headers.get("Content-Length", "0"))
            if action or not 0 < length <= 64 * 1024:
                self.reply(400, {"error": "Invalid request"})
                return
            try:
                request = json.loads(self.rfile.read(length))
                response = processor.submit(job_id, request)
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
    args = parser.parse_args()
    runner = Path(__file__).with_name("translate_archive.py").resolve()
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
            str(
                (args.state / "checkpoints" / directory.name.split("-", 1)[0]).resolve()
            ),
        ]
        if args.config:
            command += ["--config", str(args.config.resolve())]
        if args.cpu:
            command.append("--cpu")
        # Engine imports resolve from the selected checkout even when the runner
        # lives in this repository; the engine environment supplies dependencies.
        env = {**os.environ, "PYTHONPATH": str(args.engine_root.resolve())}
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
        Processor(args.state, execute, cancel=cancel),
        args.host,
        args.port,
        os.environ.get("TRANSLATION_PROCESSOR_TOKEN", ""),
    )


if __name__ == "__main__":
    main()
