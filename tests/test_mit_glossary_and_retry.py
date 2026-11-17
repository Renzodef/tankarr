from __future__ import annotations

import importlib.util
import json
import socket
import zipfile
from pathlib import Path

import httpx


def load(name):
    path = Path(__file__).resolve().parents[1] / "contrib" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def archive(path, series):
    with zipfile.ZipFile(path, "w") as target:
        target.writestr("ComicInfo.xml", f"<ComicInfo><Series>{series}</Series></ComicInfo>")
        target.writestr("001.png", b"")
    return path


def test_series_glossary_is_found_by_comicinfo_series(tmp_path, monkeypatch):
    module = load("translate_archive")
    folder = tmp_path / "glossaries"
    folder.mkdir()
    slug = module.glossary_slug("Scorpione Rosso")
    (folder / f"{slug}.json").write_text(
        json.dumps({"terms": [{"source": "韋德", "target": "Wade", "variants": ["章德"]}]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(module, "GLOSSARY_DIR", folder)
    glossary = module.series_glossary(archive(tmp_path / "a.cbz", "Scorpione Rosso"))
    assert glossary["terms"][0]["target"] == "Wade"
    assert len(glossary["_digest"]) == 64
    assert module.normalise_variants("章德來了", glossary) == "韋德來了"
    assert "Wade" in module.glossary_note(glossary)
    assert module.series_glossary(archive(tmp_path / "b.cbz", "Other series")) is None


def test_unreachable_vision_server_is_retryable(monkeypatch):
    module = load("translate_archive")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    monkeypatch.setattr("time.sleep", lambda _: None)
    request = {"vision": {"base_url": f"http://127.0.0.1:{port}/v1"}}
    assert module.vision_reachable(request) is False
    assert module.vision_reachable({}) is True
    assert module.transient_error(httpx.RemoteProtocolError("Server disconnected"))


def test_long_narrow_strip_is_laid_out_along_the_strip():
    module = load("vision_balloon")
    assert module.fit_strip("West of the city. Inside a luxurious villa...", 352, 30)
