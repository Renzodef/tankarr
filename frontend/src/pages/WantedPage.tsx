import { useCallback, useDeferredValue, useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
import { LoadError } from "../components/LoadError";
import { SeriesFilter } from "../components/SeriesFilter";
import { WantedExplanation } from "../components/WantedExplanation";
import {
  ChapterReleaseSearchModal,
  type ChapterSearchTarget,
} from "../components/ChapterReleaseSearchModal";
import {
  EmptyState,
  Icon,
  Pagination,
  Spinner,
  StatusPill,
  chapterLabel,
  formatDate,
  jobStatusPill,
  providerChainLabel,
  seriesPath,
  useAppActions,
} from "../components";
import type {
  MonitorStatus,
  WantedChapter,
  WantedEntry,
  WantedRecovery,
} from "../types";
import { msg, t, tn } from "../i18n";

const PAGE_SIZE = 20;

type InteractiveSearchTarget = {
  manga: WantedEntry["manga"];
  target: ChapterSearchTarget;
};

type WantedRow = {
  key: string;
  entry: WantedEntry;
  chapter: WantedChapter | null;
  unavailable: number;
  /** The series itself: nothing lists it, so there is no slot to show. */
  noSources?: boolean;
};

type IndexedWantedRow = WantedRow & {
  searchText: string;
  state: Exclude<WantedStateFilter, "all">;
  unit: ReturnType<typeof wantedUnit>;
  stateRank: number;
  seriesRank: number;
  item: string;
  itemNumber: number;
  published: number;
};

function rowRecovery(row: WantedRow): WantedRecovery | undefined {
  return row.noSources ? row.entry.recovery : row.chapter?.recovery;
}

type WantedStateFilter =
  | "all"
  | "missing"
  | "queued"
  | "blocked"
  | "exhausted"
  | "unavailable";
type WantedUnitFilter = "all" | "chapter" | "volume";
type WantedSort = "series" | "item" | "published" | "state";
type SortDirection = "asc" | "desc";

function intervalLabel(seconds: number): string {
  if (seconds % 86400 === 0) return t("{count}d", { count: seconds / 86400 });
  if (seconds % 3600 === 0) return t("{count}h", { count: seconds / 3600 });
  return t("{count}m", { count: Math.round(seconds / 60) });
}

function normalized(value: string): string {
  return value
    .normalize("NFKD")
    .replace(/\p{Diacritic}/gu, "")
    .toLocaleLowerCase();
}

function sourceLabel(chapter: WantedChapter): string {
  if (chapter.provider === "expected") return t("No indexed source");
  return providerChainLabel(chapter.provider || "unknown", chapter.source_name);
}

function wantedState(row: WantedRow): Exclude<WantedStateFilter, "all"> {
  if (row.noSources) {
    return row.entry.recovery?.verdict === "exhausted" ? "exhausted" : "missing";
  }
  if (!row.chapter) return "unavailable";
  if (row.chapter.queue_status) return "queued";
  // Every channel was asked and none carries it. This is the difference
  // between work that is waiting for someone and work nobody can do - and
  // it outranks "blocked": a slot whose sources were all refused and whose
  // indexers have nothing is not obtainable, not merely blocked.
  if (row.chapter.recovery?.verdict === "exhausted") return "exhausted";
  if (row.chapter.blocked) return "blocked";
  return "missing";
}

const RECOVERY_CHANNEL_LABELS: Record<string, string> = {
  sources: msg("Sources"),
  indexer_chapter: msg("Indexers (chapter)"),
  indexer_book: msg("Indexers (book)"),
};

function recoveryTooltip(recovery: WantedRecovery | undefined): string {
  if (!recovery || recovery.channels.length === 0) {
    return t("No recovery pass has run for this item yet.");
  }
  return recovery.channels
    .map((channel) => {
      const label = RECOVERY_CHANNEL_LABELS[channel.channel] ?? channel.channel;
      const detail = channel.detail ? ` — ${channel.detail}` : "";
      return `${label}: ${channel.outcome}${detail}`;
    })
    .join("\n");
}

function wantedUnit(row: WantedRow): Exclude<WantedUnitFilter, "all"> | "unavailable" {
  if (!row.chapter) return "unavailable";
  return row.chapter.volume && !row.chapter.chapter ? "volume" : "chapter";
}

function wantedStateRank(row: WantedRow): number {
  const state = wantedState(row);
  if (state === "blocked") return 0;
  if (state === "missing") return 1;
  if (state === "exhausted") return 2;
  if (state === "unavailable") return 3;
  return 4;
}

const titleCollator = new Intl.Collator(undefined, { sensitivity: "base", numeric: true });
const itemCollator = new Intl.Collator(undefined, { numeric: true });

function compareWantedRows(
  left: IndexedWantedRow,
  right: IndexedWantedRow,
  sort: WantedSort,
  direction: SortDirection,
): number {
  const multiplier = direction === "asc" ? 1 : -1;
  const seriesComparison = left.seriesRank - right.seriesRank;
  const itemComparison = Number.isFinite(left.itemNumber) && Number.isFinite(right.itemNumber)
    ? left.itemNumber - right.itemNumber
    : itemCollator.compare(left.item, right.item);
  const concreteComparison =
    left.chapter === null ? (right.chapter === null ? 0 : 1) : right.chapter === null ? -1 : 0;

  if (sort === "series") {
    return (
      seriesComparison * multiplier ||
      concreteComparison ||
      itemComparison * multiplier ||
      left.key.localeCompare(right.key)
    );
  }
  if (sort === "item") {
    return (
      concreteComparison ||
      itemComparison * multiplier ||
      seriesComparison * multiplier ||
      left.key.localeCompare(right.key)
    );
  }
  if (sort === "state") {
    return (
      (left.stateRank - right.stateRank) * multiplier ||
      seriesComparison * multiplier ||
      concreteComparison ||
      itemComparison * multiplier ||
      left.key.localeCompare(right.key)
    );
  }

  const leftPublished = left.published;
  const rightPublished = right.published;
  const leftHasDate = Number.isFinite(leftPublished);
  const rightHasDate = Number.isFinite(rightPublished);
  if (leftHasDate !== rightHasDate) return leftHasDate ? -1 : 1;
  return (
    (leftHasDate ? (leftPublished - rightPublished) * multiplier : 0) ||
    seriesComparison * multiplier ||
    concreteComparison ||
    itemComparison * multiplier ||
    left.key.localeCompare(right.key)
  );
}

export default function WantedPage() {
  const { notify, refreshJobs } = useAppActions();
  const [entries, setEntries] = useState<WantedEntry[] | null>(() => api.wantedSnapshot() ?? null);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [monitor, setMonitor] = useState<MonitorStatus | null>(null);
  const [monitorError, setMonitorError] = useState<string | null>(null);
  const [monitorLoading, setMonitorLoading] = useState(false);
  const [interactiveTarget, setInteractiveTarget] = useState<InteractiveSearchTarget | null>(null);
  const [filter, setFilter] = useState("");
  const deferredFilter = useDeferredValue(filter);
  const [stateFilter, setStateFilter] = useState<WantedStateFilter>("all");
  const [unitFilter, setUnitFilter] = useState<WantedUnitFilter>("all");
  const [sort, setSort] = useState<WantedSort>("series");
  const [sortDirection, setSortDirection] = useState<SortDirection>("asc");
  const [page, setPage] = useState(1);
  const [busy, setBusy] = useState(false);
  const [explanationKey, setExplanationKey] = useState<string | null>(null);
  const requestVersion = useRef(0);
  const wantedRequest = useRef<AbortController | null>(null);
  const monitorRequest = useRef<AbortController | null>(null);
  const lastWantedUpdate = useRef(0);

  const load = useCallback(async (fresh = false) => {
    const version = ++requestVersion.current;
    wantedRequest.current?.abort();
    const controller = new AbortController();
    wantedRequest.current = controller;
    setLoading(true);
    setLoadError(null);
    try {
      const wanted = await api.wanted(fresh, controller.signal);
      if (version !== requestVersion.current || controller.signal.aborted) return false;
      setEntries(wanted);
      lastWantedUpdate.current = Date.now();
      return true;
    } catch (caught) {
      if (version === requestVersion.current && !controller.signal.aborted) {
        setLoadError(caught instanceof Error ? caught.message : String(caught));
      }
      return false;
    } finally {
      if (version === requestVersion.current && !controller.signal.aborted) setLoading(false);
      if (wantedRequest.current === controller) wantedRequest.current = null;
    }
  }, []);

  const loadMonitor = useCallback(async () => {
    monitorRequest.current?.abort();
    const controller = new AbortController();
    monitorRequest.current = controller;
    setMonitorLoading(true);
    setMonitorError(null);
    try {
      const status = await api.monitorStatus(controller.signal);
      if (!controller.signal.aborted) {
        setMonitor(status);
        return status;
      }
    } catch (caught) {
      if (!controller.signal.aborted) {
        setMonitorError(caught instanceof Error ? caught.message : String(caught));
      }
    } finally {
      if (!controller.signal.aborted) setMonitorLoading(false);
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    let idleHandle: number | undefined;
    let timerHandle: number | undefined;
    let frameHandle: number | undefined;

    const start = async () => {
      // Paint the last snapshot at once. During active downloads the server
      // may intentionally serve stale-while-revalidate; validate that copy
      // only after the browser has rendered and become idle.
      const loaded = api.wantedSnapshot() !== undefined || await load();
      if (cancelled || !loaded) return;
      const initialVersion = requestVersion.current;
      const refresh = () => {
        // A user action may already have requested newer data during idle time.
        if (!cancelled && requestVersion.current === initialVersion) void load(true);
      };
      frameHandle = window.requestAnimationFrame(() => {
        if ("requestIdleCallback" in window) {
          idleHandle = window.requestIdleCallback(refresh, { timeout: 2_000 });
        } else {
          timerHandle = globalThis.setTimeout(refresh, 750);
        }
      });
    };

    void start();
    void loadMonitor();
    return () => {
      cancelled = true;
      requestVersion.current += 1;
      wantedRequest.current?.abort();
      monitorRequest.current?.abort();
      if (idleHandle !== undefined) window.cancelIdleCallback(idleHandle);
      if (timerHandle !== undefined) window.clearTimeout(timerHandle);
      if (frameHandle !== undefined) window.cancelAnimationFrame(frameHandle);
    };
  }, [load, loadMonitor]);

  useEffect(() => {
    if (!monitor?.wanted_search.running) return;
    let stopped = false;
    let timer: number;
    const poll = async () => {
      const status = await loadMonitor();
      if (stopped) return;
      if (status && !status.wanted_search.running) {
        void load(true);
        return;
      }
      timer = window.setTimeout(() => void poll(), document.hidden ? 30_000 : 5_000);
    };
    timer = window.setTimeout(() => void poll(), 5_000);
    return () => { stopped = true; window.clearTimeout(timer); };
  }, [monitor?.wanted_search.running, loadMonitor, load]);

  useEffect(() => {
    const revalidate = () => {
      if (document.hidden || wantedRequest.current || Date.now() - lastWantedUpdate.current < 10_000) return;
      void load(true);
      void loadMonitor();
    };
    window.addEventListener("focus", revalidate);
    document.addEventListener("visibilitychange", revalidate);
    return () => {
      window.removeEventListener("focus", revalidate);
      document.removeEventListener("visibilitychange", revalidate);
    };
  }, [load, loadMonitor]);

  const searchAll = async () => {
    setBusy(true);
    try {
      const result = await api.searchWanted();
      notify(
        result.errors.length ? "info" : "success",
        t("Wanted recovery checked {missing} missing chapters, discovered {discovered} new direct releases and queued {queued}.", result),
      );
      await Promise.all([load(true), refreshJobs()]);
    } catch (caught) {
      notify("error", String(caught));
    } finally {
      setBusy(false);
    }
  };

  const searchSeries = async (id: string, title: string) => {
    setBusy(true);
    try {
      const result = await api.searchMissing(id);
      notify("success", t("{title}: queued {queued} chapters.", { title, queued: result.queued }));
      await Promise.all([load(true), refreshJobs()]);
    } catch (caught) {
      notify("error", String(caught));
    } finally {
      setBusy(false);
    }
  };

  const searchChapter = async (manga: WantedEntry["manga"], chapter: WantedChapter) => {
    setBusy(true);
    try {
      const result = await api.automaticSearchChapter(manga.id, chapter.chapter, chapter.volume);
      notify(
        result.state === "queued" ? "success" : "info",
        result.errors.length ? `${result.message} ${result.errors.join(" · ")}` : result.message,
      );
      await Promise.all([load(true), refreshJobs()]);
    } catch (caught) {
      notify("error", String(caught));
    } finally {
      setBusy(false);
    }
  };

  // "Not obtainable" is a verdict, not an action: what happens next is the
  // operator's call. Ignoring narrows the monitor; deleting stays on the
  // series page behind its own confirmation.
  const ignoreRow = async (row: WantedRow) => {
    const { entry, chapter } = row;
    setBusy(true);
    try {
      if (chapter && !(chapter.provider ?? "").startsWith("expected")) {
        await api.setChapterMonitored(entry.manga.id, chapter.id, false);
        notify("success", t("{item} is no longer monitored.", { item: chapterLabel(chapter.volume, chapter.chapter) }));
      } else if (chapter?.volume && !chapter.chapter) {
        await api.setVolumeMonitoring(entry.manga.id, chapter.volume, "ignored");
        notify("success", t("Volume {volume} of {title} is now ignored.", { volume: chapter.volume, title: entry.manga.title }));
      } else {
        await api.updateManga(entry.manga.id, { monitor_mode: "none" });
        notify("success", t("{item} is no longer monitored.", { item: entry.manga.title }));
      }
      await load(true);
    } catch (caught) {
      notify("error", String(caught));
    } finally {
      setBusy(false);
    }
  };

  const rows = useMemo<IndexedWantedRow[]>(() => {
    // Compare series titles once per series, not once per pair of chapters.
    // Thousands of chapters then sort using inexpensive numeric ranks.
    const titles = [...new Set((entries ?? []).map((entry) => entry.manga.title))]
      .sort(titleCollator.compare);
    const titleRanks = new Map<string, number>();
    let rank = 0;
    titles.forEach((title, index) => {
      if (index && titleCollator.compare(titles[index - 1], title) !== 0) rank += 1;
      titleRanks.set(title, rank);
    });
    const rawRows = (entries ?? []).flatMap((entry) => {
      const chapterRows: WantedRow[] = entry.chapters.map((chapter) => ({
        key: `${entry.manga.id}:${chapter.id}`,
        entry,
        chapter,
        unavailable: 0,
      }));
      if (entry.unmapped_expected_count) {
        chapterRows.push({
          key: `${entry.manga.id}:unavailable`,
          entry,
          chapter: null,
          unavailable: entry.unmapped_expected_count,
        });
      }
      if (entry.no_sources) {
        chapterRows.push({
          key: `${entry.manga.id}:no-sources`,
          entry,
          chapter: null,
          unavailable: 0,
          noSources: true,
        });
      }
      return chapterRows;
    });
    return rawRows.map((row) => {
      const { entry, chapter } = row;
      const item = chapter?.chapter ?? chapter?.volume ?? "";
      return {
        ...row,
        state: wantedState(row),
        unit: wantedUnit(row),
        stateRank: wantedStateRank(row),
        seriesRank: titleRanks.get(entry.manga.title) ?? 0,
        item,
        itemNumber: Number(item),
        published: chapter?.publish_at ? Date.parse(chapter.publish_at) : Number.NaN,
        searchText: normalized([
          entry.manga.title,
          chapter?.title,
          chapter?.provider,
          chapter?.source_name,
          chapter ? sourceLabel(chapter) : "unavailable catalogue gap",
          chapter ? chapterLabel(chapter.volume, chapter.chapter) : "unavailable",
          chapter ? null : "no source",
          chapter?.blocked ? "blocked" : null,
          chapter?.queue_status,
        ].filter(Boolean).join(" ")),
      };
    });
  }, [entries]);

  const stateCounts = useMemo(() => {
    const counts: Record<Exclude<WantedStateFilter, "all">, number> = {
      missing: 0,
      queued: 0,
      blocked: 0,
      exhausted: 0,
      unavailable: 0,
    };
    rows.forEach((row) => {
      counts[row.state] += 1;
    });
    return counts;
  }, [rows]);

  const seriesFilterOptions = useMemo(
    () => (entries ?? []).map((entry) => entry.manga.title),
    [entries],
  );

  const unitCounts = useMemo(() => {
    const counts: Record<Exclude<WantedUnitFilter, "all">, number> = { chapter: 0, volume: 0 };
    rows.forEach((row) => {
      const unit = row.unit;
      if (unit !== "unavailable") counts[unit] += 1;
    });
    return counts;
  }, [rows]);

  const sortedRows = useMemo(
    () => [...rows].sort((left, right) => compareWantedRows(left, right, sort, sortDirection)),
    [rows, sort, sortDirection],
  );

  const filteredRows = useMemo(() => {
    const query = normalized(deferredFilter.trim());
    return sortedRows.filter((row) =>
      (stateFilter === "all" || row.state === stateFilter) &&
      (unitFilter === "all" || row.unit === unitFilter) &&
      (!query || row.searchText.includes(query)),
    );
  }, [deferredFilter, sortedRows, stateFilter, unitFilter]);

  useEffect(() => {
    if (stateFilter !== "all" && stateCounts[stateFilter] === 0) setStateFilter("all");
  }, [stateCounts, stateFilter]);

  useEffect(() => {
    if (unitFilter !== "all" && unitCounts[unitFilter] === 0) setUnitFilter("all");
  }, [unitCounts, unitFilter]);

  useEffect(() => {
    setPage(1);
  }, [deferredFilter, sort, sortDirection, stateFilter, unitFilter]);

  useEffect(() => {
    const lastPage = Math.max(1, Math.ceil(filteredRows.length / PAGE_SIZE));
    setPage((current) => Math.min(current, lastPage));
  }, [filteredRows.length]);

  const visibleRows = filteredRows.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE);
  const totalMissing = useMemo(() => (entries ?? []).reduce(
    (sum, entry) => sum + entry.chapters.length + entry.unmapped_expected_count + Number(Boolean(entry.no_sources)),
    0,
  ), [entries]);
  const searchableMissing = useMemo(() => (entries ?? []).reduce(
    (sum, entry) =>
      sum +
      entry.chapters.filter((chapter) => !chapter.queue_status).length +
      entry.unmapped_expected_count + Number(Boolean(entry.no_sources)),
    0,
  ), [entries]);

  return (
    <div className="page wanted-page">
      <div className="toolbar wanted-toolbar">
        <div className="page-heading">
          <h1 className="page-title">{t("Wanted")}</h1>
          <span className="muted small">
            {monitor === null
              ? monitorError ? t("Recovery status unavailable") : t("Loading recovery status…")
              : monitor.wanted_search.enabled
              ? t("Automatic recovery every {interval}", { interval: intervalLabel(monitor.wanted_search.interval_seconds) }) + (
                  monitor.wanted_search.next_search_at
                    ? ` · ${t("next {date}", { date: formatDate(monitor.wanted_search.next_search_at) })}`
                    : ""
                )
              : t("Automatic recovery disabled")}
          </span>
          {monitor?.wanted_search.running ? <p role="status">{t("Recovery in progress")}{monitor.wanted_search.progress ? ` · ${t("{done}/{total} series checked", { done: monitor.wanted_search.progress.processed, total: monitor.wanted_search.progress.total })}` : ""}</p> : null}
          {monitor?.wanted_search.last_error ? <p className="banner banner-warn">{t("Last recovery error:")} {monitor.wanted_search.last_error}</p> : null}
        </div>
        <div className="toolbar-group wanted-summary">
          <strong>{totalMissing}</strong>
          <span className="muted">{t("missing from library")}</span>
          <button type="button" className="btn" aria-label={t("Refresh wanted")} onClick={() => { void load(true); void loadMonitor(); }}>
            <Icon name="refresh" /> {t("Refresh view")}
          </button>
          <button
            type="button"
            className="btn btn-primary"
            disabled={busy || searchableMissing === 0}
            onClick={() => void searchAll()}
          >
            <Icon name="refresh" /> {t("Recover Wanted Now")}
          </button>
        </div>
      </div>

      {loadError ? (
        <LoadError message={loadError} retryLabel={t("Retry wanted")} retry={() => void load(true)} loading={loading} hasData={entries !== null} />
      ) : null}
      {monitorError ? (
        <LoadError message={t("Recovery status unavailable: {error}", { error: monitorError })} retryLabel={t("Retry recovery status")} retry={() => void loadMonitor()} loading={monitorLoading} />
      ) : null}
      {entries !== null && loading ? <p className="muted small" role="status">{t("Updating wanted…")}</p> : null}

      {entries !== null && rows.length ? (
        <div className="list-controls" aria-label={t("Filter and sort wanted items")}>
          <div className="list-control-fields">
            <SeriesFilter
              value={filter}
              series={seriesFilterOptions}
              onChange={setFilter}
              placeholder={t("Filter series or pattern…")}
              ariaLabel={t("Filter wanted items by series or pattern")}
            />
            <select
              className="input"
              value={stateFilter}
              aria-label={t("Filter wanted items by state")}
              onChange={(event) => setStateFilter(event.target.value as WantedStateFilter)}
            >
              <option value="all">{t("State: All ({count})", { count: rows.length })}</option>
              {stateCounts.missing ? <option value="missing">{t("State: Missing ({count})", { count: stateCounts.missing })}</option> : null}
              {stateCounts.queued ? <option value="queued">{t("State: Queued ({count})", { count: stateCounts.queued })}</option> : null}
              {stateCounts.blocked ? <option value="blocked">{t("State: Blocked ({count})", { count: stateCounts.blocked })}</option> : null}
              {stateCounts.exhausted ? (
                <option value="exhausted">{t("State: Not obtainable ({count})", { count: stateCounts.exhausted })}</option>
              ) : null}
              {stateCounts.unavailable ? (
                <option value="unavailable">{t("State: Unavailable ({count})", { count: stateCounts.unavailable })}</option>
              ) : null}
            </select>
            <select
              className="input"
              value={unitFilter}
              aria-label={t("Filter wanted items by type")}
              onChange={(event) => setUnitFilter(event.target.value as WantedUnitFilter)}
            >
              <option value="all">{t("Item: All")}</option>
              {unitCounts.chapter ? <option value="chapter">{t("Item: Chapters ({count})", { count: unitCounts.chapter })}</option> : null}
              {unitCounts.volume ? <option value="volume">{t("Item: Volumes ({count})", { count: unitCounts.volume })}</option> : null}
            </select>
            <select
              className="input"
              value={sort}
              aria-label={t("Sort wanted items")}
              onChange={(event) => {
                const nextSort = event.target.value as WantedSort;
                setSort(nextSort);
                setSortDirection(nextSort === "published" ? "desc" : "asc");
              }}
            >
              <option value="series">{t("Sort: Series and item")}</option>
              <option value="item">{t("Sort: Item number")}</option>
              <option value="published">{t("Sort: Published")}</option>
              <option value="state">{t("Sort: State")}</option>
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
          <span className="muted small list-result-count">
            {filteredRows.length === rows.length
              ? tn(rows.length, "{count} actionable row", "{count} actionable rows")
              : t("{shown} of {total} rows", { shown: filteredRows.length, total: rows.length })}
          </span>
        </div>
      ) : null}

      {entries === null ? (
        loadError ? null : <Spinner />
      ) : entries.length === 0 ? (
        <EmptyState
          icon="check"
          title={t("Nothing is missing")}
          hint={t("Every monitored backlog release is downloaded.")}
        />
      ) : filteredRows.length === 0 ? (
        <EmptyState icon="search" title={t("No wanted items match this filter")} />
      ) : (
        <>
          <div className="data-table-frame">
            <table className="table responsive-list-table wanted-table">
              <thead>
                <tr>
                  <th>{t("Series")}</th>
                  <th className="col-wanted-item">{t("Item")}</th>
                  <th>{t("Release")}</th>
                  <th className="col-date">{t("Published")}</th>
                  <th className="col-status">{t("Status")}</th>
                  <th className="col-actions" aria-label={t("Actions")} />
                </tr>
              </thead>
              <tbody>
                {visibleRows.map((row) => {
                  const { key, entry, chapter, unavailable, noSources } = row;
                  const pill = chapter?.queue_status ? jobStatusPill(chapter.queue_status) : null;
                  const recovery = rowRecovery(row);
                  const exhausted = recovery?.verdict === "exhausted";
                  return (
                    <tr key={key}>
                      <td className="wanted-series" data-label={t("Series")}>
                        <a className="table-link" href={seriesPath(entry.manga.id)}>
                          {entry.manga.title}
                        </a>
                      </td>
                      <td className="col-wanted-item" data-label={t("Item")}>
                        {chapter
                          ? chapterLabel(chapter.volume, chapter.chapter)
                          : noSources
                            ? t("Whole series")
                            : tn(unavailable, "{count} unindexed item", "{count} unindexed items")}
                      </td>
                      <td className="wanted-release" data-label={t("Release")}>
                        <span className="wanted-release-title">
                          {chapter?.title ||
                            (chapter
                              ? t("Untitled release")
                              : noSources
                                ? t("No installed source lists this work")
                                : t("Not offered by any configured source yet"))}
                        </span>
                        <span className="muted small">
                          {chapter
                            ? sourceLabel(chapter)
                            : noSources
                              ? t("No catalogue count either — the indexers are asked for the whole work")
                              : t("Catalogue count only")}
                        </span>
                      </td>
                      <td className="col-date muted" data-label={t("Published")}>
                        {chapter?.publish_at ? formatDate(chapter.publish_at) : "—"}
                      </td>
                      <td className="col-status" data-label={t("Status")}>
                        {pill ? (
                          <StatusPill kind={pill.kind}>{pill.label}</StatusPill>
                        ) : exhausted ? (
                          <StatusPill kind="muted">
                            <span title={recoveryTooltip(recovery)}>{t("Not obtainable")}</span>
                          </StatusPill>
                        ) : chapter?.blocked ? (
                          <StatusPill kind="danger">
                            <span
                              title={
                                chapter.block_reason ??
                                t("Every release for this item failed; retry it from History to unblock.")
                              }
                            >
                              {t("Blocked")}
                            </span>
                          </StatusPill>
                        ) : chapter?.recovery?.verdict === "needs_review" ? (
                          <StatusPill kind="warn">
                            <span title={recoveryTooltip(chapter.recovery)}>
                              {t("Needs review")}
                            </span>
                          </StatusPill>
                        ) : chapter ? (
                          <StatusPill kind="warn">
                            <span title={recoveryTooltip(chapter.recovery)}>{t("Missing")}</span>
                          </StatusPill>
                        ) : noSources ? (
                          recovery?.verdict === "exhausted" ? (
                            <StatusPill kind="muted">
                              <span title={recoveryTooltip(recovery)}>{t("Not obtainable")}</span>
                            </StatusPill>
                          ) : recovery?.verdict === "needs_review" ? (
                            <StatusPill kind="warn">
                              <span title={recoveryTooltip(recovery)}>{t("Needs review")}</span>
                            </StatusPill>
                          ) : (
                            <StatusPill kind="warn">
                              <span title={recoveryTooltip(recovery)}>{t("No source")}</span>
                            </StatusPill>
                          )
                        ) : (
                          <StatusPill kind="muted">{t("Unavailable")}</StatusPill>
                        )}
                        {recovery && recovery.channels.length > 0 ? (
                          <span className="muted small wanted-recovery-summary">
                            {recovery.summary}
                          </span>
                        ) : null}
                        <button type="button" className="btn btn-small" aria-label={t("Why still wanted: {title} {item}", { title: entry.manga.title, item: chapter ? chapterLabel(chapter.volume, chapter.chapter) : t("whole series") })} onClick={() => setExplanationKey(key)}>{t("Why still wanted?")}</button>
                      </td>
                      <td className="col-actions" data-label={t("Actions")}>
                        <div className="chapter-actions">
                          {exhausted ? (
                            <button
                              type="button"
                              className="btn btn-ghost btn-icon"
                              disabled={busy}
                              title={
                                chapter && !chapter.provider?.startsWith("expected")
                                  ? t("Ignore: stop monitoring this release (manual; never automatic)")
                                  : chapter?.volume && !chapter.chapter
                                    ? t("Ignore: stop monitoring volume {volume} (manual; never automatic)", { volume: chapter.volume })
                                    : t("Stop monitoring this series (manual; never automatic). Delete it from the series page if you want it gone.")
                              }
                              aria-label={t("Ignore {item}", {
                                item: chapter ? chapterLabel(chapter.volume, chapter.chapter) : entry.manga.title,
                              })}
                              onClick={() => void ignoreRow(row)}
                            >
                              <Icon name="close" size={16} />
                            </button>
                          ) : null}
                          <button
                            type="button"
                            className="btn btn-ghost btn-icon"
                            disabled={busy || Boolean(chapter?.queue_status)}
                            title={t("Automatic Search: queue the best verified release")}
                            aria-label={t("Automatic Search for {item}", {
                              item: chapter ? chapterLabel(chapter.volume, chapter.chapter) : entry.manga.title,
                            })}
                            onClick={() =>
                              chapter
                                ? void searchChapter(entry.manga, chapter)
                                : void searchSeries(entry.manga.id, entry.manga.title)
                            }
                          >
                            <Icon name="search" size={16} />
                          </button>
                          {chapter ? (
                            <button
                              type="button"
                              className="btn btn-ghost btn-icon"
                              disabled={busy}
                              title={t("Interactive Search: inspect every result")}
                              aria-label={t("Interactive Search for {item}", {
                                item: chapterLabel(chapter.volume, chapter.chapter),
                              })}
                              onClick={() =>
                                setInteractiveTarget({
                                  manga: entry.manga,
                                  target: {
                                    key: chapter.id,
                                    chapter: chapter.chapter,
                                    volume: chapter.volume,
                                  },
                                })
                              }
                            >
                              <Icon name="user" size={16} />
                            </button>
                          ) : null}
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          <Pagination
            page={page}
            pageSize={PAGE_SIZE}
            total={filteredRows.length}
            onPageChange={setPage}
            itemLabel={t("wanted items")}
            ariaLabel={t("Wanted pages")}
          />
        </>
      )}

      {explanationKey ? (() => {
        const row = rows.find((candidate) => candidate.key === explanationKey);
        if (!row) return null;
        const chapter = row.chapter;
        return <WantedExplanation
          mangaId={row.entry.manga.id}
          title={row.entry.manga.title}
          item={chapter ? chapterLabel(chapter.volume, chapter.chapter) : t("Whole series")}
          recovery={rowRecovery(row)}
          blockReason={chapter?.block_reason}
          busy={busy || Boolean(chapter?.queue_status)}
          onClose={() => setExplanationKey(null)}
          onSearch={() => chapter ? void searchChapter(row.entry.manga, chapter) : void searchSeries(row.entry.manga.id, row.entry.manga.title)}
          onInteractive={chapter ? () => {
            setExplanationKey(null);
            setInteractiveTarget({ manga: row.entry.manga, target: { key: chapter.id, chapter: chapter.chapter, volume: chapter.volume } });
          } : undefined}
        />;
      })() : null}
      {interactiveTarget ? (
        <ChapterReleaseSearchModal
          manga={interactiveTarget.manga}
          target={interactiveTarget.target}
          onClose={() => setInteractiveTarget(null)}
          onQueued={async () => {
            await Promise.all([load(true), refreshJobs()]);
          }}
        />
      ) : null}
    </div>
  );
}
