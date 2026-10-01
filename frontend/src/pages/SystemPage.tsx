import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import { downloadReport } from "../downloadReport";
import MaintenanceNotice, { maintenanceErrors } from "../components/MaintenanceNotice";
import SystemIntegrationHealth from "../components/SystemIntegrationHealth";
import type { MaintenanceStatus } from "../operationTypes";
import { Icon, Spinner, StatusPill, formatBytes, formatDate, useApp, Cover, seriesPath } from "../components";
import type { ProviderProbe, SystemStatus, SystemLogs, SystemTask, MatchReview, LibraryOrphans } from "../types";
import { t, tn } from "../i18n";

export default function SystemPage() {
  const { health, notify, refreshHealth } = useApp();
  const [maintenance, setMaintenance] = useState<MaintenanceStatus | null>(null);
  const [maintenanceError, setMaintenanceError] = useState<string | null>(null);
  const [diagnosticsBusy, setDiagnosticsBusy] = useState(false);
  const [status, setStatus] = useState<SystemStatus | null>(null);
  const [reviews, setReviews] = useState<MatchReview[]>([]);
  const [orphans, setOrphans] = useState<LibraryOrphans | null>(null);
  const [orphanBusy, setOrphanBusy] = useState<string | null>(null);
  const [reviewBusy, setReviewBusy] = useState<number | null>(null);
  const loadReviews = async () => {
    try {
      setReviews(await api.reviews());
    } catch {
      // the panel simply stays empty
    }
  };
  const decide = async (id: number, accept: boolean) => {
    setReviewBusy(id);
    try {
      if (accept) await api.acceptReview(id);
      else await api.rejectReview(id);
      await loadReviews();
    } catch (caught) {
      notify("error", String(caught));
    } finally {
      setReviewBusy(null);
    }
  };
  const [probes, setProbes] = useState<ProviderProbe[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [tasks, setTasks] = useState<SystemTask[] | null>(null);
  const [taskBusy, setTaskBusy] = useState<string | null>(null);
  const loadTasks = async () => {
    try {
      setTasks((await api.systemTasks()).tasks);
    } catch {
      setTasks(null);
    }
  };
  const runTask = async (id: string) => {
    setTaskBusy(id);
    try {
      const outcome = await api.runSystemTask(id);
      if (outcome.ok) notify("success", t("{task} finished.", { task: outcome.task.name }));
      else notify("error", t("{task} failed: {error}", { task: outcome.task.name, error: outcome.error ?? t("unknown error") }));
      await loadTasks();
    } catch (caught) {
      notify("error", String(caught));
    } finally {
      setTaskBusy(null);
    }
  };
  const [logs, setLogs] = useState<SystemLogs | null>(null);
  const [logTail, setLogTail] = useState<string[]>([]);
  const [logFilter, setLogFilter] = useState("");
  const [logsBusy, setLogsBusy] = useState(false);

  const loadLogTail = async (level: string) => {
    setLogsBusy(true);
    try {
      const [listing, tail] = await Promise.all([api.systemLogs(), api.systemLogTail(200, level || undefined)]);
      setLogs(listing);
      setLogTail(tail.lines);
    } catch (caught) {
      notify("error", String(caught));
    } finally {
      setLogsBusy(false);
    }
  };

  const loadOrphans = async () => {
    try {
      setOrphans(await api.libraryOrphans());
    } catch {
      setOrphans(null);
    }
  };

  const removeOrphans = async (folders?: string[]) => {
    setOrphanBusy(folders?.[0] ?? "*");
    try {
      const result = await api.deleteLibraryOrphans(folders);
      notify("success", tn(result.deleted, "Deleted {count} untracked file.", "Deleted {count} untracked files."));
      await loadOrphans();
      await load();
    } catch (caught) {
      notify("error", String(caught));
    } finally {
      setOrphanBusy(null);
    }
  };

  const load = useCallback(async () => {
    try {
      void loadOrphans();
      void loadLogTail(logFilter);
      void loadTasks();
      void api.maintenanceStatus().then((result) => { setMaintenance(result); setMaintenanceError(null); }).catch((caught: unknown) => setMaintenanceError(String(caught)));
      setStatus(await api.systemStatus());
      await loadReviews();
    } catch (caught) {
      notify("error", String(caught));
    }
  }, [notify]);

  useEffect(() => {
    void load();
    api
      .providersStatus()
      .then(setProbes)
      .catch(() => setProbes([]));
  }, [load]);

  const act = async (action: () => Promise<unknown>, success: string) => {
    setBusy(true);
    try {
      await action();
      notify("success", success);
      await Promise.all([load(), refreshHealth()]);
    } catch (caught) {
      notify("error", String(caught));
    } finally {
      setBusy(false);
    }
  };

  if (!status) return <Spinner />;

  const organization = health?.library_organization;
  const recovery = health?.deletion_recovery;
  const alignment = status.library_alignment ?? health?.library_alignment;
  const maintenanceNeedsAttention = maintenance && maintenanceErrors(maintenance).length > 0;

  return (
    <div className="page">
      <div className="toolbar">
        <h1 className="page-title">{t("System")}</h1>
        <div className="toolbar-group">
          <button
            type="button"
            className="btn"
            disabled={busy}
            onClick={() => void act(() => api.runMonitor(), t("Monitor cycle completed."))}
          >
            <Icon name="refresh" /> {t("Run Monitor")}
          </button>
          <button
            type="button"
            className="btn"
            disabled={busy}
            onClick={() => void act(() => api.organizeLibrary(), t("Library organization completed."))}
          >
            <Icon name="library" /> {t("Organize Library")}
          </button>
          <button
            type="button"
            className="btn"
            disabled={busy}
            onClick={() =>
              void act(
                () => api.refreshAllMetadata(true),
                t("Metadata enrichment started in the background."),
              )
            }
          >
            <Icon name="refresh" /> {t("Enrich Metadata")}
          </button>
        </div>
      </div>

      <div className="system-grid">
        {status.alerts?.length || maintenanceNeedsAttention || maintenanceError ? (
          <section className="panel system-alerts">
            <h2>{t("Needs attention")}</h2>
            <ul className="alert-list">
              {status.alerts?.map((alert) => (
                <li key={alert.key} className={`alert-row alert-${alert.level}`}>
                  <StatusPill kind={alert.level === "danger" ? "danger" : alert.level === "warn" ? "warn" : "info"}>
                    {alert.level === "danger" ? t("Error") : alert.level === "warn" ? t("Warning") : t("Note")}
                  </StatusPill>
                  <div>
                    <strong>{alert.href ? <a href={alert.href}>{alert.title}</a> : alert.title}</strong>
                    <div className="muted small">{alert.detail}</div>
                  </div>
                  <button
                    type="button"
                    className="btn btn-ghost btn-icon alert-dismiss"
                    title={t("Acknowledge: it comes back if this changes")}
                    onClick={() => {
                      void api
                        .dismissAlert(alert.key, alert.signature ?? alert.title)
                        .then(load);
                    }}
                  >
                    <Icon name="close" size={14} />
                  </button>
                </li>
              ))}
            </ul>
            {maintenanceError ? <p className="banner banner-danger" role="alert">{t("Scheduled maintenance status unavailable: {error}", { error: maintenanceError })}</p> : null}
            {maintenance ? <MaintenanceNotice status={maintenance} /> : null}
          </section>
        ) : null}
        <section className="panel system-alerts">
          <h2>{t("Health")}</h2>
          {!status.alerts?.length && !maintenanceNeedsAttention && !maintenanceError ? <p className="muted small"><StatusPill kind="success">{t("All clear")}</StatusPill> {t("Downloads, sources, storage and the Suwayomi runtime report no problems.")}</p> : <p className="muted small">{t("Review the items in Needs attention.")}</p>}
          <a href="#/setup" className="btn">{t("Re-run setup checks")}</a>
        </section>
        {orphans && orphans.count > 0 ? (
          <section className="panel">
            <h2>{t("Untracked library files")}</h2>
            <p className="muted small">
              {tn(orphans.count, "{count} file on disk that no series in Tankarr claims. The reader still serves it, so Tankarr and the reader disagree until it is removed. It is usually left by a series deleted without its files. Nothing is deleted automatically.", "{count} files on disk that no series in Tankarr claims. The reader still serves them, so Tankarr and the reader disagree until they are removed. They are usually left by a series deleted without its files. Nothing is deleted automatically.")}
            </p>
            <ul className="alert-list">
              {orphans.folders.map((folder) => (
                <li key={folder.folder} className="alert-row">
                  <div>
                    <strong>{folder.folder}</strong>
                    <div className="muted small">
                      {tn(folder.files, "{count} file", "{count} files")} · {formatBytes(folder.bytes)} ·{" "}
                      {folder.sample.join(", ")}
                      {folder.files > folder.sample.length ? " …" : ""}
                    </div>
                  </div>
                  <button
                    type="button"
                    className="btn btn-danger"
                    disabled={orphanBusy !== null}
                    onClick={() => void removeOrphans([folder.folder])}
                  >
                    <Icon name="trash" /> {t("Delete")} {folder.files}
                  </button>
                </li>
              ))}
            </ul>
            {orphans.folders.length > 1 ? (
              <button
                type="button"
                className="btn btn-danger"
                disabled={orphanBusy !== null}
                onClick={() => void removeOrphans()}
              >
                <Icon name="trash" /> {t("Delete all {count} untracked files", { count: orphans.count })}
              </button>
            ) : null}
          </section>
        ) : null}
        {reviews.length ? (
          <section className="panel">
            <h2>{t("To confirm")}</h2>
            <p className="muted small">{t("Matches that looked right but did not pass the automatic rules. Accept maps the source (or grabs the release); reject hides it for good.")}</p>
            <ul className="alert-list">
              {reviews.map((review) => (
                <li key={review.id} className="alert-row">
                  <Cover url={review.manga_cover_url} title={review.manga_title ?? ""} className="table-cover" />
                  <div style={{ flex: 1 }}>
                    <strong>
                      <a href={seriesPath(review.manga_id)}>{review.manga_title}</a> ← {review.title}
                    </strong>
                    <div className="muted small">
                      {[
                        `${review.source_name ?? review.provider} · ${review.kind === "source" ? "source" : "release"}`,
                        `${Math.round(review.confidence * 100)}%`,
                        review.reason,
                      ].join(" · ")}
                    </div>
                    {review.kind === "release" && review.payload ? (
                      // Two copies of one pack look identical by title alone;
                      // size, seeders and volumes are what tell them apart.
                      <div className="muted small">
                        {[
                          review.payload.volume ? t("Volumes {volume}", { volume: review.payload.volume }) : null,
                          review.payload.chapter ? t("Chapter {number}", { number: review.payload.chapter }) : null,
                          review.payload.size,
                          review.payload.protocol === "usenet"
                            ? "Usenet"
                            : typeof review.payload.seeders === "number"
                              ? tn(review.payload.seeders, "{count} seeder", "{count} seeders")
                              : null,
                        ]
                          .filter(Boolean)
                          .join(" · ")}
                      </div>
                    ) : null}
                  </div>
                  <div className="toolbar-group">
                    {review.source_url ? (
                      <a
                        className="btn btn-small btn-ghost"
                        href={review.source_url}
                        target="_blank"
                        rel="noreferrer"
                        title={review.source_url}
                      >
                        <Icon name="external" size={14} /> {t("Review")}
                      </a>
                    ) : null}
                    <button type="button" className="btn btn-small btn-primary" disabled={reviewBusy === review.id} onClick={() => void decide(review.id, true)}>
                      {t("Accept")}
                    </button>
                    <button type="button" className="btn btn-small btn-ghost" disabled={reviewBusy === review.id} onClick={() => void decide(review.id, false)}>
                      {t("Reject")}
                    </button>
                  </div>
                </li>
              ))}
            </ul>
          </section>
        ) : null}
        <section className="panel">
          <h2>{t("About")}</h2>
          <dl className="kv">
            <dt>{t("Version")}</dt>
            <dd>
              {status.version}
              {status.update?.update_available && status.update.latest ? (
                <>
                  {" · "}
                  {status.update.url ? (
                    <a href={status.update.url} target="_blank" rel="noreferrer">
                      {t("{version} available", { version: status.update.latest })}
                    </a>
                  ) : (
                    <span>{t("{version} available", { version: status.update.latest })}</span>
                  )}
                </>
              ) : status.update?.checked_at && !status.update.error ? (
                <span className="muted small"> · {t("up to date")}</span>
              ) : null}
            </dd>
            <dt>{t("Python")}</dt>
            <dd>{status.python}</dd>
            <dt>{t("Platform")}</dt>
            <dd>{status.platform}</dd>
            <dt>{t("Started")}</dt>
            <dd>{formatDate(status.started_at)}</dd>
            <dt>{t("Database")}</dt>
            <dd>
              {status.database_path} ({formatBytes(status.database_size)})
            </dd>
          </dl>
          <button type="button" className="btn" disabled={diagnosticsBusy} onClick={() => {
            setDiagnosticsBusy(true);
            void downloadReport("/api/system/diagnostics/export", "tankarr-diagnostics.json").catch((caught: unknown) => notify("error", String(caught))).finally(() => setDiagnosticsBusy(false));
          }}>{t("Download diagnostics")}</button>
        </section>

        <section className="panel">
          <h2>{t("Scheduled tasks")}</h2>
          <p className="muted small">{t("What runs on its own, when it last ran and when it is due next. Run now starts one pass without waiting; a task that is already running is left alone.")}</p>
          <div className="data-table-frame">
            <table className="table responsive-list-table">
              <thead>
                <tr>
                  <th>{t("Task")}</th>
                  <th>{t("Schedule")}</th>
                  <th>{t("Last run")}</th>
                  <th>{t("Next run")}</th>
                  <th aria-label={t("Actions")} />
                </tr>
              </thead>
              <tbody>
                {(tasks ?? []).map((task) => (
                  <tr key={task.id}>
                    <td data-label={t("Task")}>
                      <strong>{task.name}</strong>
                      <div className="muted small">{task.description}</div>
                    </td>
                    <td data-label={t("Schedule")}>{task.enabled ? task.schedule : t("Disabled")}</td>
                    <td data-label={t("Last run")}>
                      {task.last_run_at ? formatDate(task.last_run_at) : t("Never")}
                      {task.last_error ? <div className="warn-text small">{task.last_error}</div> : null}
                    </td>
                    <td data-label={t("Next run")}>
                      {task.running ? <StatusPill kind="muted">{t("Running…")}</StatusPill> : task.next_run_at ? formatDate(task.next_run_at) : "—"}
                    </td>
                    <td data-label="">
                      {task.can_run ? (
                        <button type="button" className="btn btn-small" disabled={task.running || taskBusy === task.id} onClick={() => void runTask(task.id)}>
                          <Icon name="retry" size={14} /> {t("Run now")}
                        </button>
                      ) : null}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>

        <section className="panel">
          <h2>{t("Logs")}</h2>
          <p className="muted small">
            {t("Level {level}, changed under Settings → General (advanced).", { level: logs?.level ?? "…" })}
            {logs?.directory ? <> {t("The file lives in {directory} and rotates at 5 MB; docker logs shows the same lines.", { directory: logs.directory })}</> : null}
          </p>
          <div className="toolbar-group" style={{ marginBottom: 8, flexWrap: "wrap" }}>
            <select
              className="input"
              aria-label={t("Minimum log level")}
              value={logFilter}
              onChange={(event) => {
                setLogFilter(event.target.value);
                void loadLogTail(event.target.value);
              }}
            >
              <option value="">{t("Everything")}</option>
              <option value="info">{t("Info and above")}</option>
              <option value="warning">{t("Warnings and errors")}</option>
              <option value="error">{t("Errors only")}</option>
            </select>
            <button type="button" className="btn btn-small" disabled={logsBusy} onClick={() => void loadLogTail(logFilter)}>
              <Icon name="refresh" size={14} /> {t("Refresh")}
            </button>
            {logs?.files.map((file) => (
              <button
                key={file.name}
                type="button"
                className="btn btn-small"
                onClick={() => void downloadReport(`/api/system/logs/${encodeURIComponent(file.name)}/download`, file.name).catch((caught: unknown) => notify("error", String(caught)))}
              >
                <Icon name="download" size={14} /> {file.name} ({formatBytes(file.size)})
              </button>
            ))}
          </div>
          <pre className="log-tail" aria-label={t("Last log lines")}>{logTail.length ? logTail.join("\n") : t("No log lines yet.")}</pre>
        </section>

        <section className="panel">
          <h2>{t("Library")}</h2>
          <dl className="kv">
            <dt>{t("Status")}</dt>
            <dd>
              {status.library.available ? (
                <StatusPill kind="success">{t("Available")}</StatusPill>
              ) : (
                <StatusPill kind="danger">{status.library.reason}</StatusPill>
              )}
            </dd>
            <dt>{t("Series")}</dt>
            <dd>
              {t("{total} ({monitored} monitored)", { total: status.totals.series, monitored: status.totals.monitored })}
            </dd>
            <dt>{t("Chapters")}</dt>
            <dd>
              {t("{downloaded} downloaded of {known} known", { downloaded: status.totals.downloaded, known: status.totals.chapters })}
            </dd>
            <dt>{t("Naming")}</dt>
            <dd className="mono small">{organization?.naming_format ?? "—"}</dd>
            <dt>{t("Organization")}</dt>
            <dd>
              {organization ? (
                organization.organization_blocked ? (
                  <StatusPill kind="danger">{t("Blocked")}</StatusPill>
                ) : (
                  <StatusPill kind="success">
                    {t("Clean · {moved} moved, {unchanged} unchanged", { moved: organization.moved, unchanged: organization.unchanged })}
                  </StatusPill>
                )
              ) : (
                "—"
              )}
            </dd>
            <dt>{t("Deletion recovery")}</dt>
            <dd>
              {recovery ? (
                recovery.recovery_blocked ? (
                  <StatusPill kind="danger">{t("Blocked: {reason}", { reason: recovery.warnings[0] })}</StatusPill>
                ) : (
                  <StatusPill kind="success">{t("Clean")}</StatusPill>
                )
              ) : (
                "—"
              )}
            </dd>
          </dl>
        </section>

        <section className="panel">
          <h2>{t("Storage")}</h2>
          <table className="table">
            <thead>
              <tr>
                <th>{t("Location")}</th>
                <th>{t("Path")}</th>
                <th>{t("Free")}</th>
                <th>{t("Total")}</th>
              </tr>
            </thead>
            <tbody>
              <tr>
                <td>{t("Config")}</td>
                <td className="mono small">{status.storage.data.path}</td>
                <td>{formatBytes(status.storage.data.free)}</td>
                <td>{formatBytes(status.storage.data.total)}</td>
              </tr>
              <tr>
                <td>{t("Library")}</td>
                <td className="mono small">{status.storage.library.path}</td>
                <td>{formatBytes(status.storage.library.free)}</td>
                <td>{formatBytes(status.storage.library.total)}</td>
              </tr>
            </tbody>
          </table>
        </section>

        <section className="panel">
          <h2>{t("Providers")}</h2>
          <table className="table">
            <tbody>
              {status.providers.map((provider) => {
                const probe = probes?.find((item) => item.name === provider.name);
                return (
                  <tr key={provider.name}>
                    <td>{provider.label}</td>
                    <td>
                      {probes === null ? (
                        <StatusPill kind="muted">{t("Checking…")}</StatusPill>
                      ) : probe?.ok ? (
                        <StatusPill kind="success">
                          {t("Online · {latency} ms", { latency: probe.latency_ms })}
                        </StatusPill>
                      ) : probe ? (
                        <StatusPill kind="danger" >{t("Unreachable")}</StatusPill>
                      ) : (
                        <StatusPill kind="muted">{t("Unknown")}</StatusPill>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          {status.suwayomi ? (
            <>
              <h2>{t("Suwayomi (managed)")}</h2>
              <dl className="kv">
                <dt>{t("Server")}</dt>
                <dd>
                  {status.suwayomi.ready ? (
                    <StatusPill kind="success">{t("Running · {version}", { version: status.suwayomi.version })}</StatusPill>
                  ) : status.suwayomi.installed ? (
                    <StatusPill kind="warn">{status.suwayomi.running ? t("Starting") : t("Stopped")} · {status.suwayomi.version}</StatusPill>
                  ) : (
                    <StatusPill kind="muted">{t("Not installed")}</StatusPill>
                  )}
                  {status.suwayomi.update_available ? (
                    <span className="muted small"> · {t("{version} available, applied automatically at the next quiet window", { version: status.suwayomi.latest_version })}</span>
                  ) : status.suwayomi.latest_version ? (
                    <span className="muted small"> · {t("up to date")}</span>
                  ) : null}
                </dd>
                <dt>{t("Update check")}</dt>
                <dd className="muted small">
                  {status.suwayomi.last_update_check_at ? formatDate(status.suwayomi.last_update_check_at) : t("Pending (daily)")}
                </dd>
                <dt>{t("Extensions refresh")}</dt>
                <dd className="muted small">
                  {status.suwayomi.last_extension_refresh_at
                    ? `${formatDate(status.suwayomi.last_extension_refresh_at)} · ${t("{count} updated", { count: status.suwayomi.last_extension_updates?.length ?? 0 })}`
                    : t("Pending (daily)")}
                </dd>
                <dt>{t("Memory")}</dt>
                <dd className="muted small">{t("heap {heap} MiB", { heap: status.suwayomi.heap_mb })} · {tn(status.suwayomi.restarts, "{count} restart", "{count} restarts")}</dd>
                {health?.download_tuning ? (
                  <>
                    <dt>{t("Page concurrency")}</dt>
                    <dd className="muted small">
                      {health.download_tuning.effective ?? t("Calibrating")} / {health.download_tuning.maximum}
                      {health.download_tuning.latency_ewma_ms !== null
                        ? ` · ${t("{latency} ms source latency", { latency: health.download_tuning.latency_ewma_ms })}`
                        : ""}
                    </dd>
                    <dt>{t("Page pressure")}</dt>
                    <dd className="muted small">
                      {t("{percent}% failures", { percent: Math.round(health.download_tuning.failure_pressure * 100) })}
                      {health.download_tuning.page_kib_ewma !== null
                        ? ` · ${t("{size} KiB average page", { size: health.download_tuning.page_kib_ewma })}`
                        : ""}
                    </dd>
                    <dt>{t("Chapter pipeline")}</dt>
                    <dd className="muted small">
                      {t("{active} active across {series} series · target {depth} / {maximum}", { active: health.download_worker.current_job_ids.length, series: health.download_worker.active_series, depth: health.download_worker.pipeline_depth, maximum: health.download_worker.pipeline_maximum })}
                    </dd>
                    <dt>{t("Pipeline decision")}</dt>
                    <dd className="muted small">
                      {health.download_worker.adaptive.reason}
                      {health.download_worker.adaptive.cpu_usage_percent !== null
                        ? ` · ${t("CPU {percent}% of {cores} cores", { percent: health.download_worker.adaptive.cpu_usage_percent, cores: health.download_worker.adaptive.cpu_limit_cores })}`
                        : ` · ${t("{count} CPU cores available", { count: health.download_worker.adaptive.cpu_limit_cores })}`}
                      {health.download_worker.adaptive.memory_headroom_mib !== null
                        ? ` · ${t("{size} MiB memory headroom", { size: health.download_worker.adaptive.memory_headroom_mib })}`
                        : ""}
                      {health.download_worker.adaptive.io_pressure_avg10 > 0
                        ? ` · ${t("I/O pressure {percent}%", { percent: health.download_worker.adaptive.io_pressure_avg10 })}`
                        : ""}
                    </dd>
                  </>
                ) : null}
                <dt>{t("Challenged sources")}</dt>
                <dd className="muted small">
                  {status.suwayomi.challenged_sources?.length ? (
                    <>
                      <StatusPill kind="warn">{t("{count} unreadable", { count: status.suwayomi.challenged_sources.length })}</StatusPill>
                      <span>
                        {" "}· {t("{sources} answer with an anti-bot challenge; they are ranked last.", { sources: status.suwayomi.challenged_sources.join(", ") })}
                      </span>
                    </>
                  ) : (
                    t("None · every source answers without a challenge")
                  )}
                </dd>
              </dl>
            </>
          ) : null}
          <SystemIntegrationHealth alignment={alignment} ntfyConfigured={status.ntfy_configured} notifications={status.notifications} metadata={status.metadata} />
          <h3 style={{ marginTop: 18 }}>{t("Metadata catalogues")}</h3>
          <table className="table" style={{ marginTop: 14 }}>
            <tbody>
              {status.metadata.sources.map((source) => (
                <tr key={source.name}>
                  <td>{source.label}</td>
                  <td>
                    {source.configured ? (
                      <StatusPill kind="success">
                        {t("Ready")}
                        {" · "}
                        {source.supports_series && source.supports_volumes
                          ? t("series + volumes")
                          : source.supports_volumes
                          ? t("volumes")
                          : t("series")}
                      </StatusPill>
                    ) : source.allows_link_only ? (
                      <StatusPill kind="muted">{t("Link only · exact IDs")}</StatusPill>
                    ) : (
                      <StatusPill kind="muted">{t("Optional · not configured")}</StatusPill>
                    )}
                    {source.unavailable_reason ? (
                      <div className="muted small" style={{ marginTop: 4 }}>
                        {source.unavailable_reason}
                      </div>
                    ) : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>

        <section className="panel">
          <h2>{t("Monitor")}</h2>
          <dl className="kv">
            <dt>{t("Enabled")}</dt>
            <dd>{status.monitor.enabled ? t("Yes") : t("No")}</dd>
            <dt>{t("Interval")}</dt>
            <dd>{tn(Math.round(status.monitor.interval_seconds / 60), "{count} minute", "{count} minutes")}</dd>
            <dt>{t("Running")}</dt>
            <dd>{status.monitor.running ? t("Yes") : t("No")}</dd>
            <dt>{t("Last cycle")}</dt>
            <dd>{formatDate(status.monitor.last_cycle_at)}</dd>
            {status.monitor.last_cycle_error ? (
              <>
                <dt>{t("Last error")}</dt>
                <dd className="warn-text">{status.monitor.last_cycle_error}</dd>
              </>
            ) : null}
            <dt>{t("Wanted recovery")}</dt>
            <dd>
              {status.monitor.wanted_search.enabled
                ? tn(Math.round(status.monitor.wanted_search.interval_seconds / 3600), "Every {count} hour", "Every {count} hours")
                : t("Disabled")}
            </dd>
            <dt>{t("Last Wanted search")}</dt>
            <dd>{formatDate(status.monitor.wanted_search.last_search_at)}</dd>
            <dt>{t("Next Wanted search")}</dt>
            <dd>{formatDate(status.monitor.wanted_search.next_search_at)}</dd>
            <dt>{t("Failed jobs")}</dt>
            <dd>{status.totals.failed_jobs}</dd>
          </dl>
        </section>
      </div>
    </div>
  );
}
