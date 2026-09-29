import { useEffect, useMemo, useState } from "react";
import { api } from "../api";
import {
  Cover,
  EmptyState,
  Icon,
  LANGUAGES,
  Pagination,
  Spinner,
  StatusPill,
  humanize,
  useApp,
} from "../components";
import type { AuthorPage as AuthorPageData, Manga, MangaSummary } from "../types";
import { countNoun, libraryStatus, publicationStatus, seriesCounts } from "../seriesStatus";
import { workYears } from "../workYears";
import { AddModal } from "./AddPage";
import { locale, t, tn } from "../i18n";

type WorkFilter = "all" | "in_library" | "available" | "incomplete";

/** The series-page badges, computed from the slice of series state the author page carries. */
function libraryView(work: AuthorPageData["works"][number]) {
  const library = work.library;
  if (!work.in_library || !library) return null;
  const shape = {
    publication: library.publication ?? undefined,
    status_override: library.status_override ?? null,
    library_count: library.library_count ?? undefined,
    library_status_override: library.library_status_override ?? null,
    chapter_count: library.chapter_count ?? 0,
    downloaded_count: library.downloaded_count ?? 0,
    metadata: null,
    status: null,
  } as unknown as Manga;
  const counts = seriesCounts(shape);
  const unitKey = (library.effective_series_unit ?? counts.unit).replace(/s$/, "");
  const noun = countNoun(unitKey === "volume" || unitKey === "issue" || unitKey === "book" ? unitKey : "chapter", counts.total_count);
  return {
    publication: publicationStatus(shape),
    availability: libraryStatus(shape),
    counts: `${counts.downloaded_count}/${counts.total_count} ${noun}`,
    missing: counts.missing_count,
    language: library.preferred_language ?? null,
    monitor: library.monitor_mode ?? null,
  };
}
type WorkSort = "title" | "year" | "rating";
type SortDirection = "asc" | "desc";
const WORK_PAGE_SIZE = 20;

const titleCollator = new Intl.Collator(undefined, { numeric: true, sensitivity: "base" });

function compareOptionalNumber(
  left: number | null,
  right: number | null,
  direction: SortDirection,
) {
  if (left === null && right !== null) return 1;
  if (left !== null && right === null) return -1;
  if (left === null || right === null) return 0;
  return direction === "asc" ? left - right : right - left;
}

function formatTimestamp(value: string | null) {
  if (!value) return t("Pending first refresh");
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat(locale(), {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(date);
}

export default function AuthorPage({ id }: { id: string }) {
  const { health, notify, refreshJobs } = useApp();
  const [page, setPage] = useState<AuthorPageData | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [selectedWork, setSelectedWork] = useState<MangaSummary | null>(null);
  const [workFilter, setWorkFilter] = useState<WorkFilter>("all");
  const [workSort, setWorkSort] = useState<WorkSort>("year");
  const [sortDirection, setSortDirection] = useState<SortDirection>("asc");
  const [workPage, setWorkPage] = useState(1);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    api
      .authorPage(id)
      .then((result) => {
        if (!cancelled) setPage(result);
      })
      .catch((error) => {
        if (!cancelled) notify("error", String(error));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [id, notify]);

  useEffect(() => {
    if (!page || page.last_refreshed_at || page.last_refresh_error) return;
    let cancelled = false;
    const timer = window.setTimeout(() => {
      api
        .authorPage(id)
        .then((result) => {
          if (!cancelled) setPage(result);
        })
        .catch(() => {
          // The persisted page remains usable; the server records provider errors.
        });
    }, 2500);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [id, page]);

  const enabledLanguageCodes = health?.search_languages?.length ? health.search_languages : ["en"];
  const languageOptions = LANGUAGES.filter(([code]) => enabledLanguageCodes.includes(code));
  const defaultLanguage = enabledLanguageCodes.includes(health?.default_language ?? "")
    ? health!.default_language
    : enabledLanguageCodes[0] ?? "en";
  const aliases = useMemo(
    () => page?.aliases.filter((name) => name !== page.author) ?? [],
    [page],
  );
  const workCounts = useMemo(() => {
    const inLibrary = page?.works.filter((work) => work.in_library).length ?? 0;
    const available = (page?.works.length ?? 0) - inLibrary;
    return { inLibrary, available };
  }, [page]);
  const visibleWorks = useMemo(() => {
    const filtered = (page?.works ?? []).filter((work) => {
      if (workFilter === "in_library") return Boolean(work.in_library);
      if (workFilter === "available") return !work.in_library;
      if (workFilter === "incomplete") return (libraryView(work)?.missing ?? 0) > 0;
      return true;
    });
    return [...filtered].sort((left, right) => {
      let comparison = 0;
      if (workSort === "year") {
        comparison = compareOptionalNumber(
          workYears(left).sort,
          workYears(right).sort,
          sortDirection,
        );
      } else if (workSort === "rating") {
        comparison = compareOptionalNumber(
          left.rating ?? null,
          right.rating ?? null,
          sortDirection,
        );
      } else {
        comparison = titleCollator.compare(left.title, right.title);
        if (sortDirection === "desc") comparison *= -1;
      }
      return comparison || titleCollator.compare(left.title, right.title);
    });
  }, [page, sortDirection, workFilter, workSort]);

  useEffect(() => {
    setWorkPage(1);
  }, [sortDirection, workFilter, workSort]);

  useEffect(() => {
    const lastPage = Math.max(1, Math.ceil(visibleWorks.length / WORK_PAGE_SIZE));
    setWorkPage((current) => Math.min(current, lastPage));
  }, [visibleWorks.length]);

  const pagedWorks = visibleWorks.slice(
    (workPage - 1) * WORK_PAGE_SIZE,
    workPage * WORK_PAGE_SIZE,
  );

  useEffect(() => {
    if (
      (workFilter === "in_library" && workCounts.inLibrary === 0) ||
      (workFilter === "available" && workCounts.available === 0)
    ) {
      setWorkFilter("all");
    }
  }, [workCounts, workFilter]);

  const refresh = async () => {
    setRefreshing(true);
    try {
      const refreshed = await api.refreshAuthor(id);
      setPage(refreshed);
      notify("success", tn(refreshed.work_count, "Updated {author}: {count} work.", "Updated {author}: {count} works.", { author: refreshed.author }));
    } catch (error) {
      notify("error", String(error));
    } finally {
      setRefreshing(false);
    }
  };

  if (loading) {
    return (
      <div className="page">
        <Spinner />
      </div>
    );
  }
  if (!page) {
    return (
      <div className="page">
        <EmptyState icon="library" title={t("Author not found")} />
      </div>
    );
  }

  return (
    <div className="page">
      {selectedWork ? (
        <AddModal
          work={selectedWork}
          initialLanguage={defaultLanguage}
          languageOptions={languageOptions}
          onClose={() => setSelectedWork(null)}
          onAdded={async (mangaId) => {
            const selectedId = selectedWork.id;
            setSelectedWork(null);
            setPage((current) =>
              current
                ? {
                    ...current,
                    library_manga_count: current.library_manga_count + 1,
                    works: current.works.map((work) =>
                      work.id === selectedId
                        ? { ...work, in_library: true, library_manga_id: mangaId }
                        : work,
                    ),
                  }
                : current,
            );
            await refreshJobs();
          }}
        />
      ) : null}

      <div className="author-page-header">
        <div className="author-page-heading">
          <a className="muted small" href="#/">{t("Comics")}</a>
          <h1>{page.author}</h1>
          <div className="muted small">
            {t("{works} works · {inLibrary} in library · {available} available to add", { works: page.work_count, inLibrary: page.library_manga_count, available: workCounts.available })}
          </div>
          {aliases.length ? <div className="muted small">{t("Also credited as")} {aliases.join(" · ")}</div> : null}
          <span className="muted small">{t("Refreshed")} {formatTimestamp(page.last_refreshed_at)}</span>
        </div>
        <button type="button" className="btn" onClick={() => void refresh()} disabled={refreshing}>
          <Icon name="refresh" /> {refreshing ? t("Refreshing…") : t("Refresh")}
        </button>
      </div>

      {page.last_refresh_error ? (
        <div className="banner banner-warn">
          <Icon name="alert" /> {page.last_refresh_error}
        </div>
      ) : null}
      {page.merged_from.length ? (
        <div className="banner banner-info small">
          {t("Merged duplicate MangaBaka credits:")} {page.merged_from.map((item) => item.name).join(" · ")}
        </div>
      ) : null}

      {page.works.length ? (
        <div className="author-page-controls" aria-label={t("Filter and sort author works")}>
          <select
            className="input"
            value={workFilter}
            aria-label={t("Filter author works")}
            onChange={(event) => setWorkFilter(event.target.value as WorkFilter)}
          >
            <option value="all">{t("Library: All ({count})", { count: page.work_count })}</option>
            {workCounts.inLibrary ? (
              <>
                <option value="in_library">{t("Library: In library ({count})", { count: workCounts.inLibrary })}</option>
                <option value="incomplete">{t("Library: Incomplete")}</option>
              </>
            ) : null}
            {workCounts.available ? (
              <option value="available">{t("Library: Available to add ({count})", { count: workCounts.available })}</option>
            ) : null}
          </select>
          <select
            className="input"
            value={workSort}
            aria-label={t("Sort author works")}
            onChange={(event) => {
              const nextSort = event.target.value as WorkSort;
              setWorkSort(nextSort);
              setSortDirection(nextSort === "rating" ? "desc" : "asc");
            }}
          >
            <option value="title">{t("Sort: Title")}</option>
            <option value="year">{t("Sort: Year")}</option>
            <option value="rating">{t("Sort: Rating")}</option>
          </select>
          <button
            type="button"
            className="btn sort-direction"
            onClick={() => setSortDirection((current) => current === "asc" ? "desc" : "asc")}
            aria-label={sortDirection === "asc" ? t("Sort descending") : t("Sort ascending")}
            title={sortDirection === "asc" ? t("Currently ascending; click to reverse") : t("Currently descending; click to reverse")}
          >
            <Icon name={sortDirection === "asc" ? "sortAscending" : "sortDescending"} />
            <span>{sortDirection === "asc" ? t("Ascending") : t("Descending")}</span>
          </button>
        </div>
      ) : null}

      {page.works.length === 0 ? (
        <EmptyState icon="library" title={t("No MangaBaka works indexed")} />
      ) : visibleWorks.length === 0 ? (
        <EmptyState icon="library" title={t("No works match this filter")} />
      ) : (
        <div className="result-list author-work-list">
          {pagedWorks.map((work) => {
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
                  {work.in_library ? <StatusPill kind="muted">{t("In library")}</StatusPill> : null}
                  {(() => {
                    const view = libraryView(work);
                    if (!view) return null;
                    return (
                      <>
                        <StatusPill kind={view.publication.kind} title={view.publication.title}>
                          {view.publication.label}
                        </StatusPill>
                        <StatusPill kind={view.availability.kind} title={view.availability.title}>
                          {view.availability.label}
                        </StatusPill>
                      </>
                    );
                  })()}
                </div>
                {(() => {
                  const view = libraryView(work);
                  if (!view) return null;
                  return (
                    <div className="muted small">
                      {t("In library:")} {view.counts}
                      {view.language ? ` · ${view.language}` : ""}
                      {view.monitor ? ` · ${t("monitor {mode}", { mode: view.monitor })}` : ""}
                    </div>
                  );
                })()}
                {work.native_title ? <div className="muted small">{work.native_title}</div> : null}
                <div className="muted small">
                  {[
                    work.volume_count ? tn(work.volume_count, "{count} volume", "{count} volumes") : null,
                    work.chapter_count
                      ? tn(work.chapter_count, "{count} chapter", "{count} chapters")
                      : work.latest_release_chapter
                        ? t("{count} chapters so far", { count: work.latest_release_chapter })
                        : null,
                    work.genres?.length ? work.genres.slice(0, 4).join(" · ") : null,
                  ]
                    .filter(Boolean)
                    .join(" · ")}
                </div>
                <p className="result-description">{work.description || t("No description available.")}</p>
              </div>
              <div className="result-actions author-work-actions">
                {work.source_url ? (
                  <a className="btn" href={work.source_url} target="_blank" rel="noreferrer">
                    {t("MangaBaka")} <Icon name="external" size={13} />
                  </a>
                ) : null}
                {work.in_library ? (
                  <a className="btn" href={`#/series/${work.library_manga_id}`}>
                    <Icon name="library" /> {t("Open")}
                  </a>
                ) : (
                  <button type="button" className="btn btn-primary" onClick={() => setSelectedWork(work)}>
                    <Icon name="add" /> {t("Add")}
                  </button>
                )}
              </div>
            </div>
            );
          })}
        </div>
      )}
      {visibleWorks.length ? (
        <Pagination
          page={workPage}
          pageSize={WORK_PAGE_SIZE}
          total={visibleWorks.length}
          onPageChange={setWorkPage}
          itemLabel={t("works")}
          ariaLabel={t("Author works pages")}
        />
      ) : null}
    </div>
  );
}
