---
description: Everyday operator workflows in Tankarr - setup checks, Wanted explanations, book boundaries, assembled books, backups and safe restore, import budgets, diagnostics and file audits.
---

# Operator workflows and reliability

Setup checks guide the first launch and remain available from System → Health.
Settings → Data contains application backups and retention settings. System →
About offers a redacted diagnostics download, while Needs attention shows the
scheduled backup and recycle failures. Library repairs run silently in the
background. Acquisition explanations live in Wanted.

## Setup and acquisition decisions

The local checklist checks configured directories, permissions, free space,
library identity, authentication and integration configuration. **Run connection
checks** additionally tests temporary writes/fsync (the probes are removed) and
configured providers/clients with independent timeouts and bounded concurrency.
A local pass is not proof that a remote service is reachable.

The first-launch wizard appears when required checks fail and setup has not been
completed. Skip for now dismisses it for the browser session; successful completion
persists `setup_completed_at`. Missing optional integrations remain warnings.
Library directories are configured through the deployment environment.

An acquisition preview API simulates the real missing-release selector
without changing the worker's ranking or persisting preferences. Series
language and edition remain authoritative; quality replacement is a separate opt-in
pass. At most 500 candidates are returned, with truncation explicitly reported.

Wanted's **Why still wanted?** dialog exposes recorded attempts and reasons,
timestamps, actual cooldown eligibility and relevant actions. Eligibility means
"may be attempted after", not a promised download date. Missing evidence is
shown as unknown; title-only or unresolved numbering is never silently accepted.

Torrent imports with inconclusive language, numbering or edition evidence are
refused automatically. Activity records the failure and keeps the downloaded
payload and verification evidence; there is no confirmation prompt and the
poller does not repeatedly retry the refused import.

## Mixed books and chapters

The series list groups chapters under books when their map is exact. Owned books
start collapsed; missing books show how many of their chapters are on disk.
Chapters outside the map have a separate final section. Without an exact map,
Books and Chapters remain separate and the page offers Set book boundaries.
Catalogue hints never become book badges or proof of chapter coverage.

All, Missing, Downloaded and Monitored filters work on groups and their children.
**Retire duplicate files** previews the chapter files inside one owned book. After
confirmation, only the reviewed files move to the recycle bin; chapter files for
other books remain. A changed duplicate set requires a new preview.

## Book boundaries

Open **Set book boundaries** on a series to enter each book number and its first
canonical chapter. Catalogue suggestions are labelled as unverified. Book and
chapter numbers must increase; decimal numbers and chapter zero are supported.
The next boundary ends the preceding book, and the last book ends at the last
known chapter number: the maximum observed chapter, effective chapter total
(including its override), and catalogue endpoint. Preview shows the full chapter
intervals before saving. Previously saved intervals remain valid if current
sources stop exposing their chapters.

Saving replaces only the operator map. Other sources remain available, but
operator assignments take precedence. The preview is bound to the current series
data: a changed map, release, monitoring setting or metadata requires a new
preview. Removing the operator map also requires a preview and confirmation.
API clients can send only changed rows: PUT defaults to `mode: "merge"`, retaining
omitted books and their unchanged intervals. The full editor uses `mode: "replace"`
so removing a row still removes that book boundary. Both modes require their own
preview and confirmation.
The optional `last_chapter` sets an edition's precise ending within that limit;
it does not change the catalogue. An unchanged saved ending survives later saves.

Missing books can then be acquired as their mapped chapters while the series
remains counted in books. Existing monitoring, language, edition and source
rules still apply. A hint alone never enables this fallback. Owned books imported
by hand or larger than 600 pages do not make chapter files duplicates.
An exact operator map can cover missing chapters with manually imported books
within that page limit, while preserving every existing chapter file and its
protection against retirement. Oversized books still cannot prove coverage.

Catalogue refreshes keep saved operator boundaries unchanged. The editor reports
when source numbering moves outside them or new chapters extend past the saved
last book; it does not shift or extend the map automatically.

## Assemble books

When every chapter of an exactly mapped, missing book is on disk, its group offers
**Assemble book**. **Assemble all covered books** previews the eligible books in
the series together. The preview lists chapters, page counts and final filenames.
Confirming authorizes both publication and retirement of the source chapter files
to the recycle bin. Missing or changed files require a new preview.

Tankarr creates an ordinary volume CBZ with sequential page names, volume metadata
and source/language notes. It publishes through the normal validated import path,
commits its provenance and hash ledger, then retires the source files. Interrupted
work keeps the chapter files until publication can be proven. Bulk results report
each book and any failure separately; retry starts with a fresh preview.

An assembled volume counts as an owned book even above 600 pages. If its map later
changes, it stops proving chapter duplicates. An external book has higher priority
and can replace the assembly; the old assembled file moves to the recycle bin.

In Edit, **Assemble books automatically** enables the same operation for future
covered books in that series. It is off by default. Background assembly is bounded
to four attempts per monitor cycle and waits while imports are active.

## Portable library lists

Portable list export and import are available through the API; there is no
dedicated settings panel.

Export produces version 1 JSON with stable identities and list preferences;
there are no book files, credentials, local paths or reading-progress records.
Import accepts a version 1 `items` array, up to 500 entries and 2 MiB per request.
Preview classifies each item before issuing a single-use, ten-minute token.

Only stable MangaBaka catalogue identities are newly imported. Title-only entries
need identification; unsupported local/source-only identities need explicit
addition. Existing series are never overwritten. New series are **unmonitored**,
even when an imported list says `all`; no download starts. Enable monitoring
separately after reviewing the import. Errors report partial completion explicitly.

## Automatic repair and reader alignment

Nightly maintenance previews and applies safe library repairs automatically, with
no System panel, confirmation dialog, or repair notification. The worker revalidates
the database/file snapshot while holding the mutation lock before applying it.
Changed files or busy imports defer the work for a fresh automatic attempt;
diagnostics remain in application logs and the maintenance API. Old pending
reviews are rechecked once after startup, without waiting for another night.
Opening System never starts a scan.
Ambiguous actions, active replacements and duplicate retirement are not silently
applied by this workflow. The existing per-series review remains the appropriate
place for destructive duplicate decisions.

Kavita matches downloaded books by their complete managed-relative file paths.
An identity proven by files is cached persistently and rechecked on subsequent
requests. Renamed/translated titles can fall back to the paged series catalogue;
ambiguous matches fail closed. Fallback scans are bounded to 2,000 series and a
30-second lookup deadline. A title-only link without downloaded files is labelled
unverified. Neither this workflow nor Tankarr changes reader-owned progress.

## Backup and safe restore

Tankarr creates an application backup during nightly maintenance, between 03:00
and 06:00 in the application's local timezone. The default rotation keeps seven
verified bundles; `backup_retention_count` controls that limit. A failed backup
does not prune previous copies. Busy imports defer maintenance, and failed jobs
retry automatically with their status available in System.

An existing database is also backed up before a schema upgrade, before any schema
change is applied. Backup failure prevents that upgrade. Schema changes increment
`SCHEMA_VERSION` in `database.py`; the version is recorded only after initialization
succeeds, so an ordinary restart does not create another migration backup.

**Application backups contain credentials.** They are private, checksummed ZIP
bundles containing a consistent SQLite snapshot, effective configuration,
managed secrets, library identity and the import-operation journal when present.
Use Verify before copying a bundle. Download requires the normal authenticated
API and uses `Cache-Control: no-store`.

This is a **control-plane backup**, not a backup of your books. Library media and
covers, Suwayomi's database/JAR/extensions, and upload/staging payloads require
separate backups. The bundle manifest lists these exclusions and restore needs.
Sessions are deliberately excluded so restored installations reject old cookies.

Settings → Data offers Backup now and a Restore action for each bundle. Restore
first verifies the selected backup, then provides the download and offline restore
instructions below. It never overwrites the running database.

By default bundles live under the data directory. Set
`TANKARR_BACKUP_DIRECTORY=/path/on/an/independent/device` for an external copy;
mount that destination into the container when applicable. Merely selecting a
different directory does not prove it is on an independent disk.

Restore is offline and refuses to overwrite an existing destination:

```sh
python -m tankarr.backups --restore /backups/tankarr-example.zip --destination /data/tankarr-restored
TANKARR_DATA_DIR=/data/tankarr-restored python -m tankarr
```

The restored installation starts on loopback in **restored safe mode**. Background
automation and mutating API calls are blocked. Interrupted jobs are retained for
review, not automatically resumed. Check the library identity/mount, source engine,
credentials and missing staging files. Only then restart with
`TANKARR_RESTORED_SAFE_MODE=false`; automatic monitoring/import settings remain
disabled until explicitly configured. Do not use restore to downgrade a live DB.

## Import budgets and updates

Retirement workflows move retired files to a recycle bin instead of deleting
them immediately. `recycle_bin_retention_days` defaults to seven days. The
journal preserves the original paths and retirement time across restarts;
nightly maintenance removes expired entries. Interrupted or ambiguous operations
remain available for recovery. The explicit deletion endpoints of the API are
separate from these retirement workflows.

Untrusted ZIP/RAR/PDF extraction has expanded-byte/page budgets and a disk reserve.
External decoders also have time limits and, on POSIX, address-space, CPU and
per-file size limits. Aggregate subprocess output is checked every 100 ms, not
enforced by a filesystem quota: output can overshoot within that interval.

Defaults (also editable as import safety settings):

| Setting / environment suffix | Default |
| --- | --- |
| `IMPORT_MAX_EXPANDED_BYTES` | 16 GiB |
| `IMPORT_MAX_PAGES` | 20,000 |
| `IMPORT_SUBPROCESS_MEMORY_MB` | 1,024 MiB |
| `IMPORT_DISK_RESERVE_BYTES` | 512 MiB |

Environment names use the `TANKARR_` prefix. Raise limits deliberately for trusted
large books, accounting for the device's available memory and disk.

Managed Suwayomi upgrades preserve the old JAR and a stopped-engine data snapshot
until the new version passes readiness. Failed upgrades restore both, including
the engine data, rather than running an old binary on a migrated database.
Deployment rollback never silently rewinds Tankarr's live database. An old image
must support safe mode before it can be restarted automatically against current
data; otherwise recovery remains stopped and requires operator intervention.

## Health, sessions and diagnostics

`/api/health` and `/api/ready` are minimal public probes. Detailed payloads are
available from the authenticated `/api/system/health` and `/api/system/ready`.
Forms logout revokes the server-side session, and the revocation survives
restarts.

Source refresh failures update source health and ranking; they remain eligible
for retry. A failing primary source does not prevent trying alternatives. System
warns only when every enabled, language-compatible source for a series still in
Wanted has failed continuously for more than 24 hours. A successful refresh clears
that source's failure period. Disabled sources and individual mapping errors do
not produce warnings.

The diagnostics export uses an explicit allow-list of aggregate counts, versions
and check statuses. It excludes raw errors/logs, credentials, URLs, filesystem
paths and library titles. Do not attach an application backup as diagnostics.

## Verification scope

Regression tests exercise failed settings writes/commits and crash recovery,
archive budgets, session replay, clock/cache invalidation, scheduler cancellation,
operation-count bounds, stale repair plans, backup restore and simulated failed
engine upgrades. Chromium tests exercise the real temporary-library APIs as well
as injected failures and delayed responses. These do not replace a restore drill
on your own hardware and storage, or a trial of your particular Suwayomi
database migration.

## Inspect series files

Open **Audit files** from the series actions. The paginated list shows
source, language, numbering, page count and detected anomalies; thumbnails load
when visible. Select files and choose **Retire selected files**, or reject an
exact source for that series. Review the affected files and offers before
confirming. Changes since the preview require another preview. Retired files
remain in the recycle bin for the configured retention period.

## Automatic pack decisions

Completed packs fill missing book slots, including slots currently covered only
by chapters. Owned books and unnumbered files are skipped. Conflicting editions
of one slot do not prevent importing other useful slots. A real external book
can replace an assembled book through the ordinary recoverable upgrade path.

English release tags matching the series allow inconclusive OCR when file names
do not declare another language. Sampled non-Latin text prevents this fallback;
Tesseract script detection runs alongside English OCR. Import results retain the
paths and reasons for imported, owned and skipped files for History.

A wholly refused payload is detached from its download client without deleting
files, then moved atomically into `.tankarr-download-recycle` on its configured
download filesystem. The nightly recycle job expires its bytes after the same
configured retention period as library files (7 days by default). Receipts
separate new attempts from retries and preserve recovery across interruptions.

## Verify the latest published chapter of a continuing series

When a catalogue mixes chapter numbers, prologues and specials, a verified
publication number can resolve an unknown count without setting a final edition
limit. `PATCH /api/manga/{id}` accepts `verified_chapter_count` (a positive integer)
and `verified_chapter_source` (the evidence URL or description). Both are required
together; Tankarr records the verification time. Send
`{"verified_chapter_count":"automatic"}` to clear the verification.

For example, verifying chapter 30 materialises missing chapters through 30. A
subsequently indexed chapter 31 remains expected and can appear in Wanted. The
series stays continuing and normal monitoring remains active. Explicit edition
limits still take precedence, and ended series use their catalogue/end numbering.

### Chapter numbers and additional content

The main chapter count follows the reference sequence; it is not a raw count of
files or source listings. Owned, separately labelled prologues and recognised
extras are shown alongside it on Library cards and the series page. Material
already included in expected chapter slots is never added twice, and alternate
releases of the same numbered prologue count once. Additional content does not
advance the latest chapter number or create missing chapters.

Different editions can include prologues in their numbering or omit withdrawn
chapters, so their raw totals need not match. Preserve the source label and check
the content when mixing sources: equal numbers alone do not prove equal episodes.
