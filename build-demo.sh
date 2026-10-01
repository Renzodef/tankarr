#!/usr/bin/env bash
# Build the online demo: the real interface in front of a recording of the
# fictional library (tests/demo_snapshot.py), as one static folder that any
# web host can serve. The docs site publishes it at
# https://renzodef.github.io/tankarr/demo/.
#
#   ./build-demo.sh                          -> frontend/dist-demo, for /tankarr/demo/
#   TANKARR_DEMO_DATASET=real ./build-demo.sh -> on real works (what the site publishes)
#   TANKARR_DEMO_BASE=/demo/ ./build-demo.sh  -> for another host path
#
# Needs the Python environment with Tankarr installed (TANKARR_PYTHON, default
# .venv/bin/python), Node with the frontend dependencies, and Playwright's
# Chromium (npx playwright install chromium, or PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

PYTHON="${TANKARR_PYTHON:-.venv/bin/python}"
if [[ ! -x "$PYTHON" ]] && ! command -v "$PYTHON" >/dev/null 2>&1; then
  PYTHON=python3
fi
PORT="${TANKARR_DEMO_PORT:-18880}"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/tankarr-demo.XXXXXX")"
SERVER=""
cleanup() {
  if [[ -n "$SERVER" ]]; then kill "$SERVER" 2>/dev/null || true; wait "$SERVER" 2>/dev/null || true; fi
  rm -rf "$WORK"
}
trap cleanup EXIT

# The recorder drives the production interface served by the test server.
if [[ ! -f frontend/dist/index.html ]]; then
  npm run build --prefix frontend
fi

# TANKARR_DEMO_DATASET=real adds well-known works by their MangaBaka identity
# through Tankarr's own code path (tests/demo_real.py, needs network); the
# default is the fictional, offline library of tests/demo_snapshot.py.
case "${TANKARR_DEMO_DATASET:-fictional}" in
  real) "$PYTHON" tests/demo_real.py "$WORK/demo" ;;
  fictional) "$PYTHON" tests/demo_snapshot.py "$WORK/demo" ;;
  *) echo "TANKARR_DEMO_DATASET must be real or fictional" >&2; exit 2 ;;
esac
"$PYTHON" tests/browser_server.py \
  --snapshot "$WORK/demo/tankarr.sqlite3" \
  --artwork-root "$WORK/demo/artwork" \
  --library-root "$WORK/demo/library" \
  --temp-root "$WORK" \
  --port "$PORT" &
SERVER=$!
for attempt in $(seq 1 120); do
  if curl -fsS "http://127.0.0.1:$PORT/api/auth/status" >/dev/null 2>&1; then break; fi
  if ! kill -0 "$SERVER" 2>/dev/null; then echo "the test server exited" >&2; exit 1; fi
  if [[ "$attempt" == 120 ]]; then echo "the test server did not come up on port $PORT" >&2; exit 1; fi
  sleep 1
done

TANKARR_DEMO_URL="http://127.0.0.1:$PORT" npm run demo:record --prefix frontend
npm run demo:build --prefix frontend
