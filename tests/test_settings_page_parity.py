"""The Settings page and the settings store must agree on the editable keys.

A field the page offers but the store rejects makes every save on that tab
fail with 400 as soon as it is touched (qbittorrent_public_url and
sabnzbd_public_url did exactly that).
"""

from __future__ import annotations

import re
from pathlib import Path

from tankarr.config import Settings
from tankarr.settings_store import EDITABLE_SETTINGS

ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "frontend" / "src" / "pages" / "SettingsPage.tsx"


def _page_keys() -> set[str]:
    text = PAGE.read_text(encoding="utf-8")
    return set(re.findall(r"\bkey:\s*\"([a-z0-9_]+)\"", text))


def test_every_settings_page_key_is_accepted_by_the_store():
    missing = sorted(_page_keys() - set(EDITABLE_SETTINGS))
    assert not missing, f"the Settings page offers keys the store rejects: {missing}"


def test_every_editable_setting_exists_on_settings():
    unknown = sorted(set(EDITABLE_SETTINGS) - set(Settings.model_fields))
    assert not unknown, f"EDITABLE_SETTINGS names unknown settings: {unknown}"
