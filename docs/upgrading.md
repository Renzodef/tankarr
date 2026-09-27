---
description: How Tankarr is released, which Docker image tag to run, how to update safely, back up and roll back.
---

# Upgrading

## Release channels

Tankarr is published the same way as Sonarr, Radarr and the other *arr
applications: a stable branch, a development branch and immutable builds.

| Image tag | Built from | Recommended for |
| --- | --- | --- |
| `latest` | the newest stable release | Most installations |
| `X.Y.Z` (for example `1.2.3`) | that exact release | Pinning a version; updates only when you change the tag |
| `X.Y` | the newest patch of that minor version | Receiving fixes without new features |
| `sha-<commit>` | one exact build | Rolling back, or reproducing a problem |
| `branch-<name>` | a branch with an open pull request | Trying a change before it is merged; removed when the branch is deleted |

All tags are multi-architecture (`linux/amd64`, `linux/arm64`). Release notes
for every stable version are on the
[releases page](https://github.com/Renzodef/tankarr/releases); a `branch-<name>`
image corresponds to an open pull request and disappears with its branch.

Stable versions follow [semantic versioning](https://semver.org): a patch
release (`1.2.3` → `1.2.4`) only fixes problems, a minor release adds features
without breaking existing configurations, and a major release announces
breaking changes in its notes. Before 1.0, minor releases may still contain
breaking changes, always listed in the notes.

## Updating

```sh
docker compose pull tankarr
docker compose up -d tankarr
```

Tankarr can be restarted at any time: interrupted chapter jobs are queued
again and a torrent or Usenet import resumes where it stopped. It is still
kinder to update while **Activity** shows no import in progress.

After an update the web interface is available within seconds. The container
reports healthy (`/api/ready`) once the library, the download queue and the
deletion recovery have finished their safe startup, which can take several
minutes on a large library.

### Automatic updates

Tools like Watchtower or Diun work with Tankarr. If you use one, follow a
fixed channel (`latest`, or `X.Y` for fixes only) rather than a branch image, and
keep automatic backups enabled.

## Backups

Tankarr writes an application backup bundle automatically, keeps the last
seven (**Settings → Data**, `TANKARR_BACKUP_RETENTION_COUNT`) and can create
one on demand from **System**. A bundle is a ZIP with a consistent SQLite
snapshot, the effective configuration and the library identity, each member
verified by size and SHA-256. Bundles are stored in `/config/backups` unless
`TANKARR_BACKUP_DIRECTORY` points elsewhere.

A bundle contains your integration secrets **unencrypted**: keep it on trusted
storage. It does not contain your library files, the managed Suwayomi runtime
or its extensions; back up the library folder with your usual tools.

Verify and restore a bundle offline into a new, not yet existing directory:

```sh
docker run --rm -v /srv/backups:/backups -v /srv/restore:/restore \
  ghcr.io/renzodef/tankarr:latest \
  python -m tankarr.backups --verify /backups/tankarr-example.zip
docker run --rm -v /srv/backups:/backups -v /srv/restore:/restore \
  ghcr.io/renzodef/tankarr:latest \
  python -m tankarr.backups --restore /backups/tankarr-example.zip --destination /restore/config
```

Then point the `/config` mount at the restored directory. A restored
installation starts in *safe mode*: no worker, monitor, recovery or write
operation runs. Check mounts, jobs and paths on the **System** page, then
restart with `TANKARR_RESTORED_SAFE_MODE=false`. See
[Operations and safety](operations-safety.md) for the details.

## Rolling back

The database schema only moves forward: a newer version may upgrade it on
its first start, and an older image cannot read an upgraded database. To roll
back:

1. Stop the container.
2. If the release notes of the version you are leaving mention a database
   change, restore the backup taken before the upgrade (see above).
3. Start the previous image by its version or `sha-` tag, for example
   `ghcr.io/renzodef/tankarr:1.2.3`.

## Moving from a self-built image

If you built Tankarr yourself before images were published, replace
`build: .` with `image: ghcr.io/renzodef/tankarr:latest` in your Compose file
and run `docker compose pull && docker compose up -d`. Your `/config` and
`/library` folders are used as they are.
