---
description: How Tankarr keeps its Library and Wanted screens responsive on small servers, how to run the browser tests and benchmark your own catalogue, and how it relates to similar projects.
---

# Performance

Tankarr is meant to run on modest home servers next to the rest of an *arr
stack. This page explains the design choices that keep the interface
responsive, how to measure them with the browser suite and the benchmark
script, and how Tankarr relates to similar projects. It is an engineering
description, not a claim that Tankarr is faster or more complete than every
other project.

## How the interface stays responsive

- **Snapshots and revalidation.** Library and Wanted keep bounded browser
  snapshots and reuse parsed responses when the server answers HTTP 304.
  Library and the global search share one in-flight request and update
  together after edits. An explicit refresh bypasses stale server snapshots.
- **Code splitting.** The application loads only the code of the current
  route. Background polling yields during startup, and a stable action context
  avoids re-rendering the lists on every health update. An automated check
  keeps the initial JavaScript below 85,000 bytes after gzip compression.
- **Bounded rendering.** Wanted precomputes search text and sort keys, reuses
  collators and does not sort again on every keystroke. Tables are paginated
  at 20 rows, Library renders 30 cards per page, and suggestion menus show at
  most 50 matching titles.
- **Incremental server work.** Library reads its inputs in batches and
  rebuilds only the series cards that changed, including changes to recovery
  evidence and publication dates. Canonical unit selection reuses its
  normalisation and coverage work. Pure source classification and URL host
  parsing use bounded caches.
- **Precise invalidation.** Time-dependent caches expire without requiring a
  database write. Changes to source ranking, health, recovery evidence and
  monitoring invalidate the relevant snapshots. Related SQLite reads share one
  consistent read transaction.
- **Resilient loading.** Wanted rows do not wait for the monitor status.
  Initial and refresh errors offer persistent retries, and a failed refresh
  keeps the existing data. A superseded response cannot replace newer results.
  A route whose code fails to load keeps the navigation shell and offers
  recovery.
- **Covers.** Replacing a cover that is still loading does not cancel the new
  image request. Only the first Library cover is loaded eagerly with high
  priority; the others are lazy, with reserved dimensions and asynchronous
  decoding.

Most of the cost of a cold start is on the server: reading releases and
rebuilding canonical indices. On slow links, transferring cover images also
matters. Returning to a list within the same tab is immediate, but opening a
completely cold catalogue is not.

## First paint after a restart

Library and Wanted persist the last successful JSON response under
`data_dir/cache/responses`. Explicit `cached=true` reads can display this
snapshot immediately after a restart while one background task reconciles the
current database. Fresh reads wait for reconciliation; acquisition decisions
never use the persisted HTTP snapshot.

Snapshots are disposable, atomically replaced, limited to 64 MiB each and seven
days old, and scoped to the database file identity, library root and preferred
unit. Missing, incompatible or corrupt snapshots fall back to a fresh
calculation. A new installation therefore still needs one complete calculation.
Deleting this cache is safe and only makes the next first paint slower.

## Wanted search cadence

The Wanted pass does not refresh, rediscover and search every series with a gap
on every run. Each series keeps a small state (`wanted_search_state`): after a
pass that queued nothing and discovered no new source, the next look moves out
along a ladder of 6 hours, 1 day, 3 days, 7 days and 30 days. A queued release,
a newly discovered source or a person asking (`POST /api/manga/{id}/search`)
resets the ladder.

Every pass takes at most `TANKARR_WANTED_SEARCH_BUDGET` series (default 25),
the most overdue first, so a pass cannot saturate a small host; the rest wait
for the next pass. Running works get their new chapters from the release
monitor; their older gaps follow the same ladder as finished works.

## Browser tests

The Playwright suite uses a real Chromium browser, the production frontend
build, the real FastAPI routes and a disposable SQLite database containing 60
series and 6,000 releases. It does not start the application's download or
reconciliation workers, and only the fixture's series metadata and rename
workflow may change the database. Network-error tests intercept only the
endpoint whose failure they exercise; normal workflows use the actual API.

```bash
npm ci --prefix frontend
npm run build --prefix frontend
cd frontend
npx playwright install chromium
TANKARR_TEST_PYTHON=../.venv/bin/python npm run test:e2e
```

The checks cover navigation to series; Wanted sorting, filtering and
pagination; monitor failures and delayed responses; initial API errors and
failed refreshes; lazy-chunk failure and recovery; the Library and search
sharing one request; saved renames; out-of-order refreshes; instant cached
navigation; replacing a loading cover; the reader; operator workflows such as
setup, backups and diagnostics; mobile layouts; the startup bundle budget; and
a slow profile with 150 ms latency, 200 KB/s bandwidth and a 4× CPU slowdown.
CI keeps browser traces and failure screenshots. The synthetic data contain no
cover images on purpose: cover loading is measured by the benchmark below.

Set `PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH` to use a Chromium installed on the
system instead of the one Playwright downloads.

## Benchmarking your own catalogue

The benchmark script measures a real catalogue without exposing its books or
settings. It runs against a copy of your own database: the server copies it
into a temporary directory, removes the stored settings from the copy and
never touches the original.

```bash
# Terminal 1, repository root: copies the database into a temporary directory.
.venv/bin/python tests/browser_server.py --port 18880 --temp-root /var/tmp \
  --snapshot /path/to/data/tankarr.sqlite3 \
  --artwork-root /path/to/data

# Terminal 2, frontend directory:
TANKARR_BENCH_URL=http://127.0.0.1:18880 \
TANKARR_BENCH_OUTPUT=test-results/benchmark \
npm run benchmark
```

`--artwork-root` is optional: it copies artwork, covers and already-generated
thumbnails, never the books. Omit it for an API and list benchmark, but do not
call that a cover-loading benchmark. Choose an existing disk-backed
`--temp-root` when copying a large catalogue: `/tmp` may be RAM-backed, and
several artwork copies can cause severe swapping on a small server.
`PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH` selects a system browser,
`TANKARR_BENCH_THROTTLE=1` enables the same slow network and CPU profile as the
test suite, and `TANKARR_BENCH_ROUNDS` sets the number of rounds (three by
default).

Start a fresh server for cold-cache measurements. Each benchmark round creates
a new browser context: later rounds reuse server caches, not browser caches.
Record both the time to the first Library card and the time until the visible
covers are decoded; they are not interchangeable. The script writes JSON,
screenshots, LCP observations, long tasks and observed interaction durations.
The interaction durations are **lab samples, not field INP**. Field INP
requires real-user measurements across sessions; the good threshold is 200 ms
at the 75th percentile
([measurement guidance](https://web.dev/articles/optimize-inp)).

A single cold run and a few warm runs on a shared machine are not statistically
powered results. Report the hardware, the catalogue size and the number of
runs together with any figure you publish.

## Alternatives

The table compares documented capabilities. No competing application has been
benchmarked against Tankarr on the same data and hardware, so it says nothing
about relative speed.

| Project | Strengths relevant to this comparison | Relationship to Tankarr |
| --- | --- | --- |
| [Suwayomi Server](https://github.com/Suwayomi/Suwayomi-Server) and [WebUI](https://github.com/Suwayomi/Suwayomi-WebUI) | Mihon-compatible sources, scheduled updates and downloads, backups, trackers, OPDS, browser reader and library management. | The closest comparison for source-based manga acquisition. Tankarr uses Suwayomi as the engine for its source downloads and adds its own canonical numbering, edition and language policy, recovery evidence and filesystem import decisions. |
| [Mylar3](https://github.com/MylarComics/mylar3) | Comic watchlists, missing-issue automation, NZB and torrent acquisition, alternate-release retry, renaming, metadata and story arcs. | A strong reference for comic automation. Tankarr's manga chapter and volume model has a different focus. |
| [Tranga](https://github.com/C9Glax/tranga) | Scheduled manga acquisition, CBZ and ComicInfo metadata, notifications and Komga/Kavita scan integration. | A lightweight automation alternative. Compare installation, source coverage and recovery behaviour for your own library. |
| [Komga](https://komga.org/) | Browser reading, library organisation, collections, reading lists, OPDS and metadata tools. | Primarily a reader and library server. Tankarr is acquisition-first and works with a separate reader; the roles are complementary. |
| [Kavita](https://www.kavitareader.com/) | Comics, ebooks and PDF, browser reading, OPDS, filters, users and reading features. | A broader reading and document-library scope. Tankarr does not aim to replace its reader features. |

Story arcs, reader features and the availability of third-party sources are
separate product concerns: public demos and feature lists are not controlled
benchmarks.

## Framework and language choices

Rewriting the interface or the server is not a performance strategy by itself.
TypeScript types disappear during compilation, so handwritten JavaScript would
not remove runtime work
([TypeScript documentation](https://www.typescriptlang.org/docs/handbook/2/basic-types.html#erased-types)).
A smaller UI runtime can reduce startup JavaScript, but it cannot remove the
time spent reading and decoding releases and rebuilding canonical indices in
Python. React keeps the interface responsive when expensive sorting is avoided,
data are shared and only the visible page is rendered; deferred input updates
change scheduling, not the cost of a calculation
([React documentation](https://react.dev/reference/react/useDeferredValue)).

A Go or Rust backend, or a Svelte or Solid frontend, would need an isolated
prototype with equal behaviour, a measured improvement and a justified
migration cost. Before that, consider pagination and projection at the API
boundary, incremental data updates, or virtualisation if catalogues outgrow the
current client-side model. A demonstrated repeated computation is fixed where
it happens, not by rewriting the application.
