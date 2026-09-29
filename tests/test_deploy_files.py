"""The deployment files next to the code stay consistent with it."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ElementTree
from pathlib import Path

from tankarr import __version__
from tankarr.config import Settings

ROOT = Path(__file__).resolve().parents[1]
IMAGE_VARIABLES = {"PUID", "PGID", "UMASK", "TZ"}


def _setting_variables() -> set[str]:
    return {f"TANKARR_{name.upper()}" for name in Settings.model_fields}


def test_the_helm_chart_deploys_this_version_by_default():
    chart = (ROOT / "contrib" / "helm" / "tankarr" / "Chart.yaml").read_text(
        encoding="utf-8"
    )
    match = re.search(r'^appVersion:\s*"?([^"\n]+)"?\s*$', chart, re.MULTILINE)
    assert match, "Chart.yaml has no appVersion"
    assert match.group(1) == __version__


def test_the_unraid_template_only_uses_existing_variables():
    template = ElementTree.parse(ROOT / "contrib" / "unraid" / "tankarr.xml")
    container = template.getroot()
    assert container.tag == "Container"
    assert container.findtext("Repository") == "ghcr.io/renzodef/tankarr:latest"
    variables = {
        config.get("Target")
        for config in container.findall("Config")
        if config.get("Type") == "Variable"
    }
    unknown = variables - _setting_variables() - IMAGE_VARIABLES
    assert not unknown, f"not a Tankarr setting: {sorted(unknown)}"
    ports = [
        config for config in container.findall("Config") if config.get("Type") == "Port"
    ]
    assert [port.get("Target") for port in ports] == ["8787"]
    paths = {
        config.get("Target"): config.get("Mode")
        for config in container.findall("Config")
        if config.get("Type") == "Path"
    }
    assert paths == {
        "/config": "rw",
        "/library": "rw",
        "/import": "ro",
        "/downloads": "ro",
        "/usenet": "ro",
    }


def test_the_entrypoint_never_leaves_the_application_as_root():
    script = (ROOT / "docker-entrypoint.sh").read_text(encoding="utf-8")
    assert script.startswith("#!/bin/sh\n")
    assert "setpriv --reuid=tankarr --regid=tankarr" in script
    assert 'umask "${UMASK:-022}"' in script
    for name in ("docker-compose.yml", ".env.sample"):
        text = (ROOT / name).read_text(encoding="utf-8")
        for variable in ("PUID", "PGID", "UMASK"):
            assert variable in text, f"{name} does not offer {variable}"
