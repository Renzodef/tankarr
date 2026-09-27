import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "../api";
import { Modal, Spinner } from "../components";
import type { BooksAssemblyPreview, BooksAssemblyResult } from "../types";

function errorMessage(caught: unknown) {
  let message = caught instanceof Error ? caught.message : String(caught);
  if (caught instanceof ApiError && caught.detail && typeof caught.detail === "object" && "missing_chapters" in caught.detail) {
    const missing = caught.detail.missing_chapters;
    if (Array.isArray(missing) && missing.length) {
      message += ` Missing chapters: ${missing.slice(0, 40).join(", ")}${missing.length > 40 ? ` and ${missing.length - 40} more` : ""}.`;
    }
  }
  return message;
}

export default function AssembleBooksDialog({ mangaId, title, volume, onClose, onChanged }: {
  mangaId: string;
  title: string;
  volume: string | null;
  onClose: () => void;
  onChanged: (result?: BooksAssemblyResult) => Promise<void> | void;
}) {
  const [preview, setPreview] = useState<BooksAssemblyPreview | null>(null);
  const [result, setResult] = useState<BooksAssemblyResult | null>(null);
  const [busy, setBusy] = useState<"preview" | "assemble" | null>("preview");
  const [error, setError] = useState<string | null>(null);
  const [confirmed, setConfirmed] = useState(false);
  const version = useRef(0);
  const request = useRef<AbortController | null>(null);

  const invalidate = useCallback(() => {
    version.current += 1;
    request.current?.abort();
    request.current = null;
    return version.current;
  }, []);

  const loadPreview = useCallback(async () => {
    const current = invalidate();
    const controller = new AbortController();
    request.current = controller;
    setBusy("preview");
    setError(null);
    setConfirmed(false);
    setPreview(null);
    setResult(null);
    try {
      const next = volume === null ? await api.previewBooksAssembly(mangaId, controller.signal)
        : await api.previewBookAssembly(mangaId, volume, controller.signal).then((book) => ({ books: [book], errors: [], confirmation_snapshot: book.confirmation_snapshot }));
      if (current !== version.current) return;
      if (!next.books.length || !next.confirmation_snapshot) throw new Error("No complete books are available to assemble. Refresh the series and try again.");
      setPreview(next);
    } catch (caught) {
      if (current === version.current) setError(errorMessage(caught));
    } finally {
      if (current === version.current) setBusy(null);
    }
  }, [invalidate, mangaId, volume]);

  useEffect(() => { void loadPreview(); return () => { invalidate(); }; }, [invalidate, loadPreview]);

  const close = () => {
    if (busy === "assemble") return;
    invalidate();
    onClose();
  };

  const assemble = async () => {
    if (!preview || !confirmed || busy) return;
    const current = invalidate();
    const controller = new AbortController();
    request.current = controller;
    setBusy("assemble");
    setError(null);
    try {
      const next = volume === null ? await api.assembleBooks(mangaId, preview.confirmation_snapshot, controller.signal)
        : await api.assembleBook(mangaId, volume, preview.confirmation_snapshot, controller.signal).then((book) => ({ assembled: [book], errors: [], remaining: [] }));
      if (current !== version.current) return;
      setPreview(null);
      setConfirmed(false);
      setResult(next);
      await onChanged(next);
    } catch (caught) {
      if (current !== version.current) return;
      setPreview(null);
      setConfirmed(false);
      setError(errorMessage(caught));
      await onChanged();
    } finally {
      if (current === version.current) setBusy(null);
    }
  };

  return <Modal title={volume === null ? `Assemble covered books · ${title}` : `Assemble book ${volume} · ${title}`} onClose={close} footer={<>
    <button type="button" className="btn" disabled={busy === "assemble"} onClick={close}>{result ? "Close" : "Cancel"}</button>
    {preview ? <button type="button" className="btn btn-primary" disabled={Boolean(busy) || !confirmed} onClick={() => void assemble()}>{busy === "assemble" ? "Assembling…" : "Confirm assembly"}</button> : null}
    {!busy && !preview && (!result || result.remaining.length > 0) ? <button type="button" className="btn" onClick={() => void loadPreview()}>{result ? "Preview remaining books" : "Preview again"}</button> : null}
  </>}>
    {busy === "preview" ? <><Spinner /><p className="muted">Checking mapped chapters and book filenames…</p></> : null}
    {error ? <p className="banner banner-danger" role="alert">{error} Review a new preview before confirming.</p> : null}
    {preview ? <>
      <p>Create {preview.books.length} book{preview.books.length === 1 ? "" : "s"} from the chapters below. The original chapter files will move to the recycle bin after each book is saved and verified.</p>
      {preview.errors.map((item) => <p className="banner banner-warn" key={item.volume}>Book {item.volume} cannot be assembled: {item.message}</p>)}
      {preview.books.map((book) => <details key={book.volume} open={preview.books.length === 1} className="volume-section">
        <summary>Book {book.volume} · {book.chapters.length} chapter file{book.chapters.length === 1 ? "" : "s"} · {book.pages} pages · {book.filename}</summary>
        <table className="table"><thead><tr><th>Chapter</th><th>Source</th><th>Pages</th></tr></thead><tbody>
          {book.chapters.map((chapter) => <tr key={chapter.id}><td>{chapter.chapter}{chapter.source_chapter != null && chapter.source_chapter !== chapter.chapter ? <span className="muted small"> (source chapter {chapter.source_chapter})</span> : null}</td><td>{chapter.source}</td><td>{chapter.pages}</td></tr>)}
        </tbody></table>
      </details>)}
      <p className="muted small">Retired files are kept for the configured recycle bin retention period before automatic cleanup.</p>
      <label className="checkbox-row"><input type="checkbox" checked={confirmed} disabled={Boolean(busy)} onChange={(event) => setConfirmed(event.target.checked)} />I reviewed the preview and confirm creating these books and moving their chapter files to the recycle bin</label>
    </> : null}
    {result ? <div role="status">
      <p>{result.assembled.length} book{result.assembled.length === 1 ? "" : "s"} assembled.</p>
      {result.assembled.map((book) => <div key={book.volume}><p>Book {book.volume}: {book.filename} · {book.pages} pages · {book.retirement.files_retired} chapter file{book.retirement.files_retired === 1 ? "" : "s"} moved to the recycle bin.</p>{book.retirement.cleanup_errors.map((message, index) => <p className="banner banner-warn" key={index}>{message}</p>)}</div>)}
      {result.errors.map((item) => <p className="banner banner-warn" key={item.volume}>Book {item.volume}: {item.message}</p>)}
      {result.remaining.length > 0 ? <p>Books remaining: {result.remaining.join(", ")}.</p> : null}
    </div> : null}
  </Modal>;
}
