import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import { downloadReport } from "../downloadReport";
import MaintenanceNotice, { maintenanceErrors } from "../components/MaintenanceNotice";
import SystemIntegrationHealth from "../components/SystemIntegrationHealth";
import type { MaintenanceStatus } from "../operationTypes";
import { Icon, Spinner, StatusPill, formatBytes, formatDate, useApp, Cover, seriesPath } from "../components";
import type { ProviderProbe, SystemStatus, MatchReview, LibraryOrphans } from "../types";

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
      notify("success", `Deleted ${result.deleted} untracked file${result.deleted === 1 ? "" : "s"}.`);
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
        <h1 className="page-title">System</h1>
        <div className="toolbar-group">
          <button
            type="button"
            className="btn"
            disabled={busy}
            onClick={() => void act(() => api.runMonitor(), "Monitor cycle completed.")}
          >
            <Icon name="refresh" /> Run Monitor
          </button>
          <button
            type="button"
            className="btn"
            disabled={busy}
            onClick={() => void act(() => api.organizeLibrary(), "Library organization completed.")}
          >
            <Icon name="library" /> Organize Library
          </button>
          <button
            type="button"
            className="btn"
            disabled={busy}
            onClick={() =>
              void act(
                () => api.refreshAllMetadata(true),
                "Metadata enrichment started in the background.",
              )
            }
          >
            <Icon name="refresh" /> Enrich Metadata
          </button>
        </div>
      </div>

      <div className="system-grid">
        {status.alerts?.length || maintenanceNeedsAttention || maintenanceError ? (
          <section className="panel system-alerts">
            <h2>Needs attention</h2>
            <ul className="alert-list">
              {status.alerts?.map((alert) => (
                <li key={alert.key} className={`alert-row alert-${alert.level}`}>
                  <StatusPill kind={alert.level === "danger" ? "danger" : alert.level === "warn" ? "warn" : "info"}>
                    {alert.level === "danger" ? "Error" : alert.level === "warn" ? "Warning" : "Note"}
                  </StatusPill>
                  <div>
                    <strong>{alert.href ? <a href={alert.href}>{alert.title}</a> : alert.title}</strong>
                    <div className="muted small">{alert.detail}</div>
                  </div>
                  <button
                    type="button"
                    className="btn btn-ghost btn-icon alert-dismiss"
                    title="Acknowledge: it comes back if this changes"
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
            {maintenanceError ? <p className="banner banner-danger" role="alert">Scheduled maintenance status unavailable: {maintenanceError}</p> : null}
            {maintenance ? <MaintenanceNotice status={maintenance} /> : null}
          </section>
        ) : null}
        <section className="panel system-alerts">
          <h2>Health</h2>
          {!status.alerts?.length && !maintenanceNeedsAttention && !maintenanceError ? <p className="muted small"><StatusPill kind="success">All clear</StatusPill> Downloads, sources, storage and the Suwayomi runtime report no problems.</p> : <p className="muted small">Review the items in Needs attention.</p>}
          <a href="#/setup" className="btn">Re-run setup checks</a>
        </section>
        {orphans && orphans.count > 0 ? (
          <section className="panel">
            <h2>Untracked library files</h2>
            <p className="muted small">
              {orphans.count} file{orphans.count === 1 ? "" : "s"} on disk that no series in Tankarr
              claims. The reader still serves them, so Tankarr and the reader disagree until they are
              removed. They are usually left by a series deleted without its files. Nothing is deleted
              automatically.
            </p>
            <ul className="alert-list">
              {orphans.folders.map((folder) => (
                <li key={folder.folder} className="alert-row">
                  <div>
                    <strong>{folder.folder}</strong>
                    <div className="muted small">
                      {folder.files} file{folder.files === 1 ? "" : "s"} · {formatBytes(folder.bytes)} ·{" "}
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
                    <Icon name="trash" /> Delete {folder.files}
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
                <Icon name="trash" /> Delete all {orphans.count} untracked files
              </button>
            ) : null}
          </section>
        ) : null}
        {reviews.length ? (
          <section className="panel">
            <h2>To confirm</h2>
            <p className="muted small">Matches that looked right but did not pass the automatic rules. Accept maps the source (or grabs the release); reject hides it for good.</p>
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
                          review.payload.volume ? `Volumes ${review.payload.volume}` : null,
                          review.payload.chapter ? `Chapter ${review.payload.chapter}` : null,
                          review.payload.size,
                          review.payload.protocol === "usenet"
                            ? "Usenet"
                            : typeof review.payload.seeders === "number"
                              ? `${review.payload.seeders} seeder${review.payload.seeders === 1 ? "" : "s"}`
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
                        <Icon name="external" size={14} /> Review
                      </a>
                    ) : null}
                    <button type="button" className="btn btn-small btn-primary" disabled={reviewBusy === review.id} onClick={() => void decide(review.id, true)}>
                      Accept
                    </button>
                    <button type="button" className="btn btn-small btn-ghost" disabled={reviewBusy === review.id} onClick={() => void decide(review.id, false)}>
                      Reject
                    </button>
                  </div>
                </li>
              ))}
            </ul>
          </section>
        ) : null}
        <section className="panel">
          <h2>About</h2>
          <dl className="kv">
            <dt>Version</dt>
            <dd>
              {status.version}
              {status.update?.update_available && status.update.latest ? (
                <>
                  {" · "}
                  {status.update.url ? (
                    <a href={status.update.url} target="_blank" rel="noreferrer">
                      {status.update.latest} available
                    </a>
                  ) : (
                    <span>{status.update.latest} available</span>
                  )}
                </>
              ) : status.update?.checked_at && !status.update.error ? (
                <span className="muted small"> · up to date</span>
              ) : null}
            </dd>
            <dt>Python</dt>
            <dd>{status.python}</dd>
            <dt>Platform</dt>
            <dd>{status.platform}</dd>
            <dt>Started</dt>
            <dd>{formatDate(status.started_at)}</dd>
            <dt>Database</dt>
            <dd>
              {status.database_path} ({formatBytes(status.database_size)})
            </dd>
          </dl>
          <button type="button" className="btn" disabled={diagnosticsBusy} onClick={() => {
            setDiagnosticsBusy(true);
            void downloadReport("/api/system/diagnostics/export", "tankarr-diagnostics.json").catch((caught: unknown) => notify("error", String(caught))).finally(() => setDiagnosticsBusy(false));
          }}>Download diagnostics</button>
        </section>

        <section className="panel">
          <h2>Library</h2>
          <dl className="kv">
            <dt>Status</dt>
            <dd>
              {status.library.available ? (
                <StatusPill kind="success">Available</StatusPill>
              ) : (
                <StatusPill kind="danger">{status.library.reason}</StatusPill>
              )}
            </dd>
            <dt>Series</dt>
            <dd>
              {status.totals.series} ({status.totals.monitored} monitored)
            </dd>
            <dt>Chapters</dt>
            <dd>
              {status.totals.downloaded} downloaded of {status.totals.chapters} known
            </dd>
            <dt>Naming</dt>
            <dd className="mono small">{organization?.naming_format ?? "—"}</dd>
            <dt>Organization</dt>
            <dd>
              {organization ? (
                organization.organization_blocked ? (
                  <StatusPill kind="danger">Blocked</StatusPill>
                ) : (
                  <StatusPill kind="success">
                    Clean · {organization.moved} moved, {organization.unchanged} unchanged
                  </StatusPill>
                )
              ) : (
                "—"
              )}
            </dd>
            <dt>Deletion recovery</dt>
            <dd>
              {recovery ? (
                recovery.recovery_blocked ? (
                  <StatusPill kind="danger">Blocked: {recovery.warnings[0]}</StatusPill>
                ) : (
                  <StatusPill kind="success">Clean</StatusPill>
                )
              ) : (
                "—"
              )}
            </dd>
          </dl>
        </section>

        <section className="panel">
          <h2>Storage</h2>
          <table className="table">
            <thead>
              <tr>
                <th>Location</th>
                <th>Path</th>
                <th>Free</th>
                <th>Total</th>
              </tr>
            </thead>
            <tbody>
              <tr>
                <td>Config</td>
                <td className="mono small">{status.storage.data.path}</td>
                <td>{formatBytes(status.storage.data.free)}</td>
                <td>{formatBytes(status.storage.data.total)}</td>
              </tr>
              <tr>
                <td>Library</td>
                <td className="mono small">{status.storage.library.path}</td>
                <td>{formatBytes(status.storage.library.free)}</td>
                <td>{formatBytes(status.storage.library.total)}</td>
              </tr>
            </tbody>
          </table>
        </section>

        <section className="panel">
          <h2>Providers</h2>
          <table className="table">
            <tbody>
              {status.providers.map((provider) => {
                const probe = probes?.find((item) => item.name === provider.name);
                return (
                  <tr key={provider.name}>
                    <td>{provider.label}</td>
                    <td>
                      {probes === null ? (
                        <StatusPill kind="muted">Checking…</StatusPill>
                      ) : probe?.ok ? (
                        <StatusPill kind="success">
                          Online · {probe.latency_ms} ms
                          {probe.supported_sites ? ` · ${probe.supported_sites} sites` : ""}
                        </StatusPill>
                      ) : probe ? (
                        <StatusPill kind="danger" >Unreachable</StatusPill>
                      ) : (
                        <StatusPill kind="muted">Unknown</StatusPill>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          {status.suwayomi ? (
            <>
              <h2>Suwayomi (managed)</h2>
              <dl className="kv">
                <dt>Server</dt>
                <dd>
                  {status.suwayomi.ready ? (
                    <StatusPill kind="success">Running · {status.suwayomi.version}</StatusPill>
                  ) : status.suwayomi.installed ? (
                    <StatusPill kind="warn">{status.suwayomi.running ? "Starting" : "Stopped"} · {status.suwayomi.version}</StatusPill>
                  ) : (
                    <StatusPill kind="muted">Not installed</StatusPill>
                  )}
                  {status.suwayomi.update_available ? (
                    <span className="muted small"> · {status.suwayomi.latest_version} available, applied automatically at the next quiet window</span>
                  ) : status.suwayomi.latest_version ? (
                    <span className="muted small"> · up to date</span>
                  ) : null}
                </dd>
                <dt>Update check</dt>
                <dd className="muted small">
                  {status.suwayomi.last_update_check_at ? formatDate(status.suwayomi.last_update_check_at) : "Pending (daily)"}
                </dd>
                <dt>Extensions refresh</dt>
                <dd className="muted small">
                  {status.suwayomi.last_extension_refresh_at
                    ? `${formatDate(status.suwayomi.last_extension_refresh_at)} · ${status.suwayomi.last_extension_updates?.length ?? 0} updated`
                    : "Pending (daily)"}
                </dd>
                <dt>Memory</dt>
                <dd className="muted small">heap {status.suwayomi.heap_mb} MiB · {status.suwayomi.restarts} restart{status.suwayomi.restarts === 1 ? "" : "s"}</dd>
                {health?.download_tuning ? (
                  <>
                    <dt>Page concurrency</dt>
                    <dd className="muted small">
                      {health.download_tuning.effective ?? "Calibrating"} / {health.download_tuning.maximum}
                      {health.download_tuning.latency_ewma_ms !== null
                        ? ` · ${health.download_tuning.latency_ewma_ms} ms source latency`
                        : ""}
                    </dd>
                    <dt>Page pressure</dt>
                    <dd className="muted small">
                      {Math.round(health.download_tuning.failure_pressure * 100)}% failures
                      {health.download_tuning.page_kib_ewma !== null
                        ? ` · ${health.download_tuning.page_kib_ewma} KiB average page`
                        : ""}
                    </dd>
                    <dt>Chapter pipeline</dt>
                    <dd className="muted small">
                      {health.download_worker.current_job_ids.length} active across {health.download_worker.active_series} series · target {health.download_worker.pipeline_depth} / {health.download_worker.pipeline_maximum}
                    </dd>
                    <dt>Pipeline decision</dt>
                    <dd className="muted small">
                      {health.download_worker.adaptive.reason}
                      {health.download_worker.adaptive.cpu_usage_percent !== null
                        ? ` · CPU ${health.download_worker.adaptive.cpu_usage_percent}% of ${health.download_worker.adaptive.cpu_limit_cores} cores`
                        : ` · ${health.download_worker.adaptive.cpu_limit_cores} CPU cores available`}
                      {health.download_worker.adaptive.memory_headroom_mib !== null
                        ? ` · ${health.download_worker.adaptive.memory_headroom_mib} MiB memory headroom`
                        : ""}
                      {health.download_worker.adaptive.io_pressure_avg10 > 0
                        ? ` · I/O pressure ${health.download_worker.adaptive.io_pressure_avg10}%`
                        : ""}
                    </dd>
                  </>
                ) : null}
                <dt>Challenged sources</dt>
                <dd className="muted small">
                  {status.suwayomi.challenged_sources?.length ? (
                    <>
                      <StatusPill kind="warn">{status.suwayomi.challenged_sources.length} unreadable</StatusPill>
                      <span>
                        {" "}· {status.suwayomi.challenged_sources.join(", ")} answer with an anti-bot challenge; they are ranked last.
                      </span>
                    </>
                  ) : (
                    "None · every source answers without a challenge"
                  )}
                </dd>
              </dl>
            </>
          ) : null}
          <SystemIntegrationHealth alignment={alignment} ntfyConfigured={status.ntfy_configured} metadata={status.metadata} />
          <h3 style={{ marginTop: 18 }}>Metadata catalogues</h3>
          <table className="table" style={{ marginTop: 14 }}>
            <tbody>
              {status.metadata.sources.map((source) => (
                <tr key={source.name}>
                  <td>{source.label}</td>
                  <td>
                    {source.configured ? (
                      <StatusPill kind="success">
                        Ready
                        {source.supports_series && source.supports_volumes
                          ? " · series + volumes"
                          : source.supports_volumes
                          ? " · volumes"
                          : " · series"}
                      </StatusPill>
                    ) : source.allows_link_only ? (
                      <StatusPill kind="muted">Link only · exact IDs</StatusPill>
                    ) : (
                      <StatusPill kind="muted">Optional · not configured</StatusPill>
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
          <h2>Monitor</h2>
          <dl className="kv">
            <dt>Enabled</dt>
            <dd>{status.monitor.enabled ? "Yes" : "No"}</dd>
            <dt>Interval</dt>
            <dd>{Math.round(status.monitor.interval_seconds / 60)} minutes</dd>
            <dt>Running</dt>
            <dd>{status.monitor.running ? "Yes" : "No"}</dd>
            <dt>Last cycle</dt>
            <dd>{formatDate(status.monitor.last_cycle_at)}</dd>
            {status.monitor.last_cycle_error ? (
              <>
                <dt>Last error</dt>
                <dd className="warn-text">{status.monitor.last_cycle_error}</dd>
              </>
            ) : null}
            <dt>Wanted recovery</dt>
            <dd>
              {status.monitor.wanted_search.enabled
                ? `Every ${Math.round(status.monitor.wanted_search.interval_seconds / 3600)} hours`
                : "Disabled"}
            </dd>
            <dt>Last Wanted search</dt>
            <dd>{formatDate(status.monitor.wanted_search.last_search_at)}</dd>
            <dt>Next Wanted search</dt>
            <dd>{formatDate(status.monitor.wanted_search.next_search_at)}</dd>
            <dt>Failed jobs</dt>
            <dd>{status.totals.failed_jobs}</dd>
          </dl>
        </section>
      </div>
    </div>
  );
}
