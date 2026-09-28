import { useCallback, useEffect, useRef, useState } from "react";
import { Modal, Spinner, StatusPill, formatBytes, humanize, languageName } from "../components";
import { serverUrl } from "../serverUrl";

import type { SeriesAuditFile, SeriesAuditSourceKey, SeriesAuditReport, SeriesAuditPreview, SeriesAuditResult } from "../types";

export type SeriesAuditClient = {
  seriesAudit: (id: string, signal?: AbortSignal) => Promise<SeriesAuditReport>;
  previewAuditRetirement: (id: string, chapterIds: string[], revision: string, signal?: AbortSignal) => Promise<SeriesAuditPreview>;
  retireAuditFiles: (id: string, chapterIds: string[], revision: string, snapshot: string, signal?: AbortSignal) => Promise<SeriesAuditResult>;
  previewAuditSourceRejection: (id: string, source: SeriesAuditSourceKey, revision: string, signal?: AbortSignal) => Promise<SeriesAuditPreview>;
  rejectAuditSource: (id: string, source: SeriesAuditSourceKey, revision: string, snapshot: string, signal?: AbortSignal) => Promise<SeriesAuditResult>;
};

const FILE_BATCH = 24;
const MAX_SELECTION = 1000;
function fileLabel(file: SeriesAuditFile) { return `${file.unit === "volume" ? "Book" : "Chapter"} ${file.number ?? "special"}`; }
function sourceLabel(file: { provider: string; source_name?: string | null }) { return file.source_name || file.provider || "Unknown source"; }

function AuditThumbnail({ file, edge, onLoaded }: { file: SeriesAuditFile; edge: "first" | "last"; onLoaded: () => void }) {
  const url = edge === "first" ? file.first_thumbnail_url : file.last_thumbnail_url;
  const [failedUrl, setFailedUrl] = useState<string | null>(null);
  return <figure style={{ margin: 0 }}>
    {url && url !== failedUrl ? <img src={serverUrl(url)} loading="lazy" width={192} height={272} alt={`${edge === "first" ? "First" : "Last"} page of ${fileLabel(file)} from ${sourceLabel(file)}`} style={{ width: "100%", height: 200, objectFit: "contain" }} onLoad={onLoaded} onError={() => setFailedUrl(url)} /> : <div className="muted small" style={{ height: 200, display: "grid", placeItems: "center" }}>Preview unavailable</div>}
    <figcaption className="muted small">{edge === "first" ? "First page" : "Last page"}</figcaption>
  </figure>;
}

export default function SeriesAuditDialog({ mangaId, title, client, onClose, onChanged }: {
  mangaId: string; title: string; client: SeriesAuditClient; onClose: () => void; onChanged: () => Promise<void> | void;
}) {
  const [report, setReport] = useState<SeriesAuditReport | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [review, setReview] = useState<SeriesAuditPreview | null>(null);
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState<"load" | "preview" | "confirm" | null>("load");
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [warnings, setWarnings] = useState<string[]>([]);
  const [onlyAnomalies, setOnlyAnomalies] = useState(false);
  const [visibleCount, setVisibleCount] = useState(FILE_BATCH);
  const version = useRef(0);
  const request = useRef<AbortController | null>(null);
  const phase = useRef<string | null>(null);
  const reviewing = useRef(false);
  const reviewHeading = useRef<HTMLHeadingElement | null>(null);
  const thumbnailTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const refreshedThumbnails = useRef(new Set<string>());

  const invalidate = useCallback(() => {
    version.current += 1;
    request.current?.abort();
    request.current = null;
    return version.current;
  }, []);

  const load = useCallback(async (background = false) => {
    if (background && (phase.current || reviewing.current)) return;
    const current = invalidate();
    const controller = new AbortController();
    request.current = controller;
    phase.current = "load";
    if (!background) {
      setBusy("load"); setError(null); setReview(null); setConfirmed(false); reviewing.current = false;
    }
    try {
      const next = await client.seriesAudit(mangaId, controller.signal);
      if (current !== version.current) return;
      setReport(next);
      const available = new Set(next.files.filter((file) => file.can_retire).map((file) => file.id));
      setSelected((previous) => new Set([...previous].filter((id) => available.has(id))));
    } catch (caught) {
      if (current === version.current && !background) setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      if (current === version.current) { phase.current = null; setBusy(null); }
    }
  }, [client, invalidate, mangaId]);

  useEffect(() => {
    setReport(null); setSelected(new Set()); setNotice(null); setWarnings([]); setVisibleCount(FILE_BATCH);
    refreshedThumbnails.current.clear();
    void load();
    return () => { invalidate(); if (thumbnailTimer.current) clearTimeout(thumbnailTimer.current); };
  }, [invalidate, load]);

  useEffect(() => { if (review) reviewHeading.current?.focus(); }, [review]);

  const thumbnailLoaded = (file: SeriesAuditFile) => {
    const key = file.first_thumbnail_url ?? file.last_thumbnail_url;
    if (file.thumbnail_status !== "pending" || !key || refreshedThumbnails.current.has(key)) return;
    refreshedThumbnails.current.add(key);
    if (thumbnailTimer.current) clearTimeout(thumbnailTimer.current);
    thumbnailTimer.current = setTimeout(() => { thumbnailTimer.current = null; void load(true); }, 250);
  };

  const previewAction = async (source: SeriesAuditSourceKey | null) => {
    if (!report || busy || (!source && !selected.size)) return;
    const current = invalidate();
    const controller = new AbortController();
    request.current = controller;
    phase.current = "preview";
    setBusy("preview"); setError(null); setNotice(null); setWarnings([]); setConfirmed(false);
    try {
      const next = source ? await client.previewAuditSourceRejection(mangaId, source, report.revision, controller.signal)
        : await client.previewAuditRetirement(mangaId, [...selected], report.revision, controller.signal);
      if (current !== version.current) return;
      if (!next.confirmation_snapshot || (!next.chapter_ids.length && next.action !== "reject_source")) throw new Error("No files remain in this review. Refresh the audit and try again.");
      reviewing.current = true;
      setReview(next);
    } catch (caught) {
      if (current === version.current) setError(`${caught instanceof Error ? caught.message : String(caught)} Refresh the audit before reviewing again.`);
    } finally {
      if (current === version.current) { phase.current = null; setBusy(null); }
    }
  };

  const confirmAction = async () => {
    if (!review || !confirmed || busy) return;
    const current = invalidate();
    const controller = new AbortController();
    request.current = controller;
    phase.current = "confirm";
    setBusy("confirm"); setError(null);
    try {
      const result = review.action === "reject_source" && review.source
        ? await client.rejectAuditSource(mangaId, review.source, review.revision, review.confirmation_snapshot, controller.signal)
        : await client.retireAuditFiles(mangaId, review.chapter_ids, review.revision, review.confirmation_snapshot, controller.signal);
      if (current !== version.current) return;
      setNotice(`${result.files_retired} file${result.files_retired === 1 ? "" : "s"} moved to the recycle bin.${result.source_removed ? " The source was rejected for this series." : ""}`);
      setWarnings([...result.cleanup_errors, ...(result.cleanup_warning ? [result.cleanup_warning] : [])]);
      setSelected(new Set());
      setReview(null); setConfirmed(false); reviewing.current = false;
      await onChanged();
      if (current === version.current) await load();
    } catch (caught) {
      if (current !== version.current) return;
      setError(`${caught instanceof Error ? caught.message : String(caught)} Refresh the audit and review a new preview before confirming.`);
      setReview(null); setConfirmed(false); reviewing.current = false;
    } finally {
      if (current === version.current) { phase.current = null; setBusy(null); }
    }
  };

  const close = () => { if (busy === "confirm") return; invalidate(); onClose(); };
  const matching = (report?.files ?? []).filter((file) => !onlyAnomalies || file.anomalies.length > 0);
  const visible = matching.slice(0, visibleCount);
  return <Modal wide title={`Audit files · ${title}`} onClose={close} footer={<>
    <button type="button" className="btn" disabled={busy === "confirm"} onClick={close}>Close</button>
    {review ? <><button type="button" className="btn" disabled={Boolean(busy)} onClick={() => void load()}>Back to files</button><button type="button" className="btn btn-danger" disabled={Boolean(busy) || !confirmed} onClick={() => void confirmAction()}>{busy === "confirm" ? "Applying…" : review.action === "reject_source" ? "Reject source and retire files" : "Move files to recycle bin"}</button></> : <button type="button" className="btn btn-danger" disabled={Boolean(busy) || !selected.size} onClick={() => void previewAction(null)}>Retire selected files… ({selected.size})</button>}
  </>}>
    {error ? <p className="banner banner-danger" role="alert">{error}</p> : null}
    {notice ? <p className="banner banner-success" role="status">{notice}</p> : null}
    {warnings.map((warning, index) => <p className="banner banner-warn" key={index}>{warning}</p>)}
    {busy === "load" && !report ? <Spinner /> : null}
    {busy === "preview" ? <p className="muted" role="status">Preparing file review…</p> : null}
    {review ? <>
      <h3 ref={reviewHeading} tabIndex={-1}>{review.action === "reject_source" ? "Reject this source for the series" : "Review selected files"}</h3>
      <p>{review.action === "reject_source" ? `Stop using ${sourceLabel(review.files[0] ?? review.source ?? { provider: "this source", source_name: null })} for this series and move all ${review.files.length} downloaded files from that source to the recycle bin.` : `Move these ${review.files.length} files to the recycle bin.`} Files are kept for the configured retention period before automatic cleanup.</p>
      {review.action === "reject_source" && review.releases_to_remove !== undefined ? <p>{review.releases_to_remove} releases from this source will be removed from Tankarr.</p> : null}
      <table className="table"><thead><tr><th>File</th><th>Source</th><th>Language</th><th>Pages</th></tr></thead><tbody>{review.files.map((file) => <tr key={file.id}><td>{fileLabel(file)}{file.title ? <div className="muted small">{file.title}</div> : null}</td><td>{sourceLabel(file)}</td><td>{languageName(file.language)}</td><td>{file.pages !== null && file.pages >= 0 ? file.pages : "Unknown"}</td></tr>)}</tbody></table>
      <label className="checkbox-row"><input type="checkbox" checked={confirmed} disabled={Boolean(busy)} onChange={(event) => setConfirmed(event.target.checked)} />{review.action === "reject_source" ? "I reviewed this source and its files and confirm rejection and retirement" : "I reviewed these files and confirm moving them to the recycle bin"}</label>
    </> : report ? <>
      <div className="toolbar"><div className="toolbar-group"><label className="checkbox-row"><input type="checkbox" checked={onlyAnomalies} onChange={(event) => { setOnlyAnomalies(event.target.checked); setVisibleCount(FILE_BATCH); }} />Only files with anomalies</label><button type="button" className="btn" disabled={Boolean(busy) || selected.size >= MAX_SELECTION} onClick={() => setSelected((previous) => new Set([...new Set([...previous, ...visible.filter((file) => file.can_retire).map((file) => file.id)])].slice(0, MAX_SELECTION)))}>Select visible files</button><button type="button" className="btn" disabled={Boolean(busy) || !selected.size} onClick={() => setSelected(new Set())}>Clear selection</button></div><button type="button" className="btn" disabled={Boolean(busy)} onClick={() => void load()}>Refresh audit</button></div>
      {selected.size >= MAX_SELECTION ? <p className="muted small" role="status">Select at most {MAX_SELECTION} files per action.</p> : null}
      <p className="muted small">{report.files.length} downloaded files. First and last page previews load as you scroll.</p>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 280px), 1fr))", gap: "1rem" }}>
        {visible.map((file) => {
          const verdict = typeof file.verdict?.verdict === "string" ? file.verdict.verdict : null;
          const source = report.sources.find((item) => item.provider === file.provider && item.provider_manga_id === file.provider_manga_id && item.can_reject);
          return <article className="volume-section" key={file.id} aria-label={`${fileLabel(file)} from ${sourceLabel(file)}`}>
            <label className="checkbox-row"><input type="checkbox" checked={selected.has(file.id)} disabled={Boolean(busy) || !file.can_retire || (!selected.has(file.id) && selected.size >= MAX_SELECTION)} aria-label={`Select ${fileLabel(file)} from ${sourceLabel(file)}`} onChange={(event) => setSelected((previous) => { const next = new Set(previous); if (event.target.checked) next.add(file.id); else next.delete(file.id); return next; })} /><strong>{fileLabel(file)}</strong></label>
            {file.title ? <p className="small">{file.title}</p> : null}
            <p className="muted small">{sourceLabel(file)} · {languageName(file.language)} · {file.pages === null || file.pages < 0 ? "Pages unknown" : `${file.pages} pages`}{file.size_bytes !== null ? ` · ${formatBytes(file.size_bytes)}` : ""}</p>
            <StatusPill kind={verdict === "refused" ? "danger" : verdict === "degraded" ? "warn" : "muted"}>{verdict ? humanize(verdict) : "Not checked"}</StatusPill>
            {typeof file.verdict?.reason === "string" ? <p className="muted small">{file.verdict.reason}</p> : null}
            {file.anomalies.length > 0 ? <ul className="warn-text small">{file.anomalies.map((item) => <li key={item.code}>{item.label}</li>)}</ul> : null}
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "0.5rem" }}><AuditThumbnail file={file} edge="first" onLoaded={() => thumbnailLoaded(file)} /><AuditThumbnail file={file} edge="last" onLoaded={() => thumbnailLoaded(file)} /></div>
            {file.can_reject_source && source ? <button type="button" className="btn btn-small" disabled={Boolean(busy)} aria-label={`Reject ${sourceLabel(source)} for this series`} onClick={() => void previewAction({ provider: source.provider, provider_manga_id: source.provider_manga_id })}>Reject this source…</button> : null}
          </article>;
        })}
      </div>
      {!matching.length ? <p className="muted">{onlyAnomalies ? "No files have recorded anomalies." : "No downloaded files to audit."}</p> : null}
      {matching.length > visibleCount ? <button type="button" className="btn" onClick={() => setVisibleCount((count) => count + FILE_BATCH)}>Show {Math.min(FILE_BATCH, matching.length - visibleCount)} more file{matching.length - visibleCount === 1 ? "" : "s"}</button> : null}
    </> : !busy ? <button type="button" className="btn" onClick={() => void load()}>Retry audit</button> : null}
  </Modal>;
}
