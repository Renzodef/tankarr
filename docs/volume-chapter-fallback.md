# Volumes and chapter recovery

Tankarr chooses a finished work's volume format when the catalogue provides a reliable edition count. An explicit series format wins. Japanese scrolling publications with a volume edition can use volumes; other mobile webtoons default to chapters. This choice does not prove that all files are available.

## Global automatic assembly

`TANKARR_ASSEMBLE_BOOKS_AUTOMATICALLY` defaults to `false` and is editable in Settings. It applies to every monitored series followed as volumes. The old per-series database flag is retained for schema compatibility but no longer controls execution; attempts to set it through the series API return 422. Manual reviewed assembly remains available independently of automatic scheduling.

Automatic acquisition prefers whole books. If none is eligible, confirmed operator/book-reading boundaries permit acquisition of their missing chapters. With enabled indexers or archive search, chapter fallback waits for a recorded unavailable/not-offered volume result; errors and ambiguous matches do not count as absence. Active whole-book grabs suppress overlapping chapter fallback. Automatic chapter fallback for volume series requires the global assembly switch.

A completed group can be assembled by the existing bounded monitor pass, using the native assembler's preview, provenance, atomic import, bookmark and retention checks. Missing files or unconfirmed/estimated maps never qualify. Turning the switch off prevents the next automatic assembly; an import already underway finishes safely.

## Series actions

- **Compile or import map** edits confirmed boundaries, with preview before saving. JSON is an array (or an object with `boundaries`) of `{ "volume": "1", "first_chapter": "1" }`. Identifiers are strings to preserve decimal numbering. Imported data remains a draft until reviewed and saved.
- **Download chapters anyway** previews currently mapped, eligible chapter releases and asks for confirmation. It keeps volume format and normal language, numbering, source, monitoring, block and duplicate checks. The signed preview is revalidated against the current queue before creating jobs. Without a confirmed map, chapters stay as chapters and assembly waits.

No balanced division is presented as an official map. An operator can explicitly supply their chosen boundaries; AI-assisted research and exceptional grouping stay outside the unattended product.

## Validation and rollout

Tests cover global on/off, legacy flags, manual overrides, Japanese/other webtoons, missing maps, complete maps, whole-book precedence, indexer failures, stale previews and duplicate submissions. Build the frontend and run the acquisition, assembly and settings regression tests before release. Deploy through the normal trial/release procedure; no runtime settings are changed by this code review.
