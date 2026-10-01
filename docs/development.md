---
description: Set up a Tankarr development environment, run the backend, the frontend and the test suites, and find your way around the architecture and the main modules.
---

# Developing Tankarr

Tankarr is a Python application (FastAPI and SQLite) with a React and
TypeScript interface built with Vite. This page explains how to set up a
development environment, run the checks and find your way around the code.

Changes reach the repository through pull requests against `main`; releases are
tags on `main`. Create your branch from `main` and open the pull request against
it. The
[contributing guide](https://github.com/Renzodef/tankarr/blob/main/CONTRIBUTING.md)
describes the workflow in full.

## Requirements

- **Python 3.12 or newer.** CI and the Docker image use Python 3.13.
- **Node.js 22** with npm, for the frontend.

Optional tools, needed only by the features that use them:

| Tool | Used for |
| --- | --- |
| Java 25 runtime | The managed Suwayomi server, Tankarr's source engine. |
| Tesseract OCR, with data for the languages you need | Language checks on imported archives and the local translation fallback. |
| DejaVu Sans font | Lettering of the local translation fallback. |
| `bsdtar` (libarchive), or `unar` and `lsar` | CBR and RAR archives, RAR5 included. `bsdtar` is preferred: it is maintained in Debian main and receives security updates. `unar`, `unrar` and a RAR-capable 7-Zip work as fallbacks; `lsar` also provides the page index used to recognise an unchanged book. |
| Poppler (`pdfinfo`, `pdftoppm`) | PDF import. |

The Docker image includes all of them. Tests that need Tesseract or Poppler are
skipped when the tool is missing; CI installs Tesseract with English data and
DejaVu Sans.

## Set up a development environment

```sh
git clone https://github.com/Renzodef/tankarr.git
cd tankarr
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
npm ci --prefix frontend
```

The editable install provides the `tankarr` command in `.venv/bin` and the
development tools (pytest, respx and Ruff). Optionally, run
`./install-local-git-hooks.sh` once: it enables a pre-push hook that runs
`./run-tests.sh` before every push (skip it once with `git push --no-verify`).

## Run Tankarr locally

Tankarr reads its `TANKARR_*` variables from the environment and from a `.env`
file in the working directory; the [configuration reference](configuration.md)
lists them. `.env.sample` is written for Docker Compose and uses container
paths such as `/config` and `/library`, so do not copy it unchanged for a local
run. The defaults suit development: data in `./data` and the library in
`./data/library`, both ignored by Git. Give the development instance its own
login and keep it on the loopback interface, for example with this `.env`:

```sh
TANKARR_HOST=127.0.0.1
TANKARR_AUTH_USERNAME=admin
TANKARR_AUTH_PASSWORD=choose-a-password
```

Start the backend:

```sh
.venv/bin/tankarr
```

The API listens on port 8787. The interactive OpenAPI documentation is at
<http://localhost:8787/docs>, and `/api/health` answers without a login.

For the interface, either build it once so that the backend serves it at
<http://localhost:8787>:

```sh
npm run build --prefix frontend
```

or, while working on the frontend, run the Vite development server with hot
reload in a second terminal:

```sh
npm run dev --prefix frontend
```

Open <http://localhost:5173>. The development server proxies `/api` to the
backend on port 8787 and listens on all network interfaces.

## Tests and checks

Run the same checks as CI before opening a pull request:

```sh
./run-tests.sh                   # ruff check, ruff format --check, pytest
npm test --prefix frontend       # frontend unit tests (Vitest)
npm run i18n:check --prefix frontend  # every interface string has its translations
npm run build --prefix frontend  # type check (tsc -b) and production build
```

`./run-tests.sh` creates `.venv` and installs the development dependencies when
they are missing, runs `ruff check` and `ruff format --check` on `tankarr`,
`tests` and `contrib`, then runs pytest. Extra arguments go to pytest, so
`./run-tests.sh tests/test_naming.py` lints everything and then tests one
module. While iterating, run a module directly with
`.venv/bin/python -m pytest tests/test_naming.py`.

Python tests mock external HTTP while exercising the complete API, the
background worker, page downloads, CBZ packaging, hash-checked imports,
deletion recovery and the SQLite state. Frontend unit tests sit next to the
code as `*.test.ts` and `*.test.tsx` files.

The full test suite, the browser tests, a frontend build and a Docker build all
need a fair amount of memory. On a small machine, run them one at a time.

### Browser tests

Browser tests exercise the production build and the real API on a disposable
SQLite library (60 series, 6,000 releases). No real settings, books, download
workers or external services are used:

```sh
npm ci --prefix frontend
npm run build --prefix frontend
cd frontend
npx playwright install chromium
TANKARR_TEST_PYTHON=../.venv/bin/python npm run test:e2e
```

Set `PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH` (for example `/usr/bin/chromium`) to
use a Chromium installed on the system. CI runs these tests and keeps traces
and screenshots when they fail. [Performance](performance.md) describes what
the suite covers and how to benchmark a copy of your own catalogue.

### Documentation

This site is built with MkDocs and the Material theme from `docs/` and
`mkdocs.yml`. To preview it while you edit:

```sh
.venv/bin/python -m pip install -r docs/requirements.txt
.venv/bin/mkdocs serve
```

CI builds the site with `mkdocs build --strict`, so a broken link fails the
build. Every page starts with front matter holding a one-sentence
`description`.

### Translations

The interface is written in English and translated at run time: the English
text is the key, `frontend/src/i18n/locales/<code>.json` holds the
translations for one language, and anything missing falls back to English.
Wrap every string a user can see:

```tsx
t("Refresh view")                                 // plain text
t("Monitored: {mode}", { mode })                  // interpolation
tn(count, "{count} chapter", "{count} chapters")  // plurals (CLDR rules)
msg("Comics")                                     // a string kept in a data table; render it with t()
```

Keep whole sentences in one call rather than concatenating fragments, because
word order changes between languages. `npm run i18n:check --prefix frontend`
fails when a catalogue misses a string, keeps one the interface no longer
uses, or leaves one empty; `npm run i18n:check --prefix frontend -- --fix`
adds the missing keys (empty) and removes the unused ones. CI runs the check.
A plural string is an object with the CLDR forms the language needs (`one`,
`other`, and `few` or `many` where the language has them).

To add a language, create `frontend/src/i18n/locales/<code>.json` containing
`{}`, add the code to `LOCALES` and `loaders` in `frontend/src/i18n/index.tsx`,
run the check with `--fix` and translate every value. Catalogues are loaded
only when their language is chosen, so the English interface pays nothing. The
language is a browser preference (Settings → General → Interface language,
kept in that browser only); the server, its logs and the file names stay in
English.

## How the application works

Tankarr is an acquisition coordinator, not just a catalogue interface.
Catalogue identity and metadata describe a work; linked sources describe its
releases. Canonical numbering and chapter-to-volume maps connect those releases
to the language and monitoring policy chosen for the series. Wanted reconciles
what should exist with what is available, downloaded, pending or awaiting a
decision.

A persistent job queue then coordinates source downloads and the external
download clients (qBittorrent and SABnzbd). Imports check archive contents,
numbering, language evidence and page quality before publishing to the
library. File changes go through quarantine and relocation journals, so the
database and the filesystem can be reconciled after a crash. Reader
synchronisation follows publication; reader progress and ambiguous identities
are never overwritten by guesswork.

A fast screen is therefore only one part of correctness: a provider timeout
must not look like proof that a book does not exist, and retrying a job must
not lose a good copy or touch another application's files.

Everything runs in one Python process. FastAPI serves the API and the built
frontend; background tasks run the release monitor, the Wanted pass, the
download worker and the nightly maintenance; all state lives in one SQLite
database in the data directory. When enabled, Tankarr also supervises a managed
Suwayomi server, a Java process that runs the source extensions.

### Rules that changes must keep

- **No overwrites.** Library files are published with atomic no-overwrite
  operations: on Linux `renameat2(RENAME_NOREPLACE)`, with an atomic hard-link
  fallback. When neither is available, publication fails instead of falling
  back to "check, then replace".
- **New copy first.** A replacement publishes, hash-verifies and records the
  new file before it retires the old one through the deletion journal. A
  replacement never retires a file that another release, series or language
  still references.
- **Recoverable removal.** Retired files go to a recycle bin with a journal;
  partial files have unique names and are cleaned up on failure.
- **Bounded paths.** Names that come from outside (URL-decoded file names,
  download-client paths) cannot escape their directory, symlink destinations
  are rejected, and qBittorrent and SABnzbd jobs are only touched in Tankarr's
  own category.
- **Honest evidence.** A timeout or a partial provider error is never recorded
  as an empty search, and a release is only declared unobtainable with evidence
  from both the sources and the indexers.
- **Responsive server.** Blocking work such as reconciliation, archive handling
  and image decoding runs off the asynchronous request loop.
- **Schema changes.** Increment `SCHEMA_VERSION` in `tankarr/database.py`.
  Before migrating, Tankarr backs up the existing database, and a failed backup
  stops the upgrade.

## Code map

The backend lives in the `tankarr/` package. The most important modules:

| Area | Modules | Role |
| --- | --- | --- |
| Entry point and API | `__main__.py`, `app.py`, `auth.py`, `models.py` | The `tankarr` command starts Uvicorn with `create_app()`, which wires the FastAPI routes, middleware, background tasks and the built frontend. `auth.py` provides forms and Basic authentication and the restored-safe-mode guard. |
| Configuration | `config.py`, `settings_store.py` | `Settings` reads the `TANKARR_*` variables and `.env`; the settings store validates values edited in the interface, keeps overrides in the database and secrets in a private file. |
| Persistence | `database.py`, `backups.py`, `read_model_cache.py`, `response_snapshots.py`, `library_snapshot.py` | SQLite schema, migrations and queries; backup bundles and offline restore; the caches and snapshots behind Library and Wanted. |
| Core operations | `service.py`, `worker.py`, `monitor.py` | `TankarrService` downloads, packages, imports, renames, retires and recovers files for the API and the workers. `DownloadWorker` runs the persistent job queue. `ReleaseMonitor` refreshes monitored works, queues new releases and runs the Wanted pass. |
| Catalogue and metadata | `catalogue.py`, `metadata/`, `catalogue_consensus.py`, `authors.py`, `official_*.py`, `release_cadence.py` | Identity-first series: MangaBaka is the identity index, and MangaUpdates, AniList, Kitsu and MyAnimeList are read through the identifiers it provides. Official platforms and numbering, author credits and expected release dates. |
| Numbering and series shape | `chapter_mapping.py`, `chapter_map.py`, `operator_map.py`, `numbering_reconciliation.py`, `source_numbering.py`, `unit_reconciliation.py`, `release_kind.py`, `series_unit.py`, `series_units.py`, `series_form.py`, `series_summary.py`, `completeness.py` | Canonical chapter numbers, chapter-to-volume maps, whether a series is followed as chapters or books, and what is owned, covered or missing. See [Design: mixed series](design-mixed-series.md). |
| Sources and acquisition | `providers/`, `release_sources.py`, `source_ranking.py`, `source_health.py`, `wanted_recovery.py`, `search_cadence.py`, `volume_hunt.py`, `download_tuning.py`, `internet_archive.py`, `suwayomi_runtime.py`, `suwayomi_bootstrap.py` | Provider adapters with rate-limited HTTP, source discovery and ranking, the source health ledger, the Wanted recovery ladder and its cadence, volume searches, adaptive download concurrency and the managed Suwayomi engine. |
| Indexers and download clients | `prowlarr.py`, `qbittorrent.py`, `sabnzbd.py`, `torrents.py`, `torrent_utils.py` | Prowlarr search and verified hand-off, qBittorrent and SABnzbd clients scoped to Tankarr's category, and `TorrentManager`, which imports completed downloads. |
| Import and library files | `importer.py`, `import_limits.py`, `archive.py`, `comicinfo.py`, `naming.py`, `language_audit.py`, `page_quality.py`, `content_alignment.py`, `assemble.py`, `series_audit.py`, `audit_actions.py`, `download_recycle.py`, `maintenance.py` | Archive extraction within budgets, no-overwrite publication, ComicInfo and file names, language and page-quality checks, assembled books, file audits, recycle bins and nightly maintenance. |
| Readers and notifications | `library_reader.py`, `readers.py`, `native_reader.py`, `reader_discovery.py`, `komga.py`, `stump.py`, `notify.py` | The built-in CBZ reader, reader shortcuts and discovery, Komga and Stump library management, and push notifications. |
| Operator workflows | `operations.py`, `selection.py` | Setup checks, acquisition previews and explanations, portable library lists, diagnostics. |
| Translation fallback | `translation.py`, `translation_*.py`, `local_translation.py` | Optional translation jobs with local OCR or an external processor. See [Translation fallback](translation.md). |

Outside the package:

- `frontend/src/`: `App.tsx` (routes and lazily loaded pages), `api.ts` (the
  API client, with ETag revalidation), `pages/` (one component per screen),
  `components/` and `types.ts`. Browser tests are in `frontend/e2e/`.
- `tests/`: the pytest suite. `tests/browser_server.py` starts the isolated
  real API used by the browser tests and the benchmark.
- `contrib/`: the reference external translation processor, which runs outside
  Tankarr.

## Contributing

Before opening a pull request, read the
[contributing guide](https://github.com/Renzodef/tankarr/blob/main/CONTRIBUTING.md):
it covers the branch model, the pull request checklist and the rule that keeps
values from your own installation (addresses, host names, paths, keys) out of
the repository. AI coding agents working in the repository follow
[AGENTS.md](https://github.com/Renzodef/tankarr/blob/main/AGENTS.md).
Releases are tags on `main`, published as described in
[Upgrading](upgrading.md) and [Releasing](releases.md).

## Screenshots

The screenshots in the documentation and the README come from a fictional
library, so that no real work, cover or reader appears in them, and they are
regenerated rather than edited:

```sh
.venv/bin/python tests/demo_snapshot.py /tmp/tankarr-demo
.venv/bin/python tests/browser_server.py --snapshot /tmp/tankarr-demo/tankarr.sqlite3 \
    --artwork-root /tmp/tankarr-demo/artwork --port 18880
npm run screenshots --prefix frontend      # writes docs/assets/screenshots/*.png
.venv/bin/python tests/demo_snapshot.py --shrink docs/assets/screenshots   # PNG -> WebP
```

`tests/demo_snapshot.py` invents the titles, authors, covers and a few CBZ
books with drawn pages, and gives the data realistic shapes (running and
finished works, chapters and books, a backlog, a queue, an official platform
with a weekly schedule). The capture script waits for every cover and font
before each page; `--shrink` converts the PNG captures to WebP, a fifth of
the size with the gradients intact.

## Online demo

[renzodef.github.io/tankarr/demo](https://renzodef.github.io/tankarr/demo/)
is the real interface in front of a recording of that fictional library, so
it runs on GitHub Pages without a server and cannot download anything:

```sh
./build-demo.sh                        # -> frontend/dist-demo, for /tankarr/demo/
npm run demo:test --prefix frontend    # opens the built demo in a browser
```

The script builds the snapshot, starts `tests/browser_server.py` on it,
walks every page with `frontend/e2e/demo-record.mjs` and writes each API
response it saw to `frontend/demo-data/` (JSON inline, covers and pages as
files), then `frontend/scripts/build-demo.mjs` builds the interface with
`VITE_TANKARR_DEMO=1` and the final base path, and bundles the service
worker `frontend/src/demo/sw.ts` beside it. In the browser the worker
answers every request under `demo/api/` from the recording
(`frontend/src/demo/recording.ts`): reads come back as recorded, writes are
acknowledged and never applied, which the banner says. CI builds and opens
the demo on every pull request (`e2e/demo.spec.ts`); the docs workflow
publishes it from `main` with the site, so a change to the interface or the
API republishes it.
