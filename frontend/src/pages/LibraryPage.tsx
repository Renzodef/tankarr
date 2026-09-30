import { useDeferredValue, useEffect, useMemo, useState } from "react";
import { api } from "../api";
import { useLibrary } from "../useLibrary";
import { LoadError } from "../components/LoadError";
import {
  Cover,
  EmptyState,
  Icon,
  Pagination,
  Spinner,
  StatusPill,
  languageName,
  monitorLabel,
  seriesCoverUrl,
  seriesPath,
  useApp,
  useAppActions,
} from "../components";
import { additionalContentLabel, libraryStatus, publicationStatus, seriesCounts } from "../seriesStatus";
import type { Manga } from "../types";
import { workYears } from "../workYears";
import { t, tn } from "../i18n";

type SortKey = "title" | "author" | "year" | "added";
type SortDirection = "asc" | "desc";
type LibrarySortState = { key: SortKey; direction: SortDirection };
type PublicationFilter = "all" | "continuing" | "hiatus" | "ended" | "unknown";
type AvailabilityFilter = "all" | "up_to_date" | "missing";
type LibraryFilterState = {
  publication: PublicationFilter;
  availability: AvailabilityFilter;
};

const LIBRARY_SORT_STORAGE_KEY = "tankarr.library.sort.v1";
const LIBRARY_FILTER_STORAGE_KEY = "tankarr.library.filters.v1";
const DEFAULT_LIBRARY_SORT: LibrarySortState = { key: "title", direction: "asc" };
const DEFAULT_LIBRARY_FILTERS: LibraryFilterState = { publication: "all", availability: "all" };
const SORT_KEYS = new Set<SortKey>(["title", "author", "year", "added"]);
const PUBLICATION_FILTERS = new Set<PublicationFilter>(["all", "continuing", "hiatus", "ended", "unknown"]);
const AVAILABILITY_FILTERS = new Set<AvailabilityFilter>([
  "all",
  "up_to_date",
  "missing",
]);
const LIBRARY_PAGE_SIZE = 30;

function readLibrarySort(): LibrarySortState {
  try {
    const stored = window.localStorage.getItem(LIBRARY_SORT_STORAGE_KEY);
    if (!stored) return DEFAULT_LIBRARY_SORT;
    const value = JSON.parse(stored) as Partial<LibrarySortState>;
    if (
      typeof value.key === "string" &&
      SORT_KEYS.has(value.key as SortKey) &&
      (value.direction === "asc" || value.direction === "desc")
    ) {
      return { key: value.key as SortKey, direction: value.direction };
    }
  } catch {
    // Storage can be unavailable or contain stale data; use the stable default.
  }
  return DEFAULT_LIBRARY_SORT;
}

function readLibraryFilters(): LibraryFilterState {
  try {
    const stored = window.localStorage.getItem(LIBRARY_FILTER_STORAGE_KEY);
    if (!stored) return DEFAULT_LIBRARY_FILTERS;
    const value = JSON.parse(stored) as Partial<LibraryFilterState>;
    if (
      typeof value.publication === "string" &&
      PUBLICATION_FILTERS.has(value.publication as PublicationFilter) &&
      typeof value.availability === "string" &&
      AVAILABILITY_FILTERS.has(value.availability as AvailabilityFilter)
    ) {
      return {
        publication: value.publication as PublicationFilter,
        availability: value.availability as AvailabilityFilter,
      };
    }
  } catch {
    // Use defaults when browser storage is unavailable or stale.
  }
  return DEFAULT_LIBRARY_FILTERS;
}

const titleCollator = new Intl.Collator(undefined, { numeric: true, sensitivity: "base" });

function normalizeSearch(value: string) {
  return value
    .normalize("NFKD")
    .replace(/\p{Diacritic}/gu, "")
    .toLocaleLowerCase();
}

function mangaAuthors(manga: Manga) {
  return manga.metadata?.authors?.length ? manga.metadata.authors : manga.authors;
}

function primaryAuthor(manga: Manga) {
  return mangaAuthors(manga).find((author) => author.trim())?.trim() ?? "";
}

export default function LibraryPage() {
  const { notify, refreshJobs } = useAppActions();
  const { data: library, loading, error, refresh } = useLibrary();
  const [librarySort, setLibrarySort] = useState<LibrarySortState>(readLibrarySort);
  const [statusFilters, setStatusFilters] = useState<LibraryFilterState>(readLibraryFilters);
  const [filter, setFilter] = useState("");
  const deferredFilter = useDeferredValue(filter);
  const [busy, setBusy] = useState(false);
  const [page, setPage] = useState(1);
  const sort = librarySort.key;
  const sortDirection = librarySort.direction;
  const hasStatusFilter =
    statusFilters.publication !== "all" || statusFilters.availability !== "all";
  const indexedLibrary = useMemo(() => (library ?? []).map((manga) => ({
    manga,
    author: primaryAuthor(manga),
    year: workYears(manga).sort,
    added: Date.parse(manga.created_at),
    publication: publicationStatus(manga).key,
    availability: libraryStatus(manga).key,
    text: normalizeSearch([manga.title, ...mangaAuthors(manga), ...(manga.metadata?.alternate_titles ?? [])].join(" ")),
  })), [library]);
  const availableStatusFilters = useMemo(() => {
    const publication = new Set<PublicationFilter>();
    const availability = new Set<AvailabilityFilter>();
    for (const item of indexedLibrary) {
      publication.add(item.publication as PublicationFilter);
      availability.add(item.availability as AvailabilityFilter);
    }
    return { publication, availability };
  }, [indexedLibrary]);

  const load = () => refresh().catch(() => undefined);

  useEffect(() => {
    if (library === null) return;
    setStatusFilters((current) => {
      const publication =
        current.publication === "all" ||
        availableStatusFilters.publication.has(current.publication)
          ? current.publication
          : "all";
      const availability =
        current.availability === "all" ||
        availableStatusFilters.availability.has(current.availability)
          ? current.availability
          : "all";
      return publication === current.publication && availability === current.availability
        ? current
        : { publication, availability };
    });
  }, [library, availableStatusFilters]);

  useEffect(() => {
    try {
      window.localStorage.setItem(LIBRARY_SORT_STORAGE_KEY, JSON.stringify(librarySort));
    } catch {
      // Sorting still works for this session when persistent storage is blocked.
    }
  }, [librarySort]);

  useEffect(() => {
    try {
      window.localStorage.setItem(LIBRARY_FILTER_STORAGE_KEY, JSON.stringify(statusFilters));
    } catch {
      // Filtering still works for this session when persistent storage is blocked.
    }
  }, [statusFilters]);

  const sortedLibrary = useMemo(() => {
    return [...indexedLibrary].sort((left, right) => {
      let comparison = 0;
      if (sort === "author") {
        const leftAuthor = left.author;
        const rightAuthor = right.author;
        // Keep incomplete metadata at the end in both directions.
        if (!leftAuthor && rightAuthor) return 1;
        if (leftAuthor && !rightAuthor) return -1;
        comparison = titleCollator.compare(leftAuthor, rightAuthor);
      } else if (sort === "added") {
        comparison = left.added - right.added;
      } else if (sort === "year") {
        const leftYear = left.year;
        const rightYear = right.year;
        // Unknown years stay last in both directions.
        if (leftYear === null && rightYear !== null) return 1;
        if (leftYear !== null && rightYear === null) return -1;
        comparison = (leftYear ?? 0) - (rightYear ?? 0);
      } else {
        comparison = titleCollator.compare(left.manga.title, right.manga.title);
      }

      if (!Number.isFinite(comparison) || comparison === 0) {
        comparison = titleCollator.compare(left.manga.title, right.manga.title);
      }
      return sortDirection === "asc" ? comparison : -comparison;
    });
  }, [indexedLibrary, sort, sortDirection]);

  const items = useMemo(() => {
    const textFilter = normalizeSearch(deferredFilter.trim());
    return sortedLibrary.filter((item) =>
      (statusFilters.publication === "all" || item.publication === statusFilters.publication) &&
      (statusFilters.availability === "all" || item.availability === statusFilters.availability) &&
      (!textFilter || item.text.includes(textFilter)),
    ).map((item) => item.manga);
  }, [sortedLibrary, deferredFilter, statusFilters]);

  useEffect(() => {
    setPage(1);
  }, [deferredFilter, sort, sortDirection, statusFilters]);

  useEffect(() => {
    const lastPage = Math.max(1, Math.ceil(items.length / LIBRARY_PAGE_SIZE));
    setPage((current) => Math.min(current, lastPage));
  }, [items.length]);

  const visibleItems = items.slice(
    (page - 1) * LIBRARY_PAGE_SIZE,
    page * LIBRARY_PAGE_SIZE,
  );

  const refreshAll = async () => {
    setBusy(true);
    try {
      const result = await api.runMonitor();
      notify("success", t("Checked {checked} series, queued {queued} chapters.", { checked: result.checked, queued: result.queued }));
      await Promise.all([load(), refreshJobs()]);
    } catch (caught) {
      notify("error", String(caught));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="page">
      <div className="toolbar">
        <div className="toolbar-group">
          <button type="button" className="btn" aria-label={t("Refresh library")} onClick={() => void load()}>
            <Icon name="refresh" /> {t("Refresh view")}
          </button>
          <button type="button" className="btn" onClick={() => void refreshAll()} disabled={busy}>
            <Icon name="refresh" /> {t("Refresh All")}
          </button>
          <a className="btn btn-primary" href="#/add">
            <Icon name="add" /> {t("Add New")}
          </a>
        </div>
        <div className="toolbar-group">
          <input
            className="input"
            placeholder={t("Filter library…")}
            value={filter}
            onChange={(event) => setFilter(event.target.value)}
          />
          <select
            className="input"
            value={statusFilters.publication}
            aria-label={t("Filter by publication status")}
            onChange={(event) =>
              setStatusFilters((current) => ({
                ...current,
                publication: event.target.value as PublicationFilter,
              }))
            }
          >
            <option value="all">{t("Publication: All")}</option>
            {availableStatusFilters.publication.has("continuing") ? (
              <option value="continuing">{t("Publication: Continuing")}</option>
            ) : null}
            {availableStatusFilters.publication.has("hiatus") ? (
              <option value="hiatus">{t("Publication: Hiatus")}</option>
            ) : null}
            {availableStatusFilters.publication.has("ended") ? (
              <option value="ended">{t("Publication: Ended")}</option>
            ) : null}
            {availableStatusFilters.publication.has("unknown") ? (
              <option value="unknown">{t("Publication: Unknown")}</option>
            ) : null}
          </select>
          <select
            className="input"
            value={statusFilters.availability}
            aria-label={t("Filter by library status")}
            onChange={(event) =>
              setStatusFilters((current) => ({
                ...current,
                availability: event.target.value as AvailabilityFilter,
              }))
            }
          >
            <option value="all">{t("Library: All")}</option>
            {availableStatusFilters.availability.has("up_to_date") ? (
              <option value="up_to_date">{t("Library: Up to date")}</option>
            ) : null}
            {availableStatusFilters.availability.has("missing") ? (
              <option value="missing">{t("Library: Missing")}</option>
            ) : null}
          </select>
          <select
            className="input"
            value={sort}
            onChange={(event) =>
              setLibrarySort((current) => ({
                ...current,
                key: event.target.value as SortKey,
              }))
            }
          >
            <option value="title">{t("Sort: Title")}</option>
            <option value="author">{t("Sort: Author")}</option>
            <option value="year">{t("Sort: Year")}</option>
            <option value="added">{t("Sort: Date Added")}</option>
          </select>
          <button
            type="button"
            className="btn sort-direction"
            onClick={() =>
              setLibrarySort((current) => ({
                ...current,
                direction: current.direction === "asc" ? "desc" : "asc",
              }))
            }
            aria-label={sortDirection === "asc" ? t("Sort descending") : t("Sort ascending")}
            title={sortDirection === "asc" ? t("Currently ascending; click to reverse") : t("Currently descending; click to reverse")}
          >
            <Icon name={sortDirection === "asc" ? "sortAscending" : "sortDescending"} />
            <span>{sortDirection === "asc" ? t("Ascending") : t("Descending")}</span>
          </button>
        </div>
      </div>

      {error ? (
        <LoadError message={error} retryLabel={t("Retry library")} retry={() => void load()} loading={loading} hasData={library !== null} />
      ) : null}
      {library !== null && loading ? <p className="muted small" role="status">{t("Updating library…")}</p> : null}
      {library === null ? (
        error ? null : <Spinner />
      ) : items.length === 0 ? (
        <EmptyState
          icon="library"
          title={
            filter || hasStatusFilter
              ? t("No series match the filter")
              : t("Your library is empty")
          }
          hint={filter || hasStatusFilter ? undefined : <EmptyLibraryHint />}
        />
      ) : (
        <>
          <div className="poster-grid">
          {visibleItems.map((manga, index) => {
            const authors = mangaAuthors(manga).filter((item) => item.trim());
            const authorLabel = authors.length ? authors.join(", ") : t("Unknown author");
            const publication = publicationStatus(manga);
            const availability = libraryStatus(manga);
            const counts = seriesCounts(manga);
            // The series is followed in one unit and counted in it; the other
            // unit still says how large the work is, and a card that shows
            // only one of the two hides half the answer.
            //
            // Books first, then chapters, whichever unit the series is
            // followed in. Ordering them by the followed unit makes the shelf
            // unreadable - the eye cannot compare cards whose fields move -
            // and it put Moonlight Mile's two counts side by side as
            // "24 / ? chapters · 24 books", which reads as 24 of 24.
            // "141 / 147 chapters": the noun follows the total, the count on
            // the shelf comes first.
            const books = (count: number) => tn(count, "book", "books");
            const chapters = (count: number) => tn(count, "chapter", "chapters");
            const extent = counts.reference_unknown ? "?" : counts.total_count;
            const tracked = `${counts.downloaded_count} / ${extent}`;
            const other =
              counts.unit === "volume" ? counts.chapter_progress : counts.volume_progress;
            const booksPart =
              counts.unit === "volume"
                ? `${tracked} ${books(counts.total_count)}`
                : other
                  ? `${other.expected} ${books(other.expected)}`
                  : "";
            const chaptersPart =
              counts.unit === "volume"
                ? other
                  ? `${other.expected} ${chapters(other.expected)}`
                  : ""
                : `${tracked} ${chapters(counts.total_count)}`;
            const chapterLabel = [booksPart, chaptersPart].filter(Boolean).join(" · ");
            const years = workYears(manga);
            return (
              <a key={manga.id} className="poster-card" href={seriesPath(manga.id)} title={manga.title}>
                <div className="poster-frame">
                  <Cover
                    url={seriesCoverUrl(manga)}
                    title={manga.title}
                    className="poster-image"
                    priority={index === 0}
                  />
                  {manga.monitored ? (
                    <span className="poster-ribbon" title={t("Monitored: {mode}", { mode: monitorLabel(manga.monitor_mode) })}>
                      <Icon name="bookmark" size={14} />
                    </span>
                  ) : null}
                  <span className="poster-lang">{languageName(manga.preferred_language)}</span>
                </div>
                <div className="poster-meta">
                  <span className="poster-title">{manga.title}</span>
                  <span
                    className="poster-count"
                    title={availability.title}
                  >
                    {chapterLabel}
                    {additionalContentLabel(counts) ? ` + ${additionalContentLabel(counts)}` : ""}
                  </span>
                </div>
                <div className="poster-byline">
                  <span className="poster-author" title={authorLabel}>
                    {authorLabel}
                  </span>
                  {years.label ? (
                    <span className="poster-year" title={years.title ?? undefined}>{years.label}</span>
                  ) : null}
                </div>
                <div className="poster-statuses">
                  <StatusPill kind={publication.kind} title={publication.title}>
                    {publication.label}
                  </StatusPill>
                  <StatusPill kind={availability.kind} title={availability.title}>
                    {availability.label}
                  </StatusPill>
                </div>
              </a>
            );
          })}
          </div>
          <Pagination
            page={page}
            pageSize={LIBRARY_PAGE_SIZE}
            total={items.length}
            onPageChange={setPage}
            itemLabel={t("series")}
            ariaLabel={t("Library pages")}
          />
        </>
      )}
    </div>
  );
}

/** Mounted only while the library is empty, so only this hint re-renders on
 * health polls: a fresh installation has no chapter source yet. */
function EmptyLibraryHint() {
  const { health } = useApp();
  const noSources = health ? health.providers.every((item) => item.name === "local") : false;
  return (
    <>
      {noSources
        ? t("Use Add New to search the catalogue. Chapters download from the sources you enable in Settings → Sources; until then only whole volumes from the Internet Archive can be fetched.")
        : t("Use Add New to search the catalogue and add a series.")}
      {noSources ? <> <a href="#/settings?tab=sources">{t("Open Sources settings")}</a></> : null}
    </>
  );
}
