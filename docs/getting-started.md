---
description: First steps with Tankarr - sign in, connect download sources and indexers, add your first manga series and follow its downloads.
---

# Getting started

This guide takes a fresh installation to its first downloaded chapter. It
assumes Tankarr is running and reachable; if not, start with
[Installation](installation.md).

## 1. Sign in and run the setup checklist

Open `http://<your-server>:8787` and sign in: with the credentials you set in
`TANKARR_AUTH_USERNAME` and `TANKARR_AUTH_PASSWORD`, or with the login Tankarr
created and printed to its log on the first start
(`docker compose logs tankarr | grep "created one"`). Change it under
**Settings → Security**. Until the checklist is finished, a banner links to the
**Setup** page, which checks four things in order:

1. **Library**: the library folder is mounted, writable and has free space.
   Its location comes from the container mount (`/library`), not from the UI.
2. **Reader**: which application opens your books. Tankarr's built-in reader
   works out of the box; you can also pick Komga, Kavita, Stump or a URL
   template, or none.
3. **Suwayomi sources**: the engine that downloads chapters from manga sites.
4. **Optional indexers**: Prowlarr with qBittorrent and/or SABnzbd, for
   torrent and Usenet releases of whole volumes.

Every step links to the matching tab of **Settings**. Each panel there has a
test button that checks the values before you save them, and saved changes
apply immediately, without a restart.

## 2. Choose your languages

In **Settings → General** set:

- **Default language**: the language you read in, used for new series.
- **Search languages**: the languages Tankarr is allowed to search, in order.

A series is followed in one language. Tankarr treats the language as a
property of each release, so a Japanese manga can be downloaded in English
while another series is followed in Italian.

## 3. Enable the Suwayomi engine

[Suwayomi](https://github.com/Suwayomi/Suwayomi-Server) is an open-source
server that runs Mihon/Tachiyomi-compatible source extensions. Tankarr can run
it for you inside its own container ("managed" mode):

1. Open **Settings → Sources**, turn **Enabled** on and leave **Mode** on
   `managed`.
2. Enter the index URL of a Mihon/Tachiyomi-compatible **Extension
   repository**. Tankarr ships no repository and recommends none: it installs
   sources only from the repository you choose to trust.
3. Click **Install Suwayomi**. Tankarr downloads the official Suwayomi-Server
   release from GitHub, verifies its checksum and starts it inside the
   container.
4. Tankarr installs the repository's extensions for your search languages.
   NSFW extensions and extensions that need a full browser engine are left
   out.
5. Click **Test & load sources** to probe every source, then choose which ones
   Tankarr may use. An empty selection means every installed source.

If you already run a Suwayomi server, choose `external` mode and enter its
**Server URL** and login instead.

## 4. Optional: connect Prowlarr, qBittorrent and SABnzbd

Complete volumes, especially licensed English releases, are often easier to
find on torrent trackers and Usenet than on manga sites. In
**Settings → Indexers & torrents**:

- **Prowlarr indexers**: enter the Prowlarr URL and API key, click the test
  button to load the indexers, and choose the categories (Books, EBooks and
  Comics by default).
- **Torrent client (qBittorrent)**: URL, user name, password and the
  **Category** Tankarr uses for its own torrents.
- **Usenet client (SABnzbd)**: URL, API key, category and the
  **Completed folder (as SABnzbd sees it)**.

Both clients must share their download folder with Tankarr, see
[Download clients](installation.md#download-clients).

## 5. Add your first series

Open **Add New** and search the catalogue by title, or paste a MangaBaka URL.
Results come from the [MangaBaka](https://mangabaka.org) catalogue, never from
a download site; works already in your library are marked **In library**.

Pick a result, then choose:

- **Reading language**: the language to download.
- **Monitor**:

  | Option | What Tankarr downloads |
  | --- | --- |
  | All chapters | Every available chapter, then new releases as they appear |
  | Future chapters | Only chapters published after today |
  | Existing chapters | The chapters available today, without following new ones |
  | None | Nothing automatically: the series is tracked, you download by hand |

- **Search for missing chapters as soon as the work is added**, to start
  right away instead of waiting for the next scheduled pass.

Tankarr decides by itself whether a series is followed as chapters or as whole
books (volumes), from what the sources actually offer, and never mixes the
two. Download sources are mapped to the series in the background during the
next minutes. A match that the rules cannot settle is never guessed: it waits
for your decision on the **System** page.

## 6. Follow the downloads

- **Activity** shows the queue: chapter jobs, torrents and Usenet downloads
  with live progress, grouped by series.
- **Wanted** lists every monitored chapter or volume that is missing, what
  Tankarr already tried for it, and a manual search for each item.
- **Calendar** shows past and expected releases of the series you follow.
- **History** keeps every import and failure, with a retry button.
- The **series page** lists chapters or books, lets you toggle monitoring per
  chapter or volume, search releases interactively (**Interactive Search**),
  choose covers, edit titles and inspect the downloaded files (**Audit
  files**).

Imported books appear in the library folder as soon as they are complete, and
the built-in reader opens them immediately.

## 7. Bring your existing library

To add files you already own, put them in the folder mounted at `/import` and
open **Library Import**. Tankarr groups CBZ, CBR, ZIP and PDF files and image
folders into series, normalizes them to CBZ with `ComicInfo.xml` and files
them in the library. The import folder is read-only for Tankarr: your
originals are never modified or deleted.

## 8. Notifications and backups

- **Settings → Notifications** sends [ntfy](https://ntfy.sh) notifications for
  imported chapters, failed downloads and decisions that need you.
- **Settings → Data** keeps automatic application backups (seven by default)
  and a recycle bin for removed files. **System** can create a verified backup
  on demand; see [Upgrading](upgrading.md#backups).

Next: [How Tankarr works](how-it-works.md) explains the rules behind
identification, numbering and Wanted recovery, and [Integrations](integrations.md)
covers every external service in detail.
