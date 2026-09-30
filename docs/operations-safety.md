---
description: How Tankarr backs up and restores its control plane, the limits it applies to untrusted archives, and how Suwayomi engine updates and deployments stay recoverable.
---

# Operations and safety

This page covers the application backup bundle and its offline restore, the
budgets that protect the host when Tankarr opens untrusted archives, and how
engine updates and deployments can be recovered. For the backup schedule and
the restore workflow in the interface, see
[Backup and safe restore](operator-workflows.md#backup-and-safe-restore).

## Application backup bundle (v1)

A `tankarr-application-v1` ZIP backup contains a consistent SQLite snapshot
(taken with the SQLite backup API and checked with `quick_check`), the
effective configuration as JSON, `metadata.env`, the library identity and, when
present, the import journal. Every member is verified by size and SHA-256. The
bundle is published atomically without overwriting anything, and retention runs
only after verification. Files are created with mode 0600 and new directories
with mode 0700.

!!! warning "Backups contain secrets"
    The bundle contains secrets **unencrypted**. Keep it only on trusted
    destinations, and add volume or backup encryption outside Tankarr if you
    need it.

`TANKARR_BACKUP_DIRECTORY` selects an external destination mounted into the
process or container; the default is `<data_dir>/backups`. It is not a remote
replication service. The expanded bundle is limited to 4 GiB. Verifying the
SQLite snapshot uses scratch space in `/var/tmp` when it is available. Copying
the SQLite database has a 180-second deadline.

The bundle explicitly excludes:

- library media and covers;
- the Suwayomi database, JAR and extensions;
- upload and staging payloads;
- authentication sessions.

The first three need separate backups: the bundle is complete for Tankarr's
control plane, but it is **not** a complete backup of all media or of the
databases of external services. Sessions are excluded on purpose: previous
login tokens are not reactivated by a restore.

## Verify and restore offline

```sh
python -m tankarr.backups --verify /backup/tankarr-example.zip
python -m tankarr.backups --restore /backup/tankarr-example.zip --destination /restore/tankarr-new
TANKARR_DATA_DIR=/restore/tankarr-new python -m tankarr
```

With Docker, run the same commands in a one-off container of the Tankarr image
with the backup and restore directories mounted; [Upgrading](upgrading.md)
shows an example.

The destination must **not exist**: even an existing empty directory is
refused, and no active installation is ever overwritten. Before publishing the
restored directory, the restore verifies the bytes and the SQLite database
again. It writes the configuration to `restored-settings.json`, keeps passwords
literal (a `${...}` sequence in a password is not interpolated) and forces safe
mode. Configuration set explicitly in the environment takes precedence over the
restored JSON.

### Restored safe mode

In safe mode no worker, monitor, recovery or write operation starts. Pending
jobs keep their references but are moved to failed or review, with a
diagnostic. The import journal is kept as `restored-import-operation.json` and
is never replayed automatically without its payload.

Check mounts, library identity, the source engine, jobs and paths. Only after
that review, restart with `TANKARR_RESTORED_SAFE_MODE=false` and explicitly
re-enable the automations you want. Do not rename the journal blindly.

The library identity (`.tankarr-library-id` in the data directory and at the
library root) is part of the bundle. A restored configuration therefore
expects the library it was backed up with; safe mode never writes a new
identity, so a restore pointed at another library is reported, not adopted.

## Importing untrusted archives

The import budgets can be changed in Settings, through the API or with
environment variables:

| Setting | Environment variable | Default |
| --- | --- | --- |
| `import_max_expanded_bytes` | `TANKARR_IMPORT_MAX_EXPANDED_BYTES` | 16 GiB |
| `import_max_pages` | `TANKARR_IMPORT_MAX_PAGES` | 20,000 |
| `import_subprocess_memory_mb` | `TANKARR_IMPORT_SUBPROCESS_MEMORY_MB` | 1,024 MiB |
| `import_disk_reserve_bytes` | `TANKARR_IMPORT_DISK_RESERVE_BYTES` | 512 MiB |

**ZIP.** The declared pages and bytes are checked first, even before the
archive is fingerprinted. Members are streamed in 1 MiB blocks and the bytes
actually written are counted. `ComicInfo.xml` is limited to 2 MiB, checked
before decompression. The image headers of recognised pages are limited to
40 megapixels before OCR or slicing. None of this is a hard memory limit for
the Python process.

**PDF.** `pdfinfo` is limited to 30 seconds and the page count is checked
before rendering. Pages are rasterised one at a time, with at most 120 seconds
per page and 30 minutes in total.

**RAR.** Extraction is limited to 15 minutes.

### Decoder limits on POSIX systems

On POSIX systems, external decoders run with hard limits on address space (not
RSS), CPU time and file size. The file-size limit allows a single sentinel byte
beyond the budget, so that a decoder that ignores write errors is detected
instead of leaving a truncated file that looks complete. The aggregate bytes
and the number of extracted files are checked every 100 ms: this is **not a
filesystem quota**, and extraction can overshoot for up to one interval before
the process group is killed. Diagnostic output is capped at 1 MiB, and the
messages reported are truncated.

These limits are not a sandbox against vulnerabilities in the decoders, and
they are not a global quota across concurrent imports. For stronger isolation,
use a filesystem or container with quotas.

## Suwayomi engine updates

Tankarr installs the Suwayomi-Server release pinned in its source
(`PINNED_RELEASE` in `tankarr/suwayomi_runtime.py`, tag and SHA-256). The pin
proves that the JAR is the one reviewed with that Tankarr release; the checksum
file published upstream only proves the download was not corrupted, and both
must agree or nothing is installed. Newer upstream releases are reported daily
and installed only when `TANKARR_SUWAYOMI_AUTO_UPDATE` is on or the operator
presses **Update**; a server newer than the pin is never downgraded. Extension
updates apply only to extensions installed from the configured repository.

A managed Suwayomi update checks the official SHA-256 checksum, snapshots the
engine's data directory (its *home*) with the engine stopped before replacing
the JAR, and requires the new version to become ready within 180 seconds before
the update is confirmed. A failure restores the JAR and the data together. A
persistent journal allows the rollback to complete even after a crash in either
of the two windows in which the home is being renamed.

Homes from failed updates remain in `.failed-update-*` directories for manual
recovery; they are never deleted automatically. A home that contains symbolic
links is refused, because the snapshot could not guarantee data stored outside
it. An engine installed without being started (`start=False`) is prepared but
not reported healthy until its first verified start. An update needs free space
for a second copy of the home plus at least 64 MiB.

## Deployment and rollback

Release images are built from a clean export of `main`; the values of each
installation stay separate, in `.env` and Settings. See
[Upgrading](upgrading.md) for release channels, updates and rollbacks, and
[Releasing](releases.md) for how releases are made. Tankarr includes no
host-specific wrappers and does not promise an automatic container rollback.

Before starting a previous image, check that it is compatible with the current
schema and data. Never restore or downgrade the SQLite database automatically:
deleting newer ledgers while the files they published still exist would lose
data. Keep the previous image, the verified backups and the homes of failed
engine updates until the recovery is complete.

Primary references:
[Python POSIX resource limits](https://docs.python.org/3/library/resource.html),
[Docker Compose `up` options](https://docs.docker.com/reference/cli/docker/compose/up/),
[`docker run` isolation](https://docs.docker.com/reference/cli/docker/container/run/).
