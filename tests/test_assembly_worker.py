from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import stat
import struct
import subprocess
import sys
import zipfile
import zlib
from pathlib import Path

import pytest
from PIL import Image

SCRIPT = Path(__file__).resolve().parents[1] / "tools/assemble_volume.py"
SPEC = importlib.util.spec_from_file_location("standalone_assembly_worker", SCRIPT)
worker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(worker)


def image_bytes(format="PNG"):
    output = io.BytesIO()
    Image.new("RGB", (32, 24), "orange").save(output, format=format)
    return output.getvalue()


def assembly_input(tmp_path, entries=None):
    entries = entries or [("10.jpg", image_bytes("JPEG")), ("2.png", image_bytes())]
    source = tmp_path / "sources"
    source.mkdir()
    path = source / "000001.cbz"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload in entries:
            archive.writestr(name, payload)
    manifest = {
        "version": 1,
        "pages": len(entries),
        "expanded_bytes": sum(len(payload) for _, payload in entries),
        "comic_info": "<ComicInfo><Title>Fixture</Title></ComicInfo>",
        "sources": [
            {
                "id": "chapter-fixture",
                "file": path.name,
                "sha256": worker.checksum(path),
                "size": path.stat().st_size,
                "pages": len(entries),
            }
        ],
    }
    return manifest, source, tmp_path / "output.cbz", entries


def test_one_decompression_per_page_preserves_order_and_bytes(tmp_path, monkeypatch):
    manifest, sources, output, entries = assembly_input(tmp_path)
    opened = []
    original = zipfile.ZipFile.open

    def counted(archive, member, *args, **kwargs):
        if isinstance(archive.filename, int):
            opened.append(member.filename)
        return original(archive, member, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, "open", counted)
    report = worker.package(manifest, sources, output, reserve_bytes=0)
    assert opened == ["2.png", "10.jpg"]
    assert report["sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()
    with zipfile.ZipFile(output) as archive:
        assert archive.namelist() == ["ComicInfo.xml", "000001.png", "000002.jpg"]
        assert archive.read("000001.png") == entries[1][1]
        assert archive.read("000002.jpg") == entries[0][1]


def test_large_real_jpeg_header_spills_without_reopening_source(tmp_path, monkeypatch):
    jpeg = image_bytes("JPEG")
    # APP1 sections place the real geometry beyond the bounded header cache.
    segment = b"\xff\xe1" + struct.pack(">H", 60_002) + b"x" * 60_000
    payload = jpeg[:2] + segment * 20 + jpeg[2:]
    manifest, sources, output, _ = assembly_input(tmp_path, [("1.jpg", payload)])
    spool = worker.tempfile.TemporaryFile
    sizes = []

    def counted(*args, **kwargs):
        sizes.append(True)
        return spool(*args, **kwargs)

    monkeypatch.setattr(worker.tempfile, "TemporaryFile", counted)
    worker.package(manifest, sources, output, reserve_bytes=0)
    assert sizes == [True]
    with zipfile.ZipFile(output) as archive:
        assert archive.read("000001.jpg") == payload
    assert sorted(x.name for x in tmp_path.iterdir()) == ["output.cbz", "sources"]


@pytest.mark.parametrize("dimensions", [(7000, 6000), (10000, 10000)])
def test_real_image_geometry_limits_remain_enforced(tmp_path, dimensions):
    png = image_bytes()
    ihdr = struct.pack(">II", *dimensions) + png[24:29]
    payload = png[:16] + ihdr + struct.pack(">I", zlib.crc32(b"IHDR" + ihdr)) + png[33:]
    manifest, sources, output, _ = assembly_input(tmp_path, [("1.png", payload)])
    with pytest.raises((ValueError, Image.DecompressionBombWarning)):
        worker.package(manifest, sources, output, reserve_bytes=0)
    assert not output.exists()
    assert not list(tmp_path.glob(".assemble-*"))


def test_opaque_codec_is_copied_without_modification(tmp_path):
    payload = b"opaque unsupported image codec"
    manifest, sources, output, _ = assembly_input(tmp_path, [("1.avif", payload)])
    worker.package(manifest, sources, output, reserve_bytes=0)
    with zipfile.ZipFile(output) as archive:
        assert archive.read("000001.avif") == payload


def test_source_mutation_during_copy_never_publishes(tmp_path, monkeypatch):
    manifest, sources, output, _ = assembly_input(tmp_path)
    original = worker._copy_page

    def mutate(reader, writer, scratch, page_size):
        original(reader, writer, scratch, page_size)
        with (sources / "000001.cbz").open("ab") as changed:
            changed.write(b"changed during packaging")

    monkeypatch.setattr(worker, "_copy_page", mutate)
    with pytest.raises(ValueError, match="changed while packaging"):
        worker.package(manifest, sources, output, reserve_bytes=0)
    assert not output.exists()


def test_source_replacement_with_symlink_after_preflight_is_not_followed(
    tmp_path, monkeypatch
):
    manifest, sources, output, _ = assembly_input(tmp_path)
    original = worker.preflight

    def replace(*args, **kwargs):
        result = original(*args, **kwargs)
        path = sources / "000001.cbz"
        path.rename(sources / "original.cbz")
        path.symlink_to("original.cbz")
        return result

    monkeypatch.setattr(worker, "preflight", replace)
    with pytest.raises(OSError):
        worker.package(manifest, sources, output, reserve_bytes=0)
    assert not output.exists()


def test_concurrent_destination_is_never_overwritten(tmp_path, monkeypatch):
    manifest, sources, output, _ = assembly_input(tmp_path)
    original = worker.os.link

    def competing(source, destination):
        destination.write_bytes(b"other successful worker")
        original(source, destination)

    monkeypatch.setattr(worker.os, "link", competing)
    with pytest.raises(FileExistsError):
        worker.package(manifest, sources, output, reserve_bytes=0)
    assert output.read_bytes() == b"other successful worker"


def test_success_waits_for_directory_fsync(tmp_path, monkeypatch):
    manifest, sources, output, _ = assembly_input(tmp_path)
    synced = []
    original = worker.os.fsync

    def record(descriptor):
        synced.append(stat.S_ISDIR(os.fstat(descriptor).st_mode))
        original(descriptor)

    monkeypatch.setattr(worker.os, "fsync", record)
    worker.package(manifest, sources, output, reserve_bytes=0)
    assert synced == [False, True]


def test_directory_fsync_failure_returns_no_success_receipt(tmp_path, monkeypatch):
    manifest, sources, output, _ = assembly_input(tmp_path)
    original = worker.os.fsync

    def fail_directory(descriptor):
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError("fixture directory sync failure")
        original(descriptor)

    monkeypatch.setattr(worker.os, "fsync", fail_directory)
    with pytest.raises(OSError, match="directory sync failure"):
        worker.package(manifest, sources, output, reserve_bytes=0)
    # The uncertain output is retained for reconciliation, never overwritten.
    with zipfile.ZipFile(output) as archive:
        assert archive.testzip() is None


def test_preflight_is_read_only_and_budgets_one_spilled_page(tmp_path):
    manifest, sources, output, entries = assembly_input(tmp_path)
    output = tmp_path / "not-created" / output.name
    result = worker.preflight(manifest, sources, output, reserve_bytes=0)
    assert not output.parent.exists()
    assert result["required_free_bytes"] == (
        manifest["expanded_bytes"]
        + max(len(payload) for _, payload in entries)
        + len(manifest["comic_info"].encode())
        + len(entries) * 1024
    )
    assert result["ready"] is True


@pytest.mark.parametrize(
    "policy", [{"reserve_bytes": -1}, {"max_pages": 0}, {"max_expanded_bytes": 1}]
)
def test_invalid_resource_policy_is_rejected(tmp_path, policy):
    manifest, sources, output, _ = assembly_input(tmp_path)
    with pytest.raises(ValueError):
        worker.package(manifest, sources, output, **policy)
    assert not output.exists()


def test_configurable_page_policy_is_enforced_before_work(tmp_path):
    manifest, sources, output, _ = assembly_input(tmp_path)
    with pytest.raises(ValueError, match="page budget"):
        worker.preflight(manifest, sources, output, max_pages=1)
    result = worker.preflight(manifest, sources, output, max_pages=2)
    assert result["pages"] == 2


def test_capabilities_and_preflight_cli(tmp_path):
    manifest, sources, output, _ = assembly_input(tmp_path)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest))
    capabilities = subprocess.run(
        [sys.executable, str(SCRIPT), "--capabilities", "--scratch-dir", str(tmp_path)],
        text=True,
        capture_output=True,
        check=True,
    )
    report = json.loads(capabilities.stdout)
    assert report["manifest_versions"] == [1]
    assert report["cpu_count"] >= 1
    assert report["policy"]["reserve_bytes"] == 512 * 1024**2
    assert "durable-output" in report["features"]
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--preflight",
            str(manifest_path),
            str(sources),
            str(output),
            "--reserve-gib",
            "0",
        ],
        text=True,
        capture_output=True,
        check=True,
    )
    assert json.loads(result.stdout)["ready"] is True
    assert not output.exists()


@pytest.mark.parametrize("reserve", ["-0.5", "nan", "inf"])
def test_cli_rejects_invalid_reserve(reserve):
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--capabilities", "--reserve-gib", reserve],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode != 0
    assert not result.stdout
