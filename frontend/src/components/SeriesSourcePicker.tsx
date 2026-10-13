import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import { t } from "../i18n";
import type { Manga } from "../types";

type Candidate = { provider: string; id: string; title: string; source_name: string; language: string };

export default function SeriesSourcePicker({ manga, onChanged }: { manga: Manga; onChanged: () => void }) {
  const [query, setQuery] = useState(manga.title);
  const [candidates, setCandidates] = useState<Candidate[]>([]);
  const [selection, setSelection] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [searched, setSearched] = useState(false);
  const request = useRef<AbortController | null>(null);
  const mounted = useRef(true);
  const preferredName = manga.chapters?.find((release) => (release.source_key ?? release.provider) === manga.preferred_source_key)?.source_name ?? t("Selected source");
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; request.current?.abort(); };
  }, []);

  const search = async () => {
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    setBusy(true); setError(null); setCandidates([]); setSelection("");
    try {
      const found = await api.releaseSourceCandidates(manga.id, query.trim(), controller.signal);
      if (!mounted.current || controller.signal.aborted) return;
      setCandidates(found.candidates);
      setSearched(true);
      if (found.errors.length) setError(found.errors.map((item) => `${item.provider}: ${item.error}`).join(" · "));
    } catch (caught) {
      if (mounted.current && !controller.signal.aborted) setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      if (mounted.current && !controller.signal.aborted) setBusy(false);
    }
  };

  const save = async (automatic: boolean) => {
    const candidate = candidates[Number(selection)];
    if (!automatic && (!selection || !candidate)) return;
    setBusy(true); setError(null);
    try {
      if (automatic) await api.clearReleaseSourcePreference(manga.id);
      else await api.addReleaseSource(manga.id, candidate.provider, candidate.id, true);
      if (mounted.current) onChanged();
    } catch (caught) {
      if (mounted.current) setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      if (mounted.current) setBusy(false);
    }
  };

  return <details className="card">
    <summary>{t("Download source")}</summary>
    <p>{manga.preferred_source_key
      ? t("Preferred source: {source}", { source: preferredName })
      : t("Automatic source selection")}</p>
    <p>{t("Choose a source for this series. Other sources remain available when it cannot supply a release. Language and import checks still apply.")}</p>
    <form onSubmit={(event) => { event.preventDefault(); void search(); }}>
      <label htmlFor={`source-query-${manga.id}`}>{t("Search download sources")}</label>
      <input id={`source-query-${manga.id}`} className="input" value={query} maxLength={200} onChange={(event) => setQuery(event.target.value)} />
      <button type="submit" className="btn" disabled={busy || !query.trim()}>{busy ? t("Searching…") : t("Search")}</button>
    </form>
    {candidates.length ? <>
      <label htmlFor={`source-choice-${manga.id}`}>{t("Choose a source")}</label>
      <select id={`source-choice-${manga.id}`} className="input" value={selection} onChange={(event) => setSelection(event.target.value)} disabled={busy}>
        <option value="">{t("Choose a source")}</option>
        {candidates.map((candidate, index) => <option key={`${candidate.provider}:${candidate.id}`} value={String(index)}>{candidate.source_name} · {candidate.title} · {candidate.language}</option>)}
      </select>
      <button type="button" className="btn btn-primary" onClick={() => void save(false)} disabled={busy || !selection}>{t("Use this source")}</button>
    </> : searched ? <p>{t("No matching download source found.")}</p> : null}
    {manga.preferred_source_key ? <button type="button" className="btn" onClick={() => void save(true)} disabled={busy}>{t("Use automatic source selection")}</button> : null}
    {manga.release_sources?.map((source) => <p key={`${source.provider}:${source.provider_manga_id}`}>
      {source.source_name ?? source.provider} · {source.title}{source.last_error ? ` · ${source.last_error}` : ""}
    </p>)}
    {error ? <p role="alert">{error}</p> : null}
  </details>;
}
