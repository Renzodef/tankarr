"""Test-wide defaults.

Outside the test suite Tankarr refuses to run without a login and creates one
on first start (see ``tankarr.auth.ensure_login``). Most tests build an app
without credentials on purpose, so they opt out here; the tests of that
behaviour set ``auth_required=True`` explicitly.
"""

from __future__ import annotations

import os

os.environ.setdefault("TANKARR_AUTH_REQUIRED", "false")
