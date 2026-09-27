import { FormEvent, useEffect, useState } from "react";
import { api } from "../api";
import {
  Cover,
  EmptyState,
  Icon,
  LANGUAGES,
  MONITOR_OPTIONS,
  Modal,
  Pagination,
  Spinner,
  StatusPill,
  humanize,
  languageName,
  navigate,
  useApp,
} from "../components";
import type { MangaPreview, MangaSummary, MonitorMode } from "../types";
import { workYears } from "../workYears";

const CATALOGUE = "catalogue";
const RESULT_PAGE_SIZE = 20;

export default function AddPage({ initialQuery }: { initialQuery: string }) {
  const { health, notify, refreshJobs } = useApp();
  const [query, setQuery] = useState(initialQuery);
  const [language, setLanguage] = useState<string | null>(null);
  const [results, setResults] = useState<MangaSummary[] | null>(null);
  const [errors, setErrors] = useState<{ provider: string; error: string }[]>([]);
  const [searching, setSearching] = useState(false);
  const [selected, setSelected] = useState<MangaSummary | null>(null);
  const [resultPage, setResultPage] = useState(1);

  const enabledLanguageCodes = health?.search_languages?.length ? health.search_languages : ["en"];
  const languageOptions = LANGUAGES.filter(([code]) => enabledLanguageCodes.includes(code));
  const configuredDefault = enabledLanguageCodes.includes(health?.default_language ?? "")
    ? health!.default_language
    : enabledLanguageCodes[0] ?? "en";
  const selectedLanguage = language && enabledLanguageCodes.includes(language) ? language : configuredDefault;
  const pastedId = /mangabaka\.(org|dev)\//i.test(query.trim());

  const runSearch = async (term: string) => {
    if (term.trim().length < 2) return;
    setSearching(true);
    try {
      const response = await api.search(term.trim(), selectedLanguage, CATALOGUE);
      setResults(response.works ?? response.results);
      setResultPage(1);
      setErrors(response.errors);
    } catch (caught) {
      notify("error", String(caught));
    } finally {
      setSearching(false);
    }
  };

  useEffect(() => {
    if (initialQuery.trim().length >= 2) void runSearch(initialQuery);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialQuery]);

  const submit = (event: FormEvent) => {
    event.preventDefault();
    void runSearch(query);
  };

  return (
    <div className="page">
      <form className="toolbar add-search-toolbar" onSubmit={submit}>
        <div className="toolbar-group toolbar-grow">
          <input
            className="input input-grow"
            placeholder="Title or MangaBaka URL…"
            aria-label="Search the catalogue"
            type="search"
            enterKeyHint="search"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
          />
          <select className="input" aria-label="Search language" value={selectedLanguage} onChange={(event) => setLanguage(event.target.value)}>
            {languageOptions.map(([code, label]) => (
              <option key={code} value={code}>
                {label}
              </option>
            ))}
          </select>
          <button type="submit" className="btn btn-primary" disabled={searching || query.trim().length < 2}>
            <Icon name="search" /> {pastedId ? "Look up" : "Search"}
          </button>
        </div>
      </form>

      <div className="provider-scope-note">
        <Icon name="info" size={16} />
        <span>
          Search by title, then choose the language you want to read. Download sources are matched automatically.
        </span>
      </div>

      {errors.map((item, index) => (
        <div key={`${item.provider}:${index}`} className="banner banner-warn">
          <Icon name="alert" /> {item.provider}: {item.error}
        </div>
      ))}

      {searching ? (
        <Spinner />
      ) : results === null ? (
        <EmptyState
          icon="search"
          title="Search the catalogue"
          hint="Type a title in any language. The translation language is chosen per series when you add it."
        />
      ) : results.length === 0 ? (
        <EmptyState
          icon="search"
          title="No works found"
          hint="Try the original title, a romanized title, or paste the MangaBaka page URL."
        />
      ) : (
        <>
        <div className="result-list">
          {results
            .slice((resultPage - 1) * RESULT_PAGE_SIZE, resultPage * RESULT_PAGE_SIZE)
            .map((work) => {
            const years = workYears(work);
            return (
            <div key={work.id} className={`result-card ${work.in_library ? "result-card-existing" : ""}`}>
              <Cover url={work.cover_url} title={work.title} className="result-cover" />
              <div className="result-body">
                <div className="result-title-row">
                  <h3>{work.title}</h3>
                  {years.label ? <span className="muted" title={years.title ?? undefined}>({years.label})</span> : null}
                  {work.work_type ? <StatusPill kind="muted">{work.work_type}</StatusPill> : null}
                  {work.status ? <StatusPill kind="muted">{humanize(work.status)}</StatusPill> : null}
                  {work.rating ? <StatusPill kind="info">{work.rating.toFixed(1)}</StatusPill> : null}
                  {work.in_library ? <StatusPill kind="muted">In library</StatusPill> : null}
                </div>
                {work.native_title ? <div className="muted small">{work.native_title}</div> : null}
                {work.authors.length ? <div className="muted">{work.authors.join(", ")}</div> : null}
                <div className="muted small">
                  {[
                    work.volume_count ? `${work.volume_count} volumes` : null,
                    work.chapter_count
                      ? `${work.chapter_count} chapters`
                      : work.latest_release_chapter
                        ? `${work.latest_release_chapter} chapters so far`
                        : null,
                    work.genres?.length ? work.genres.slice(0, 4).join(" · ") : null,
                  ]
                    .filter(Boolean)
                    .join(" · ")}
                </div>
                <p className="result-description">{work.description || "No description available."}</p>
              </div>
              <div className="result-actions">
                {work.in_library ? (
                  <a className="btn" href={`#/series/${work.library_manga_id}`}>
                    <Icon name="library" /> Open
                  </a>
                ) : (
                  <button type="button" className="btn btn-primary" onClick={() => setSelected(work)}>
                    <Icon name="add" /> Add
                  </button>
                )}
              </div>
            </div>
            );
          })}
        </div>
        <Pagination
          page={resultPage}
          pageSize={RESULT_PAGE_SIZE}
          total={results.length}
          onPageChange={setResultPage}
          itemLabel="works"
          ariaLabel="Search result pages"
        />
        </>
      )}

      {selected ? (
        <AddModal
          work={selected}
          initialLanguage={selectedLanguage}
          languageOptions={languageOptions}
          onClose={() => setSelected(null)}
          onAdded={async (id) => {
            setSelected(null);
            await refreshJobs();
            navigate(`/series/${encodeURIComponent(id)}`);
          }}
        />
      ) : null}
    </div>
  );
}

export function AddModal({
  work,
  initialLanguage,
  languageOptions,
  onClose,
  onAdded,
}: {
  work: MangaSummary;
  initialLanguage: string;
  languageOptions: readonly (readonly [string, string])[];
  onClose: () => void;
  onAdded: (id: string) => Promise<void>;
}) {
  const { notify } = useApp();
  const [language, setLanguage] = useState(initialLanguage);
  const [mode, setMode] = useState<MonitorMode>("all");
  const [searchNow, setSearchNow] = useState(true);
  const [preview, setPreview] = useState<MangaPreview | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    api
      .previewManga(work.id, language, CATALOGUE)
      .then((value) => {
        if (!cancelled) setPreview(value);
      })
      .catch((caught) => notify("error", String(caught)))
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [work.id, language]);

  const ended = preview?.status === "ended";

  const save = async () => {
    setSaving(true);
    try {
      const added = await api.addManga(work.id, CATALOGUE, language, mode, { searchNow });
      notify("success", `${work.title} added. Metadata and download sources are being fetched in the background.`);
      await onAdded(added.id);
    } catch (caught) {
      notify("error", String(caught));
      setSaving(false);
    }
  };

  return (
    <Modal
      title={`Add ${work.title}`}
      onClose={onClose}
      footer={
        <>
          <button type="button" className="btn" onClick={onClose}>
            Cancel
          </button>
          <button
            type="button"
            className="btn btn-primary"
            onClick={() => void save()}
            disabled={saving || loading}
            aria-busy={saving}
          >
            {saving ? <Spinner /> : <Icon name="add" />} {saving ? "Adding…" : "Add to library"}
          </button>
        </>
      }
    >
      <div className="form-row">
        <label htmlFor="add-series-language">Reading language</label>
        <select id="add-series-language" className="input" value={language} onChange={(event) => setLanguage(event.target.value)}>
          {languageOptions.map(([code, label]) => (
            <option key={code} value={code}>
              {label}
            </option>
          ))}
        </select>
        <p className="muted small">
          Chapters are searched in {languageName(language)} on every enabled source; the language belongs
          to the release, not to the series.
        </p>
      </div>
      {loading ? (
        <div className="preview-loading">
          <Spinner />
          <span>Reading the catalogue record…</span>
        </div>
      ) : preview ? (
        <div className="preview-summary">
          {preview.volume_count ? (
            <span>
              <strong>{preview.volume_count}</strong> volumes
            </span>
          ) : null}
          {preview.chapter_count ? (
            <span>
              <strong>{preview.chapter_count}</strong> chapters{preview.status === "ended" ? "" : " so far"}
            </span>
          ) : null}
          {preview.status ? <span>{humanize(preview.status)}</span> : null}
          {ended ? (
            <span className="muted small">
              Ended works are refreshed less often; late specials or extra volumes are still picked up while monitored.
            </span>
          ) : null}
        </div>
      ) : null}
      <div className="form-row">
        <label>Monitor</label>
        <div className="option-cards">
          {MONITOR_OPTIONS.map((option) => (
            <button
              key={option.value}
              type="button"
              className={`option-card ${mode === option.value ? "selected" : ""}`}
              aria-pressed={mode === option.value}
              onClick={() => setMode(option.value)}
              title={option.description}
            >
              <strong>{option.title}</strong>
              <span>{option.description}</span>
            </button>
          ))}
        </div>
      </div>
      <div className="form-row">
        <p className="muted small">
          Tankarr follows the work in whole books or in single chapters, never both, choosing from what
          the sources actually offer{preview?.suggested_unit ? ` (right now: ${preview.suggested_unit})` : ""}.
          Extras, omakes and side stories are left out of the index; find them through the manual release search.
        </p>
      </div>
      <div className="form-row add-options">
        <label className="checkbox-row">
          <input
            type="checkbox"
            checked={searchNow}
            disabled={mode === "future" || mode === "none"}
            onChange={(event) => setSearchNow(event.target.checked)}
          />
          Search for missing chapters as soon as the work is added
        </label>
      </div>
    </Modal>
  );
}
