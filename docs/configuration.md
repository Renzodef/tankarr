---
description: Every Tankarr environment variable - its default, allowed values, and where to change it on the Settings page.
---

# Configuration reference

Tankarr is configured with environment variables whose names start with
`TANKARR_`. Most of them can also be changed on the **Settings** page of the
web interface. This page lists every variable, its default and what it does.

## How configuration works

Tankarr reads its environment variables when it starts. When it runs from
source it also reads a `.env` file in its working directory; a variable set in
the real environment wins over the same variable in that file. The `.env` file
that Docker Compose reads next to `docker-compose.yml` is a different thing:
Compose only uses it to fill in the `${...}` references of the Compose file, so
a variable reaches Tankarr only if the Compose file passes it under
`environment:`.

Write booleans as `true` or `false` and lists as comma-separated values. To
keep a default, leave the variable out: an empty value is not the same as an
unset one. An empty path means the working directory, and an empty number or
choice stops Tankarr at startup.

Values saved on the Settings page are stored in the database
(`tankarr.sqlite3` in the data directory) and from then on take precedence over
the environment, also after a restart. Clearing a field on the page saves an
empty value; it does not bring back the environment value.

Secrets (passwords, API keys and tokens) saved on the Settings page are not
stored in the database. They go to a separate file, `metadata.env` in the data
directory (`/config/metadata.env` in the container), created with mode 0600 so
that only the user Tankarr runs as can read it. A secret in that file takes
precedence over the environment. Clearing the field removes it from the file,
and the environment value, if any, applies again after the next restart.
Secrets are never sent back to the browser, whether they come from the
environment or from the secrets file: the page only shows that one is set.
Backups contain the secrets.

Changes saved on the Settings page apply immediately, without a restart.
Environment variables are read only at startup: after changing one, restart
Tankarr. With Docker, recreate the container so that it receives the new
environment, for example with `docker compose up -d`. Rows marked **Restart
required** have no field on the Settings page.

In the tables, "Settings → General" names the tab of the Settings page where a
variable can be changed. "(advanced)" means the field appears only after
switching on **Show advanced** at the top of the page; other words in
parentheses say when the field is shown.

The Settings page saves through the settings API, `PUT /api/settings`, which
uses the variable names in lower case without the prefix (`downloads_paused`
for `TANKARR_DOWNLOADS_PAUSED`). The API also accepts some settings that have no
field on the page: `downloads_paused`, `provider_priority`,
`official_edition_alignment_enabled`, `page_quality_recovery_enabled`,
`metadata_enabled`, `suwayomi_language`, `suwayomi_public_url`,
`setup_completed_at`, `komga_link_enabled`, `komga_url`, `komga_library_id`,
`komga_auth_method`, `komga_api_key`, `komga_username`, `komga_password` and
`komga_refresh_interval_minutes`. Values saved through the API apply
immediately and are stored like the ones saved on the page. Tankarr saves some
of them itself: installing the managed Suwayomi server switches Suwayomi on,
the setup checklist records its completion, and choosing Komga on the Reader
tab sets up the Komga link.

Addresses saved on the Settings page for Suwayomi, Prowlarr and qBittorrent,
and Komga addresses that use plain `http://`, must be a root address (no path)
on your own network: a host name without dots (such as the Docker service name
`prowlarr`), `localhost`, a name ending in `.local`, `.lan` or `.home.arpa`, or
a private, loopback, link-local or Tailscale (`100.64.0.0/10`) IP address. The
environment variables are not limited to local addresses.

## Container defaults

The Docker image sets these variables. Keep them, and map your own folders
onto the container paths with volumes (see [Installation](installation.md)):

```sh
TANKARR_HOST=0.0.0.0
TANKARR_PORT=8787
TANKARR_DATA_DIR=/config
TANKARR_LIBRARY_DIR=/library
TANKARR_IMPORT_DIR=/import
TANKARR_FRONTEND_DIR=/app/frontend/dist
```

The image also reads four variables that Tankarr itself never sees:

| Variable | Default | Description |
| --- | --- | --- |
| `PUID`, `PGID` | `1000`, `1000` | User and group the application runs as when the container starts as root: the owner of your folders. See [File permissions](installation.md#file-permissions). |
| `UMASK` | `022` | Permissions mask of the files Tankarr writes; `002` makes them group-writable. |
| `TZ` | `Etc/UTC` | Time zone of log timestamps, the Calendar and the nightly maintenance window. |

## Docker Compose variables

The `docker-compose.yml` in the repository uses a few variables of its own.
Compose substitutes them when it creates the container, from your shell or
from the `.env` file next to the Compose file; Tankarr never reads them.

| Variable | Default | Description |
| --- | --- | --- |
| `TANKARR_HOST_CONFIG_DIR` | `./data` | Host folder mounted at `/config`. |
| `TANKARR_HOST_LIBRARY_DIR` | `./data/library` | Host folder mounted at `/library`. |
| `TANKARR_HOST_IMPORT_DIR` | `./data/import` | Host folder mounted read-only at `/import`. |
| `TANKARR_HOST_TORRENT_DIR` | `./data/downloads/tankarr` | Host folder mounted read-only at `/downloads`: the folder where qBittorrent saves Tankarr's torrents. |
| `TANKARR_HOST_USENET_DIR` | `./data/downloads/usenet` | Host folder mounted read-only at `/usenet`: SABnzbd's completed folder. |
| `TANKARR_BIND_ADDRESS` | `127.0.0.1` | Host address the web port is published on. The default accepts connections only from the Docker host itself; `0.0.0.0` publishes it on every interface. |
| `TANKARR_IMAGE_TAG` | `latest` | Tag of `ghcr.io/renzodef/tankarr` to run: `latest`, a version such as `1.2` or `1.2.3`, or `branch-<name>` for a branch under test. See [Upgrading](upgrading.md). |

The same file publishes the web interface on the host port given by
`TANKARR_PORT` (default `8787`).

## Server and storage

| Variable | Default | Description |
| --- | --- | --- |
| `TANKARR_HOST` | `0.0.0.0` | Address the web server listens on. `0.0.0.0` accepts connections on every interface, `127.0.0.1` only from the same machine. While it is anything other than `127.0.0.1`, `::1` or `localhost`, the Settings page refuses to remove the login. Restart required. |
| `TANKARR_PORT` | `8787` | Port of the web interface and the API. Restart required. |
| `TANKARR_LOG_LEVEL` | `info` | Detail of the application log: `debug`, `info`, `warning` or `error`. The console (`docker logs`) and the log file share it; the file, `logs/tankarr.log` in the data directory, rotates at 5 MB and keeps five predecessors, and System → Logs tails and downloads it. `debug` also writes one line per HTTP request. Applies at once. Settings → General (advanced). |
| `TANKARR_URL_BASE` | *empty* | Path Tankarr is served under behind a reverse proxy, for example `/tankarr`: the interface answers at `/tankarr/`, the API at `/tankarr/api/...`, and outside the base only the health checks (`/api/health`, `/api/ready`) answer, so a container runtime need not know it. The proxy must forward the path unchanged. Empty serves everything from the root. Restart required. |
| `TANKARR_DATA_DIR` | `data`; image: `/config` | Directory for Tankarr's own data: the database, the secrets file, covers, caches, staging space for downloads, login sessions, the managed Suwayomi server and, unless `TANKARR_BACKUP_DIRECTORY` is set, the backups. A relative path starts from the working directory. Must be writable. Restart required. |
| `TANKARR_LIBRARY_DIR` | `data/library`; image: `/library` | The library Tankarr writes, one folder per series. Point your reader at the same folder. Must be writable. Restart required. |
| `TANKARR_IMPORT_DIR` | *unset*; image: `/import` | Folder that **Library Import** scans for files you already own; read access is enough. Without it, Library Import only takes files uploaded from the browser. Restart required. |
| `TANKARR_FRONTEND_DIR` | *unset*; image: `/app/frontend/dist` | Folder with the built web interface (`index.html` and `assets/`). Unset: `frontend/dist` in the source tree Tankarr runs from. If the folder does not exist, only the API is served. Restart required. |

## Languages

Languages are written as codes. Supported codes: `ar`, `bg`, `bn`, `ca`, `cs`,
`da`, `de`, `el`, `en`, `es`, `es-la`, `fa`, `fi`, `fil`, `fr`, `he`, `hi`,
`hu`, `id`, `it`, `ja`, `ja-ro`, `ko`, `ko-ro`, `lt`, `mn`, `ms`, `my`, `nl`,
`no`, `pl`, `pt`, `pt-br`, `ro`, `ru`, `sr`, `sv`, `th`, `tr`, `uk`, `vi`,
`zh`, `zh-hk`, `zh-ro`.

| Variable | Default | Description |
| --- | --- | --- |
| `TANKARR_SEARCH_LANGUAGES` | `en` | The translation languages you want, comma-separated, for example `en,it`. Searches and new series must use one of them. In managed mode, Suwayomi extensions are installed for these languages unless `TANKARR_SUWAYOMI_SOURCE_IDS` lists sources. Settings → General. |
| `TANKARR_DEFAULT_LANGUAGE` | `en` | Language preselected for searches and new series. It must be one of the search languages, otherwise Tankarr does not start. Settings → General. |

## Monitoring and Wanted

The release monitor checks monitored series for new chapters. The Wanted
search looks again, on its own schedule, for monitored chapters and books that
are still missing from the library. A series whose Wanted searches keep finding
nothing is looked at less often: after 6 hours, then 1, 3, 7 and 30 days. A
queued release or a newly found source resets that, and a search you start
yourself ignores it.

| Variable | Default | Description |
| --- | --- | --- |
| `TANKARR_MONITOR_ENABLED` | `true` | Run the release monitor. When `false`, the scheduled Wanted search does not run either. Settings → General. |
| `TANKARR_MONITOR_INTERVAL_SECONDS` | `900` | Pause between two monitor cycles, in seconds: 5 to 86400 (the Settings page accepts 60 to 86400). Settings → General (advanced). |
| `TANKARR_WANTED_SEARCH_ENABLED` | `true` | Run the Wanted search on a schedule. The upgrade and cleanup passes described under [Sources and acquisition preferences](#sources-and-acquisition-preferences) run at the end of every Wanted pass. Settings → General. |
| `TANKARR_WANTED_SEARCH_INTERVAL_SECONDS` | `21600` | Time between scheduled Wanted passes, in seconds: 900 to 604800 (21600 is 6 hours). Settings → General (advanced). |
| `TANKARR_WANTED_SEARCH_BUDGET` | `25` | Most series one Wanted pass looks at, most overdue first; the others wait for the next pass. Also applies to passes you start yourself. 1 to 500. Restart required. |

## Sources and acquisition preferences

These settings decide which release Tankarr takes when several sources offer
the same chapter or book, and what it may do with files you already have. A
chapter published in the last 21 days counts as new; anything older is
backlog. The release policy itself, `TANKARR_RELEASE_ACQUISITION_POLICY`, is
described under [Advanced](#advanced).

| Variable | Default | Description |
| --- | --- | --- |
| `TANKARR_PREFER_OFFICIAL_RELEASES` | `true` | Older form of the release policy: `true` means `prefer_official`, `false` means `first_available`. Used only while `TANKARR_RELEASE_ACQUISITION_POLICY` is unset and no policy has been saved on the Settings page. Restart required. |
| `TANKARR_RELEASE_PREFERENCE_PROFILE` | `balanced` | Which kind of source comes first: `balanced` keeps Tankarr's normal order for new chapters and backlog, `official` puts publisher platforms first, `curated` puts curated scanlation catalogues first. Language and numbering rules always apply, and the source lists below take precedence. Settings → Sources. |
| `TANKARR_SOURCE_PRIORITY_FRESH` | *empty* | Optional source order for new chapters, comma-separated, for example `suwayomi:mangaplus,suwayomi:weebcentral`. Each entry is a provider (`suwayomi`), `provider:source-id` or `provider:source-name`, using letters, digits, `-` and `_`. A source name of 4 or more characters also matches longer names that start with it. Unlisted sources come after the listed ones, in their usual order. Settings → Sources. |
| `TANKARR_SOURCE_PRIORITY_BACKFILL` | *empty* | The same for backlog chapters and for upgrades. Empty: the order of `TANKARR_PROVIDER_PRIORITY`. Settings → Sources. |
| `TANKARR_SOURCE_UPGRADE_ENABLED` | `false` | Replace a chapter you own when a strictly preferred source offers the same chapter in the same language and numbering. The old file stays until the replacement is verified. Runs during each Wanted pass. Settings → Sources. |
| `TANKARR_OFFICIAL_UPGRADE_ENABLED` | `false` | Replace a non-official file when the same chapter later appears on the work's official platform, whatever the release policy. Runs during each Wanted pass. Settings → Sources. |
| `TANKARR_OFFICIAL_EDITION_ALIGNMENT_ENABLED` | `true` | Keep monitored series on the numbering of the official edition in the series language: chapter files numbered beyond what the publisher has released belong to another edition and are moved to the recycle bin during each Wanted pass. Set to `false` to keep them. Restart required. |
| `TANKARR_PAGE_QUALITY_RECOVERY_ENABLED` | `true` | During each Wanted pass, compare imported chapters with the page size of the rest of the series and download a replacement for those that carry only a fraction of it, when another source has the chapter. Nothing is deleted without a replacement. Restart required. |
| `TANKARR_DUPLICATE_CLEANUP_ENABLED` | `true` | When the map of chapters to volumes, checked against the pages, proves that a chapter file is already inside a book you own, move the chapter file to the recycle bin during the next Wanted pass (at most 10 series per pass). When `false`, duplicates are only reported on the series page. Settings → Sources. |
| `TANKARR_PREFERRED_UNIT` | `volumes` | `volumes` or `chapters`: which of the two Tankarr collects when both whole books and chapters could complete a finished work. A work that is still running follows chapters unless only books exist. Settings → Sources. |
| `TANKARR_SPECIAL_CHAPTERS_OUTSIDE_BOOKS` | `false` | On a series shelf made of books, also show rows for specials that no book contains: half chapters, omake, a story a source published on its own. They are never counted as missing either way. Settings → Sources. |

## Suwayomi

Suwayomi runs the Mihon/Tachiyomi-compatible extensions that Tankarr uses as chapter
sources. In `managed` mode Tankarr downloads, runs and updates the official
Suwayomi-Server inside its own container (the image includes Java). In
`external` mode it connects to a Suwayomi server you run yourself.

| Variable | Default | Description |
| --- | --- | --- |
| `TANKARR_SUWAYOMI_ENABLED` | `false` | Use Suwayomi as a chapter source. Installing the managed server from Settings → Sources switches this on. Settings → Sources. |
| `TANKARR_SUWAYOMI_MODE` | `managed` | `managed` or `external`. A managed server listens only inside the container unless Tankarr has a login; then it also serves its own web interface on container port 4567, protected by the same user name and password. Settings → Sources. |
| `TANKARR_SUWAYOMI_MANAGED_HEAP_MB` | `256` | Maximum Java heap of the managed server, in MiB: 128 to 1024. With a large extension catalogue, 512 is a safer value; raise it if the log shows `OutOfMemoryError`. Saving a new value restarts the managed server. Settings → Sources (advanced, managed mode). |
| `TANKARR_SUWAYOMI_AUTO_INSTALL_OFFICIAL` | `true` | Managed mode: when a work's catalogue record names a free official platform (for example MANGA Plus, WEBTOON, Tapas, Comikey or Manga UP!), install that platform's extension so it becomes a source for the work. Settings → Sources (managed mode). |
| `TANKARR_SUWAYOMI_URL` | `http://suwayomi:4567` | Root address of your own Suwayomi server, without `/api/graphql`. Used only in external mode. Settings → Sources (external mode). |
| `TANKARR_SUWAYOMI_USERNAME` | *unset* | User name configured on your Suwayomi server, in external mode. Set it together with the password. Settings → Sources (external mode). |
| `TANKARR_SUWAYOMI_EXTENSION_STORE` | *empty* | Index URL (https) of the Mihon/Tachiyomi-compatible extension repository the managed server installs extensions from, usually ending in `index.min.json`. Tankarr ships no repository and recommends none: choose one you trust. Empty: no extensions can be installed and **System** shows an alert. Applied when the managed server restarts. Settings → Sources (managed mode). |
| `TANKARR_SUWAYOMI_PASSWORD` | *unset* | Password of that user. Secret; stored in the secrets file. Settings → Sources (external mode). |
| `TANKARR_SUWAYOMI_PUBLIC_URL` | *unset* | Address a browser uses for the **Open Suwayomi** link on Settings → Sources, for example `http://nas.local:4567`. Unset: in managed mode, port 4568 on the host name the browser uses for Tankarr; in external mode, `TANKARR_SUWAYOMI_URL`. Restart required. |
| `TANKARR_SUWAYOMI_LANGUAGE` | `en` | Language of the sources that **Test & load sources** lists and counts, and the language assumed for a source that does not report one. Searches use the language of the series, not this value. Restart required. |
| `TANKARR_SUWAYOMI_SOURCE_IDS` | *empty* | Comma-separated numeric IDs of the Suwayomi sources to use, as listed by **Test & load sources**. Empty: every installed source in the language being searched, including sources installed later; in managed mode Tankarr then also installs the store's extensions for the search languages, except NSFW, obsolete ones and a few that cannot work inside its container. Settings → Sources. |
| `TANKARR_SUWAYOMI_SEARCH_TIMEOUT_SECONDS` | `30` | Time limit for one search on one Suwayomi source, and for each source probe, in seconds: 2 to 120. Restart required. |

## Prowlarr and Internet Archive

Prowlarr gives Tankarr access to your torrent and Usenet indexers. Torrent
releases are sent to qBittorrent, NZB releases to SABnzbd. archive.org is
searched and downloaded directly.

| Variable | Default | Description |
| --- | --- | --- |
| `TANKARR_PROWLARR_ENABLED` | `false` | Search your indexers through Prowlarr for books and chapters: in Interactive Search on series and Wanted, and in the automatic Wanted recovery, which grabs only unambiguous matches. Needs the API key. Settings → Indexers & torrents. |
| `TANKARR_PROWLARR_URL` | `http://prowlarr:9696` | Root address of Prowlarr as Tankarr reaches it, without a path. Settings → Indexers & torrents. |
| `TANKARR_PROWLARR_API_KEY` | *unset* | Prowlarr API key, from Prowlarr → Settings → General → Security. Secret; stored in the secrets file. Settings → Indexers & torrents. |
| `TANKARR_PROWLARR_INDEXER_IDS` | *empty* | Comma-separated Prowlarr indexer IDs to search. Empty: every indexer enabled in Prowlarr; the category filter still applies. Settings → Indexers & torrents. |
| `TANKARR_PROWLARR_CATEGORIES` | `7000,7020,7030` | Comma-separated Newznab category IDs to search. Results in other categories are ignored. The default covers Books, Books/EBook and Books/Comics. Settings → Indexers & torrents. |
| `TANKARR_INTERNET_ARCHIVE_ENABLED` | `true` | Use archive.org as a source of whole volumes (CBZ, CBR, PDF), searched by title in the series release search and the Wanted book search. Tankarr downloads the file itself, without a download client, and applies the same identity, language and page-quality checks as to any release. archive.org ranks below every indexer. Settings → Indexers & torrents. |

## qBittorrent

qBittorrent downloads the torrent releases found through Prowlarr. Tankarr uses
it only when the address, the user name and the password are all set. The
polling, import and after-import settings in this table also apply to SABnzbd.

| Variable | Default | Description |
| --- | --- | --- |
| `TANKARR_QBITTORRENT_URL` | *unset* | Root address of the qBittorrent Web UI as Tankarr reaches it, for example `http://qbittorrent:8080`. Settings → Indexers & torrents. |
| `TANKARR_QBITTORRENT_PUBLIC_URL` | *unset* | Address a browser can open, for example `http://nas.local:8080`, for the qBittorrent link on each torrent in Activity. Unset: no link. Root address only. Restart required. |
| `TANKARR_QBITTORRENT_USERNAME` | *unset* | qBittorrent Web UI user name. Set it together with the password; it cannot contain `:`. Settings → Indexers & torrents. |
| `TANKARR_QBITTORRENT_PASSWORD` | *unset* | qBittorrent Web UI password. Secret; stored in the secrets file. Settings → Indexers & torrents. |
| `TANKARR_QBITTORRENT_CATEGORY` | `tankarr` | qBittorrent category of Tankarr's torrents: letters, digits, `.`, `_` and `-`, at most 50 characters. Tankarr creates it with `TANKARR_QBITTORRENT_SAVE_PATH` as its save path and refuses an existing category with a different save path. Every torrent in this category is treated as Tankarr's own. Settings → Indexers & torrents (advanced). |
| `TANKARR_QBITTORRENT_SAVE_PATH` | `/data/downloads/tankarr` | Folder where qBittorrent saves Tankarr's torrents, as qBittorrent sees it: an absolute path other than `/`. See [Paths seen by the download clients](#paths-seen-by-the-download-clients). Restart required. |
| `TANKARR_TORRENT_DOWNLOAD_DIR` | *unset* | The same folder as Tankarr sees it, for example `/downloads`. Without it, finished torrents cannot be imported. Restart required. |
| `TANKARR_TORRENT_POLL_INTERVAL_SECONDS` | `10` | How often Tankarr checks the downloads it handed to qBittorrent, SABnzbd or archive.org, in seconds: 2 to 300. Restart required. |
| `TANKARR_TORRENT_AUTO_IMPORT` | `true` | Import finished torrent, Usenet and archive.org downloads automatically once the numbering and OCR language checks pass. When `false`, finished downloads wait in Activity for you to import them. Settings → Indexers & torrents. |
| `TANKARR_TORRENT_COMPLETED_ACTION` | `seed` | What happens after import. `seed`: leave the torrent to qBittorrent's own ratio and time rules, and the Usenet download in SABnzbd's history. `remove_after_import`: delete the torrent or Usenet download and its files as soon as the books are in the library. `remove_when_seeded`: delete a torrent with its files once qBittorrent stops it at its seeding limits; Usenet downloads are deleted right after import. Settings → Indexers & torrents. |
| `TANKARR_TORRENT_ORPHAN_GRACE_HOURS` | `24` | A torrent Tankarr added to its category whose download was deleted afterwards is removed with its files once it is older than this, in hours: 1 to 720. Checked about once an hour. Torrents Tankarr did not add are never removed; the System page lists them. Settings → Indexers & torrents (advanced). |
| `TANKARR_TORRENT_METADATA_TIMEOUT_MINUTES` | `30` | A magnet link whose metadata no peer delivers within this many minutes is removed from qBittorrent and its download fails, so that the next candidate release, for example a Usenet book, gets its turn. 5 to 1440. Restart required. |

## SABnzbd

SABnzbd downloads the Usenet (NZB) releases found through Prowlarr. Tankarr
uses it when both the address and the API key are set.

| Variable | Default | Description |
| --- | --- | --- |
| `TANKARR_SABNZBD_URL` | *unset* | Address of SABnzbd as Tankarr reaches it, for example `http://sabnzbd:8080`. Settings → Indexers & torrents. |
| `TANKARR_SABNZBD_PUBLIC_URL` | *unset* | Address a browser can open, for example `http://nas.local:8080`, for the SABnzbd link on each Usenet download in Activity. Unset: no link. Root address only. Restart required. |
| `TANKARR_SABNZBD_API_KEY` | *unset* | SABnzbd API key, from SABnzbd → Config → General. Secret; stored in the secrets file. Settings → Indexers & torrents. |
| `TANKARR_SABNZBD_CATEGORY` | `tankarr` | SABnzbd category of Tankarr's downloads. Tankarr creates it when it is missing, with a folder of the same name inside SABnzbd's completed folder. Every download in this category is treated as Tankarr's own. Settings → Indexers & torrents (advanced). |
| `TANKARR_SABNZBD_COMPLETE_PATH` | `/data/downloads/usenet` | SABnzbd's completed-downloads folder as SABnzbd sees it. See [Paths seen by the download clients](#paths-seen-by-the-download-clients). Settings → Indexers & torrents (advanced). |
| `TANKARR_USENET_DOWNLOAD_DIR` | *unset* | The same folder as Tankarr sees it, for example `/usenet`. Without it, finished Usenet downloads cannot be imported. Restart required. |

### Paths seen by the download clients

qBittorrent and SABnzbd usually run in their own containers, where the download
folder has a different path than in Tankarr's container. Tankarr therefore
works with two paths for each client, the same idea as the remote path mappings
of Sonarr and Radarr:

- `TANKARR_QBITTORRENT_SAVE_PATH` is the folder as qBittorrent sees it. Tankarr
  sends it with every torrent, and qBittorrent writes there.
  `TANKARR_TORRENT_DOWNLOAD_DIR` is the same folder as Tankarr sees it.
- `TANKARR_SABNZBD_COMPLETE_PATH` is SABnzbd's completed-downloads folder as
  SABnzbd sees it. `TANKARR_USENET_DOWNLOAD_DIR` is the same folder as Tankarr
  sees it.

When a client reports a finished download, Tankarr replaces the client's path
with its own and imports the files from there. A download reported outside the
client's path is refused. Tankarr only reads these files, so a read-only mount
is enough for importing.

For example, with both clients mounting `/path/to/downloads` at
`/data/downloads`, and SABnzbd's completed folder set to
`/data/downloads/usenet`:

```yaml
services:
  qbittorrent:
    volumes:
      - /path/to/downloads:/data/downloads
  sabnzbd:
    volumes:
      - /path/to/downloads:/data/downloads
  tankarr:
    environment:
      TANKARR_QBITTORRENT_SAVE_PATH: /data/downloads/tankarr
      TANKARR_TORRENT_DOWNLOAD_DIR: /downloads
      TANKARR_SABNZBD_COMPLETE_PATH: /data/downloads/usenet
      TANKARR_USENET_DOWNLOAD_DIR: /usenet
    volumes:
      - /path/to/downloads/tankarr:/downloads:ro
      - /path/to/downloads/usenet:/usenet:ro
```

A torrent that qBittorrent reports at `/data/downloads/tankarr/Series v01-05`
is read from `/downloads/Series v01-05`. A Usenet download that SABnzbd stores
at `/data/downloads/usenet/tankarr/Series.v06` (inside the `tankarr` category
folder) is read from `/usenet/tankarr/Series.v06`.

## Downloads, imports and limits

| Variable | Default | Description |
| --- | --- | --- |
| `TANKARR_DOWNLOADS_PAUSED` | `false` | Pause the chapter download queue: chapters already downloading finish, and queued jobs wait, also across restarts, until this is `false` again. Downloads handed to qBittorrent or SABnzbd, library imports and external translation processors are not affected. Restart required. |
| `TANKARR_DOWNLOAD_CONCURRENCY` | `12` | Most pages downloaded at the same time for one chapter: 1 to 12. Tankarr lowers the effective value itself when latency, source errors, memory pressure or temperature call for it. Settings → General (advanced). |
| `TANKARR_DOWNLOAD_PIPELINE_MAX` | `0` | Most chapters processed at the same time: 0 to 64. `0` lets Tankarr decide from the live CPU, memory, network and source load; a positive value only sets a ceiling. Settings → General (advanced). |
| `TANKARR_REQUEST_TIMEOUT_SECONDS` | `30` | Timeout of HTTP requests to Suwayomi, Prowlarr, qBittorrent, archive.org and the metadata catalogues, in seconds. Restart required. |
| `TANKARR_IMPORT_MAX_EXPANDED_BYTES` | `17179869184` (16 GiB) | Largest uncompressed size accepted for one import, in bytes: 1048576 (1 MiB) to 1099511627776 (1 TiB). Larger imports are refused. The Settings page shows this value in MiB. Settings → General (advanced). |
| `TANKARR_IMPORT_MAX_PAGES` | `20000` | Most pages accepted in one import: 1 to 100000. Settings → General (advanced). |
| `TANKARR_IMPORT_SUBPROCESS_MEMORY_MB` | `1024` | Memory limit of the helper process that unpacks an archive during an import, in MiB: 128 to 16384. It is a ceiling, not a concurrency setting. Settings → General (advanced). |
| `TANKARR_IMPORT_DISK_RESERVE_BYTES` | `536870912` (512 MiB) | Free disk space, in bytes, that an import must leave untouched; an import that would use it is refused. 0 to 1099511627776 (1 TiB). The setup checks also flag a data, library, import or download folder with less free space than this. The Settings page shows this value in MiB. Settings → General (advanced). |

## Backups and recycle bin

Nightly maintenance runs between 03:00 and 06:00 local time (set `TZ` for the
container) while no download or import is active. It writes a backup and
removes recycle bin entries older than the retention period.

| Variable | Default | Description |
| --- | --- | --- |
| `TANKARR_BACKUP_DIRECTORY` | *unset* | Folder for the nightly backups (`tankarr-*.zip`, with the database, the settings and the secrets). Unset: `backups` inside the data directory. Use a folder on another disk so that a backup survives the loss of the data disk, and keep it private. Restart required. |
| `TANKARR_BACKUP_RETENTION_COUNT` | `7` | Number of automatic backups to keep: 1 to 365. Older ones are removed. Settings → Data. |
| `TANKARR_RECYCLE_BIN_RETENTION_DAYS` | `7` | Days a deleted or replaced file stays in the recycle bin before nightly maintenance removes it for good: 1 to 365. Library files wait in hidden `.tankarr-delete-*` folders inside the library. Settings → Data. |

## Authentication

Tankarr always has a login. A login saved under **Settings → Security** comes
first, then `TANKARR_AUTH_USERNAME` and `TANKARR_AUTH_PASSWORD`. With neither,
the first start creates the user `admin` with a random password, prints it once
to the log and keeps it in `generated-login.json` in the data directory (mode
0600) until you save a login on the Settings page or set the two variables.

Failed sign-ins are logged with the client address and slowed down after five
attempts from the same address, up to one attempt per minute.

Other applications (dashboards, scripts, a mobile client) authenticate with the
**API key** instead of the login: they send it in the `X-Api-Key` header, or as
a bearer token (`Authorization: Bearer <key>`, what a Prometheus scrape can
send). The
key is created on the first start in `api-key` in the data directory (mode
0600) and shown under **Settings → Security**, where it can be regenerated; the
old key stops working at once. It grants the same access as the login, and a
wrong key is slowed down like a wrong password.

```sh
curl -H "X-Api-Key: $API_KEY" http://localhost:8787/api/system/health
```

| Variable | Default | Description |
| --- | --- | --- |
| `TANKARR_AUTH_REQUIRED_FOR_LOCAL` | `true` | `false` lets requests from private and loopback addresses in without a login, like "Disabled for local addresses" in the other *arr applications. Behind a reverse proxy set `TANKARR_AUTH_TRUSTED_PROXIES`, so the forwarded client address is used; forwarding headers from an address Tankarr does not trust never count as local. Settings → Security. |
| `TANKARR_AUTH_TRUSTED_PROXIES` | *empty* | Comma-separated addresses or networks (`172.18.0.2`, `10.0.0.0/8`) of the reverse proxies whose forwarded client address is believed. Required by the `external` method, which accepts requests from these addresses only. Settings → Security. |
| `TANKARR_AUTH_METHOD` | `forms` | `forms`: a login page with a session cookie that lasts 12 hours, or 30 days with **Remember me**. `basic`: the browser's own credential prompt. `external`: a reverse proxy (Authelia, Authentik, Caddy `forward_auth`) signs users in and Tankarr accepts requests from `TANKARR_AUTH_TRUSTED_PROXIES` only, showing the name from the `Remote-User` header. API clients can always use HTTP Basic authentication or the API key. Settings → Security. |
| `TANKARR_AUTH_USERNAME` | *unset* | Login user name; it cannot contain `:`. Set it together with the password: setting only one of the two stops Tankarr at startup. Changing it signs out existing browser sessions. Settings → Security. |
| `TANKARR_AUTH_REQUIRED` | `true` | `false` lets an installation without credentials run with no login at all. Only for a development machine that nothing else can reach. Restart required. |
| `TANKARR_AUTH_PASSWORD` | *unset* | Login password, at least 8 characters when set on the Settings page. The managed Suwayomi server uses the same user name and password. Secret; stored in the secrets file. Settings → Security. |

## Reader, Komga and Stump

Tankarr has a built-in reader. It can instead link series and books to an
external reader that reads the same library folder: Komga, Kavita, Stump, or
any web reader through a URL template. Tankarr also keeps Komga (through the
Komga link below) or Stump (once its account is set) scanned and up to date.

| Variable | Default | Description |
| --- | --- | --- |
| `TANKARR_READER_KIND` | `auto` | Reader that series and book links open: `tankarr` (built-in), `komga`, `kavita`, `stump`, `url` (the series URL template) or `none`. `auto` means Komga when the Komga link is enabled, otherwise the URL template when one is set, otherwise the built-in reader. Settings → Reader. |
| `TANKARR_READER_DISPLAY_MODE` | `auto` | Default mode of the built-in reader: `manga` (pages, right to left unless the series says otherwise), `webtoon` (one continuous vertical strip) or `auto` (chosen per series from its metadata, then from the shape of its pages). A choice made on a series wins. Settings → Reader. |
| `TANKARR_READER_URL` | *unset* | Address of the external reader as your browser reaches it, starting with `http://` or `https://`, for example `http://nas.local:25600` (Komga), `http://nas.local:5000` (Kavita) or `http://nas.local:10801` (Stump). Settings → Reader (Komga, Kavita or Stump). |
| `TANKARR_READER_INTERNAL_URL` | *unset* | Address Tankarr uses for the reader's API when the browser address does not work from inside Tankarr's container, typically the Docker service name, for example `http://kavita:5000`. Settings → Reader (advanced). |
| `TANKARR_READER_API_KEY` | *unset* | API key of Komga (Account → API keys) or Kavita (Settings → Account). Secret; stored in the secrets file. Settings → Reader (Komga or Kavita). |
| `TANKARR_READER_USERNAME` | *unset* | Stump account Tankarr uses to look up series and books. Settings → Reader (Stump). |
| `TANKARR_READER_PASSWORD` | *unset* | Password of that Stump account. Secret; stored in the secrets file. Settings → Reader (Stump). |
| `TANKARR_READER_LIBRARY_PATH` | `/data/comics` | Path of Tankarr's library folder inside the Stump container. Stump series and books are matched by exact folder and file path, never by title. Settings → Reader (advanced, Stump). |
| `TANKARR_READER_SERIES_URL_TEMPLATE` | *unset* | Series link for the `url` reader, starting with `http://` or `https://`, for example `https://reader.example.com/search?q={title}`. Placeholders: `{title}`, `{folder}` and `{id}` are URL-encoded, `{title_raw}` is inserted as it is. Settings → Reader (URL template). |

### Komga link

The Komga link is Tankarr's own connection to a Komga library. Choosing
**Komga** on Settings → Reader with an address and an API key fills in these
values and switches the link on; choosing another reader there switches it off.
Values saved that way take precedence over the variables below.

| Variable | Default | Description |
| --- | --- | --- |
| `TANKARR_KOMGA_LINK_ENABLED` | `false` | Keep a Komga library in step with Tankarr: targeted scans after imports, a periodic full scan, and Tankarr's titles, metadata and covers. Reading progress in Komga is left alone. While the link is on, Tankarr sets the Komga library to hash files, to keep its trash after a scan, and not to scan on its own schedule or at startup. Needs `TANKARR_KOMGA_URL` and the credentials of the chosen method, otherwise Tankarr does not start. Restart required. |
| `TANKARR_KOMGA_URL` | *unset* | Root address of Komga, without a path, used for its API and for browser links, for example `http://nas.local:25600`. Restart required. |
| `TANKARR_KOMGA_INTERNAL_URL` | *unset* | Address Tankarr uses for Komga's API when `TANKARR_KOMGA_URL` does not work from inside its container, for example `http://komga:25600`. Root address only. Restart required. |
| `TANKARR_KOMGA_LIBRARY_ID` | *unset* | ID of the Komga library to manage, at most 100 characters without spaces. May stay unset when Komga has exactly one library. Restart required. |
| `TANKARR_KOMGA_AUTH_METHOD` | `auto` | How Tankarr signs in to Komga: `api_key`, `basic` (user name and password) or `auto` (the API key when one is set, otherwise the user name and password when both are set). Restart required. |
| `TANKARR_KOMGA_API_KEY` | *unset* | Komga API key, sent as the `X-API-Key` header. Secret; stored in the secrets file. Restart required. |
| `TANKARR_KOMGA_USERNAME` | *unset* | Komga user name for the `basic` method. Set it together with the password; it cannot contain `:`. Restart required. |
| `TANKARR_KOMGA_PASSWORD` | *unset* | Komga password for the `basic` method. Secret; stored in the secrets file. Restart required. |
| `TANKARR_KOMGA_REFRESH_INTERVAL_MINUTES` | `360` | Interval of the full library scan that Tankarr starts in Komga or Stump as a safety net for changes made outside Tankarr, in minutes: 15 to 10080. Imports start their own scans right away. Restart required. |
| `TANKARR_KOMGA_RECONCILE_TIMEOUT_SECONDS` | `600` | How long Tankarr waits for a Komga or Stump scan to show the expected books, in seconds: 1 to 3600. Restart required. |

## Metadata

MangaBaka identifies each work and provides its counts, description and
official links. The MangaUpdates, AniList, Kitsu and MyAnimeList records it
links to are read by ID only, as second opinions on the numbers. The catalogue
addresses are listed under [Advanced](#advanced).

| Variable | Default | Description |
| --- | --- | --- |
| `TANKARR_METADATA_ENABLED` | `true` | Look up series in the metadata catalogues. Settings → Metadata shows it as always on and cannot change it; set it to `false` only to stop every catalogue request. Restart required. |
| `TANKARR_METADATA_REFRESH_INTERVAL_HOURS` | `168` | How long the metadata of an existing series is kept before the catalogues are asked again, in hours: 1 to 8760 (168 is one week). New series are looked up within minutes. Settings → Metadata (advanced). |
| `TANKARR_AUTHOR_REFRESH_INTERVAL_HOURS` | `24` | How often each author's list of works is refreshed from MangaBaka, in hours: 1 to 720. Restart required. |

## Notifications

Tankarr sends the same events to every channel that is configured: an ntfy
topic, a generic webhook, a Discord channel, a Telegram chat and an Apprise
server, which reaches most other services. Imports and decisions use normal
priority, failures high priority. A failed notification never fails the import
or the job that triggered it, and failures are logged by type and HTTP status
only, never with the URL, which may hold a token.

| Variable | Default | Description |
| --- | --- | --- |
| `TANKARR_NTFY_ON_CHAPTER_IMPORTED` | `true` | Notify when a chapter or book has been written to the library. Applies to every channel. Settings → Notifications. |
| `TANKARR_NTFY_ON_DOWNLOAD_FAILED` | `true` | Notify, with high priority, when a download job fails. Applies to every channel. Settings → Notifications. |
| `TANKARR_NTFY_ON_DECISION_NEEDED` | `true` | Notify once for each new match review and for each Wanted item that every source has given up on ("Not obtainable"). Applies to every channel. Settings → Notifications. |
| `TANKARR_NTFY_URL` | *unset* | Root address of the ntfy server, for example `https://ntfy.sh` or `http://nas.local:8081`. On iOS it must match the default server of the ntfy app exactly. Settings → Notifications. |
| `TANKARR_NTFY_TOPIC` | `tankarr` | Topic to publish to; subscribe to the same topic in the ntfy app. Settings → Notifications. |
| `TANKARR_WEBHOOK_URL` | *unset* | Address that receives one JSON document per event (`event`, `title`, `message`, `priority`, `tags`, `at`, `data`, `application`, `version`) by POST, for Home Assistant, n8n or a script of your own. Settings → Notifications. |
| `TANKARR_WEBHOOK_TOKEN` | *unset* | Sent as `Authorization: Bearer <token>` with every webhook request. Secret; stored in the secrets file. Settings → Notifications. |
| `TANKARR_DISCORD_WEBHOOK_URL` | *unset* | Webhook URL of a Discord channel (Channel settings → Integrations → Webhooks). Events arrive as embeds, red for failures. Secret; stored in the secrets file. Settings → Notifications. |
| `TANKARR_TELEGRAM_BOT_TOKEN` | *unset* | Token of a Telegram bot created with @BotFather; set together with the chat ID. Secret; stored in the secrets file. Settings → Notifications. |
| `TANKARR_TELEGRAM_CHAT_ID` | *unset* | Chat, group or channel the bot posts to (a number, negative for groups). Settings → Notifications. |
| `TANKARR_APPRISE_URL` | *unset* | Root address of an [Apprise API](https://github.com/caronc/apprise-api) server, for example `http://apprise:8000`. Set together with a configuration key or notification URLs. Settings → Notifications. |
| `TANKARR_APPRISE_KEY` | *unset* | Key of a configuration stored on the Apprise server; Tankarr posts to `/notify/<key>`. Settings → Notifications. |
| `TANKARR_APPRISE_URLS` | *unset* | Comma-separated Apprise notification URLs (`mailto://…`, `pover://…`, …) sent with each request when no configuration key is set. Secret; stored in the secrets file. Settings → Notifications. |

## Translation

Optional machine translation of missing chapters or books from another
language. It has to be switched on here and on each series; see
[Translation](translation.md) for how it works.

| Variable | Default | Description |
| --- | --- | --- |
| `TANKARR_TRANSLATION_ENABLED` | `false` | Allow translation fallback. It also has to be enabled on each series, and it only runs when the AI provider URL, model and API key are set. Settings → Translation. |
| `TANKARR_TRANSLATION_SOURCE_LANGUAGES` | `original` | Languages to translate from, in order of preference: 1 to 12 comma-separated language codes, for example `original,ja,en,fr`. `original` stands for the work's original language when it is known. The series' own language is skipped. A series can override this list. Settings → Translation. |
| `TANKARR_TRANSLATION_AI_URL` | *unset* | Base URL of an AI provider with an OpenAI-compatible chat API, including its API path when the provider needs one. HTTP or HTTPS, without credentials, query or fragment. Settings → Translation. |
| `TANKARR_TRANSLATION_AI_MODEL` | *empty* | Model identifier at that provider. Settings → Translation. |
| `TANKARR_TRANSLATION_AI_API_KEY` | *unset* | API key of the AI provider. An external processor, when configured, receives it too. Secret; stored in the secrets file. Settings → Translation. |
| `TANKARR_TRANSLATION_PROCESSOR_URL` | *unset* | Address of an external translation processor for OCR and lettering. When set, it replaces the local processing on the Tankarr host. Settings → Translation (advanced). |
| `TANKARR_TRANSLATION_PROCESSOR_TOKEN` | *unset* | Bearer token Tankarr sends to the external processor, if the processor requires one. It is not the AI provider key. Secret; stored in the secrets file. Settings → Translation (advanced). |

## Advanced

Internal settings and service addresses that rarely need changing.

| Variable | Default | Description |
| --- | --- | --- |
| `TANKARR_RELEASE_ACQUISITION_POLICY` | *unset* | The **New release policy** of Settings → Sources: `prefer_official` (official platforms first when they have the chapter) or `first_available` (the earliest published release wins; source order only breaks ties). Unset: follows `TANKARR_PREFER_OFFICIAL_RELEASES`. The retired value `official_only` stops Tankarr at startup. Settings → Sources. |
| `TANKARR_PROVIDER_PRIORITY` | `suwayomi` | Order of the direct chapter-download providers. `suwayomi` is currently the only one; an unknown name stops Tankarr at startup. It is also the backlog order when `TANKARR_SOURCE_PRIORITY_BACKFILL` is empty. Restart required. |
| `TANKARR_LEGACY_LIBRARY_ROOTS` | *empty* | Comma-separated absolute paths where an earlier setup of this installation kept its library, for example a host path used before moving to Docker. Files recorded under them are looked up at the same relative path inside the current library directory; `/library` is always treated this way. Restart required. |
| `TANKARR_SETUP_COMPLETED_AT` | *unset* | When the first-run setup checklist was completed: an ISO 8601 timestamp with a time zone, stored in UTC. The checklist writes it; while it is unset and a required check fails, the web interface opens the checklist. |
| `TANKARR_UPDATE_CHECK_ENABLED` | `true` | Ask GitHub once a day whether a newer Tankarr release exists and show it on the System page, under About and in Needs attention. Nothing is downloaded or installed, and the request carries only the running version in its user agent. `false` disables it, for hosts without internet access. Restart required. |
| `TANKARR_RESTORED_SAFE_MODE` | `false` | Safe mode of a restored backup. Restoring writes `restored-settings.json` into the new data directory with this set to `true`, and with the monitor, the Wanted search, metadata lookups, automatic import and duplicate cleanup saved as off; that file fills in whatever the environment does not set. In safe mode no background automation runs and the web interface and API refuse changes, so the restored installation can be checked first. Restart with `false` to leave it. Restart required. |
| `TANKARR_MANGABAKA_API_URL` | `https://api.mangabaka.org/v1` | Base URL of the MangaBaka API, the catalogue that identifies works. Restart required. |
| `TANKARR_MANGAUPDATES_API_URL` | `https://api.mangaupdates.com/v1` | Base URL of the MangaUpdates API. Restart required. |
| `TANKARR_ANILIST_API_URL` | `https://graphql.anilist.co` | AniList GraphQL endpoint. Restart required. |
| `TANKARR_MYANIMELIST_API_URL` | `https://api.myanimelist.net/v2` | Currently unused: MyAnimeList data is read through the public Jikan API, whose address is fixed. |
