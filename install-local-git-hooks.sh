#!/usr/bin/env bash
# Opt-in: run lint and tests before every push (see .githooks/).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"
[[ -x .githooks/pre-push ]] || { printf 'Missing or non-executable hook: .githooks/pre-push\n' >&2; exit 1; }
git config --local core.hooksPath .githooks
printf 'Git hooks enabled (core.hooksPath=.githooks): lint and tests run before each push.\n'
