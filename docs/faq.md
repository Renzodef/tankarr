---
description: Answers to common questions about Tankarr and solutions to common problems - authentication, Suwayomi, torrents, Usenet, imports and readers.
---

# FAQ and troubleshooting

## General questions

### What is Tankarr?

A self-hosted application that manages a manga, manhwa, manhua, webtoon and
comics library the way Sonarr manages TV series: you add the works you follow,
choose a language and a monitoring profile, and Tankarr finds, downloads,
verifies, names and files every chapter or volume into a CBZ library.

### How is it different from Suwayomi, Komga or Kavita?

They solve different problems, and Tankarr works with all of them.
[Suwayomi](https://github.com/Suwayomi/Suwayomi-Server) is a source engine and
reader that runs Mihon/Tachiyomi extensions; Tankarr can run it for you and
uses it as one of its download channels. [Komga](https://komga.org),
[Kavita](https://www.kavitareader.com) and [Stump](https://www.stumpapp.dev)
are readers and library servers; Tankarr fills the library they read, and can
trigger their scans and publish metadata and covers to them.

### Is there a Sonarr or Radarr for manga?

That is the gap Tankarr fills: monitored series, Wanted, Activity, Calendar,
History, interactive search and Prowlarr indexers with qBittorrent and SABnzbd,
for manga and comics.

### Does Tankarr include manga sources or content?

No. Tankarr ships no content and no source of its own. It automates the
extensions, indexers and clients that you choose to configure. You are
responsible for complying with the laws of your country and the terms of the
services you use. Please support the authors and official publishers of the
works you read.

### Which languages are supported?

Any language that your sources carry. The language is recorded per release,
so different series can follow different languages. Indexer releases are
checked with OCR before import, because a release name is only a claim about
its language.

### Which extension repository should I use?

Tankarr does not ship, host or recommend one. The managed Suwayomi server
installs extensions from whatever Mihon/Tachiyomi-compatible repository you
enter under **Settings → Sources → Extension repository**; choosing and
trusting it is your decision, exactly as it is in Mihon or Suwayomi.

### Does it need an account anywhere?

No. The [MangaBaka](https://mangabaka.org) catalogue and the other metadata
sources are public and need no key. Prowlarr, qBittorrent, SABnzbd, Komga and
Stump use the credentials of your own servers.

## Troubleshooting

### The container is "unhealthy" right after an update

The health check calls `/api/ready`, which succeeds only after the library,
the download queue and the deletion recovery have finished their safe start.
On a large library this takes minutes. The web interface already works in the
meantime; `GET /api/system/ready` (signed in) tells you what is still pending.
If it stays unhealthy, check that the library folder is mounted and writable.

### What is my password? / Tankarr refuses to start

Unless you set `TANKARR_AUTH_USERNAME` and `TANKARR_AUTH_PASSWORD`, the first
start creates the user `admin` with a random password and prints it to the log:
`docker logs tankarr 2>&1 | grep "created one"`. Until you save your own login
under **Settings → Security**, it is also in `/config/generated-login.json`. A
login saved on the Settings page is stored in `/config/metadata.env`. The two
variables must be set together: setting only one stops Tankarr on purpose.

### "Too many failed sign-in attempts"

After five wrong passwords from the same address Tankarr waits before it
accepts the next attempt, up to a minute. Wait and try again with the right
password.

### "Permission denied" when writing to the library

The container runs as UID/GID 1000. Either give that user write access to
the host folders, or run the container as the owner of your media with
`user: "UID:GID"`. See [File permissions](installation.md#file-permissions).

### Installing the managed Suwayomi server fails

The container needs outbound HTTPS access to GitHub to download the
Suwayomi-Server release and to the extension repository. On a small host,
check that the container has at least 1.5 GB of memory; the Java heap can be
tuned in **Settings → Sources** (`TANKARR_SUWAYOMI_MANAGED_HEAP_MB`).

### A source fails with Cloudflare or "403" errors

Some sites only answer after a browser challenge or a captcha. The managed
Suwayomi engine does not run a challenge solver or an embedded browser, so
the extensions that need one are not installed. A source that keeps failing
is ranked lower automatically and recovers its place when it works again;
**System** shows the health of every source.

### A torrent or Usenet download finished but was not imported

Check, in order:

1. **Paths**: the folder the client writes to must be mounted into Tankarr and
   configured as described in [Download clients](installation.md#download-clients).
2. **Review**: Activity shows why a release waits. Releases whose numbering is
   ambiguous, or whose pages contradict the expected language, stop in review
   instead of entering the library; decisions wait on **System → To confirm**.
3. **Tesseract**: the OCR language check needs Tesseract, which the Docker
   image includes. When running from source, install `tesseract-ocr`.

### New files take a while to appear in Komga or Stump

When Tankarr manages the reader (Komga or Stump), it starts a scan when the
download queue drains and every six hours as a safety net
(`TANKARR_KOMGA_REFRESH_INTERVAL_MINUTES`). Other readers scan the folder on
their own schedule; you can always start a scan from the reader itself.

### A downloaded chapter is wrong or unreadable

Open the series and use **Audit files**: it lists every downloaded file with
its source, page count and detected anomalies, and lets you retire a bad file
or reject a source that keeps supplying wrong files for that series. Tankarr
also measures page geometry after every import and replaces chapters that are
clearly unreadable when another source has them.

### Where are the logs?

`docker logs tankarr` shows the application log. The **System** page lists
alerts, pending decisions and the health of every integration, and can produce
redacted diagnostics for a bug report.

## Still stuck?

Search the [issues](https://github.com/Renzodef/tankarr/issues) and
[discussions](https://github.com/Renzodef/tankarr/discussions), or open a new
one with the version you run and the relevant, redacted log lines.
