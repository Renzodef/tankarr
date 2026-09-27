# Contributing to Tankarr

Thank you for helping. Bug reports, documentation fixes, ideas and code are all
welcome.

## Before you start

- **Questions and ideas**: open a [discussion](https://github.com/Renzodef/tankarr/discussions).
  For a larger change, agree on the approach there or in an issue before you
  write the code.
- **Bugs**: open an [issue](https://github.com/Renzodef/tankarr/issues/new/choose)
  with the version you run, what you expected and what happened.
- **Security problems**: never in a public issue. Follow the
  [security policy](SECURITY.md).

## How changes flow

Tankarr is developed in the maintainer's private working repository and
published here one version at a time: every commit on `main` is either a
release ("Release vX.Y.Z", one commit carrying that whole version) or a merged
pull request. Tags on `main` build the Docker images.

| Branch or tag | Purpose | Published as |
| --- | --- | --- |
| `main` | Released versions and merged pull requests | Nothing by itself |
| `vX.Y.Z` tag | A release | `ghcr.io/renzodef/tankarr:X.Y.Z`, `:X.Y`, `:latest` |
| Any other branch here | A change under test | `ghcr.io/renzodef/tankarr:branch-<name>` |

Nobody pushes directly to `main`, the maintainer included: it only changes
through pull requests that pass CI. To contribute:

1. Fork the repository and create a branch from `main`, named after the change
   (`fix/torrent-import-path`, `feature/kavita-scan`, `docs/reverse-proxy`).
2. Make focused commits with messages in the imperative mood that say what the
   change does, for example "Retry chapter downloads after a source timeout".
3. Open a pull request against `main` and fill in the template.
4. CI must be green: backend lint and tests, frontend tests and build, browser
   tests, the Docker image build, and dependency review.
5. The maintainer reviews and squash-merges it. Your change is on `main` at
   once, in the next release image after that, and it is carried into the
   maintainer's working repository so every later version keeps it.

## Development setup

The [development guide](docs/development.md) explains the setup in detail. In
short, with Python 3.12+ and Node.js 22:

```sh
python -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
npm ci --prefix frontend
./install-local-git-hooks.sh   # optional: run lint and tests before each push
```

Run the backend with `.venv/bin/tankarr` and the frontend dev server with
`npm run dev --prefix frontend`.

## Checks to run before opening a pull request

```sh
./run-tests.sh                      # ruff check, ruff format --check, pytest
npm test --prefix frontend          # frontend unit tests
npm run build --prefix frontend     # type check (tsc -b) and production build
```

- Add or update tests for the behaviour you change. Bug fixes should come with
  a test that fails without the fix.
- Python is formatted and linted with [Ruff](https://docs.astral.sh/ruff/)
  (`ruff format`, `ruff check`); TypeScript must pass `tsc -b`.
- Update the documentation in `docs/` when you change behaviour, settings or
  environment variables. `docs/configuration.md` must list every `TANKARR_*`
  variable; a test checks it.

## Keep Tankarr generic

Tankarr runs on many different setups. Pull requests must not contain values
from your own installation: IP addresses, host names, user names, home
directories, mount points, API keys, or paths that only exist on your machine.
Defaults are neutral (`./data/library`), examples use documentation values
(`nas.local`, `192.0.2.10`, `/path/to/comics`). Anything specific to one
installation belongs in its `.env` file or in the Settings page. Tankarr also
ships no download sources and no extension repository: do not add defaults
that point at specific sites.

## AI-assisted contributions

AI coding assistants are welcome as tools; you remain responsible for every
line you submit and must be able to explain it. Agents working in this
repository follow [AGENTS.md](AGENTS.md).

## Licence

Tankarr is released under the [GNU General Public License v3.0](LICENSE). By
contributing you agree that your contribution is licensed under the same terms.
