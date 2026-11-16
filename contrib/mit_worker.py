#!/usr/bin/env python3
"""Run one Tankarr job using an administrator-configured MIT checkout."""

from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

MIT_REPOSITORY = "https://github.com/zyddnys/manga-image-translator.git"


def engine_root() -> Path:
    root = Path(os.environ.get("TANKARR_MIT_ENGINE_ROOT") or "/mit-engine").resolve()
    if root.is_dir():
        return root
    revision = os.environ.get("TANKARR_MIT_ENGINE_REVISION", "")
    if not re.fullmatch(r"[a-f0-9]{40}", revision):
        raise ValueError("Provide a MIT checkout or a full upstream commit")
    root.parent.mkdir(parents=True, exist_ok=True)
    with root.with_suffix(".checkout.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if root.is_dir():
            return root
        staging = Path(tempfile.mkdtemp(prefix=".mit-checkout-", dir=root.parent))
        try:
            subprocess.run(["git", "init", str(staging)], check=True)
            subprocess.run(
                ["git", "fetch", "--depth", "1", MIT_REPOSITORY, revision],
                cwd=staging,
                check=True,
            )
            subprocess.run(
                ["git", "checkout", "--detach", "FETCH_HEAD"], cwd=staging, check=True
            )
            staging.rename(root)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
    return root


def engine_python(root: Path) -> Path:
    configured = os.environ.get("TANKARR_MIT_ENGINE_PYTHON")
    if configured:
        return Path(configured).absolute()
    venv = Path(os.environ["TANKARR_MIT_VENV"]).resolve()
    venv.parent.mkdir(parents=True, exist_ok=True)
    requirements = root / "requirements.txt"
    revision = subprocess.check_output(
        ["git", "-C", str(root), "rev-parse", "HEAD"], text=True
    ).strip()
    python = venv / "bin" / "python"
    ready = venv / ".tankarr-mit-revision"
    with venv.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if python.is_file() and ready.is_file() and ready.read_text() == revision:
            return python
        ready.unlink(missing_ok=True)
        setup_python = os.environ.get("TANKARR_MIT_SETUP_PYTHON") or sys.executable
        subprocess.run([setup_python, "-m", "venv", str(venv)], check=True)
        subprocess.run(
            [str(python), "-m", "pip", "install", "-r", str(requirements)],
            cwd=root,
            check=True,
        )
        ready.write_text(revision)
    return python


def main() -> int:
    request = json.loads(sys.stdin.buffer.read(64 * 1024))
    if (
        request.get("protocol_version") != 1
        or request.get("event") != "translation.requested"
    ):
        raise ValueError("Unsupported translation job")
    root = engine_root()
    python = engine_python(root)
    runner = Path(__file__).resolve().with_name("translate_archive.py")
    command = [
        str(python),
        str(runner),
        "--source",
        request["source_path"],
        "--output",
        request["result_path"],
        "--checkpoint-directory",
        request["checkpoint_directory"],
    ]
    if os.environ.get("TANKARR_MIT_GPU", "").lower() != "true":
        command.append("--cpu")
    if config := os.environ.get("TANKARR_MIT_CONFIG"):
        command.extend(["--config", str(Path(config).resolve())])
    env = {**os.environ, "PYTHONPATH": str(root)}
    return subprocess.run(
        command, cwd=root, env=env, input=json.dumps(request).encode(), check=False
    ).returncode


if __name__ == "__main__":
    raise SystemExit(main())
