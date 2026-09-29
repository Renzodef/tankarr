import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
import { SeriesFilter } from "../components/SeriesFilter";
import { LoadError } from "../components/LoadError";
import ImportDecisionsDetails, { importDecisionsSearchText } from "../components/ImportDecisionsDetails";
import {
  Cover,
  EmptyState,
  Icon,
  Pagination,
  Spinner,
  StatusPill,
  chapterLabel,
  compareChapterNumbers,
  formatDate,
  jobStatusPill,
  providerChainLabel,
  seriesPath,
  useApp,
} from "../components";
import type { Job, TorrentDownload } from "../types";
import { t, tn } from "../i18n";

type HistoryRow = Pick<Job, "id" | "manga_id" | "manga_title" | "manga_cover_url" | "chapter_volume" | "chapter_number" | "chapter_title" | "chapter_provider" | "chapter_source_name" | "status" | "message" | "result_path" | "updated_at"> & {
  key: string;
  torrent?: TorrentDownload;
};

function torrentHistoryRow(torrent: TorrentDownload): HistoryRow {
  return {
    key: `torrent:${torrent.id}`, id: torrent.id, torrent,
    manga_id: torrent.manga_id, manga_title: torrent.manga_title, manga_cover_url: torrent.manga_cover_url,
    chapter_volume: torrent.volume_hint, chapter_number: torrent.chapter_hint, chapter_title: torrent.title,
    chapter_provider: torrent.source, chapter_source_name: torrent.indexer,
    status: torrent.status === "imported" ? "completed" : "failed", message: torrent.message,
    result_path: null, updated_at: torrent.updated_at,
  };
}

type HistoryStateFilter = "all" | "completed" | "failed";
type HistorySort = "updated" | "series" | "item" | "state";
type SortDirection = "asc" | "desc";
const PAGE_SIZE = 20;

function normalized(value: string): string {
  return value
    .normalize("NFKD")
    .replace(/\p{Diacritic}/gu, "")
    .toLocaleLowerCase();
}

function historyStateRank(job: HistoryRow): number {
  return job.status === "failed" ? 0 : 1;
}

function compareHistory(
  left: HistoryRow,
  right: HistoryRow,
  sort: HistorySort,
  direction: SortDirection,
): number {
  const multiplier = direction === "asc" ? 1 : -1;
  const seriesComparison = (left.manga_title ?? left.manga_id).localeCompare(
    right.manga_title ?? right.manga_id,
    undefined,
    { sensitivity: "base", numeric: true },
  );
  const itemComparison = compareChapterNumbers(
    left.chapter_number ?? left.chapter_volume,
    right.chapter_number ?? right.chapter_volume,
  );

  if (sort === "series") {
    return seriesComparison * multiplier || itemComparison * multiplier || right.key.localeCompare(left.key, undefined, { numeric: true });
  }
  if (sort === "item") {
    return itemComparison * multiplier || seriesComparison * multiplier || right.key.localeCompare(left.key, undefined, { numeric: true });
  }
  if (sort === "state") {
    return (historyStateRank(left) - historyStateRank(right)) * multiplier ||
      seriesComparison * multiplier ||
      itemComparison * multiplier ||
      right.key.localeCompare(left.key, undefined, { numeric: true });
  }

  const leftUpdated = Date.parse(left.updated_at);
  const rightUpdated = Date.parse(right.updated_at);
  return (leftUpdated - rightUpdated) * multiplier || right.key.localeCompare(left.key, undefined, { numeric: true });
}

export default function HistoryPage() {
  const { notify, refreshJobs } = useApp();
  const [history, setHistory] = useState<HistoryRow[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const requestVersion = useRef(0);
  const [filter, setFilter] = useState("");
  const [stateFilter, setStateFilter] = useState<HistoryStateFilter>("all");
  const [sort, setSort] = useState<HistorySort>("updated");
  const [sortDirection, setSortDirection] = useState<SortDirection>("desc");
  const [page, setPage] = useState(1);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    const current = ++requestVersion.current;
    setLoading(true);
    setLoadError(null);
    const [jobs, torrents] = await Promise.allSettled([
      api.jobs(["completed", "failed"], 500),
      api.torrents(undefined, ["imported", "failed"], 500),
    ]);
    if (current !== requestVersion.current) return;
    setHistory((previous) => [
      ...(jobs.status === "fulfilled" ? jobs.value.filter((job) => ["completed", "failed"].includes(job.status)).map((job) => ({ ...job, key: `job:${job.id}` })) : (previous ?? []).filter((row) => !row.torrent)),
      ...(torrents.status === "fulfilled" ? torrents.value.filter((torrent) => ["imported", "failed"].includes(torrent.status)).map(torrentHistoryRow) : (previous ?? []).filter((row) => row.torrent)),
    ]);
    const errors = [jobs.status === "rejected" ? t("Chapter history unavailable: {error}", { error: String(jobs.reason) }) : "", torrents.status === "rejected" ? t("Release history unavailable: {error}", { error: String(torrents.reason) }) : ""].filter(Boolean);
    setLoadError(errors.length ? errors.join(" · ") : null);
    setLoading(false);
  }, []);

  useEffect(() => {
    void load();
    return () => { requestVersion.current += 1; };
  }, [load]);

  useEffect(() => {
    setPage(1);
  }, [filter, sort, sortDirection, stateFilter]);

  const stateCounts = useMemo(() => {
    const counts: Record<Exclude<HistoryStateFilter, "all">, number> = {
      completed: 0,
      failed: 0,
    };
    (history ?? []).forEach((job) => {
      if (job.status === "completed" || job.status === "failed") counts[job.status] += 1;
    });
    return counts;
  }, [history]);

  const seriesFilterOptions = useMemo(
    () => (history ?? []).map((job) => job.manga_title ?? job.manga_id),
    [history],
  );

  const filteredHistory = useMemo(() => {
    const query = normalized(filter.trim());
    return (history ?? [])
      .filter((job) => stateFilter === "all" || job.status === stateFilter)
      .filter((job) => {
        if (!query) return true;
        return normalized(
          [
            job.manga_title,
            job.manga_id,
            job.chapter_title,
            chapterLabel(job.chapter_volume, job.chapter_number),
            job.chapter_provider,
            job.chapter_source_name,
            providerChainLabel(job.chapter_provider ?? "unknown", job.chapter_source_name),
            job.status,
            job.message,
            job.result_path,
            job.torrent ? importDecisionsSearchText(job.torrent.language_evidence ?? {}, job.torrent.imported_paths) : "",
          ]
            .filter(Boolean)
            .join(" "),
        ).includes(query);
      })
      .sort((left, right) => compareHistory(left, right, sort, sortDirection));
  }, [filter, history, sort, sortDirection, stateFilter]);

  useEffect(() => {
    if (stateFilter !== "all" && stateCounts[stateFilter] === 0) setStateFilter("all");
  }, [stateCounts, stateFilter]);

  useEffect(() => {
    const lastPage = Math.max(1, Math.ceil(filteredHistory.length / PAGE_SIZE));
    setPage((current) => Math.min(current, lastPage));
  }, [filteredHistory.length]);

  const visibleHistory = filteredHistory.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE);
  const changeSort = (nextSort: HistorySort) => {
    setSort(nextSort);
    setSortDirection(nextSort === "updated" ? "desc" : "asc");
  };
  const historyControls = (
    <HistoryControls
      filter={filter}
      series={seriesFilterOptions}
      onFilterChange={setFilter}
      stateFilter={stateFilter}
      stateCounts={stateCounts}
      onStateFilterChange={setStateFilter}
      sort={sort}
      sortDirection={sortDirection}
      onSortChange={changeSort}
      onSortDirectionChange={() => setSortDirection((current) => current === "asc" ? "desc" : "asc")}
      total={history?.length ?? 0}
      filteredTotal={filteredHistory.length}
    />
  );

  const retry = async (row: HistoryRow, overrideQuality = false) => {
    setBusy(true);
    try {
      if (row.torrent) await api.retryTorrent(row.id);
      else await api.retryJob(row.id, { overrideQuality });
      notify("success", row.torrent ? t("Release requeued.") : overrideQuality ? t("Job requeued; the length gate is waived for it.") : t("Job requeued."));
      await Promise.all([load(), refreshJobs()]);
    } catch (caught) {
      notify("error", String(caught));
    } finally {
      setBusy(false);
    }
  };

  const removeJob = async (jobId: number) => {
    setBusy(true);
    try {
      await api.deleteJob(jobId);
      await load();
    } catch (caught) {
      notify("error", String(caught));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="page">
      <div className="toolbar activity-toolbar">
        <div>
          <h1 className="page-title">{t("Activity")}</h1>
          <nav className="page-tabs" aria-label={t("Activity sections")}>
            <a className="page-tab" href="#/activity">
              {t("Queue")}
            </a>
            <a className="page-tab active" href="#/history" aria-current="page">
              {t("History")}
            </a>
          </nav>
        </div>
      </div>
      {loadError ? <LoadError message={loadError} retryLabel={t("Retry history")} retry={() => void load()} loading={loading} hasData={Boolean(history?.length)} /> : null}
      {history === null ? (
        <Spinner />
      ) : history.length === 0 ? (
        loadError ? null : <EmptyState icon="history" title={t("No history yet")} />
      ) : filteredHistory.length === 0 ? (
        <>
          {historyControls}
          <EmptyState icon="search" title={t("No history records match these filters")} />
        </>
      ) : (
        <>
        {historyControls}
        <div className="data-table-frame">
        <table className="table responsive-list-table history-table">
          <thead>
            <tr>
              <th className="col-cover" aria-label={t("Cover")} />
              <th>{t("Series")}</th>
              <th>{t("Item")}</th>
              <th className="col-provider">{t("Provider")}</th>
              <th className="col-status">{t("Status")}</th>
              <th>{t("Detail")}</th>
              <th className="col-date">{t("Updated")}</th>
              <th className="col-actions" aria-label={t("Actions")} />
            </tr>
          </thead>
          <tbody>
            {visibleHistory.map((job) => {
              const pill = jobStatusPill(job.status);
              return (
                <tr key={job.key}>
                  <td className="col-cover" data-label={t("Cover")}>
                    <Cover url={job.manga_cover_url} title={job.manga_title ?? "?"} className="table-cover" />
                  </td>
                  <td data-label={t("Series")}>
                    <a className="table-link" href={seriesPath(job.manga_id)}>
                      {job.manga_title ?? job.manga_id}
                    </a>
                  </td>
                  <td data-label={t("Item")}>{chapterLabel(job.chapter_volume, job.chapter_number)}{job.torrent ? <div className="muted small">{job.torrent.title}</div> : null}</td>
                  <td className="col-provider" data-label={t("Source")}>
                    <StatusPill kind="provider">
                      {providerChainLabel(job.chapter_provider ?? "unknown", job.chapter_source_name)}
                    </StatusPill>
                  </td>
                  <td className="col-status" data-label={t("Status")}>
                    {!job.torrent && job.status === "failed" && (job.message ?? "").startsWith("DegradedPagesError") ? (
                      <StatusPill kind="muted">
                        <span title={t("Tankarr refused this download because the pages were unreadable (strips or fragments); the failover moves on to another source.")}>
                          {t("Refused · unreadable")}
                        </span>
                      </StatusPill>
                    ) : (
                      <StatusPill kind={pill.kind}>{pill.label}</StatusPill>
                    )}
                  </td>
                  <td className="muted history-message" data-label={t("Detail")} title={job.message}>
                    <div>{job.status === "completed" && job.result_path ? job.result_path : job.message}</div>
                    {job.torrent ? <ImportDecisionsDetails evidence={job.torrent.language_evidence ?? {}} importedPaths={job.torrent.imported_paths} /> : null}
                  </td>
                  <td className="col-date muted" data-label={t("Updated")}>{formatDate(job.updated_at)}</td>
                  <td className="col-actions" data-label={t("Actions")}>
                    {job.status === "failed" ? (
                      <button
                        type="button"
                        className="btn btn-ghost btn-icon"
                        disabled={busy}
                        title={t("Retry download")}
                        onClick={() => void retry(job)}
                      >
                        <Icon name="retry" size={15} />
                      </button>
                    ) : null}
                    {!job.torrent && job.status === "failed" &&
                    (job.message ?? "").startsWith("DegradedPagesError") &&
                    !(job.message ?? "").includes("taller than they are wide") ? (
                      <button
                        type="button"
                        className="btn btn-ghost btn-icon"
                        disabled={busy}
                        title={t("Download anyway: accept this chapter although it is shorter than the series' usual (unreadable strips can never be accepted)")}
                        onClick={() => void retry(job, true)}
                      >
                        <Icon name="download" size={15} />
                      </button>
                    ) : null}
                    {!job.torrent ? <button
                      type="button"
                      className="btn btn-ghost btn-icon"
                      disabled={busy}
                      title={t("Remove from history")}
                      onClick={() => void removeJob(job.id)}
                    >
                      <Icon name="trash" size={15} />
                    </button> : null}
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
          total={filteredHistory.length}
          onPageChange={setPage}
          itemLabel={t("history records")}
          ariaLabel={t("History pages")}
        />
        </>
      )}
    </div>
  );
}

function HistoryControls({
  filter,
  series,
  onFilterChange,
  stateFilter,
  stateCounts,
  onStateFilterChange,
  sort,
  sortDirection,
  onSortChange,
  onSortDirectionChange,
  total,
  filteredTotal,
}: {
  filter: string;
  series: readonly string[];
  onFilterChange: (value: string) => void;
  stateFilter: HistoryStateFilter;
  stateCounts: Record<Exclude<HistoryStateFilter, "all">, number>;
  onStateFilterChange: (value: HistoryStateFilter) => void;
  sort: HistorySort;
  sortDirection: SortDirection;
  onSortChange: (value: HistorySort) => void;
  onSortDirectionChange: () => void;
  total: number;
  filteredTotal: number;
}) {
  return (
    <div className="list-controls" aria-label={t("Filter and sort history")}>
      <div className="list-control-fields">
        <SeriesFilter
          value={filter}
          series={series}
          onChange={onFilterChange}
          placeholder={t("Filter series or pattern…")}
          ariaLabel={t("Filter history by series or pattern")}
        />
        <select
          className="input"
          value={stateFilter}
          aria-label={t("Filter history by state")}
          onChange={(event) => onStateFilterChange(event.target.value as HistoryStateFilter)}
        >
          <option value="all">{t("State: All ({count})", { count: total })}</option>
          {stateCounts.completed ? <option value="completed">{t("State: Imported ({count})", { count: stateCounts.completed })}</option> : null}
          {stateCounts.failed ? <option value="failed">{t("State: Failed ({count})", { count: stateCounts.failed })}</option> : null}
        </select>
        <select
          className="input"
          value={sort}
          aria-label={t("Sort history")}
          onChange={(event) => onSortChange(event.target.value as HistorySort)}
        >
          <option value="updated">{t("Sort: Updated")}</option>
          <option value="series">{t("Sort: Series title")}</option>
          <option value="item">{t("Sort: Item number")}</option>
          <option value="state">{t("Sort: State")}</option>
        </select>
        <button
          type="button"
          className="btn sort-direction"
          onClick={onSortDirectionChange}
          aria-label={sortDirection === "asc" ? t("Sort descending") : t("Sort ascending")}
          title={sortDirection === "asc" ? t("Currently ascending; click to reverse") : t("Currently descending; click to reverse")}
        >
          <Icon name={sortDirection === "asc" ? "sortAscending" : "sortDescending"} />
          <span>{sortDirection === "asc" ? t("Ascending") : t("Descending")}</span>
        </button>
      </div>
      <span className="muted small list-result-count">
        {filteredTotal === total
          ? tn(total, "{count} history record", "{count} history records")
          : t("{shown} of {total} history records", { shown: filteredTotal, total })}
      </span>
    </div>
  );
}
