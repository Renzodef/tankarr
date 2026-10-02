from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from pathlib import Path

import pytest
import respx
from httpx import Response

from tankarr.suwayomi_runtime import (
    MAX_CONSECUTIVE_CRASHES,
    PINNED_RELEASE,
    RESTART_BACKOFF_CAP_SECONDS,
    PinnedRelease,
    SuwayomiRuntime,
    SuwayomiRuntimeError,
    jvm_arguments,
    parse_checksums,
    release_order,
    render_server_conf,
    select_release_assets,
    server_settings,
)

RELEASES = "https://api.github.test/releases/latest"
STORE = "https://example.com/extensions/index.min.json"
JAR_URL = "https://github.test/download/Suwayomi-Server-v9.9.9.jar"
SUMS_URL = "https://github.test/download/Checksums.sha256"
JAR_BYTES = b"PK\x03\x04 fake suwayomi jar"


def release_payload() -> dict:
    return {
        "tag_name": "v9.9.9",
        "published_at": "2026-08-01T00:00:00Z",
        "assets": [
            {
                "name": "Suwayomi-Server-v9.9.9-linux-x64.tar.gz",
                "browser_download_url": "x",
            },
            {"name": "Checksums.sha256", "browser_download_url": SUMS_URL},
            {
                "name": "Suwayomi-Server-v9.9.9.jar",
                "browser_download_url": JAR_URL,
                "size": 20,
            },
        ],
    }


def test_parse_checksums_and_asset_selection():
    sums = parse_checksums(
        "abc\n"
        f"{'a' * 64}  Suwayomi-Server-v9.9.9.jar\n"
        f"{'b' * 64} *Suwayomi-Server-v9.9.9-windows-x64.zip\n"
    )
    assert sums == {
        "Suwayomi-Server-v9.9.9.jar": "a" * 64,
        "Suwayomi-Server-v9.9.9-windows-x64.zip": "b" * 64,
    }
    assets = select_release_assets(release_payload())
    assert assets.tag == "v9.9.9"
    assert assets.jar_name == "Suwayomi-Server-v9.9.9.jar"
    assert assets.checksums_url == SUMS_URL
    with pytest.raises(SuwayomiRuntimeError):
        select_release_assets({"tag_name": "v1", "assets": []})


def test_jvm_arguments_are_loopback_only_headless_and_bounded():
    argv = jvm_arguments(
        jar=Path("/x/server.jar"), home=Path("/x/home"), port=4567, heap_mb=384
    )
    joined = " ".join(argv)
    assert argv[0] == "java" and argv[-2:] == ["-jar", "/x/server.jar"]
    assert "-Xmx384m" in argv and "-XX:+ExitOnOutOfMemoryError" in argv
    assert "-XX:MaxHeapFreeRatio=50" in argv
    assert "-XX:-ShrinkHeapInSteps" in argv
    assert "server.ip=127.0.0.1" in joined and "server.authMode=none" in joined
    assert (
        "server.webUIEnabled=false" in joined and "server.kcefEnabled=false" in joined
    )
    assert "server.globalUpdateInterval=0" in joined
    assert "server.flareSolverrEnabled=false" in joined
    assert "extensionStores" not in joined  # lists cannot ride on -D flags
    assert "-Duser.home=/x/home" in argv

    exposed = " ".join(
        jvm_arguments(
            jar=Path("/x/server.jar"),
            home=Path("/x/home"),
            port=4567,
            heap_mb=384,
            credentials=("fixture-user", "s3cret"),
        )
    )
    assert "server.ip=0.0.0.0" in exposed and "server.authMode=basic_auth" in exposed
    # Any user of the host can read a process's command line: the login is
    # only ever written to the owner-only server.conf.
    assert "fixture-user" not in exposed and "s3cret" not in exposed
    assert "server.webUIEnabled=true" in exposed


@pytest.mark.asyncio
@respx.mock
async def test_install_verifies_checksum_and_records_state(tmp_path: Path):
    digest = hashlib.sha256(JAR_BYTES).hexdigest()
    respx.get(RELEASES).mock(return_value=Response(200, json=release_payload()))
    respx.get(SUMS_URL).mock(
        return_value=Response(200, text=f"{digest}  Suwayomi-Server-v9.9.9.jar\n")
    )
    respx.get(JAR_URL).mock(return_value=Response(200, content=JAR_BYTES))
    runtime = SuwayomiRuntime(
        tmp_path / "suwayomi", releases_api=RELEASES, pinned_release=None
    )
    assert runtime.status()["installed"] is False

    result = await runtime.install(start=False)

    assert result["installed"] is True and result["updated"] is True
    assert result["version"] == "v9.9.9"
    jar = tmp_path / "suwayomi" / "server" / "Suwayomi-Server-v9.9.9.jar"
    assert jar.read_bytes() == JAR_BYTES
    state = json.loads((tmp_path / "suwayomi" / "server" / "current.json").read_text())
    assert state["sha256"] == digest
    # Same tag again: nothing downloaded.
    again = await runtime.install(start=False)
    assert again["updated"] is False
    await runtime.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_install_rejects_a_jar_whose_checksum_does_not_match(tmp_path: Path):
    respx.get(RELEASES).mock(return_value=Response(200, json=release_payload()))
    respx.get(SUMS_URL).mock(
        return_value=Response(200, text=f"{'0' * 64}  Suwayomi-Server-v9.9.9.jar\n")
    )
    respx.get(JAR_URL).mock(return_value=Response(200, content=JAR_BYTES))
    runtime = SuwayomiRuntime(
        tmp_path / "suwayomi", releases_api=RELEASES, pinned_release=None
    )

    with pytest.raises(SuwayomiRuntimeError, match="SHA-256 mismatch"):
        await runtime.install(start=False)

    assert runtime.status()["installed"] is False
    assert not list((tmp_path / "suwayomi" / "server").glob("*.jar"))
    await runtime.aclose()


def fake_installed(root: Path) -> None:
    server = root / "server"
    server.mkdir(parents=True)
    (server / "fake.jar").write_bytes(b"jar")
    (server / "current.json").write_text(json.dumps({"tag": "v0", "jar": "fake.jar"}))


def python_command(code: str):
    def build(**_: object) -> list[str]:
        return ["java", "-c", code]

    return build


@pytest.mark.asyncio
async def test_start_wait_ready_and_stop_supervise_the_process(tmp_path: Path):
    root = tmp_path / "suwayomi"
    fake_installed(root)
    runtime = SuwayomiRuntime(
        root,
        java_executable=sys.executable,
        command_builder=python_command("import time; time.sleep(30)"),
        ready_probe=lambda: True,
    )
    assert runtime.status()["installed"] is True

    status = await runtime.start()
    assert status["running"] is True and status["pid"]
    assert await runtime.wait_ready(timeout=5) is True
    assert runtime.status()["ready"] is True
    assert (root / "suwayomi.log").exists()

    stopped = await runtime.stop()
    assert stopped["running"] is False and stopped["ready"] is False
    await runtime.aclose()


@pytest.mark.asyncio
async def test_supervisor_keeps_restarting_the_engine_and_never_gives_up(
    tmp_path: Path,
):
    """The engine is the only way to reach a source: a run of crashes slows the
    retries to a ceiling, it never ends them, or the library goes blind."""

    root = tmp_path / "suwayomi"
    fake_installed(root)
    sleeps: list[float] = []

    async def no_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) >= MAX_CONSECUTIVE_CRASHES + 6:
            runtime._stopping = True  # only the caller ends the supervision

    runtime = SuwayomiRuntime(
        root,
        java_executable=sys.executable,
        command_builder=python_command("import sys; sys.exit(3)"),
        ready_probe=lambda: False,
        sleep=no_sleep,
    )
    await runtime.start()
    assert runtime._supervisor is not None
    await asyncio.wait_for(runtime._supervisor, 30)

    status = runtime.status()
    assert status["last_exit_code"] == 3
    assert status["consecutive_crashes"] > MAX_CONSECUTIVE_CRASHES
    assert "giving up" not in status["last_error"]
    assert "still retrying" in status["last_error"]
    assert sleeps[:3] == [5.0, 10.0, 20.0]  # it backs off
    assert sleeps[-1] == RESTART_BACKOFF_CAP_SECONDS  # and settles at the ceiling
    await runtime.aclose()


def test_render_server_conf_rewrites_managed_keys_and_appends_missing_ones():
    existing = (
        "# Network\n"
        'server.ip = "0.0.0.0" # default: "0.0.0.0"\n'
        "server.port = 4567 # default: 4567 ; range: [1, 65535]\n"
        "server.extensionStores = [] # default: [] ; List of extension store index URLs\n"
        "server.authMode = NONE # default: NONE ; options: NONE, BASIC_AUTH\n"
        "server.kcefEnabled = true # default: true\n"
        "server.opdsItemsPerPage = 100 # untouched\n"
    )
    managed = server_settings(
        port=4567,
        credentials=("fixture-user", "s3cret"),
        extension_stores=["https://example.com/extensions/index.min.json"],
    )
    rendered = render_server_conf(existing, managed)
    lines = rendered.splitlines()
    assert 'server.ip = "0.0.0.0" # default: "0.0.0.0"' in lines
    assert (
        'server.extensionStores = ["https://example.com/extensions/index.min.json"]'
        " # default: [] ; List of extension store index URLs"
    ) in lines
    assert (
        "server.authMode = BASIC_AUTH # default: NONE ; options: NONE, BASIC_AUTH"
        in lines
    )
    assert "server.kcefEnabled = false # default: true" in lines
    assert "server.opdsItemsPerPage = 100 # untouched" in lines
    assert (
        'server.authUsername = "fixture-user"' in lines
        and 'server.authPassword = "s3cret"' in lines
    )
    assert "server.webUIEnabled = true" in lines
    # Idempotent.
    assert render_server_conf(rendered, managed) == rendered
    # Without a configured repository the server has no store at all.
    assert server_settings(port=4567)["extensionStores"] == []


# What Suwayomi writes on its first start: the list spans several lines and
# the comment sits after the closing bracket.
MULTILINE_CONF = (
    "server.port = 4567 # default: 4567\n"
    "server.extensionStores = [\n"
    '    "https://raw.githubusercontent.com/suwayomi/tachiyomi-extension/repo/index.min.json"\n'
    '    "https://raw.githubusercontent.com/Suwayomi/tachiyomi-extension/repo/repo.json"\n'
    "] # default: [] ; List of extension store index URLs\n"
    "server.authMode = NONE # default: NONE\n"
    "server.opdsItemsPerPage = 100 # untouched\n"
)
# What Tankarr 0.9.0 made of it: the first line replaced, the rest left behind
# (issue 23), so that Suwayomi could not start.
CORRUPTED_CONF = (
    "server.port = 4567 # default: 4567\n"
    'server.extensionStores = ["https://raw.githubusercontent.com/suwayomi/tachiyomi-extension/repo/index.min.json"]\n'
    '    "https://raw.githubusercontent.com/Suwayomi/tachiyomi-extension/repo/repo.json"\n'
    "] # default: [] ; List of extension store index URLs\n"
    "server.authMode = NONE # default: NONE\n"
    "server.opdsItemsPerPage = 100 # untouched\n"
)
EXPECTED_STORE_LINE = (
    f'server.extensionStores = ["{STORE}"]'
    " # default: [] ; List of extension store index URLs"
)


def _managed() -> dict:
    return server_settings(port=4567, extension_stores=[STORE])


def test_a_multiline_extension_list_is_replaced_as_one_value():
    rendered = render_server_conf(MULTILINE_CONF, _managed())
    lines = rendered.splitlines()
    assert lines.count(EXPECTED_STORE_LINE) == 1
    # Nothing of the old list is left behind, and the neighbours are intact.
    assert not any("tachiyomi-extension" in line for line in lines)
    assert "]" not in [line.strip() for line in lines]
    assert "server.opdsItemsPerPage = 100 # untouched" in lines
    assert "server.authMode = NONE # default: NONE" in lines
    assert render_server_conf(rendered, _managed()) == rendered


def test_a_file_corrupted_by_an_earlier_version_is_repaired_on_the_next_start():
    rendered = render_server_conf(CORRUPTED_CONF, _managed())
    lines = rendered.splitlines()
    assert lines.count(EXPECTED_STORE_LINE) == 1
    assert not any("tachiyomi-extension" in line for line in lines)
    assert lines == render_server_conf(MULTILINE_CONF, _managed()).splitlines()
    assert render_server_conf(rendered, _managed()) == rendered


def test_a_list_that_never_closes_does_not_swallow_the_next_setting():
    broken = (
        "server.extensionStores = [\n"
        '    "https://example.com/old.json"\n'
        "server.authMode = NONE # default: NONE\n"
    )
    lines = render_server_conf(broken, _managed()).splitlines()
    assert "server.authMode = NONE # default: NONE" in lines
    assert f'server.extensionStores = ["{STORE}"]' in lines


def test_a_url_with_a_fragment_is_not_taken_for_a_comment():
    fragment = "https://example.com/index.json#stable"
    rendered = render_server_conf(
        'server.extensionStores = ["https://example.com/old.json#x"] # default: []\n',
        server_settings(port=4567, extension_stores=[fragment]),
    )
    assert rendered.splitlines()[0] == (
        f'server.extensionStores = ["{fragment}"] # default: []'
    )


def test_the_extension_repository_is_the_operators_choice(tmp_path: Path):
    runtime = SuwayomiRuntime(tmp_path / "suwayomi", java_executable=sys.executable)
    assert runtime.extension_store is None
    assert runtime.status()["extension_store"] is None
    assert runtime.configure(extension_store=STORE) is True  # needs a restart
    assert runtime.configure(extension_store=STORE) is False
    assert runtime.status()["extension_store"] == STORE
    fake_installed(tmp_path / "suwayomi")
    assert f'server.extensionStores = ["{STORE}"]' in (
        runtime.write_server_conf().read_text()
    )
    assert runtime.configure(extension_store="") is True
    assert "server.extensionStores = []" in runtime.write_server_conf().read_text()


@pytest.mark.asyncio
async def test_start_writes_server_conf_before_launching(tmp_path: Path):
    root = tmp_path / "suwayomi"
    fake_installed(root)
    runtime = SuwayomiRuntime(
        root,
        java_executable=sys.executable,
        command_builder=python_command("import time; time.sleep(30)"),
        ready_probe=lambda: True,
    )
    await runtime.start()
    conf = runtime.server_conf_path.read_text()
    assert 'server.ip = "127.0.0.1"' in conf and "server.authMode = NONE" in conf
    assert "server.webUIEnabled = false" in conf
    await runtime.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_check_update_reports_the_latest_release_against_the_installed_one(
    tmp_path: Path,
):
    root = tmp_path / "suwayomi"
    fake_installed(root)  # tag v0
    respx.get(RELEASES).mock(return_value=Response(200, json=release_payload()))
    runtime = SuwayomiRuntime(root, releases_api=RELEASES, pinned_release=None)
    check = await runtime.check_update()
    assert check == {
        "installed": "v0",
        "latest": "v9.9.9",
        "update_available": True,
        "checked_at": check["checked_at"],
    }
    assert runtime.status()["update_available"] is True
    assert runtime.status()["latest_version"] == "v9.9.9"
    await runtime.aclose()


@pytest.mark.asyncio
async def test_the_login_stays_in_an_owner_only_conf_and_out_of_the_jvm_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = tmp_path / "suwayomi"
    fake_installed(root)
    monkeypatch.setenv("TANKARR_PROWLARR_API_KEY", "prowlarr-secret-sentinel")
    captured = tmp_path / "environment.json"
    runtime = SuwayomiRuntime(
        root,
        java_executable=sys.executable,
        command_builder=python_command(
            "import json, os, sys, time; "
            f"json.dump(dict(os.environ), open({str(captured)!r}, 'w')); "
            "time.sleep(30)"
        ),
        ready_probe=lambda: True,
        credentials=("fixture-user", "s3cret"),
    )
    await runtime.start()
    try:
        for _ in range(100):
            if captured.exists() and captured.stat().st_size:
                break
            await asyncio.sleep(0.05)
        environment = json.loads(captured.read_text())
    finally:
        await runtime.aclose()

    assert not any(name.startswith("TANKARR_") for name in environment)
    conf = runtime.server_conf_path
    assert 'server.authPassword = "s3cret"' in conf.read_text()
    assert conf.stat().st_mode & 0o777 == 0o600
    assert conf.parent.stat().st_mode & 0o777 == 0o700


# --- The pinned release (issue #4) -----------------------------------------

PIN = PinnedRelease(tag="v1.2.3", sha256=hashlib.sha256(JAR_BYTES).hexdigest())
DOWNLOADS = "https://github.test/releases/download"
PIN_JAR_URL = f"{DOWNLOADS}/v1.2.3/Suwayomi-Server-v1.2.3.jar"
PIN_SUMS_URL = f"{DOWNLOADS}/v1.2.3/Checksums.sha256"


def pinned_runtime(tmp_path: Path, pin: PinnedRelease = PIN) -> SuwayomiRuntime:
    return SuwayomiRuntime(
        tmp_path / "suwayomi",
        releases_api=RELEASES,
        release_downloads=DOWNLOADS,
        pinned_release=pin,
    )


@pytest.mark.asyncio
@respx.mock
async def test_install_uses_the_pinned_release_without_asking_github_for_latest(
    tmp_path: Path,
):
    latest = respx.get(RELEASES).mock(
        return_value=Response(200, json=release_payload())
    )
    respx.get(PIN_SUMS_URL).mock(
        return_value=Response(200, text=f"{PIN.sha256}  Suwayomi-Server-v1.2.3.jar\n")
    )
    respx.get(PIN_JAR_URL).mock(return_value=Response(200, content=JAR_BYTES))
    runtime = pinned_runtime(tmp_path)

    result = await runtime.install(start=False)

    assert result["updated"] is True and result["version"] == "v1.2.3"
    assert result["pinned_version"] == "v1.2.3"
    assert latest.called is False
    await runtime.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_a_published_checksum_that_disagrees_with_the_pin_installs_nothing(
    tmp_path: Path,
):
    respx.get(PIN_SUMS_URL).mock(
        return_value=Response(200, text=f"{'b' * 64}  Suwayomi-Server-v1.2.3.jar\n")
    )
    jar = respx.get(PIN_JAR_URL).mock(return_value=Response(200, content=JAR_BYTES))
    runtime = pinned_runtime(tmp_path)

    with pytest.raises(SuwayomiRuntimeError, match="pinned"):
        await runtime.install(start=False)

    assert jar.called is False
    assert runtime.status()["installed"] is False
    await runtime.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_the_pin_never_downgrades_a_newer_installed_server(tmp_path: Path):
    root = tmp_path / "suwayomi"
    server = root / "server"
    server.mkdir(parents=True)
    (server / "Suwayomi-Server-v9.9.9.jar").write_bytes(JAR_BYTES)
    (server / "current.json").write_text(
        json.dumps({"tag": "v9.9.9", "jar": "Suwayomi-Server-v9.9.9.jar"})
    )
    sums = respx.get(PIN_SUMS_URL).mock(return_value=Response(200, text=""))
    runtime = pinned_runtime(tmp_path)

    result = await runtime.install(start=False)

    assert result["updated"] is False and result["version"] == "v9.9.9"
    assert sums.called is False
    await runtime.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_the_latest_channel_upgrades_past_the_pin_with_the_published_checksum(
    tmp_path: Path,
):
    digest = hashlib.sha256(JAR_BYTES).hexdigest()
    respx.get(RELEASES).mock(return_value=Response(200, json=release_payload()))
    respx.get(SUMS_URL).mock(
        return_value=Response(200, text=f"{digest}  Suwayomi-Server-v9.9.9.jar\n")
    )
    respx.get(JAR_URL).mock(return_value=Response(200, content=JAR_BYTES))
    runtime = pinned_runtime(tmp_path)

    result = await runtime.install(start=False, channel="latest")

    assert result["updated"] is True and result["version"] == "v9.9.9"
    await runtime.aclose()


def test_release_order_compares_upstream_tags():
    assert release_order("v2.4.2366") > release_order("v2.4.1900")
    assert release_order("v2.4.2366") < release_order("v2.5.1")
    assert release_order("") == ()


def test_the_shipped_pin_is_a_real_release_digest():
    assert PINNED_RELEASE.tag.startswith("v")
    assert len(PINNED_RELEASE.sha256) == 64
    assert PINNED_RELEASE.jar_name == f"Suwayomi-Server-{PINNED_RELEASE.tag}.jar"
