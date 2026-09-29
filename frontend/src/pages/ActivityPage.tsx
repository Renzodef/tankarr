import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
import { SeriesFilter } from "../components/SeriesFilter";
import { LoadError } from "../components/LoadError";
import {
  Cover,
  EmptyState,
  Icon,
  Modal,
  Pagination,
  ProgressBar,
  StatusPill,
  chapterLabel,
  formatBytes,
  formatDate,
  jobStatusPill,
  providerChainLabel,
  seriesPath,
  useApp,
  Spinner,
} from "../components";
import type { TorrentDownload, Job, QueueSeriesSummary } from "../types";
import { locale, t, tn } from "../i18n";

const TERMINAL = new Set(["completed", "failed"]);
const TORRENT_TERMINAL = new Set(["imported", "failed", "review"]);
const ACTIVE_STATUSES = ["running", "downloading", "packaging", "importing", "queued"];
const PAGE_SIZE = 15;

type ActivityStateFilter = "all" | "working" | "waiting" | "ready";
type ActivitySort = "queue" | "title" | "remaining" | "state";
type SortDirection = "asc" | "desc";
type ActivitySection = QueueSeriesSummary & {
  torrents: TorrentDownload[];
  queueIndex: number;
};

const WORKING_TORRENT_STATUSES = new Set(["checking", "downloading", "importing"]);

function normalized(value: string): string {
  return value
    .normalize("NFKD")
    .replace(/\p{Diacritic}/gu, "")
    .toLocaleLowerCase();
}

function activityState(section: ActivitySection): Exclude<ActivityStateFilter, "all"> {
  if (section.torrents.some((job) => job.status === "completed")) return "ready";
  if (
    section.working > 0 ||
    (section.current !== null && section.current.status !== "queued") ||
    section.torrents.some((job) => WORKING_TORRENT_STATUSES.has(job.status))
  ) {
    return "working";
  }
  return "waiting";
}

function activityStateRank(section: ActivitySection): number {
  const state = activityState(section);
  if (state === "ready") return 1;
  if (state === "working") return 2;
  return 3;
}

export default function ActivityPage() {
  const { health, refreshJobs, notify } = useApp();
  const [torrentJobs, setTorrentJobs] = useState<TorrentDownload[]>([]);
  const [queueTotal, setQueueTotal] = useState<number | null>(null);
  const [series, setSeries] = useState<QueueSeriesSummary[]>([]);
  const expandedRef = useRef<Set<string>>(new Set());
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [rowsBySeries, setRowsBySeries] = useState<Record<string, Job[]>>({});
  const [torrentBusy, setTorrentBusy] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [confirmBulkRemove, setConfirmBulkRemove] = useState(false);
  const [bulkBusy, setBulkBusy] = useState(false);
  const [filter, setFilter] = useState("");
  const [stateFilter, setStateFilter] = useState<ActivityStateFilter>("all");
  const [sort, setSort] = useState<ActivitySort>("queue");
  const [sortDirection, setSortDirection] = useState<SortDirection>("asc");
  const [page, setPage] = useState(1);
  const [loadErrors, setLoadErrors] = useState<Record<string, string>>({});
  const [loading, setLoading] = useState(false);
  const [torrentsLoaded, setTorrentsLoaded] = useState(false);
  const refreshVersion = useRef(0);
  const mounted = useRef(true);

  const clearLoadError = useCallback((key: string) => {
    setLoadErrors((current) => {
      const next = { ...current };
      delete next[key];
      return next;
    });
  }, []);

  const loadSeriesRows = useCallback(async (id: string) => {
    try {
      const rows = await api.jobs(ACTIVE_STATUSES, 500, id);
      if (!mounted.current) return;
      setRowsBySeries((current) => ({ ...current, [id]: rows }));
      clearLoadError(`series:${id}`);
    } catch (caught) {
      if (mounted.current) setLoadErrors((current) => ({ ...current, [`series:${id}`]: String(caught) }));
    }
  }, [clearLoadError]);

  const refreshTorrents = useCallback(async () => {
    const version = ++refreshVersion.current;
    setLoading(true);
    const current = () => mounted.current && version === refreshVersion.current;
    const failed = (key: string, caught: unknown) => {
      if (current()) setLoadErrors((errors) => ({ ...errors, [key]: String(caught) }));
    };
    // Each source publishes independently: an unavailable torrent client must
    // not hide the direct-download queue (or the other way around).
    await Promise.all([
      api.torrents(undefined, undefined, 200).then((torrents) => {
        if (!current()) return;
        setTorrentJobs(torrents);
        setTorrentsLoaded(true);
        clearLoadError("torrents");
      }).catch((caught: unknown) => failed("torrents", caught)),
      api.jobsSummary().then((summary) => {
        if (!current()) return;
        setQueueTotal(summary.active);
        setSeries(summary.series);
        clearLoadError("queue");
      }).catch((caught: unknown) => failed("queue", caught)),
      ...[...expandedRef.current].map(loadSeriesRows),
    ]);
    if (current()) setLoading(false);
  }, [clearLoadError, loadSeriesRows]);

  useEffect(() => {
    mounted.current = true;
    let stopped = false;
    let polling = false;
    let timer: number | undefined;

    const poll = async () => {
      if (stopped || polling) return;
      polling = true;
      await refreshTorrents();
      polling = false;
      if (!stopped) {
        timer = window.setTimeout(poll, document.hidden ? 30000 : 5000);
      }
    };
    const visibilityChanged = () => {
      if (document.hidden || stopped || polling) return;
      if (timer !== undefined) window.clearTimeout(timer);
      timer = window.setTimeout(poll, 0);
    };

    void poll();
    document.addEventListener("visibilitychange", visibilityChanged);
    return () => {
      stopped = true;
      mounted.current = false;
      refreshVersion.current += 1;
      if (timer !== undefined) window.clearTimeout(timer);
      document.removeEventListener("visibilitychange", visibilityChanged);
    };
  }, [refreshTorrents]);

  const active = Object.values(rowsBySeries).flat().filter((job) => !TERMINAL.has(job.status));
  const toggleSeries = (id: string) => {
    setExpanded((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      expandedRef.current = next;
      return next;
    });
    if (!expanded.has(id)) {
      void loadSeriesRows(id);
    }
  };
  const activeTorrents = useMemo(
    () => torrentJobs.filter((job) => !TORRENT_TERMINAL.has(job.status)),
    [torrentJobs],
  );
  // One queue, whatever fetched it: releases are grouped by series and every
  // row reads the same, so a Prowlarr grab and a Suwayomi chapter differ only
  // in what they say, not in how they look.
  const sections = useMemo<ActivitySection[]>(() => {
    const torrentsBySeries = new Map<string, TorrentDownload[]>();
    for (const job of activeTorrents) {
      torrentsBySeries.set(job.manga_id, [...(torrentsBySeries.get(job.manga_id) ?? []), job]);
    }
    const summarizedIds = new Set(series.map((entry) => entry.manga_id));
    const combined: Omit<ActivitySection, "queueIndex">[] = [
      ...series.map((entry) => ({
        ...entry,
        torrents: torrentsBySeries.get(entry.manga_id) ?? [],
      })),
      // A release can be the only thing a series has in flight (a grabbed pack
      // with no chapter queue behind it): it still deserves its own section.
      ...[...torrentsBySeries.entries()]
        .filter(([id]) => !summarizedIds.has(id))
        .map(([id, jobs]) => ({
          manga_id: id,
          manga_title: jobs[0].manga_title,
          manga_cover_url: jobs[0].manga_cover_url,
          queued: 0,
          working: 0,
          current: null,
          torrents: jobs,
        })),
    ];
    return combined.map((entry, queueIndex) => ({ ...entry, queueIndex }));
  }, [activeTorrents, series]);

  const stateCounts = useMemo(() => {
    const counts: Record<Exclude<ActivityStateFilter, "all">, number> = {
      working: 0,
      waiting: 0,
      ready: 0,
    };
    sections.forEach((entry) => {
      counts[activityState(entry)] += 1;
    });
    return counts;
  }, [sections]);

  const seriesFilterOptions = useMemo(
    () => sections.map((entry) => entry.manga_title ?? entry.manga_id),
    [sections],
  );

  const filteredSections = useMemo(() => {
    const query = normalized(filter.trim());
    const direction = sortDirection === "asc" ? 1 : -1;
    return sections
      .filter((entry) => stateFilter === "all" || activityState(entry) === stateFilter)
      .filter((entry) => {
        if (!query) return true;
        const current = entry.current;
        return normalized(
          [
            entry.manga_title,
            entry.manga_id,
            current?.chapter_title,
            current?.chapter_number,
            current?.chapter_volume,
            current?.status,
            current?.message,
            current?.chapter_provider,
            current?.chapter_source_name,
            ...entry.torrents.flatMap((job) => [
              job.title,
              job.status,
              job.message,
              job.indexer,
              job.volume_hint,
              job.chapter_hint,
            ]),
            ...(rowsBySeries[entry.manga_id] ?? []).flatMap((job) => [
              job.chapter_title,
              job.chapter_number,
              job.chapter_volume,
              job.status,
              job.message,
              job.chapter_provider,
              job.chapter_source_name,
            ]),
          ]
            .filter(Boolean)
            .join(" "),
        ).includes(query);
      })
      .sort((left, right) => {
        let comparison = 0;
        if (sort === "title") {
          comparison = (left.manga_title ?? left.manga_id).localeCompare(
            right.manga_title ?? right.manga_id,
            undefined,
            { sensitivity: "base", numeric: true },
          );
        } else if (sort === "remaining") {
          comparison =
            left.queued + left.working + left.torrents.length -
            (right.queued + right.working + right.torrents.length);
        } else if (sort === "state") {
          comparison = activityStateRank(left) - activityStateRank(right);
        }
        if (comparison) return comparison * direction;
        return left.queueIndex - right.queueIndex;
      });
  }, [filter, rowsBySeries, sections, sort, sortDirection, stateFilter]);

  useEffect(() => {
    if (stateFilter !== "all" && stateCounts[stateFilter] === 0) setStateFilter("all");
  }, [stateCounts, stateFilter]);

  useEffect(() => {
    setPage(1);
  }, [filter, sort, sortDirection, stateFilter]);

  useEffect(() => {
    const lastPage = Math.max(1, Math.ceil(filteredSections.length / PAGE_SIZE));
    setPage((current) => Math.min(current, lastPage));
  }, [filteredSections.length]);
  const visibleSections = filteredSections.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE);
  const selectableKeys = [
    ...active.filter((job) => job.status !== "importing").map((job) => `job:${job.id}`),
    ...activeTorrents.filter((job) => job.status !== "importing").map((job) => `torrent:${job.id}`),
  ];
  const selectedKeys = selectableKeys.filter((key) => selected.has(key));
  const allSelected = selectableKeys.length > 0 && selectedKeys.length === selectableKeys.length;

  useEffect(() => {
    const available = new Set(selectableKeys);
    setSelected((current) => {
      const next = new Set([...current].filter((key) => available.has(key)));
      if (next.size === current.size && [...next].every((key) => current.has(key))) return current;
      return next;
    });
    // The stable joined key is intentional: job polling replaces the arrays every cycle.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectableKeys.join("|")]);

  const toggleSelected = (key: string, checked: boolean) => {
    setSelected((current) => {
      const next = new Set(current);
      if (checked) next.add(key);
      else next.delete(key);
      return next;
    });
  };

  const toggleAll = () => {
    setSelected(allSelected ? new Set() : new Set(selectableKeys));
  };

  const removeSelected = async () => {
    const directIds = selectedKeys
      .filter((key) => key.startsWith("job:"))
      .map((key) => Number(key.slice(4)));
    const torrentIds = selectedKeys
      .filter((key) => key.startsWith("torrent:"))
      .map((key) => Number(key.slice(8)));
    setBulkBusy(true);
    try {
      let removed = 0;
      const failures: string[] = [];
      if (directIds.length) {
        const result = await api.cancelJobs(directIds);
        removed += result.removed.length;
        failures.push(...result.failed.map((item) => `#${item.id}: ${item.error}`));
      }
      const torrentResults = await Promise.allSettled(
        torrentIds.map((id) => api.discardTorrent(id, true)),
      );
      torrentResults.forEach((result, index) => {
        if (result.status === "fulfilled") removed += 1;
        else failures.push(t("Torrent #{id}: {error}", { id: torrentIds[index], error: String(result.reason) }));
      });
      if (removed) notify("success", tn(removed, "Removed {count} selected download.", "Removed {count} selected downloads."));
      if (failures.length) notify("error", t("{count} could not be removed: {reason}", { count: failures.length, reason: failures[0] }));
      setSelected(new Set());
      setConfirmBulkRemove(false);
      await Promise.all([refreshJobs(), refreshTorrents()]);
    } catch (caught) {
      notify("error", String(caught));
    } finally {
      setBulkBusy(false);
    }
  };

  const remove = async (jobId: number) => {
    try {
      await api.deleteJob(jobId);
      notify("success", t("Removed from queue."));
      await refreshJobs();
    } catch (caught) {
      notify("error", String(caught));
    }
  };

  const torrentAction = async (action: () => Promise<unknown>, message: string) => {
    setTorrentBusy(true);
    try {
      const result = await action();
      if (result && typeof result === "object" && "status" in result && result.status === "failed") {
        notify("error", "message" in result ? String(result.message) : t("Release import failed."));
      } else notify("success", message);
      await refreshTorrents();
    } catch (caught) {
      notify("error", String(caught));
    } finally {
      setTorrentBusy(false);
    }
  };

  return (
    <div className="page">
      {Object.entries(loadErrors).map(([key, message]) => (
        <LoadError key={key} message={t("{scope} unavailable: {error}", { scope: key === "torrents" ? t("Torrent activity") : key === "queue" ? t("Download queue") : t("Series queue"), error: message })} retryLabel={t("Retry activity")} retry={() => void refreshTorrents()} loading={loading} hasData={queueTotal !== null || torrentsLoaded} />
      ))}
      <div className="toolbar activity-toolbar">
        <div>
          <h1 className="page-title">{t("Activity")}</h1>
          <nav className="page-tabs" aria-label={t("Activity sections")}>
            <a className="page-tab active" href="#/activity" aria-current="page">
              {t("Queue")}
            </a>
            <a className="page-tab" href="#/history">
              {t("History")}
            </a>
          </nav>
        </div>
        <div className="toolbar-group activity-summary">
          <span className="muted" title={queueTotal !== null && queueTotal > active.length ? t("Showing the first {count}", { count: active.length }) : undefined}>
            {queueTotal === null && !torrentsLoaded ? t("Queue status unavailable") : t("{count} in queue", { count: (queueTotal ?? active.length) + activeTorrents.length })}
          </span>
          {selectableKeys.length ? (
            <>
              <button type="button" className="btn btn-ghost" onClick={toggleAll}>
                {allSelected ? t("Clear selection") : t("Select all")}
              </button>
              <button
                type="button"
                className="btn btn-danger"
                disabled={selectedKeys.length === 0}
                onClick={() => setConfirmBulkRemove(true)}
              >
                <Icon name="trash" /> {t("Remove selected ({count})", { count: selectedKeys.length })}
              </button>
            </>
          ) : null}
        </div>
      </div>
      {health?.download_worker && !health.download_worker.running ? (
        <div className="banner banner-danger">
          <Icon name="alert" /> {t("Download worker stopped")}
          {health.download_worker.error ? `: ${health.download_worker.error}` : "."}
        </div>
      ) : null}
      {sections.length ? (
        <div className="list-controls" aria-label={t("Filter and sort activity queue")}>
          <div className="list-control-fields">
            <SeriesFilter
              value={filter}
              series={seriesFilterOptions}
              onChange={setFilter}
              placeholder={t("Filter series or pattern…")}
              ariaLabel={t("Filter activity by series or pattern")}
            />
            <select
              className="input"
              value={stateFilter}
              aria-label={t("Filter activity by state")}
              onChange={(event) => setStateFilter(event.target.value as ActivityStateFilter)}
            >
              <option value="all">{t("State: All ({count})", { count: sections.length })}</option>
              {stateCounts.working ? <option value="working">{t("State: Working ({count})", { count: stateCounts.working })}</option> : null}
              {stateCounts.waiting ? <option value="waiting">{t("State: Waiting ({count})", { count: stateCounts.waiting })}</option> : null}
              {stateCounts.ready ? <option value="ready">{t("State: Ready to import ({count})", { count: stateCounts.ready })}</option> : null}
            </select>
            <select
              className="input"
              value={sort}
              aria-label={t("Sort activity queue")}
              onChange={(event) => {
                const nextSort = event.target.value as ActivitySort;
                setSort(nextSort);
                setSortDirection(nextSort === "remaining" ? "desc" : "asc");
              }}
            >
              <option value="queue">{t("Sort: Queue priority")}</option>
              <option value="title">{t("Sort: Series title")}</option>
              <option value="remaining">{t("Sort: Remaining items")}</option>
              <option value="state">{t("Sort: State")}</option>
            </select>
            <button
              type="button"
              className="btn sort-direction"
              disabled={sort === "queue"}
              onClick={() => setSortDirection((current) => current === "asc" ? "desc" : "asc")}
              aria-label={
                sort === "queue"
                  ? t("Queue priority order is controlled by the scheduler")
                  : sortDirection === "asc" ? t("Sort descending") : t("Sort ascending")
              }
              title={
                sort === "queue"
                  ? t("Matches the scheduler's live queue priority")
                  : sortDirection === "asc" ? t("Currently ascending; click to reverse") : t("Currently descending; click to reverse")
              }
            >
              <Icon name={sortDirection === "asc" ? "sortAscending" : "sortDescending"} />
              <span>{sort === "queue" ? t("Priority order") : sortDirection === "asc" ? t("Ascending") : t("Descending")}</span>
            </button>
          </div>
          <span className="muted small list-result-count">
            {filteredSections.length === sections.length
              ? tn(sections.length, "{count} series", "{count} series")
              : t("{shown} of {total} series", { shown: filteredSections.length, total: sections.length })}
          </span>
        </div>
      ) : null}
      {sections.length === 0 && (queueTotal === null || !torrentsLoaded) ? (
        Object.keys(loadErrors).length ? <p role="status">{t("Some queue data is unavailable. Retry above to check whether downloads are waiting.")}</p> : <Spinner />
      ) : sections.length === 0 ? (
        <EmptyState icon="activity" title={t("The queue is empty")} hint={t("Monitored releases are checked every cycle.")} />
      ) : filteredSections.length === 0 ? (
        <EmptyState icon="search" title={t("No queued series match these filters")} />
      ) : (
        <>
        {visibleSections.map((entry) => {
          const isOpen = expanded.has(entry.manga_id);
          const rows = (rowsBySeries[entry.manga_id] ?? []).filter((job) => !TERMINAL.has(job.status));
          // The header reads the same whatever is in flight: the release or
          // chapter currently working, however it was fetched.
          const busyRelease =
            entry.torrents.find((job) => job.status === "importing" || job.status === "downloading") ??
            entry.torrents[0];
          const headline = entry.current
            ? {
                label: chapterLabel(entry.current.chapter_volume, entry.current.chapter_number),
                pill: jobStatusPill(entry.current.status),
                progress: entry.current.progress,
                message:
                  entry.current.status === "queued" && entry.current.next_retry_at
                    ? `${t("Retry {count}", { count: entry.current.retry_count ?? 0 })} · ${new Date(entry.current.next_retry_at * 1000).toLocaleTimeString(locale())} · ${entry.current.failure_code ?? t("Source unavailable")}`
                    : "",
              }
            : busyRelease
              ? {
                  label: busyRelease.volume_hint
                    ? t("Volume {number}", { number: busyRelease.volume_hint })
                    : busyRelease.chapter_hint
                      ? t("Chapter {number}", { number: busyRelease.chapter_hint })
                      : t("Release"),
                  pill: torrentPill(busyRelease.status),
                  progress: busyRelease.progress,
                  message: "",
                }
              : null;
          const total = entry.queued + entry.working + entry.torrents.length;
          return (
            <section key={entry.manga_id} className="queue-series-section">
              <header className="queue-series-header">
                <button
                  type="button"
                  className="volume-toggle queue-series-toggle"
                  title={isOpen ? t("Hide items") : t("Show items")}
                  onClick={() => toggleSeries(entry.manga_id)}
                >
                  <Icon name={isOpen ? "chevronDown" : "chevronRight"} />
                </button>
                <Cover
                  url={entry.manga_cover_url}
                  title={entry.manga_title ?? "?"}
                  className="queue-series-cover"
                />
                <a className="table-link queue-series-title" href={seriesPath(entry.manga_id)}>
                  {entry.manga_title ?? entry.manga_id}
                </a>
                {headline ? (
                  <span className={`queue-current${headline.message ? " has-message" : ""}`}>
                    <span className="queue-current-label">{headline.label}</span>
                    <StatusPill kind={headline.pill.kind}>{headline.pill.label}</StatusPill>
                    <ProgressBar value={headline.progress} />
                    {headline.message ? <span className="queue-message">{headline.message}</span> : null}
                  </span>
                ) : null}
                <span className="muted queue-series-count">
                  {[
                    entry.working ? t("{count} active", { count: entry.working }) : null,
                    entry.queued ? t("{count} queued", { count: entry.queued }) : null,
                    entry.torrents.length
                      ? tn(entry.torrents.length, "{count} release", "{count} releases")
                      : null,
                  ]
                    .filter(Boolean)
                    .join(" · ") || tn(total, "{count} item", "{count} items")}
                </span>
              </header>
              {isOpen ? (
                entry.torrents.length || rows.length ? (
                  <div className="data-table-frame queue-table-frame">
                  <table className="table responsive-list-table queue-table">
                    <tbody>
                      {entry.torrents.map((job) => {
                        const rowPill = torrentPill(job.status);
                        const books = releaseBooks(job);
                        const importedBooks = books.filter((book) => book.imported).length;
                        return (
                          <tr key={`torrent-${job.id}`} className="queue-item-row">
                            <td className="col-select" data-label={t("Select")}>
                              <input
                                type="checkbox"
                                aria-label={t("Select release {id}", { id: job.id })}
                                disabled={job.status === "importing"}
                                checked={selected.has(`torrent:${job.id}`)}
                                onChange={(event) => toggleSelected(`torrent:${job.id}`, event.target.checked)}
                                title={job.status === "importing" ? t("Atomic library import in progress") : undefined}
                              />
                            </td>
                            <td className="queue-item-identity" data-label={t("Item")}>
                              {job.source_url ? (
                                <a
                                  className="table-link queue-item-heading"
                                  href={job.source_url}
                                  target="_blank"
                                  rel="noreferrer"
                                >
                                  {job.volume_hint
                                    ? t("Volume {number}", { number: job.volume_hint })
                                    : job.chapter_hint
                                      ? t("Chapter {number}", { number: job.chapter_hint })
                                      : t("Release")}
                                </a>
                              ) : (
                                <span className="queue-item-heading">
                                  {job.volume_hint ? t("Volume {number}", { number: job.volume_hint }) : t("Release")}
                                </span>
                              )}
                              <div className="muted small queue-item-detail" title={job.title}>
                                {job.title} · {formatBytes(job.size_bytes)}
                              </div>
                              {books.length ? (
                                <details className="queue-coverage">
                                  <summary>
                                    {t("{imported} / {total} volumes imported", { imported: importedBooks, total: books.length })}
                                  </summary>
                                  <div className="queue-coverage-items">
                                    {books.map((book) => (
                                      <span
                                        key={book.label}
                                        className={book.imported ? "imported" : "waiting"}
                                      >
                                        {book.label}
                                      </span>
                                    ))}
                                  </div>
                                </details>
                              ) : null}
                            </td>
                            <td className="col-provider queue-source-cell" data-label={t("Source")}>
                              <span className="queue-source-label">
                                <strong>
                                  {job.indexer ||
                                    (job.protocol === "usenet" ? "Usenet" : job.protocol === "http" ? t("Direct") : "Torrent")}
                                </strong>
                                <span>
                                  {job.source === "prowlarr"
                                    ? t("via Prowlarr")
                                    : job.protocol === "http"
                                      ? t("direct download")
                                      : t("download source")}
                                </span>
                              </span>
                            </td>
                            <td
                              className="col-progress queue-status-cell"
                              data-label={t("Status")}
                              title={job.message || undefined}
                            >
                              <StatusPill kind={rowPill.kind}>{rowPill.label}</StatusPill>
                              <ProgressBar value={job.progress} />
                            </td>
                            <td className="col-actions" data-label={t("Actions")}>
                              <div className="queue-row-actions">
                                {job.client_url ? (
                                  <a
                                    className="btn btn-ghost btn-icon"
                                    title={t("Open in {client}", { client: job.protocol === "usenet" ? "SABnzbd" : "qBittorrent" })}
                                    href={job.client_url}
                                    target="_blank"
                                    rel="noreferrer"
                                  >
                                    <Icon name="external" size={15} />
                                  </a>
                                ) : job.protocol === "http" && job.source_url ? (
                                  <a
                                    className="btn btn-ghost btn-icon"
                                    title={t("Open on archive.org")}
                                    href={job.source_url}
                                    target="_blank"
                                    rel="noreferrer"
                                  >
                                    <Icon name="external" size={15} />
                                  </a>
                                ) : null}
                                {job.status === "completed" ? (
                                  <button
                                    type="button"
                                    className="btn btn-primary btn-icon"
                                    title={t("Import now")}
                                    disabled={torrentBusy}
                                    onClick={() => void torrentAction(() => api.importTorrent(job.id), t("Release imported."))}
                                  >
                                    <Icon name="library" size={15} />
                                  </button>
                                ) : null}
                                <button
                                  type="button"
                                  className="btn btn-ghost btn-icon"
                                  title={t("Remove release and staging files")}
                                  disabled={torrentBusy || job.status === "importing"}
                                  onClick={() => {
                                    if (window.confirm(t("Remove this release and its download-client staging files?")))
                                      void torrentAction(() => api.discardTorrent(job.id), t("Release removed."));
                                  }}
                                >
                                  <Icon name="trash" size={15} />
                                </button>
                              </div>
                            </td>
                          </tr>
                        );
                      })}
                      {rows.map((job) => {
                        const rowPill = jobStatusPill(job.status);
                        return (
                          <tr key={`job-${job.id}`} className="queue-item-row">
                            <td className="col-select" data-label={t("Select")}>
                              <input
                                type="checkbox"
                                aria-label={t("Select chapter {id}", { id: job.id })}
                                disabled={job.status === "importing"}
                                checked={selected.has(`job:${job.id}`)}
                                onChange={(event) => toggleSelected(`job:${job.id}`, event.target.checked)}
                              />
                            </td>
                            <td className="queue-item-identity" data-label={t("Item")}>
                              <span className="queue-item-heading">
                                {chapterLabel(job.chapter_volume, job.chapter_number)}
                              </span>
                              {job.chapter_title ? (
                                <span className="muted small queue-item-detail">{job.chapter_title}</span>
                              ) : null}
                            </td>
                            <td className="col-provider queue-source-cell" data-label={t("Source")}>
                              <span className="queue-source-label">
                                <strong>
                                  {providerChainLabel(
                                    job.chapter_provider ?? "unknown",
                                    job.chapter_source_name,
                                  )}
                                </strong>
                                <span>{t("direct source")}</span>
                              </span>
                            </td>
                            <td
                              className="col-progress queue-status-cell"
                              data-label={t("Status")}
                              title={job.status === "failed" ? job.message : undefined}
                            >
                              <StatusPill kind={rowPill.kind}>{rowPill.label}</StatusPill>
                              {job.status !== "queued" ? <ProgressBar value={job.progress} /> : null}
                            </td>
                            <td className="col-actions" data-label={t("Actions")}>
                              <div className="queue-row-actions">
                                {job.status !== "importing" ? (
                                  <button
                                    type="button"
                                    className="btn btn-ghost btn-icon"
                                    title={t("Remove from queue")}
                                    onClick={() => void remove(job.id)}
                                  >
                                    <Icon name="close" size={15} />
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
                ) : (
                  <Spinner />
                )
              ) : null}
            </section>
          );
        })}
        <Pagination
          page={page}
          pageSize={PAGE_SIZE}
          total={filteredSections.length}
          onPageChange={setPage}
          itemLabel={t("series")}
          ariaLabel={t("Queue pages")}
        />
        </>
      )}
      {confirmBulkRemove ? (
        <Modal
          title={t("Remove selected downloads")}
          onClose={() => !bulkBusy && setConfirmBulkRemove(false)}
          footer={
            <>
              <button type="button" className="btn" disabled={bulkBusy} onClick={() => setConfirmBulkRemove(false)}>
                {t("Cancel")}
              </button>
              <button type="button" className="btn btn-danger" disabled={bulkBusy} onClick={() => void removeSelected()}>
                {bulkBusy ? (
                  <>
                    <Spinner /> {t("Removing…")}
                  </>
                ) : (
                  <>
                    <Icon name="trash" /> {tn(selectedKeys.length, "Remove {count} download", "Remove {count} downloads")}
                  </>
                )}
              </button>
            </>
          }
        >
          <p>
            {t("Queued chapter jobs will be removed. Downloads already transferring will be stopped and their temporary files cleaned. Selected torrents will also be removed from qBittorrent with their staging data.")}
          </p>
          <p className="muted small">
            {t("Files already imported into the Comics library are never deleted by this action. Jobs currently committing an atomic library import cannot be selected.")}
          </p>
        </Modal>
      ) : null}
    </div>
  );
}

// A pack is many books under one release. Reading the volumes it covers and
// the paths already published turns "Volume 1-10" into ten answerable rows.
function releaseBooks(job: TorrentDownload): { label: string; imported: boolean }[] {
  const importedNumbers = new Set(
    job.imported_paths
      .map((path) => /\bv(\d+)/i.exec(path)?.[1])
      .filter((value): value is string => Boolean(value))
      .map((value) => String(Number(value))),
  );
  const span = /^(\d+)\s*-\s*(\d+)$/.exec(job.volume_hint ?? "");
  if (span) {
    const first = Number(span[1]);
    const last = Number(span[2]);
    if (Number.isFinite(first) && Number.isFinite(last) && last >= first && last - first < 200) {
      return Array.from({ length: last - first + 1 }, (_unused, offset) => {
        const number = first + offset;
        return {
          label: t("Volume {number}", { number }),
          imported: importedNumbers.has(String(number)),
        };
      });
    }
  }
  return [];
}

function torrentPill(status: string) {
  if (status === "completed") return { kind: "info", label: t("Downloaded") };
  if (status === "checking") return { kind: "info", label: t("Checking") };
  return jobStatusPill(status);
}
