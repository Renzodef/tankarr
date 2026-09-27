---
description: Design note on how Tankarr handles a series owned partly as books and partly as chapters - map trust, coverage, grouping, assembled books, file audit and the form of a series.
---

# Mixed series: books and chapters in one library

This design note describes how Tankarr handles a series that exists in the
library partly as books (volumes) and partly as chapters.

## Why
A series often exists in the library as books for part of its run and chapters
for the rest: the books that were published, the chapters the books do not
cover yet. A flat chapter list decorated with "in v4" badges is not enough:
when the map behind those badges is wrong (for example a catalogue's release
log) the page is misleading, and when there is no map it says nothing.

## 1. Map trust
A chapter↔book map is operational only when its boundaries were saved by the
operator or explicitly submitted through the book-reading API. MangaDex,
official platforms and other catalogue mappings are suggestions to review in
the boundary editor, even when their entries are dense and ordered. Stored
source data is retained; it cannot establish coverage or duplicate files.

Unmapped books share the current chapters evenly. New releases and new volume
counts recalculate estimates, including previously rendered estimates; saved
boundaries remain fixed. The remaining chapters are split between those fixed
intervals. Estimated assignments are never written back as chapter membership
and never authorize assembly or deletion.

## 2. Coverage both ways
- A chapter slot is covered by an owned book when the exact map says so
  (`covered_by_volume`). Without a map and with owned books it is
  `covered_unmapped`: neither wanted nor fetched again.
- A book slot is covered by chapters when every chapter the exact map assigns
  to it is on disk, or when the chapters between its mapped neighbours are all
  there (`covered_by_chapters`).
- Duplicate chapter files are found through the exact map only, for chapter
  series through the slots and for book series directly from the rows.

## 3. Content conformity
Freshly downloaded pages are judged before packaging: black cards above 15 %
of the chapter, or strip slices in a series that reads in pages, mark the
download degraded and the recovery replaces it from another source.

## 4. Series page: one list, grouped by book
Operators set book boundaries in a dedicated editor.
`GET/PUT/DELETE /api/manga/{id}/chapter-map` reads, previews and replaces only
the operator source. PUT and DELETE require a confirmation bound to the current
revision; catalogue refreshes preserve manual boundaries. Chapter zero and
decimal boundaries use exact numeric labels. Explicit operator assignments
enable chapter fallback for missing books without changing the series counting
unit.

The series page displays **units in reading order**:

```
▸ Volume 12            not owned · covered by chapters 52-56 ✓      [Search book]
    Chapter 52 · 48 pages · downloaded
    Chapter 53 · 51 pages · downloaded
    ...
▸ Volume 13            owned · 188 pages                            [Open]
    (chapters 57-61 inside this book — 2 duplicate files, retire)
▸ Volume 14            not owned · chapters 62-66: 3 of 5 on disk   [Search]
▸ Chapters 77-81       not in any book (extras)
```

Rules for the grouping:
- With an exact map, chapters nest under their book. Owned book: the group is
  collapsed, shows page count and a "duplicates inside" note with a retire
  action. Unowned book: expanded, shows which chapters cover it.
- Without an exact map: two flat sections, "Books" and "Chapters", and one
  sentence: "Which chapters each book contains is not known; chapters are
  kept and no book is counted as covered." Offer the operator map editor
  (start of each book as a chapter number; the end is implied).
- Never print a book badge from a hint.
- All, Missing, Downloaded and Monitored filters retain relevant groups and
  matching children; the order selector reverses reading order. A split chapter
  is on disk only when all its parts are present.
- Duplicate retirement previews the file IDs and paths for one book and checks
  the reviewed set again before moving those files to the recycle bin.
- `GET /api/manga/{id}/units` loads the series in five bulk SELECTs (plus fixed
  application unit-context lookups). It builds the index once and opens no CBZs.

## 5. Assembled books
When a book is not owned but its chapters are all on disk (exact map), offer
"Assemble book": Tankarr builds `Series - v012 [en].cbz` from the chapter
files in order (pages renumbered, ComicInfo of a volume, provenance noting
the chapter files and their sources), imports it as the book, and retires
the chapter files as duplicates. Readers then show one book per volume
throughout. This is a generic operation: it needs only the exact map.

The preview and confirmation are bound to book boundaries, source files and
their hashes. Missing chapters, changed files, ambiguous maps or an owned book
return 409. Complete parts from one source can supply a whole chapter; known
missing parts cannot be truncated. Pages retain reading order and receive fresh
sequential filenames, with volume ComicInfo and source/language notes.

`chapter_release.assembled_from` stores versioned provenance: source release IDs,
canonical chapters, volume and source notes. It is committed with the published
book and its hash ledger before any source file is retired. A proven assembled
book covers its chapters even above 600 pages; changed boundaries disable that
coverage. External volumes have higher priority and can replace the assembly,
retaining the old book in the recycle bin.

The single-book and series assembly endpoints support dry runs. The series action
reports individual outcomes and allows a fresh preview of remaining books. The
series flag `assemble_books_automatically` is off by default. The monitor attempts
at most four opted-in books per cycle and defers during active imports.

## 6. Audit
“Audit files” opens from the series actions. It lists every downloaded
file across languages and sources, with page counts, numbering, source and
anomaly flags. First/last-page thumbnails are generated lazily through a
single bounded archive worker and cached by file fingerprint; the list itself
opens no archives. The UI shows 24 files at a time.

Retiring selected files and rejecting one exact provider/source mapping each
require a fresh preview and confirmation. The signed snapshot covers file
identity, releases and mappings. A changed file, active download or changed
source invalidates it. Retirement moves bytes to the ordinary recycle bin;
source rejection also removes that mapping and its offers and records the
rejection. Other mappings from the same provider remain available.

## 7. The form of a series
A series page is a list of books or a list of chapters, never both
(`series_form.py`). Finished or paused works with a known book count are
books. Chapter membership requires an explicit map for the managed edition.
Unknown boundaries remain unknown, with chapter files shown separately.
Chapter/volume totals, neighbouring numbers and statistical division never
establish ownership or justify renaming files. Estimated maps are never
stored as chapter membership: a stored estimate is ignored by every map reader
and removed on reconciliation, and unsupported file tags derived from one are
cleared. Running works, webtoons and works without a book count are chapters.
Mapped optional extras retain their book membership without increasing the
main chapter count. Each book carries a completion plan: *book* on disk,
*assemble* when its chapters are complete, *in part*, *missing*, worded by
the user's preference (books first or chapters first). When every book
completes from chapters the series is "all chapters" and a stray book is
redundant; when every book is on disk it is "all books". The page shows one
row per book with the plan, the primary action and a menu; owned books stay
closed; "Problems only" hides what is fine.

Conflicting claims never assign a chapter by choosing the higher volume,
including contradictions within a single catalogue. Such coverage remains
unmapped until evidence resolves it. A map with owned ordinary chapters outside
its books is partial.

Reviewed source numbering correspondences can be stored through
`Database.replace_numbering_overrides`. These are scoped to release IDs and the
observed source number, require evidence, and precede book assignment. A refresh
preserves them; changed source numbering invalidates them. Replacing them with an
empty mapping withdraws the correction. They do not edit or delete page images.
An operator entry with an empty chapter set explicitly leaves that book's
membership unknown and suppresses rejected catalogue claims for the book.
