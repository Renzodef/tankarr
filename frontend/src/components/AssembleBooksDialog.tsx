import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "../api";
import { Modal, Spinner } from "../components";
import type { BooksAssemblyPreview, BooksAssemblyResult } from "../types";
import { t, tn } from "../i18n";

function errorMessage(caught: unknown) {
  let message = caught instanceof Error ? caught.message : String(caught);
  if (caught instanceof ApiError && caught.detail && typeof caught.detail === "object" && "missing_chapters" in caught.detail) {
    const missing = caught.detail.missing_chapters;
    if (Array.isArray(missing) && missing.length) {
      message += " " + (missing.length > 40
        ? t("Missing chapters: {list} and {count} more.", { list: missing.slice(0, 40).join(", "), count: missing.length - 40 })
        : t("Missing chapters: {list}.", { list: missing.join(", ") }));
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

  return <Modal title={volume === null ? t("Assemble covered books · {title}", { title }) : t("Assemble book {volume} · {title}", { volume, title })} onClose={close} footer={<>
    <button type="button" className="btn" disabled={busy === "assemble"} onClick={close}>{result ? t("Close") : t("Cancel")}</button>
    {preview ? <button type="button" className="btn btn-primary" disabled={Boolean(busy) || !confirmed} onClick={() => void assemble()}>{busy === "assemble" ? t("Assembling…") : t("Confirm assembly")}</button> : null}
    {!busy && !preview && (!result || result.remaining.length > 0) ? <button type="button" className="btn" onClick={() => void loadPreview()}>{result ? t("Preview remaining books") : t("Preview again")}</button> : null}
  </>}>
    {busy === "preview" ? <><Spinner /><p className="muted">{t("Checking mapped chapters and book filenames…")}</p></> : null}
    {error ? <p className="banner banner-danger" role="alert">{error} {t("Review a new preview before confirming.")}</p> : null}
    {preview ? <>
      <p>{tn(preview.books.length, "Create {count} book from the chapters below. The original chapter files will move to the recycle bin after each book is saved and verified.", "Create {count} books from the chapters below. The original chapter files will move to the recycle bin after each book is saved and verified.")}</p>
      {preview.errors.map((item) => <p className="banner banner-warn" key={item.volume}>{t("Book {volume} cannot be assembled: {message}", { volume: item.volume, message: item.message })}</p>)}
      {preview.books.map((book) => <details key={book.volume} open={preview.books.length === 1} className="volume-section">
        <summary>{t("Book {volume}", { volume: book.volume })} · {tn(book.chapters.length, "{count} chapter file", "{count} chapter files")} · {tn(book.pages, "{count} page", "{count} pages")} · {book.filename}</summary>
        <table className="table"><thead><tr><th>{t("Chapter")}</th><th>{t("Source")}</th><th>{t("Pages")}</th></tr></thead><tbody>
          {book.chapters.map((chapter) => <tr key={chapter.id}><td>{chapter.chapter}{chapter.source_chapter != null && chapter.source_chapter !== chapter.chapter ? <span className="muted small"> {t("(source chapter {chapter})", { chapter: chapter.source_chapter })}</span> : null}</td><td>{chapter.source}</td><td>{chapter.pages}</td></tr>)}
        </tbody></table>
      </details>)}
      <p className="muted small">{t("Retired files are kept for the configured recycle bin retention period before automatic cleanup.")}</p>
      <label className="checkbox-row"><input type="checkbox" checked={confirmed} disabled={Boolean(busy)} onChange={(event) => setConfirmed(event.target.checked)} />{t("I reviewed the preview and confirm creating these books and moving their chapter files to the recycle bin")}</label>
    </> : null}
    {result ? <div role="status">
      <p>{tn(result.assembled.length, "{count} book assembled.", "{count} books assembled.")}</p>
      {result.assembled.map((book) => <div key={book.volume}><p>{t("Book {volume}: {file} · {pages} pages · {retired}", { volume: book.volume, file: book.filename, pages: book.pages, retired: tn(book.retirement.files_retired, "{count} chapter file moved to the recycle bin.", "{count} chapter files moved to the recycle bin.") })}</p>{book.retirement.cleanup_errors.map((message, index) => <p className="banner banner-warn" key={index}>{message}</p>)}</div>)}
      {result.errors.map((item) => <p className="banner banner-warn" key={item.volume}>{t("Book")} {item.volume}: {item.message}</p>)}
      {result.remaining.length > 0 ? <p>{t("Books remaining:")} {result.remaining.join(", ")}.</p> : null}
    </div> : null}
  </Modal>;
}
