---
description: How Tankarr works with Suwayomi, Prowlarr, qBittorrent, SABnzbd, the Internet Archive, Komga, Kavita, Stump, ntfy and the public manga catalogues, what each one is used for and how to configure it.
---

# Integrations

Tankarr runs without any other service: the built-in reader and the public
metadata catalogues work out of the box. Everything else on this page is
optional and is configured in **Settings**, one tab per area. Each panel has a
test button that checks the values in the form before you save them, and saved
values apply without a restart. Values saved in Settings are stored in
Tankarr's database and take precedence over the container environment.
Passwords and API keys are shown masked in Settings and are kept apart from the
other settings, in `metadata.env` in the data directory (`/config` in the
Docker image), readable only by its owner. Rarely needed fields appear after
turning on **Show advanced**.

Every setting also has a `TANKARR_*` environment variable. The
[configuration reference](configuration.md) lists them all with their defaults.

| Service | What Tankarr uses it for | Settings tab |
| --- | --- | --- |
| [Suwayomi](#suwayomi) | Chapters from manga sites, through Mihon/Tachiyomi-compatible extensions | Sources |
| [Prowlarr](#prowlarr) | Searching your torrent and Usenet indexers | Indexers & torrents |
| [qBittorrent](#qbittorrent) | Downloading the torrent releases found through Prowlarr | Indexers & torrents |
| [SABnzbd](#sabnzbd) | Downloading the Usenet releases found through Prowlarr | Indexers & torrents |
| [Internet Archive](#internet-archive) | Whole volumes downloaded directly from archive.org | Indexers & torrents |
| [Readers](#readers) | Opening the library in the built-in reader, Komga, Kavita, Stump or another reader | Reader |
| [ntfy](#notifications-ntfy) | Push notifications | Notifications |
| [Metadata catalogues](#metadata-catalogues) | Identifying works: titles, creators, covers, counts | Metadata |
| [AI provider](#translation-fallback) | Optional machine translation of missing books and chapters | Translation |

## Suwayomi

[Suwayomi-Server](https://github.com/Suwayomi/Suwayomi-Server) is an
open-source server that runs Mihon/Tachiyomi-compatible source extensions.
Tankarr uses it only as a source engine: through Suwayomi it searches the
sources, reads their chapter lists and fetches the pages of a chapter.
Monitoring, the download queue, CBZ packaging, the checks, naming and the
import into the library stay with Tankarr. Tankarr does not use Suwayomi's own
downloader or library updates; in managed mode they are switched off.

Configure it in **Settings → Sources**, panel **Suwayomi**. The variables are
listed under [Suwayomi](configuration.md#suwayomi) in the configuration
reference.

| | Managed (default, recommended) | External |
| --- | --- | --- |
| Who runs Suwayomi | Tankarr, inside its own container, with the Java runtime included in the image | You, as a separate server |
| Setup | **Install Suwayomi** | **Server URL**, **Username** and **Password** |
| Extensions | Installed and updated by Tankarr, manageable from the panel | Whatever is installed on your server |
| Updates | Server and extensions checked once a day | Yours to manage |

### Managed mode

Choose **Managed by Tankarr (recommended)** as the **Mode** and click
**Install Suwayomi**. Tankarr downloads the latest Suwayomi-Server release from
GitHub, verifies the server JAR against the SHA-256 checksum file published
with the release, installs it and starts it. Its data lives in the `suwayomi`
folder of the data directory. Tankarr supervises the process: it restarts it
with a back-off if it exits and stops it cleanly on shutdown. The panel shows
its status and offers **Update / reinstall**, **Check updates**, **Restart**,
**Stop** and the server **Log**.

Once a day Tankarr checks GitHub for a new server release and installs it when
no download is running, updates the installed extensions that have a newer
version, and installs new extensions for your languages as described below.
**Check updates** does the same immediately. If an updated server does not
become ready, Tankarr restores the previous server and its data.

**Java heap (MiB)** (advanced; `TANKARR_SUWAYOMI_MANAGED_HEAP_MB`, default
256, from 128 to 1024) bounds the server's memory. With many extensions
installed, 512 is a safer value; raise it if the log shows
`OutOfMemoryError`. For the memory of the whole container, see
[Requirements](installation.md#requirements).

#### Network access and login

- With a Tankarr login, which is the normal case, the managed server listens
  on port 4567 of the container's network interfaces, requires HTTP Basic
  authentication with Tankarr's own user name and password, and serves its
  bundled web interface. The credentials are written only to the server's
  `server.conf`, readable by its owner only; they never appear on the Java
  command line, and the Java process does not inherit Tankarr's `TANKARR_*`
  environment variables.
- Without a Tankarr login (possible only with `TANKARR_AUTH_REQUIRED=false`,
  on a development machine), the server listens only on the container's
  loopback interface, without authentication and without a web interface.

To open Suwayomi's own web interface, publish container port 4567 and use
**Open Suwayomi** in the panel. The button points to port 4568 on the host
name your browser uses for Tankarr, so either publish the port as `4568:4567`
or set `TANKARR_SUWAYOMI_PUBLIC_URL` to the address you published. Other
containers on the same Docker network can reach port 4567 as well, and they
also need the Tankarr login.

#### Extensions

Tankarr ships no extension repository and recommends none. Enter the index
URL of a Mihon/Tachiyomi-compatible repository you trust in **Extension
repository** (`TANKARR_SUWAYOMI_EXTENSION_STORE`); the change is applied when
the managed server restarts, and until a repository is configured the panel
has nothing to offer and **System** shows an alert. The panel lists that
repository's extensions, plus any extension already installed. After
**Install Suwayomi**, and again during the daily maintenance, Tankarr installs
every extension of the repository for your **Search languages** (Settings →
General), except:

- extensions labelled NSFW;
- extensions the repository marks as obsolete;
- extensions for sources that answer only after a browser challenge (such as a
  Cloudflare challenge or a captcha) or that need an embedded browser.

When you have chosen your sources by hand (the **Sources** field is not empty),
Tankarr leaves the installed extensions exactly as they are.

Tankarr never uninstalls an extension because it is slow, failing or obsolete.
Reliability is handled by ranking, not by removal: see
[Source ranking and health](how-it-works.md#source-ranking-and-health).

The **Extensions** list in the panel shows the repository's extensions by
language, with **NSFW**, **Update available** and **Obsolete** badges, and lets you install, update or remove any of them. An extension you
install yourself is used whatever its content label. **Test sources** searches
every enabled source for a few well-known titles and reports a verdict (good,
slow when the average answer takes more than six seconds, or unreachable), the
number of hits and the average answer time. The results also give new sources
their first place in the ranking.

**Auto-install official extensions** (`TANKARR_SUWAYOMI_AUTO_INSTALL_OFFICIAL`,
on by default, managed mode only): when a work's catalogue record names a free
official platform, for example MANGA Plus or WEBTOON, Tankarr installs that
platform's extension and maps it as a source for the work.

#### Sources that are not supported

Sources that need a challenge solver or an embedded browser are not supported.
The managed server runs without a challenge solver (its FlareSolverr support is
switched off) and without Suwayomi's embedded browser, and Tankarr does not
install extensions that depend on them. A source that answers with an
anti-bot challenge is listed on **System** under **Challenged sources** and is
ranked last.

### Choosing sources

Click **Test & load sources** to list the installed sources for your search
languages, then choose in **Sources** which ones Tankarr may use. The automatic
selection (an empty field) uses every installed source in the language of the
series being searched, including sources installed later. A manual selection
stores Suwayomi's numeric source IDs (`TANKARR_SUWAYOMI_SOURCE_IDS`). A series
is only ever searched in the sources of its own language.

### External mode

Choose **External server** as the **Mode** to use a Suwayomi server you already
run. Enter its **Server URL**, the service root without `/api/graphql` (for
example `http://suwayomi:4567`), and the **Username** and **Password** already
configured in Suwayomi, both or neither. Tankarr never creates or changes the
Suwayomi account. It uses whatever extensions are installed on that server;
install, update and extension management are available only in managed mode.

## Prowlarr

[Prowlarr](https://prowlarr.com) manages your torrent and Usenet indexers.
Through it Tankarr searches for whole volumes, packs and single chapters. It
sends torrent results to [qBittorrent](#qbittorrent) and NZB results to
[SABnzbd](#sabnzbd), then imports the finished files itself.

### Configure Prowlarr

In **Settings → Indexers & torrents**, panel **Prowlarr indexers**:

1. Turn on **Enabled** and enter the **Server URL**: the address of Prowlarr as
   Tankarr reaches it, without a path, for example `http://prowlarr:9696`.
2. Paste the **API key** from Prowlarr → Settings → General → Security.
3. Click **Test & load indexers** to load the indexers and their categories.
4. **Indexers**: the automatic selection uses every indexer that is enabled in
   Prowlarr and advertises book, literature, manga or comic categories. You can
   pick indexers instead; indexers without such categories cannot be selected.
5. **Categories**: only results in these categories are used, and at least one
   must stay selected. **Select recommended** picks the default: 7000 (Books),
   7020 (EBooks) and 7030 (Comics).

The variables are listed under
[Prowlarr and Internet Archive](configuration.md#prowlarr-and-internet-archive).

### How searches and grabs work

- **Interactive Search** on a series page, for one chapter, one book or the
  whole series, and the manual search on **Wanted** show indexer results next
  to the releases of your Suwayomi sources. Each result is labelled as a
  chapter match, a volume match or a series result and has a **Grab** button.
- The scheduled Wanted pass asks the indexers by itself for missing chapters,
  for the books that contain them, and for the missing volumes of series
  followed as books. It grabs a result on its own only when the match is
  unambiguous; anything else waits on **System → To confirm**. See
  [Wanted recovery](how-it-works.md#wanted-recovery).
- The optional translation fallback also searches the indexers for editions in
  other languages, see [Translation fallback](translation.md).

For a grab, Tankarr resolves the exact result on the server and removes any API
key from its download link. For a torrent it checks that the magnet link or the
`.torrent` file carries the expected info-hash before handing it to qBittorrent.
The Prowlarr API key never reaches the browser.

Releases from indexers are imported only into series followed in English, after
the [OCR language check](how-it-works.md#language-per-release-and-the-ocr-language-audit).

## qBittorrent

[qBittorrent](https://www.qbittorrent.org) downloads the torrent releases found
through Prowlarr. Chapters from Suwayomi never go through it.

In **Settings → Indexers & torrents**, panel **Torrent client (qBittorrent)**:

| Field | Variable | Default | Notes |
| --- | --- | --- | --- |
| **qBittorrent URL** | `TANKARR_QBITTORRENT_URL` | unset | Web UI address as Tankarr reaches it, for example `http://qbittorrent:8080` |
| **Username**, **Password** | `TANKARR_QBITTORRENT_USERNAME`, `TANKARR_QBITTORRENT_PASSWORD` | unset | Web UI login; set both |
| **Category** (advanced) | `TANKARR_QBITTORRENT_CATEGORY` | `tankarr` | Tankarr treats every torrent in this category as its own |
| **Automatic import** | `TANKARR_TORRENT_AUTO_IMPORT` | on | Import finished downloads once the numbering and language checks pass; when off, they wait in **Activity** for **Import now** |
| **After import** | `TANKARR_TORRENT_COMPLETED_ACTION` | Keep seeding | See below |
| **Orphan grace period (hours)** (advanced) | `TANKARR_TORRENT_ORPHAN_GRACE_HOURS` | 24 | See below |
| **qBittorrent link (browser)** (advanced) | `TANKARR_QBITTORRENT_PUBLIC_URL` | unset | Address a browser can open, for the shortcut on each torrent in Activity |

**Test connection** checks the address and the login.

### Paths seen by the download client

qBittorrent usually runs in its own container, where the download folder has a
different path than in Tankarr's container. Tankarr works with both, like the
remote path mappings of Sonarr and Radarr:

- `TANKARR_QBITTORRENT_SAVE_PATH` (default `/data/downloads/tankarr`) is the
  folder as qBittorrent sees it; Tankarr sends it with every torrent.
- `TANKARR_TORRENT_DOWNLOAD_DIR` (for example `/downloads`) is the same folder
  as Tankarr's container sees it. Without it, finished torrents cannot be
  imported.

Tankarr only reads these files, so a read-only mount is enough to import. With
a read-only mount, a download whose import is refused stays where it is, for
you to inspect. With a writable mount, Tankarr detaches a refused download from
qBittorrent without deleting it and moves its files into a
`.tankarr-download-recycle` folder on the download filesystem, where they are
kept for the recycle-bin retention period. See
[Paths seen by the download clients](configuration.md#paths-seen-by-the-download-clients)
and [Download clients](installation.md#download-clients) for a complete
example.

### After import

| **After import** | What happens to the torrent |
| --- | --- |
| **Keep seeding** (`seed`, default) | It stays in qBittorrent under qBittorrent's own ratio and time rules. |
| **Remove after import** (`remove_after_import`) | Tankarr deletes it and its files from qBittorrent as soon as the books are in the library. |
| **Remove when seeding is done** (`remove_when_seeded`) | Tankarr deletes it and its files once qBittorrent stops it at its seeding limits. |

About once an hour, Tankarr deletes, with their files, the torrents it added
to its category whose download was deleted afterwards (a series removed, a
chapter unmonitored) and that are older than the orphan grace period. Tankarr
keeps a record of every torrent it hands to qBittorrent and only ever removes
those: a torrent another application or you put in the same category is left
alone and listed on the System page. Torrents outside the category are never
touched.

A magnet link whose metadata no peer delivers within 30 minutes
(`TANKARR_TORRENT_METADATA_TIMEOUT_MINUTES`) is removed from qBittorrent and its
download fails, so the next candidate release gets its turn.

In **Activity**, **Remove release and staging files** removes a release and its
files from the download client; books already imported stay in the library.

## SABnzbd

[SABnzbd](https://sabnzbd.org) downloads the Usenet (NZB) releases found
through Prowlarr. In **Settings → Indexers & torrents**, panel
**Usenet client (SABnzbd)**:

| Field | Variable | Default | Notes |
| --- | --- | --- | --- |
| **SABnzbd URL** | `TANKARR_SABNZBD_URL` | unset | Address as Tankarr reaches it, for example `http://sabnzbd:8080` |
| **API key** | `TANKARR_SABNZBD_API_KEY` | unset | SABnzbd → Config → General → API Key |
| **Category** (advanced) | `TANKARR_SABNZBD_CATEGORY` | `tankarr` | Tankarr treats every download in this category as its own |
| **Completed folder (as SABnzbd sees it)** (advanced) | `TANKARR_SABNZBD_COMPLETE_PATH` | `/data/downloads/usenet` | SABnzbd's completed-downloads folder, as SABnzbd sees it |
| **SABnzbd link (browser)** (advanced) | `TANKARR_SABNZBD_PUBLIC_URL` | unset | Address a browser can open, for the shortcut in Activity |

`TANKARR_USENET_DOWNLOAD_DIR` (for example `/usenet`) is the completed folder as
Tankarr's container sees it; without it, finished Usenet downloads cannot be
imported. A read-only mount is enough. **Test connection** checks the address
and the key, and creates the category in SABnzbd when it is missing.

**Automatic import** and **After import** in the qBittorrent panel apply to
Usenet downloads too. With **Keep seeding**, a finished download stays in
SABnzbd's history; with either removal option, Tankarr deletes it and its files
from SABnzbd right after the import.

## Internet Archive

[archive.org](https://archive.org) holds scanned and digital comics as whole
volumes in CBZ, CBR and PDF. Tankarr uses it as a direct-download book source,
with no download client: it searches archive.org by the work's title (as an
exact phrase, among text items), reads each item's file list and downloads the
book file itself into the `direct-downloads` folder of the data directory. The
file then goes through the same import path as a finished torrent: identity
review, OCR language check, numbering and page quality. The downloaded copy is
removed once the book is in the library.

- Only the files the uploader provided are considered, not archive.org's own
  converted copies, and only files between 1 MB and 2 GiB.
- Tankarr asks archive.org at most once per second, once per Wanted pass for a
  series, and only for whole books, never chapter by chapter.
- Results rank below every indexer result, so archive.org is the fallback for
  books no indexer carries, typically out-of-print works.
- Like indexer releases, archive.org books are imported only into series
  followed in English.

It is on by default. In **Settings → Indexers & torrents**, panel
**Internet Archive (direct download)**, **Enabled**
(`TANKARR_INTERNET_ARCHIVE_ENABLED`) includes archive.org in the series release
search and the Wanted book search, and **Test archive.org** runs a sample search.

## Readers

Tankarr writes a plain folder of CBZ files, so any reader that opens such a
folder works with it. **Settings → Reader** decides which reader the
**Open** and **Read** links in Tankarr point to, and whether Tankarr keeps that
reader's library in step.

| **Reader** | What Tankarr does |
| --- | --- |
| **Tankarr (built-in)** | Opens the CBZ files in its own web reader. Nothing to configure. |
| **Automatic** (default) | Komga when the Komga link is enabled, the URL template when one is set, otherwise the built-in reader. |
| **Komga** | Links to Komga and, with an API key, manages its library: scans, titles and covers. |
| **Kavita** | Links to the matching Kavita series. Kavita scans the folder on its own schedule. |
| **Stump** | Links to Stump and manages its library: scans and covers. |
| **URL template** | Builds a series link from a template. |
| **No shortcut** | No reader links. |

**Default reading mode** (`TANKARR_READER_DISPLAY_MODE`) sets how the built-in
reader shows pages: **Automatic**, **Manga (right to left)** or
**Webtoon (vertical)**. See
[Built-in reader and bookmarks](how-it-works.md#built-in-reader-and-bookmarks).

**Discover reader** tries the reader addresses already configured and the usual
Docker service names (`komga:25600`, `kavita:5000`, `stump:10801`) and fills in
the fields for the reader it finds; it never scans your network, and nothing is
saved until you save. **Test reader connection** checks the current values.
When Tankarr cannot reach the browser address from its container, set
**Reader URL from inside Tankarr** (advanced) to the service name, for example
`http://komga:25600`.

### Komga

Enter the **Komga URL** (the address your browser uses) and a
**Komga API key** from Komga → Account → API keys. With both, Tankarr manages
the Komga library automatically:

- it asks Komga to scan after the download queue drains, and runs a full
  safety-net scan every 360 minutes (`TANKARR_KOMGA_REFRESH_INTERVAL_MINUTES`,
  from 15 to 10080); concurrent requests are merged into one scan;
- it publishes canonical titles, metadata and the selected covers to Komga;
- it empties Komga's trash of the records its own renames and deletions left
  behind, only once the library is proven mounted and complete, and it leaves
  the trash untouched if a single record in it cannot be explained;
- it never changes reading progress.

The API key is sent in the `X-API-Key` header. Komga can also be managed with
an administrator's user name and password over HTTP Basic, configured through
the environment: `TANKARR_KOMGA_LINK_ENABLED=true`, `TANKARR_KOMGA_URL`,
`TANKARR_KOMGA_AUTH_METHOD=basic`, `TANKARR_KOMGA_USERNAME` and
`TANKARR_KOMGA_PASSWORD`. Changing a setting in the Reader panel afterwards
replaces that setup with the panel's values. See
[Reader, Komga and Stump](configuration.md#reader-komga-and-stump).

### Stump

Enter the **Stump URL**, a **Stump username** and **Stump password**, and in
**Library path inside Stump** (advanced, default `/data/comics`) the path at
which Stump's container sees the library folder. Tankarr finds series and books
by their exact folder and file paths, never by title. It asks Stump to scan
after the download queue drains and on the same safety-net interval as Komga,
and uploads the selected covers.

### Kavita

Enter the **Reader URL** and a **Kavita API key** from Kavita → Settings →
Account → API key. Tankarr uses the key to find the Kavita series that holds a
Tankarr series' files, matched by their paths in the library, and links to it.
It does not start Kavita scans.

### URL template

**Series URL template** builds a series link from a template, for example
`https://reader.example.com/search?q={title}`. The placeholders are `{title}`,
`{title_raw}`, `{folder}` and `{id}`.

## Notifications (ntfy)

Tankarr can publish push notifications to an [ntfy](https://ntfy.sh) server,
the public one or your own. In **Settings → Notifications**, panel
**Notifications (ntfy)**:

| Field | Variable | Default |
| --- | --- | --- |
| **ntfy URL** | `TANKARR_NTFY_URL` | unset |
| **Topic** | `TANKARR_NTFY_TOPIC` | `tankarr` |
| **On chapter imported** | `TANKARR_NTFY_ON_CHAPTER_IMPORTED` | on |
| **On download failed** | `TANKARR_NTFY_ON_DOWNLOAD_FAILED` | on |
| **On decision needed** | `TANKARR_NTFY_ON_DECISION_NEEDED` | on |

Notifications are active when both the URL (the server root) and the topic are
set. Tankarr publishes without credentials, so use a topic that accepts
anonymous publishing. The three events are:

- **Chapter imported**: a verified file was written to the library (normal
  priority).
- **Download failed**: a download job reached Failed (high priority).
- **Decision needed**: new matches waiting on **System → To confirm**, and
  Wanted items that every channel has given up on ("Not obtainable"). Each item
  is announced once (normal priority).

**Send test notification** publishes a real test message with the values
currently in the form, even before you save them. A failed notification never
fails the import or the job that triggered it.

## Metadata catalogues

Tankarr identifies works through public catalogues that need no account and no
API key. **Settings → Metadata**, panel **Metadata catalogues**, shows
**MangaBaka · Always on** with a **Test connection** button, and the
**Refresh interval (hours)** (advanced; `TANKARR_METADATA_REFRESH_INTERVAL_HOURS`,
default 168, one week).

- [MangaBaka](https://mangabaka.org) is the catalogue spine. **Add New**
  searches it, and each series' record gives titles, creators, description,
  cover, type, status, counts, official platform links and the identifiers of
  the same work on the other catalogues. Author work lists are refreshed once a
  day (`TANKARR_AUTHOR_REFRESH_INTERVAL_HOURS`, default 24).
- [MangaUpdates](https://www.mangaupdates.com), [AniList](https://anilist.co),
  [Kitsu](https://kitsu.app) and [MyAnimeList](https://myanimelist.net),
  the latter through the public [Jikan](https://jikan.moe) API, are read only
  by the identifiers MangaBaka provides, never searched by title. Their counts
  and statuses corroborate MangaBaka's, see
  [Catalogue consensus](how-it-works.md#catalogue-consensus). A catalogue that
  does not answer only removes one opinion.

**System → Enrich Metadata** refreshes every series immediately. See
[How Tankarr works](how-it-works.md#identity-first) for how the catalogue
decides identity, titles and covers.

## Translation fallback

When no release exists in a series' language, Tankarr can machine-translate a
book or chapter from another language. It is off by default, both globally and
for each series. Configure **Settings → Translation** (an OpenAI-compatible
**AI provider API base URL**, **AI model** and **AI provider API key**, and
optionally an external processor under advanced settings), then enable the
fallback in each series' Edit dialog. OCR and lettering run on the Tankarr host
by default; the AI provider receives the recognized dialogue text and may
charge for it. The variables are listed under
[Translation](configuration.md#translation), and
[Translation fallback](translation.md) explains the whole process.

## External services Tankarr contacts

Besides the services you configure yourself (Prowlarr and, through it, your
indexers; qBittorrent; SABnzbd; your readers; your ntfy server; your AI
provider or translation processor), Tankarr contacts only these public
services. Its requests identify themselves with the user agent
`Tankarr/<version> (+https://github.com/Renzodef/tankarr)`.

| Service | Address | When |
| --- | --- | --- |
| MangaBaka | `api.mangabaka.org` | Searches in Add New, series and author records, metadata refreshes |
| MangaUpdates | `api.mangaupdates.com` | Metadata refreshes, by catalogue ID |
| AniList | `graphql.anilist.co` | Metadata refreshes, by catalogue ID |
| Kitsu | `kitsu.io` | Metadata refreshes, by catalogue ID |
| Jikan (MyAnimeList data) | `api.jikan.moe` | Metadata refreshes, by catalogue ID |
| Catalogue image hosts | The image servers of MangaBaka and of the catalogues its records link to (AniList, MangaUpdates, Kitsu, MyAnimeList, Anime-Planet, Anime News Network) | Downloading the covers named in catalogue records; other hosts are refused |
| NAVER Webtoon | `comic.naver.com` | Reading the official episode list of a work whose catalogue record links there, as numbering evidence only; nothing is downloaded from it |
| Internet Archive | `archive.org` | When enabled: searches, item file lists and book downloads |
| GitHub | `api.github.com`, `github.com` | Managed Suwayomi only: checking for and downloading Suwayomi-Server releases (at install and once a day), and the extension repository's index and packages |

The websites behind your Suwayomi extensions are contacted by the Suwayomi
server, managed or external, when it searches and downloads for Tankarr.
**Discover reader** contacts only the reader addresses you configured and the
Docker service names listed under [Readers](#readers).
