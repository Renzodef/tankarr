import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "../api";
import { Modal, Spinner, StatusPill } from "../components";
import type { ChapterMapBoundary, ChapterMapPreview, ChapterMapState } from "../types";

const MAX_BOUNDARIES = 500;

// Keep decimal identifiers exact, including values beyond Number's precision.
function decimal(value: string): string | null {
  const trimmed = value.trim();
  if (!/^\d+(?:\.\d+)?$/.test(trimmed)) return null;
  const [whole, fraction = ""] = trimmed.split(".");
  const integer = whole.replace(/^0+(?=\d)/, "");
  const tail = fraction.replace(/0+$/, "");
  return tail ? `${integer}.${tail}` : integer;
}

function compareDecimal(left: string, right: string) {
  const [leftWhole, leftTail = ""] = left.split(".");
  const [rightWhole, rightTail = ""] = right.split(".");
  if (leftWhole.length !== rightWhole.length) return leftWhole.length - rightWhole.length;
  if (leftWhole !== rightWhole) return leftWhole < rightWhole ? -1 : 1;
  const length = Math.max(leftTail.length, rightTail.length);
  const a = leftTail.padEnd(length, "0");
  const b = rightTail.padEnd(length, "0");
  return a === b ? 0 : a < b ? -1 : 1;
}

function validatedRows(rows: ChapterMapBoundary[]): ChapterMapBoundary[] {
  if (!rows.length) throw new Error("Add at least one book boundary before previewing.");
  if (rows.length > MAX_BOUNDARIES) throw new Error(`Use at most ${MAX_BOUNDARIES} book boundaries.`);
  const result: ChapterMapBoundary[] = [];
  for (const [index, row] of rows.entries()) {
    const volume = decimal(row.volume);
    const chapter = decimal(row.first_chapter);
    if (volume === null || compareDecimal(volume, "1") < 0) throw new Error(`Row ${index + 1}: enter a book number of at least 1, using digits and an optional decimal point.`);
    if (chapter === null) throw new Error(`Row ${index + 1}: enter a first chapter of 0 or greater, using digits and an optional decimal point.`);
    const previous = result.at(-1);
    if (previous && compareDecimal(previous.volume, volume) >= 0) throw new Error(`Row ${index + 1}: book numbers must increase without duplicates.`);
    if (previous && compareDecimal(previous.first_chapter, chapter) >= 0) throw new Error(`Row ${index + 1}: first chapters must increase without duplicates.`);
    result.push({ volume, first_chapter: chapter });
  }
  return result;
}

type Preview = { kind: "save" | "remove"; result: ChapterMapPreview; boundaries: ChapterMapBoundary[] };

export default function ChapterMapEditor({ mangaId, title, onClose, onSaved }: {
  mangaId: string;
  title: string;
  onClose: () => void;
  onSaved: (result: ChapterMapPreview) => Promise<void> | void;
}) {
  const [saved, setSaved] = useState<ChapterMapState | null>(null);
  const [rows, setRows] = useState<ChapterMapBoundary[]>([]);
  const [fromSuggestions, setFromSuggestions] = useState(false);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [busy, setBusy] = useState<"load" | "preview" | "save" | null>("load");
  const [error, setError] = useState<string | null>(null);
  const version = useRef(0);
  const request = useRef<AbortController | null>(null);

  const invalidate = useCallback(() => {
    version.current += 1;
    request.current?.abort();
    request.current = null;
    return version.current;
  }, []);

  const load = useCallback(async () => {
    const current = invalidate();
    const controller = new AbortController();
    request.current = controller;
    setBusy("load");
    setError(null);
    setSaved(null);
    setPreview(null);
    try {
      const result = await api.chapterMap(mangaId, controller.signal);
      if (current !== version.current) return;
      setSaved(result);
      setRows(result.boundaries.length ? result.boundaries : result.suggestions);
      setFromSuggestions(!result.boundaries.length && result.suggestions.length > 0);
    } catch (caught) {
      if (current === version.current) setError(caught instanceof Error ? caught.message : String(caught));
    } finally { if (current === version.current) setBusy(null); }
  }, [mangaId, invalidate]);

  useEffect(() => {
    void load();
    return () => { invalidate(); };
  }, [load, invalidate]);

  const edit = (next: ChapterMapBoundary[]) => {
    invalidate();
    setRows(next);
    setPreview(null);
    setError(null);
    setBusy(null);
  };

  const previewChanges = async (kind: Preview["kind"]) => {
    const current = invalidate();
    const controller = new AbortController();
    request.current = controller;
    setError(null);
    setPreview(null);
    setBusy("preview");
    try {
      const boundaries = kind === "save" ? validatedRows(rows) : [];
      const result = kind === "save"
        ? await api.previewChapterMap(mangaId, boundaries, controller.signal)
        : await api.previewChapterMapRemoval(mangaId, controller.signal);
      if (current === version.current) setPreview({ kind, result, boundaries });
    } catch (caught) {
      if (current === version.current) setError(caught instanceof Error ? caught.message : String(caught));
    } finally { if (current === version.current) setBusy(null); }
  };

  const confirm = async () => {
    if (!preview?.result.confirmation_snapshot) return;
    const current = invalidate();
    const controller = new AbortController();
    request.current = controller;
    setBusy("save");
    setError(null);
    try {
      const result = preview.kind === "save"
        ? await api.saveChapterMap(mangaId, preview.boundaries, preview.result.confirmation_snapshot, controller.signal)
        : await api.removeChapterMap(mangaId, preview.result.confirmation_snapshot, controller.signal);
      if (current === version.current) await onSaved(result);
    } catch (caught) {
      if (current !== version.current) return;
      if (caught instanceof ApiError && caught.status === 409) {
        setPreview(null);
        setError(`The saved map or chapter list changed. Preview again before confirming. ${caught.message}`);
      } else setError(caught instanceof Error ? caught.message : String(caught));
    } finally { if (current === version.current) setBusy(null); }
  };

  const close = () => { if (busy !== "save") { invalidate(); onClose(); } };
  const warnings = [...new Set(preview ? preview.result.warnings : saved?.warnings ?? [])];
  const finalInterval = preview?.result.intervals.at(-1);

  return <Modal title="Set book boundaries" onClose={close} wide footer={<>
    <button type="button" className="btn" disabled={busy === "save"} onClick={close}>Cancel</button>
    {preview ? <>
      <button type="button" className="btn" disabled={busy === "save"} onClick={() => { invalidate(); setPreview(null); setError(null); }}>Edit boundaries</button>
      <button type="button" className={`btn ${preview.kind === "remove" ? "btn-danger" : "btn-primary"}`} disabled={busy === "save" || !preview.result.confirmation_snapshot} onClick={() => void confirm()}>{busy === "save" ? "Saving…" : preview.kind === "remove" ? "Confirm removal" : "Confirm and save boundaries"}</button>
    </> : saved ? <button type="button" className="btn btn-primary" disabled={busy !== null} onClick={() => void previewChanges("save")}>{busy === "preview" ? "Previewing…" : "Preview changes"}</button> : null}
  </>}>
    <p><strong>{title}</strong></p>
    <p>Enter the first chapter in each book. A book ends before the next book starts; the last book ends at the last known canonical chapter. Decimal chapters such as 55.5 are preserved.</p>
    <p className="muted small">Without confirmed boundaries, chapters are divided evenly and estimates update with new releases. Saving boundaries updates chapter assignments and Missing. It never queues downloads or retires files. Saved boundaries remain fixed when the catalogue changes.</p>
    {error ? <p className="banner banner-danger" role="alert">{error}</p> : null}
    {!saved && busy === "load" ? <Spinner /> : null}
    {!saved && error ? <button type="button" className="btn" onClick={() => void load()}>Retry loading boundaries</button> : null}
    {saved ? <>
      {!preview ? saved.last_known_chapter !== null ? <p>Last known canonical chapter: <strong>{saved.last_known_chapter}</strong>.</p> : <p className="banner banner-warn">The last canonical chapter is not known. A complete boundary map cannot be confirmed yet.</p> : null}
      {warnings.map((warning) => <p key={warning} className="banner banner-warn">{warning}</p>)}
      {!preview ? <>
        <p><StatusPill kind={fromSuggestions ? "warn" : saved.boundaries.length ? "info" : "muted"}>{fromSuggestions ? "Catalogue suggestions — not verified" : saved.boundaries.length ? "Operator boundaries" : "No book boundaries yet"}</StatusPill></p>
        <div className="data-table-frame"><table className="table responsive-list-table"><thead><tr><th>Book</th><th>First chapter</th><th>Actions</th></tr></thead><tbody>
          {rows.map((row, index) => <tr key={index}>
            <td data-label="Book"><input className="input" aria-label={`Book number in row ${index + 1}`} inputMode="decimal" maxLength={64} value={row.volume} onChange={(event) => edit(rows.map((item, position) => position === index ? { ...item, volume: event.target.value } : item))} /></td>
            <td data-label="First chapter"><input className="input" aria-label={`First chapter in row ${index + 1}`} inputMode="decimal" maxLength={64} value={row.first_chapter} onChange={(event) => edit(rows.map((item, position) => position === index ? { ...item, first_chapter: event.target.value } : item))} /></td>
            <td data-label="Actions"><button type="button" className="btn btn-ghost" aria-label={`Remove boundary row ${index + 1}`} onClick={() => edit(rows.filter((_, position) => position !== index))}>Remove</button></td>
          </tr>)}
        </tbody></table></div>
        {!rows.length ? <p className="muted">No boundaries in this draft. Add the books you can verify.</p> : null}
        <div className="toolbar-group">
          <button type="button" className="btn" disabled={rows.length >= MAX_BOUNDARIES} onClick={() => edit([...rows, { volume: "", first_chapter: "" }])}>Add book boundary</button>
          {saved.boundaries.length > 0 && saved.suggestions.length > 0 ? <button type="button" className="btn" onClick={() => { edit(saved.suggestions); setFromSuggestions(true); }}>Use catalogue suggestions</button> : null}
          {saved.boundaries.length > 0 ? <button type="button" className="btn btn-danger" disabled={busy !== null} onClick={() => void previewChanges("remove")}>Preview removing operator map</button> : null}
        </div>
      </> : <section aria-label="Boundary change preview">
        <h3>{preview.kind === "remove" ? "Remove operator boundaries" : "Review book assignments"}</h3>
        {preview.kind === "remove" ? <p>The operator map will be removed. Books without explicit readings return to an even estimate; catalogue suggestions remain unconfirmed.</p> : <p>These assignments will replace the current operator map and take priority over catalogue suggestions.</p>}
        {preview.result.intervals.length ? <ul>{preview.result.intervals.map((interval) => <li key={interval.volume}>
          <strong>Book {interval.volume}: chapters {interval.first_chapter}–{interval.last_chapter}</strong>
          <details><summary>{interval.chapters.length} canonical chapter{interval.chapters.length === 1 ? "" : "s"}</summary><p>{interval.chapters.join(", ")}</p></details>
        </li>)}</ul> : <p>No operator book assignments remain in this preview.</p>}
        {preview.kind === "save" && finalInterval ? <p className="banner banner-info">Book {finalInterval.volume} ends at chapter {finalInterval.last_chapter}, the last canonical chapter known for this preview. Future chapters will not extend this map automatically.</p> : null}
        <p>{preview.result.chapter_index.mapped_missing_count} missing {preview.result.chapter_index.unit === "volume" ? "books" : "chapters"} after this change.</p>
      </section>}
    </> : null}
  </Modal>;
}
