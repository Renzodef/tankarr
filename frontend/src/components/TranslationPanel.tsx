import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import { LANGUAGES, Spinner, languageName, useApp } from "../components";
import type { Manga } from "../types";

export default function TranslationPanel({ manga }: { manga: Manga }) {
  const { notify } = useApp();
  const [data, setData] = useState<Awaited<ReturnType<typeof api.translations>> | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [showUpload, setShowUpload] = useState(false);
  const [file, setFile] = useState<File | null>(null);
  const [language, setLanguage] = useState("ja");
  const [unit, setUnit] = useState<"volume" | "chapter">("volume");
  const [number, setNumber] = useState("");
  const refresh = useCallback(async () => {
    try { setData(await api.translations(manga.id)); setError(""); }
    catch { setError("Unable to load translation jobs."); }
  }, [manga.id]);
  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => { void refresh(); }, 15000);
    return () => window.clearInterval(timer);
  }, [refresh]);
  async function run(action: () => Promise<unknown>) {
    setBusy(true);
    try { await action(); await refresh(); }
    catch (failure) { notify("error", failure instanceof Error ? failure.message : "Translation action failed"); }
    finally { setBusy(false); }
  }
  return <section className="panel" aria-label="Translation fallback">
    <div className="panel-header"><h2>Translation fallback</h2>{busy ? <Spinner /> : null}</div>
    <p className="muted small">Missing books and chapters can be translated into {languageName(manga.preferred_language)}. Machine translations retain their source and model in the archive metadata.</p>
    {error ? <p role="alert">{error}</p> : null}
    {data && !data.enabled ? <p role="status">Translation is paused. Configure and enable it in Settings → Translation.</p> : null}
    <div className="button-row">
      <button className="btn" disabled={busy || !data?.enabled} onClick={() => void run(() => api.searchTranslations(manga.id))}>Search missing translations</button>
      <button className="btn btn-ghost" disabled={busy || !data?.enabled} onClick={() => setShowUpload(!showUpload)}>Translate a source CBZ</button>
    </div>
    {showUpload ? <form onSubmit={(event) => {
      event.preventDefault();
      if (file) void run(async () => { await api.uploadTranslation(manga.id, file, language, unit, number); setShowUpload(false); });
    }}>
      <p className="muted small">Assign a source archive to the matching book or chapter in this series. Check edition numbering before submitting. Only the translated result enters the library.</p>
      <div className="form-row"><label htmlFor="translation-file">Source CBZ</label><input id="translation-file" type="file" accept=".cbz,.zip" required onChange={(event) => setFile(event.target.files?.[0] ?? null)} /></div>
      <div className="form-row"><label htmlFor="translation-language">Source language</label><select id="translation-language" className="input" value={language} onChange={(event) => setLanguage(event.target.value)}>{LANGUAGES.filter(([code]) => code !== manga.preferred_language).map(([code, label]) => <option key={code} value={code}>{label}</option>)}</select></div>
      <div className="form-row"><label htmlFor="translation-unit">Archive contains</label><select id="translation-unit" className="input" value={unit} onChange={(event) => setUnit(event.target.value as "volume" | "chapter")}><option value="volume">One book</option><option value="chapter">One chapter</option></select></div>
      <div className="form-row"><label htmlFor="translation-number">{unit === "volume" ? "Book" : "Chapter"} number in this edition</label><input id="translation-number" className="input" type="text" pattern="[0-9]+([.][0-9]+)?" required value={number} onChange={(event) => setNumber(event.target.value)} /></div>
      <button className="btn" disabled={busy || !data?.enabled || !file}>Queue translation</button>
    </form> : null}
    {data?.jobs.length ? <div className="table-scroll"><table className="table"><thead><tr><th>Book / chapter</th><th>Languages</th><th>Status</th><th>Actions</th></tr></thead><tbody>{data.jobs.map((job) => <tr key={job.id}>
      <td>{job.slot_key.replace(":", " ")}</td><td>{job.source.language} → {job.target_language}</td><td>{job.status}<div className="muted small">{job.message}</div></td>
      <td>{["failed", "cancelled"].includes(job.status) ? <button className="btn btn-ghost" disabled={busy || !data.enabled} onClick={() => void run(() => api.retryTranslation(job.id))}>Retry</button> : job.status !== "completed" ? <button className="btn btn-ghost" disabled={busy} onClick={() => void run(() => api.cancelTranslation(job.id))}>Cancel</button> : null}</td>
    </tr>)}</tbody></table></div> : <p className="muted small">No translation jobs queued.</p>}
  </section>;
}
