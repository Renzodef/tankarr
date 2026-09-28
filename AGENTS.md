# Working in this repository as an AI agent (Codex, Claude, others)

Tankarr is a generic, self-hosted manga monitor published as open source. Anyone
may run it, on any host. These rules keep it that way, keep `main` releasable,
and keep two agents from stepping on each other.

## Which repository is this?

Look at `git remote get-url origin`.

- `…/tankarr.git` is the **public repository**: `main` holds the published
  versions and merged pull requests, changes arrive only through pull requests,
  and releases are tags. The rules below apply as written.
- The maintainer develops in a **private working repository**
  (`…/tankarr-dev.git`) and publishes each version here as one commit. There,
  `AGENTS.local.md` (never committed) describes the local workflow and takes
  precedence over "How changes reach users" below; everything else applies
  there too.

## The product stays generic

- No installation-specific values in tracked files: no LAN addresses, host
  names, user names, mount points, home directories or API keys. Defaults are
  neutral (`./data/library`); examples use `nas.local`, `192.0.2.x`
  (documentation addresses) and `/path/to/comics`. Real values live in `.env`
  and the Settings page.
- No private pipelines. Machine translation queues, personal download scripts,
  scripts for one person's NAS: they live outside this repository. If Tankarr
  needs a hook for such a thing, it is a generic feature, off by default.
- Tankarr ships no download sources and no extension repository: the operator
  configures them. Do not add defaults that point at specific sites. The
  built-in integrations with public catalogues and libraries (MangaBaka,
  AniList, archive.org and the like) are documented, generic features with an
  on/off switch, not exceptions to this rule.
- Notes for a specific installation go in `AGENTS.local.md`; hand-offs between
  agents in `HANDOFF-*.md`. Both are git-ignored: never commit them.
- Before committing, search the staged diff for your own installation's
  details, for example
  `git diff --cached | grep -inE '192\.168\.|/home/|/mnt/|\.ts\.net'`.

## How changes reach users

| Where | Who writes it | What it publishes |
| --- | --- | --- |
| `main` | Pull requests only: contributors' changes and the maintainer's release commits | Nothing by itself |
| `vX.Y.Z` tag on `main` | The maintainer | `ghcr.io/renzodef/tankarr:X.Y.Z`, `:X.Y`, `:latest` and a GitHub release |
| Any other branch pushed here | Whoever pushes it | `ghcr.io/renzodef/tankarr:branch-<name>`, to try the change on a real installation |

- **Never push to `main`**, never force-push, never delete a branch you did not
  create. `main` is protected; a direct push is rejected anyway.
- Work in a branch created from the latest `main`:
  `git fetch origin main && git switch -c fix/<topic> origin/main`.
- Push the branch and open a pull request against `main`:
  `git push -u origin fix/<topic>` then `gh pr create --base main --fill`.
  Fill in the template; say what you tested with real test counts.
- A pull request is merged only when CI is green and the maintainer has
  reviewed it. Do not merge pull requests you did not open unless asked.
- Never create tags or GitHub releases unless the maintainer asks for a
  release; then follow `docs/releases.md` exactly.
- Installations run published images. Never deploy an image built from a
  working tree unless the installation's `AGENTS.local.md` says how.

## Before opening a pull request

- Backend: `ruff check` and `ruff format` clean, and the tests for what you
  touched green: `.venv/bin/python -m pytest tests/test_<module>.py`.
- Frontend: `npm run build --prefix frontend` (runs `tsc -b`) for any change to
  TypeScript; `npx tsc --noEmit -p .` does not see errors across project
  references.
- New or changed settings: document every `TANKARR_*` variable in
  `docs/configuration.md` (a test enforces it) and update
  `docker-compose.yml`/`.env.sample` when users need it.
- Report real test counts ("N passed"), never a summary you did not read. Do not
  pipe pytest into `tail` when you rely on its exit status.

## Security rules

- Never weaken authentication, the cross-site request check, the login
  throttle or the security headers (`tankarr/auth.py`) to make a feature or a
  test work. Tests that need an open instance set `auth_required=False`.
- Secrets never reach logs, job messages or API responses: an error that may
  quote a URL goes through `tankarr.redaction.redact_secrets`, and a secret
  sent in a query string is reported by status code only.
- Tests use obvious placeholders for secrets (`fixture-password`,
  `0123456789abcdef...`), never a real key.

## Small hosts

- One heavy job at a time: the full `pytest` suite, Playwright/Chromium,
  `npm run build` and a Docker build all compete for memory. Check free memory
  before starting one and wait if another is running.
- Never start browser tests while the application container is restarting.
- While developing, run targeted tests; CI runs the full suite on every pull
  request.

## Shared working tree

- Prefer your own branch in your own worktree
  (`git worktree add ../tankarr-<topic> -b fix/<topic> origin/main`) so
  another agent's uncommitted work is never in your way.
- Never `git stash`, `git checkout -- .`, `git reset --hard` or `git add -A`
  over files you did not change: another agent may have uncommitted hunks in
  them. Before committing a shared file check `git diff -U0` and stage only
  your own hunks.
- Commit early and small, with green tests. Keep pull requests focused on one
  change.
