#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

if [[ ! -x .venv/bin/python ]]; then
  python -m venv .venv
  .venv/bin/python -m pip install --upgrade pip
fi

if ! .venv/bin/python -c 'import pytest, ruff' >/dev/null 2>&1; then
  .venv/bin/python -m pip install -e '.[dev]'
fi

.venv/bin/python -m ruff check tankarr tests contrib
.venv/bin/python -m ruff format --check tankarr tests contrib
.venv/bin/python -m pytest "$@"
