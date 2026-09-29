from __future__ import annotations

import re
from pathlib import Path

from tankarr.config import Settings

ROOT = Path(__file__).resolve().parents[1]
# Variables of docker-compose.yml and the image itself, not of Settings.
COMPOSE_ONLY = re.compile(r"TANKARR_(HOST_\w+|BIND_ADDRESS|IMAGE_TAG|BUILD_COMMIT)$")


def _setting_variables() -> set[str]:
    return {f"TANKARR_{name.upper()}" for name in Settings.model_fields}


def test_every_setting_is_documented_in_the_configuration_reference():
    document = (ROOT / "docs" / "configuration.md").read_text(encoding="utf-8")
    rows = re.findall(r"^\|\s*`(TANKARR_[A-Z0-9_]+)`", document, re.MULTILINE)
    missing = _setting_variables() - set(rows)
    assert not missing, f"document these in docs/configuration.md: {sorted(missing)}"
    duplicated = sorted({row for row in rows if rows.count(row) > 1})
    assert not duplicated


def test_the_configuration_reference_names_no_unknown_variable():
    document = (ROOT / "docs" / "configuration.md").read_text(encoding="utf-8")
    mentioned = set(re.findall(r"TANKARR_[A-Z0-9_]*[A-Z0-9]", document))
    unknown = {
        name
        for name in mentioned - _setting_variables()
        if not COMPOSE_ONLY.match(name)
    }
    assert not unknown, f"not a Tankarr setting: {sorted(unknown)}"


def test_the_example_files_only_use_existing_variables():
    for name in ("docker-compose.yml", ".env.sample"):
        text = (ROOT / name).read_text(encoding="utf-8")
        used = set(re.findall(r"TANKARR_[A-Z0-9_]*[A-Z0-9]", text))
        unknown = {
            variable
            for variable in used - _setting_variables()
            if not COMPOSE_ONLY.match(variable)
        }
        assert not unknown, f"{name} uses unknown variables: {sorted(unknown)}"


def test_every_image_the_readme_and_the_docs_show_exists():
    """A broken screenshot is the first thing a visitor would notice."""

    pages = [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md"))]
    missing: list[str] = []
    for page in pages:
        text = page.read_text(encoding="utf-8")
        sources = re.findall(r'<img[^>]+src="([^"]+)"', text)
        sources += re.findall(r"!\[[^\]]*\]\(([^)\s]+)", text)
        for source in sources:
            if source.startswith(("http://", "https://")):
                continue
            target = (page.parent / source).resolve()
            if not target.is_file():
                missing.append(f"{page.relative_to(ROOT)} -> {source}")
    assert not missing, f"images that do not exist: {missing}"
