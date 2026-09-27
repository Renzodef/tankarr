import hashlib
import json
import sys

import pytest
import respx
from httpx import Response

from tankarr.suwayomi_runtime import SuwayomiRuntime, SuwayomiRuntimeError
from tests.test_suwayomi_runtime import (
    JAR_BYTES,
    JAR_URL,
    RELEASES,
    SUMS_URL,
    fake_installed,
    python_command,
    release_payload,
)


def mock_release():
    digest = hashlib.sha256(JAR_BYTES).hexdigest()
    respx.get(RELEASES).mock(return_value=Response(200, json=release_payload()))
    respx.get(SUMS_URL).mock(
        return_value=Response(200, text=f"{digest}  Suwayomi-Server-v9.9.9.jar\n")
    )
    respx.get(JAR_URL).mock(return_value=Response(200, content=JAR_BYTES))


def runtime_fixture(tmp_path):
    root = tmp_path / "suwayomi"
    fake_installed(root)
    runtime = SuwayomiRuntime(
        root,
        releases_api=RELEASES,
        java_executable=sys.executable,
        command_builder=python_command("import time; time.sleep(30)"),
        ready_probe=lambda: True,
    )
    runtime.home.mkdir()
    (runtime.home / "database.fixture").write_text("old schema and data")
    return runtime


@pytest.mark.asyncio
@respx.mock
async def test_failed_health_restores_old_binary_and_data_and_restarts(
    tmp_path, monkeypatch
):
    mock_release()
    runtime = runtime_fixture(tmp_path)
    await runtime.start()

    async def unhealthy(*, timeout):
        if runtime.installed()["tag"] != "v0":
            (runtime.home / "database.fixture").write_text("new incompatible schema")
            return False
        return True

    monkeypatch.setattr(runtime, "wait_ready", unhealthy)
    try:
        with pytest.raises(
            SuwayomiRuntimeError, match="previous engine and data restored"
        ):
            await runtime.install()
        assert runtime.running
        assert runtime.installed()["tag"] == "v0"
        assert (runtime.home / "database.fixture").read_text() == "old schema and data"
        assert (
            next(runtime.root.glob(".failed-update-*/database.fixture")).read_text()
            == "new incompatible schema"
        )
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_successful_update_is_health_verified_before_snapshot_retirement(
    tmp_path,
):
    mock_release()
    runtime = runtime_fixture(tmp_path)
    try:
        await runtime.install()
        state = json.loads(runtime.state_path.read_text())
        assert state["health_verified"] is True
        assert "pending_update" not in state
        assert not list(runtime.root.glob(".rollback-home-*"))
        assert (runtime.server_dir / "fake.jar").exists()
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_restart_rolls_back_an_interrupted_attempt_without_running_new_binary(
    tmp_path,
):
    mock_release()
    runtime = runtime_fixture(tmp_path)
    await runtime.install(start=False)
    state = json.loads(runtime.state_path.read_text())
    state["pending_update"]["attempted"] = True
    runtime._write_state(state)
    (runtime.home / "database.fixture").write_text("partial migration")
    await runtime.aclose()
    restarted = SuwayomiRuntime(
        runtime.root,
        java_executable=sys.executable,
        command_builder=python_command("import time; time.sleep(30)"),
        ready_probe=lambda: True,
    )
    try:
        await restarted.start()
        assert restarted.installed()["tag"] == "v0"
        assert (
            restarted.home / "database.fixture"
        ).read_text() == "old schema and data"
    finally:
        await restarted.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_deferred_update_runs_health_check_when_first_started(tmp_path):
    mock_release()
    runtime = runtime_fixture(tmp_path)
    try:
        await runtime.install(start=False)
        assert not runtime.running
        assert runtime.installed()["health_verified"] is False
        await runtime.start()
        assert runtime.installed()["health_verified"] is True
        assert runtime.installed()["tag"] == "v9.9.9"
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("restored_home", [False, True])
@respx.mock
async def test_rollback_restart_survives_both_directory_rename_crash_windows(
    tmp_path, restored_home
):
    mock_release()
    runtime = runtime_fixture(tmp_path)
    await runtime.install(start=False)
    state = json.loads(runtime.state_path.read_text())
    state["pending_update"]["attempted"] = True
    runtime._write_state(state)
    pending = state["pending_update"]
    (runtime.home / "database.fixture").write_text("partly migrated data")
    runtime.home.rename(runtime.root / pending["failed_home"])
    if restored_home:
        (runtime.root / pending["backup"]).rename(runtime.home)
    try:
        await runtime.start()
        assert runtime.installed()["tag"] == "v0"
        assert (runtime.home / "database.fixture").read_text() == "old schema and data"
        assert (
            runtime.root / pending["failed_home"] / "database.fixture"
        ).read_text() == "partly migrated data"
    finally:
        await runtime.aclose()
