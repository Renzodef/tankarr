---
description: How Tankarr identifies manga and comics, numbers chapters, chooses between chapters and volumes, recovers missing releases and keeps the library safe.
---

# How Tankarr works

A few rules run through everything Tankarr does:

- **A work is identified by a catalogue, never by a download site.** Sources are
  mapped to a work after it exists, so a badly named or badly numbered mirror
  cannot decide what a series is.
- **The language belongs to each release**, not to the series. A Japanese manga
  can be followed in English, Italian or any language a translation exists in.
- **What the rules cannot settle is shown, not guessed.** Ambiguous matches wait
  for your confirmation, and Wanted says what was tried and why an item is
  still missing.
- **Files leave the library only through recoverable steps.** Every removal is
  journalled so an interruption can be recovered, and files you retire by hand
  go to a recycle bin first.

This page explains these rules one area at a time. To connect the services
mentioned here, see [Integrations](integrations.md); every option is listed in
the [configuration reference](configuration.md).

## Identity first

### The catalogue names the work

**Add New** searches one catalogue, [MangaBaka](https://mangabaka.org), by
title, or accepts a pasted MangaBaka series URL. It never searches a download
site. A work already in your library is marked **In library**. The series is
created from its catalogue identity, and download sources are mapped to it
afterwards, in the background.

MangaBaka is an aggregate record: one identity carries the titles, creators,
description, cover, type, status, year, counts, official platform links and the
identifiers of the same work on MangaUpdates, AniList, MyAnimeList and Kitsu.
Tankarr searches by title once and reads everything else by identifier.
Records are cached in Tankarr's database and artwork in the `metadata/artwork`
folder of the data directory. Stale records are refreshed weekly by default;
**System → Enrich Metadata** refreshes them immediately. Creator names link to
author pages built from the same catalogue.

### Mapping download sources

Once the series exists, Tankarr looks for it on your sources and scores every
candidate with independent evidence. A match on the primary title counts more
than a match on an alternate title. Creators, original publication year, type
and stable counts corroborate the match, contradictions weaken it, and the
position in a site's search results is ignored. A source is mapped
automatically only when its score clears a high threshold with a clear margin
over the next candidate. A close call waits on **System → To confirm**, where
**Accept** maps the source and **Reject** hides it for good.

### Pinned identities

**Metadata sources** on the series page shows the catalogue identities attached
to a series and lets you pin exact ones. **Save & enrich** validates the pins
and refreshes the series. A pinned identity survives every refresh; clearing it
restores the automatic matching.

### Titles in three layers

A series keeps three titles:

1. the title the download source uses, which stays the matching identity;
2. the canonical title from the catalogue, where an explicitly English title
   outranks an unlabelled romanization;
3. your own title, set as **Series title** in the series' Edit dialog, which
   always wins and is never replaced by a refresh.

Changing the displayed title renames the series' folder and files and updates
the metadata embedded in them.

### Covers

Tankarr collects cover candidates from the catalogue and from the mapped
sources, and prefers the catalogue's cover unless it is unusable. **Choose
cover** on the series page pins any candidate or returns to automatic, and the
choice survives refreshes. The selected image is rendered as a 1000×1500 RGB
JPEG of at most 900 KB, scaled to fit without stretching or cropping, and
published as `cover.jpg` in the series folder, as a poster next to each book
(`Book name.cbz` gets `Book name.jpg`) and to a managed Komga or Stump.

## Acquisition channels and the order they are tried

| Channel | What it delivers | Language | When it is used |
| --- | --- | --- | --- |
| Suwayomi sources | Chapters, and some volumes, from manga sites and official platforms | Declared by each source; only sources in the series' language are searched | Release poll, Wanted pass, interactive search |
| Prowlarr indexers, through qBittorrent or SABnzbd | Whole volumes, packs and chapters | English series only, checked with OCR | Wanted pass (unambiguous matches only), interactive search |
| Internet Archive | Whole volumes | English series only, checked with OCR | Wanted book search, interactive search |
| Library Import | Files you already own | The language you choose | When you import |
| Translation fallback | Machine translation from another language | The series' language | Optional, per series, when no native release can be obtained |

The order follows the cost of each channel:

1. **New chapters.** The release poll refreshes the mapped sources of every
   monitored series and queues the new chapters it finds.
2. **Missing chapters.** The Wanted pass takes each missing chapter down a
   ladder: the mapped sources and the Suwayomi catalogue first, then Prowlarr
   for that chapter, then Prowlarr for the book that contains it. See
   [Wanted recovery](#wanted-recovery).
3. **Missing books.** For series followed as books, the Wanted pass searches
   Prowlarr and archive.org for the missing volumes. Usenet releases rank
   first, torrents need seeders, and archive.org ranks below every indexer.
4. **Translation.** When the series has translation fallback enabled and no
   release in its language can be obtained, Tankarr can translate one from
   another language, see [Translation fallback](translation.md).

When several sources carry the same chapter, Tankarr chooses between them only
after every candidate has been mapped to the same canonical chapter. A chapter
published in the last 21 days is fresh: with the default **New release policy**
(**Prefer official when available**), the publisher's platform wins because it
is the legitimate day-one release. For older chapters, completeness and scan
quality come first and official platforms, which often keep only a few recent
chapters, come last. **First available** drops the official preference. The
**Source preference profile** (**Balanced**, **Official sources** or
**Curated scans**) and the optional ordered source lists refine this order;
all of these are in **Settings → Sources → Download sources**.

Replacing a file you already own is opt-in. **Upgrade to preferred sources
automatically** replaces a chapter only with a strictly preferred source in the
same language and numbering, and keeps the old file until the replacement is
verified. **Upgrade to official releases** replaces a non-official file when
the publisher's platform later carries the same chapter, up to 20 per series
per cycle.

## Language per release and the OCR language audit

A series is followed in one language, chosen when you add it from your
**Search languages** (Settings → General). Tankarr records the language on each
release, so different series can follow different languages. How the language
is established depends on the channel:

- **Suwayomi sources** declare their language structurally. Tankarr searches
  only the sources in the series' language, and no further check is needed.
- **Library Import** uses the language you choose at import time.
- **Indexer and archive.org releases** are only a claim: a release name or an
  indexer category says nothing reliable about the pages. These channels are
  therefore limited to series followed in English, and every book is checked
  before import.

The check samples three pages, at about 18%, 50% and 82% of the book, and reads
them with [Tesseract](https://github.com/tesseract-ocr/tesseract) OCR in English,
together with Tesseract's script detection. For an automatic import:

| Evidence | Result |
| --- | --- |
| Text in a non-Latin alphabet on the sampled pages | Refused |
| The release or a file name is tagged with another language, such as `[raw]` or `(French)` | Refused |
| The OCR reads mostly short fragments rather than dialogue | Refused, for you to review |
| The OCR confirms English dialogue | Imported |
| The OCR cannot read the pages, and the release is tagged English and nothing contradicts it | Imported on the strength of the English tag |

A script guess on pages the OCR could not read is not treated as evidence. A
refused import stays in **Activity** with its reason and evidence and is not
retried automatically. Its files stay on the download mount when that mount is
read-only; otherwise they are moved to a recycle folder on the download
filesystem. The Docker image includes Tesseract; when running from source,
install it yourself.

## Chapters or volumes

### One form per series

A series page shows either a shelf of books or a list of chapters, never both:

- a running work, a webtoon, or a work whose number of books nobody knows is
  shown as chapters;
- a finished or paused work with a known number of books is shown as books;
- a shelf that holds only books and no chapter file is shown as books even
  while the work is running, except for webtoons.

For downloading, Tankarr follows what the sources can deliver in full. A
running work follows chapters unless only books exist. When both chapters and
books can complete a finished work, **Preferred unit** in
**Settings → Sources → Download sources** decides; the default is
**Whole volumes (books)**.

### Which chapter is in which book

Book membership comes from evidence: the sources' own volume tags, a map you
save with **Set book boundaries**
([Book boundaries](operator-workflows.md#book-boundaries)), or the pages
themselves. A catalogue's chapter and volume totals alone never decide which
book contains a chapter. When a book has no map, the shelf spreads the chapters
evenly between its known neighbours so the page stays readable, but that
estimate is only a display: it never changes file metadata, never counts as
coverage, and never authorizes assembling or removing anything.

Once an owned book provably contains a chapter, the chapter file is a
duplicate. **Remove duplicate chapter files automatically** (on by default)
deletes such files during the monitor's cycle, up to 10 series per cycle,
unless a comparison of the pages vetoes it. On the series page,
**Retire duplicate chapter files** on a book moves them to the recycle bin
instead. A book whose chapters are all on disk and exactly mapped is shown as
**From chapters** and can be assembled into a single volume CBZ, see
[Assemble books](operator-workflows.md#assemble-books).

Specials that no book contains, such as a half chapter or an omake, are never
counted as missing. **Show specials no book contains** (off by default) lists
them on a shelf of books.

## Canonical chapter index and cross-source numbering

Tankarr keeps three things apart: the chapter slots a work is expected to have,
the releases each source offers, and the files in your library. A slot joins
the catalogue's idea of how long the work is with what the sources can deliver.
A decimal special never fills a missing whole chapter, and a catalogue total
that does not reveal the actual numbering never creates chapter numbers out of
nothing.

Sources do not always number chapters the way the work does. Webtoon platforms
number episodes, so notices and season breaks push the episode number away from
the chapter number, and aggregators keep their own running index. When most of
a source's episode titles carry the real chapter number ("250. Title",
"Chapter 250", "(ch. 250)") and those numbers form one coherent run, Tankarr
renumbers that source from its titles. Episodes behind a paywall (marked with a
lock) or dated in the future are not download candidates, and a listing of
three pages or fewer, such as an announcement or a "coming soon" card, is not
treated as the chapter. For how additional content such as prologues and extras
is counted, see
[Chapter numbers and additional content](operator-workflows.md#chapter-numbers-and-additional-content).

## The official edition sets the numbering

With the default **Prefer official when available** policy, a work that has an
official platform in your language takes that platform's numbering as the
library's numbering. When the platform numbers a prologue as
chapter 0, it counts as a chapter. Chapters past what the publisher has
released belong to another edition: scanlations number from the original, which
runs ahead of every translation. They are neither wanted nor downloaded. For a
finished work, the limit is the final chapter count, but only when the
catalogues agree on it (see [Catalogue consensus](#catalogue-consensus)) or
when you have set the edition's total yourself.

Removing files that are already in the library needs stronger evidence, an
enforceable frontier computed from the official releases themselves:

- at least 20 numbered official chapters;
- an unbroken tail: all of the 10 chapters below the newest one are present;
- a run that starts where the work starts, at chapter 0 or 1, and covers at
  least 90% of its range;
- a file counts as surplus only while it runs ahead of the frontier by a
  plausible margin, at most 200 chapters.

Only monitored series are aligned, and the surplus files are deleted through
the same journalled removal as any other deletion. Set
`TANKARR_OFFICIAL_EDITION_ALIGNMENT_ENABLED=false` to keep them and only have
the surplus reported.

## Catalogue consensus

MangaBaka names the work and hands over its identifiers on MangaUpdates,
AniList, Kitsu and MyAnimeList (through the public Jikan API). Those catalogues
are read by identifier, never searched by title, so every record is the same
work by construction. Their chapter and volume counts and publication statuses
are then compared, and each count gets a confidence:

| Confidence | Meaning |
| --- | --- |
| agreed | Two or more catalogues are within a chapter or ten percent of each other |
| lone | Exactly one catalogue knows the count |
| conflict | Several catalogues know it and none of them agree |
| unconfirmed | No catalogue knows it |

Only an agreed count can set the acquisition limit of a finished work or
declare it complete. The value kept is MangaBaka's when it is inside the
agreeing group, otherwise the group's own. A catalogue that is unreachable only
removes one opinion. The English edition is a separate question: MangaBaka's
note on the English publisher, such as "2 Vols - Complete", gives the volume
count that a series followed as books expects, which can differ from the
original's.

## Monitoring profiles and schedules

Each series has a monitoring profile, chosen in **Add New** and changeable in
the series' Edit dialog:

| Profile | What Tankarr downloads |
| --- | --- |
| All | Every available chapter, then new releases as they appear |
| Future | Only chapters published after the series was added |
| Existing | The chapters available today, without following new ones |
| None | Nothing automatically; you download by hand |

For a finished work, following new chapters continues until the translation
reaches the declared final chapter.

Two independent schedules drive the automation. Both can be switched off and
tuned in **Settings → General**:

- **Release monitoring** (`TANKARR_MONITOR_INTERVAL_SECONDS`, default 900, every
  15 minutes): every series monitored for new chapters (All or Future) has its
  sources refreshed and new chapters queued. Existing and None series only have
  their metadata refreshed, and series imported locally are not polled.
- **Scheduled Wanted recovery** (`TANKARR_WANTED_SEARCH_INTERVAL_SECONDS`,
  default 21600, every 6 hours): retries what is still missing. One pass looks
  at no more than 25 series (`TANKARR_WANTED_SEARCH_BUDGET`), the most overdue
  first. A series whose pass finds nothing is looked at again later and later:
  after 6 hours, 1 day, 3 days, 7 days, then 30 days. Queuing a release,
  finding a new source or searching by hand resets that series to the start.

The **Calendar** shows past releases and, for works with a regular rhythm, the
expected next ones. An expected date is estimated from the work's dated release
history and appears only when there are enough dated releases at a stable
interval.

## Wanted recovery

**Wanted** lists every monitored chapter or book that is still missing. Each
missing slot is taken down a ladder of channels, in increasing cost:

1. **Sources**: the mapped sources and the Suwayomi catalogue, just refreshed.
   If a release exists, it is queued.
2. **Indexer, chapter**: Prowlarr is asked for that chapter by name.
3. **Indexer, book**: Prowlarr is asked for the book that contains it, because
   a chapter nobody released on its own is often inside a volume.

Every rung records what it answered in a per-slot ledger, so Wanted can tell
the slots apart: a release is on its way; the indexers offer something that
needs your confirmation (**Needs review**); no source and no indexer carries the
release (**Not obtainable**); or no channel has been asked yet. **Not
obtainable** needs both the sources and at least one indexer rung to have come
back empty; a single channel is never enough. **Why still wanted?** on each
item shows the recorded attempts, their answers and what you can do.

An indexer result is grabbed automatically only when it is unambiguous: the
work's title appears as a contiguous phrase, the release names the chapter
number the slot asks for, the release can be downloaded (a torrent needs
seeders), and no other result names a different work with that title.
Everything else becomes a match to confirm on **System → To confirm**, where
**Accept** grabs the release. For series followed as books, a volume is grabbed
automatically only when every word of the title is in the release name and the
release names exactly that volume, at most 10 grabs per cycle.

The indexers are asked with care. A chapter costs at most three indexer
searches per pass, a channel that answered "nobody has it" is not asked again
for seven days, and a series gets at most 40 chapter searches per pass. Slots
never asked come first and, within them, the newest chapters, so a long backlog
moves forward a few chapters per pass instead of repeating the same searches.
The indexer rungs apply only to series followed in English; for other
languages they are recorded as unavailable.

With **On decision needed** turned on in the notification settings, each new
match to confirm and each item that becomes **Not obtainable** is announced
once, see [Notifications (ntfy)](integrations.md#notifications-ntfy).

## Source ranking and health

Tankarr sorts sources into classes: publishers' official platforms, groups that
publish their own series, curated scanlation catalogues, aggregators, and
sources it does not know. The class decides the order for fresh chapters and
for the backlog, as described in
[Acquisition channels](#acquisition-channels-and-the-order-they-are-tried).

Within a class, a health ledger decides. Every download outcome feeds a moving
average of each source's success rate (weighted 80%) and speed (20%); a
failure caused by a passing problem, such as a dropped connection, weighs less
than a hard one. A source nobody has measured starts in the middle, and a
record that receives no news slides back towards the middle, halfway every 30
days. A source with at least five attempts and a score under 0.25 is placed
behind every healthy source, and it climbs back by itself when it starts
working again. A source can also be demoted for a single series.

Demotion is an ordering, never an exclusion: when a demoted source is the only
one carrying a release, it is still tried. Tankarr never uninstalls a source
for being slow, failing or obsolete; only removing an extension yourself takes
a source away.

## Unreadable imports found and replaced

A source can serve a CBZ that unzips, validates and imports while being
unreadable: thumbnail-sized tiles, or one episode chopped into fragments.
Nothing in the archive says so, so Tankarr looks at the geometry of the pages:

- **Length.** Every page is rescaled to a common width of 800 pixels and the
  heights are summed, which makes chapters comparable across sources that serve
  different resolutions. Once a series has at least eight measured chapters, a
  chapter carrying less than 35% of the series' first quartile is a fragment,
  not a short chapter. If another source's copy of the same chapter measures
  about the same, within 20%, the chapter is simply short and is accepted.
- **Shape.** A page more than five times taller than it is wide cannot be read
  on a screen without panning, and a page narrower than 400 pixels is a
  thumbnail, whatever the series.

Decimal chapters, such as a two-page extra, have no expected length and are not
judged on it. New downloads are checked before they are packaged, so a refused
chapter never reaches the library; **Download anyway** in **History** accepts a
short chapter, but a page shape that cannot be read is never accepted. A
background audit (`TANKARR_PAGE_QUALITY_RECOVERY_ENABLED`, on by default)
measures the chapters already in the library and replaces the unreadable ones
when another source has them. When no other source has the chapter, the file is
kept: a verdict alone never deletes anything.

## Safe library management

### Naming and portable metadata

Tankarr writes the library as

```text
{Series} ({Authors})/{Series} - v{Volume} c{Chapter} [{Language}].cbz
```

Numbers are padded to three digits and a token without a value is left out, so
a chapter becomes `Series - v001 c001 [en].cbz` and a book
`Series - v027 [en].cbz`. A series without known creators goes into
`Series (Unknown Author)`. Each part of a name is shortened when needed so that
file names stay within the usual 255-byte limit. Every CBZ carries a
`ComicInfo.xml` with the canonical series and book data, the series folder holds
`cover.jpg`, and each book can have a poster next to it with the same name.
**System → Organize Library** brings existing files into this layout.

### Imports

Imports are atomic and hash-checked: a file is packaged and validated in a
staging area and published into the library without overwriting anything, and
its content hash is recorded. Untrusted archives are opened within limits on
their expanded size (16 GiB), page count (20,000), subprocess memory (1 GiB)
and free disk space (512 MiB kept in reserve), all adjustable in
**Settings → General** (advanced).

### Recycle bin and deletions

Files retired from the library, for example with **Audit files** or
**Retire duplicate chapter files**, go to a recycle bin. They are kept for the
**Recycle bin retention (days)** set in **Settings → Data** (seven by default,
`TANKARR_RECYCLE_BIN_RETENTION_DAYS`) and then removed by the nightly
maintenance. Every removal, recycled or not, is journalled: files are first
moved into a quarantine with a manifest, so an interruption is recovered on the
next start instead of leaving a half-finished deletion.

**System → Untracked library files** lists files on disk that no series claims,
usually left by a series removed without its files. Nothing is deleted there
until you click **Delete**.

### Removing a series

**Delete** on a series page removes the series and its chapter index from
Tankarr immediately. The option **Permanently delete this series' managed
library files in every language** is selected by default:

- The database removal and a durable cleanup request are committed in one
  transaction. Removing the files and bringing a managed reader up to date then
  happen in the background, and resume after a restart.
- The files are deleted, not moved to the recycle bin.
- Keeping the files creates no cleanup request: they stay on disk and remain
  available in your reader.
- A series with active downloads cannot be removed until they finish or are
  cancelled.
- The cleanup never removes a file that changed after the request, a file shared
  with another series, or the files of a series you have added again. It stops
  instead, leaves the files in place and tries again later.
- Storage errors are retried with a growing delay, up to once an hour, and are
  recorded in the application log.

For API clients, `DELETE /api/manga/{id}?delete_files=true` answers with
`cleanup_pending` and a `deletion_id` as soon as the database has committed,
without a filesystem preview. Clients that need the older synchronous behaviour
can fetch `/api/manga/{id}/delete-preview` and pass its `snapshot` as
`confirmation_snapshot` together with `background=false`; the removal is then
refused if anything changed since the preview.

## Resilient queue

- Download jobs survive restarts. A chapter interrupted by a restart goes back
  into the queue, and a torrent or Usenet import resumes.
- A failed download can be retried from **History** with **Retry download**.
- `downloads_paused`, set through `PUT /api/settings`, holds pending chapter
  downloads across restarts: active chapters finish, local imports continue,
  and setting it back to `false` resumes the queue. The automatic searches have
  their own switches, **Release monitoring** and **Scheduled Wanted recovery**
  in Settings → General.
- Download concurrency adapts to the host. **Maximum page concurrency** and
  **Maximum simultaneous chapters** (Settings → General, advanced) are ceilings;
  within them Tankarr follows source latency and errors, CPU, memory pressure
  and temperature.

## Built-in reader and bookmarks

The built-in web reader opens the library's CBZ files directly, so it needs no
second catalogue and no scan: a book can be read as soon as it is imported.
Open a book from its row on the series page.

The display mode is chosen in this order: the series' own setting in its Edit
dialog, then **Default reading mode** in **Settings → Reader**, then the
catalogue (a webtoon scrolls vertically, a manga is paged), then the page shape
(tall strips scroll vertically). Paged books read right to left for manga and
left to right for manhwa, manhua, comics and books, with Japanese, Korean and
Chinese originals as a fallback rule; a series can override the direction. The
reader's toolbar switches between paged and vertical display for the current
session and between **Fit page** and **Fit width**.

Turning pages records nothing. **Save bookmark** in the reader records one
resume point per series, the book and the page, and the series page then offers
**Continue from bookmark**. The **Bookmarks** page lists them all.

## External readers

Komga, Kavita, Stump and any reader that opens a folder of CBZ files can read
the library directly. Tankarr can manage Komga and Stump, asking them to scan
after downloads and publishing covers to them, without ever touching reading
progress. See [Readers](integrations.md#readers).

## Library Import

**Library Import** adds files you already own. Choose files or a folder from
your browser, or **Scan drop folder** to read the import folder mounted at
`/import` (`TANKARR_IMPORT_DIR`), which Tankarr only reads. Tankarr groups CBZ,
ZIP, CBR/RAR and PDF files and folders of images into series. For each group
you pick the **Target series**, an existing one or **Create new local series**,
whether to **Import as** chapters or volumes, and the language of a new series.
RAR archives are recognised by their content even with a wrong extension and
read with `bsdtar` (libarchive), `unar`, `unrar` or 7-Zip; PDFs are rendered with
Poppler.

Every file is normalized to a CBZ with `ComicInfo.xml` and published through
the same atomic, hash-checked import as a download. A new local series has no
remote source, so it is never polled.

## Audit files

**Audit files** on a series page helps you find and remove a bad downloaded
book or chapter, or reject a source that keeps supplying bad files for that
series.

### Using Audit files

Use **Audit files** when a downloaded book or chapter looks wrong in the
reader: for example, it contains only a few pages, ends with a black page, uses
an unexpected page layout, or comes from the wrong source. It is a
troubleshooting tool for one series, not a routine step after every download.
Open the series page and select **Audit files** from its actions.

The list shows the downloaded files of the series. Each entry shows its book or
chapter number, source, language, page count, recorded quality verdict and any
detected anomalies, with previews of the first and last page that load as you
scroll. **Only files with anomalies** filters the list and **Refresh audit**
updates it. An anomaly is a reason to inspect a file, not proof that its
content is wrong. The preview shows only the first and last page, so open the
book in the reader to check the middle. Opening or refreshing the audit never
removes files or changes sources.

If a **particular file** is bad, select it and choose **Retire selected files**.
You can select several files, or **Select visible files**. Tankarr then shows
exactly which files are affected; tick the confirmation and click **Move files
to recycle bin**. Their bytes move to the recycle bin and the releases are
marked as no longer downloaded. The source is not rejected: if monitoring still
wants the release, Tankarr may download it again from the same source.

If an **entire source entry** is wrong for this series, choose **Reject this
source** on one of its files. The review shows all the files it supplied.
Confirming with **Reject source and retire files** retires those files, removes
the source mapping and its releases, and records the rejection so that
automatic mapping does not add the same entry back. Only that exact entry is
rejected: other sources stay available for the series.

Both actions need their own preview and confirmation. If a file or the source
changes after the preview, refresh the audit and review it again. Retirement is
blocked while the series has active downloads. Retired files stay in the
recycle bin for the configured retention period (seven days by default), then
the nightly maintenance removes them. See also
[Inspect series files](operator-workflows.md#inspect-series-files).

## Sign-in and access control

Tankarr always has a login. With no credentials in **Settings → Security** or
in `TANKARR_AUTH_USERNAME` and `TANKARR_AUTH_PASSWORD`, the first start creates
the user `admin` with a random password, prints it once to the log and keeps it
in `generated-login.json` in the data directory, readable only by its owner,
until you save a login in Settings or set the two variables.
`TANKARR_AUTH_REQUIRED=false` turns the login off and is meant only for a
development machine that nothing else can reach.

The default method, `forms`, shows a login page and keeps a signed session
cookie; `basic` uses the browser's credential prompt, and API clients can use
HTTP Basic authentication with either method. The health checks
`/api/health` and `/api/ready` answer without signing in, so container health
checks work. Requests that would change something and that come from another
site are refused, and failed sign-ins are slowed down per client address. See [Authentication](installation.md#authentication)
and the [configuration reference](configuration.md#authentication).
