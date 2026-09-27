import { useCallback, useEffect, useMemo, useState } from "react";

import { api } from "../api";
import { Icon, StatusPill } from "../components";
import type { SuwayomiExtension, SuwayomiRuntimeStatus, SuwayomiSourceTest } from "../types";

function formatBytes(value: number) {
  if (value < 1024 * 1024) return `${Math.round(value / 1024)} KiB`;
  return `${(value / (1024 * 1024)).toFixed(0)} MiB`;
}

function formatUptime(seconds: number | null) {
  if (seconds === null) return null;
  if (seconds < 90) return `${seconds}s`;
  if (seconds < 5400) return `${Math.round(seconds / 60)} min`;
  if (seconds < 172800) return `${(seconds / 3600).toFixed(1)} h`;
  return `${Math.round(seconds / 86400)} d`;
}

const LANGUAGE_LABELS: Record<string, string> = {
  all: "Multi-language",
  en: "English",
  it: "Italian",
  es: "Spanish",
  fr: "French",
  de: "German",
  "pt-br": "Portuguese (BR)",
  pt: "Portuguese",
  ja: "Japanese",
  ko: "Korean",
  zh: "Chinese",
  ru: "Russian",
  id: "Indonesian",
  tr: "Turkish",
  pl: "Polish",
  vi: "Vietnamese",
  th: "Thai",
  ar: "Arabic",
};

function languageLabel(code: string) {
  return LANGUAGE_LABELS[code] ?? code.toUpperCase();
}

const VERDICT_ORDER: Record<SuwayomiSourceTest["verdict"], number> = {
  good: 0,
  partial: 1,
  slow: 2,
  empty: 3,
  unreachable: 4,
};

const VERDICT_KIND: Record<SuwayomiSourceTest["verdict"], "success" | "warn" | "danger" | "muted"> = {
  good: "success",
  partial: "warn",
  slow: "warn",
  empty: "muted",
  unreachable: "danger",
};

export function SuwayomiManager({
  mode,
  languages,
  publicUrl,
}: {
  mode: string;
  languages: string[];
  publicUrl?: string;
}) {
  const managed = mode !== "external";
  const [status, setStatus] = useState<SuwayomiRuntimeStatus | null>(null);
  const [statusError, setStatusError] = useState<string | null>(null);
  const [action, setAction] = useState<string | null>(null);
  const [actionResult, setActionResult] = useState<string | null>(null);
  const [showLog, setShowLog] = useState(false);

  const [language, setLanguage] = useState<string>(languages[0] ?? "en");
  const [storeLanguages, setStoreLanguages] = useState<{ code: string; extensions: number }[]>([]);
  const [extraLanguages, setExtraLanguages] = useState<string[]>([]);
  const [extensions, setExtensions] = useState<SuwayomiExtension[] | null>(null);
  const [extensionsError, setExtensionsError] = useState<string | null>(null);
  const [extensionsLoading, setExtensionsLoading] = useState(false);
  const [filter, setFilter] = useState("");
  const [pending, setPending] = useState<Set<string>>(new Set());

  const [testResults, setTestResults] = useState<SuwayomiSourceTest[] | null>(null);
  const [testing, setTesting] = useState(false);
  const [testError, setTestError] = useState<string | null>(null);
  const [testProgress, setTestProgress] = useState<{ done: number; total: number } | null>(null);

  const loadStatus = useCallback(async () => {
    try {
      const next = await api.suwayomiStatus();
      setStatus(next);
      setStatusError(null);
      return next;
    } catch (caught) {
      setStatusError(String(caught));
      return null;
    }
  }, []);

  useEffect(() => {
    void loadStatus();
  }, [loadStatus]);

  // Poll while something is in flight so the card follows the JVM.
  useEffect(() => {
    const busy = action !== null || status?.installing || (status?.running && !status?.ready);
    if (!busy) return;
    const timer = window.setInterval(() => void loadStatus(), 3000);
    return () => window.clearInterval(timer);
  }, [action, status?.installing, status?.running, status?.ready, loadStatus]);

  const loadExtensions = useCallback(
    async (refresh = false) => {
      if (!managed || !status?.ready) return;
      setExtensionsLoading(true);
      setExtensionsError(null);
      try {
        const result = await api.suwayomiExtensions(language, refresh);
        setExtensions(result.extensions);
      } catch (caught) {
        setExtensionsError(String(caught));
      } finally {
        setExtensionsLoading(false);
      }
    },
    [language, managed, status?.ready],
  );

  useEffect(() => {
    void loadExtensions(false);
  }, [loadExtensions]);

  useEffect(() => {
    if (!managed || !status?.ready) return;
    void api
      .suwayomiLanguages()
      .then((payload) => setStoreLanguages(payload.languages))
      .catch(() => setStoreLanguages([]));
  }, [managed, status?.ready]);

  const runAction = async (name: string, call: () => Promise<SuwayomiRuntimeStatus>, done: (s: SuwayomiRuntimeStatus) => string) => {
    setAction(name);
    setActionResult(null);
    try {
      const next = await call();
      setStatus(next);
      setActionResult(done(next));
    } catch (caught) {
      setActionResult(String(caught));
    } finally {
      setAction(null);
      void loadStatus();
    }
  };

  const install = () =>
    runAction("install", () => api.suwayomiInstall(), (next) =>
      next.ready
        ? `Suwayomi ${next.version ?? ""} is running · ${next.default_extensions?.length ?? 0} default extension${
            (next.default_extensions?.length ?? 0) === 1 ? "" : "s"
          } ready`
        : `Suwayomi ${next.version ?? ""} installed but not ready yet: ${next.last_error ?? "still starting"}`,
    );
  const restart = () =>
    runAction("restart", () => api.suwayomiRestart(), (next) =>
      next.ready ? "Suwayomi restarted." : `Restarted, not ready yet: ${next.last_error ?? "still starting"}`,
    );
  const stop = () => runAction("stop", () => api.suwayomiStop(), () => "Suwayomi stopped.");
  const updateNow = () =>
    runAction("update", () => api.suwayomiUpdateNow(), (next) => {
      const result = (next.maintenance?.result ?? {}) as { server?: { updated_to?: string; latest?: string }; extensions_updated?: string[]; error?: string };
      if (result.error) return `Update check failed: ${result.error}`;
      const parts = [
        result.server?.updated_to ? `server updated to ${result.server.updated_to}` : `server ${next.version ?? ""} is current`,
        `${result.extensions_updated?.length ?? 0} extension${(result.extensions_updated?.length ?? 0) === 1 ? "" : "s"} updated`,
      ];
      return parts.join(" · ");
    });

  const toggleExtension = async (item: SuwayomiExtension) => {
    setPending((current) => new Set(current).add(item.pkg_name));
    try {
      const result = await api.suwayomiSetExtension(item.pkg_name, !item.installed);
      setExtensions((current) =>
        (current ?? []).map((entry) =>
          entry.pkg_name === item.pkg_name
            ? { ...entry, installed: result.installed, has_update: result.has_update }
            : entry,
        ),
      );
    } catch (caught) {
      setExtensionsError(String(caught));
    } finally {
      setPending((current) => {
        const next = new Set(current);
        next.delete(item.pkg_name);
        return next;
      });
    }
  };

  const testSources = async () => {
    setTesting(true);
    setTestError(null);
    setTestResults([]);
    setTestProgress(null);
    try {
      await api.suwayomiTestSourcesStream(
        language === "*" ? languages[0] ?? "en" : language,
        (event) => {
          if (event.type === "start") {
            setTestProgress({ done: 0, total: event.count });
          } else if (event.type === "result") {
            setTestProgress((progress) =>
              progress ? { done: progress.done + 1, total: progress.total } : progress,
            );
            setTestResults((current) =>
              [...(current ?? []), event].sort(
                (a, b) =>
                  (VERDICT_ORDER[a.verdict] ?? 9) - (VERDICT_ORDER[b.verdict] ?? 9) ||
                  (a.average_seconds ?? 99) - (b.average_seconds ?? 99),
              ),
            );
          }
        },
      );
    } catch (caught) {
      setTestError(String(caught));
    } finally {
      setTesting(false);
      setTestProgress(null);
    }
  };

  const languageTabs = useMemo(() => {
    const tabs = [...languages, ...extraLanguages.filter((code) => !languages.includes(code))];
    if (language !== "*" && !tabs.includes(language)) tabs.push(language);
    return tabs;
  }, [languages, extraLanguages, language]);
  const otherLanguages = useMemo(
    () => storeLanguages.filter((item) => item.code !== "all" && !languageTabs.includes(item.code)),
    [storeLanguages, languageTabs],
  );

  const visibleExtensions = useMemo(() => {
    const needle = filter.trim().toLowerCase();
    return (extensions ?? []).filter(
      (item) => !needle || item.name.toLowerCase().includes(needle) || item.pkg_name.includes(needle),
    );
  }, [extensions, filter]);
  const installedCount = (extensions ?? []).filter((item) => item.installed).length;

  const openLink = publicUrl ? (
    <a className="btn btn-small" href={publicUrl} target="_blank" rel="noreferrer">
      <Icon name="external" size={14} /> Open Suwayomi
    </a>
  ) : null;

  if (!managed) {
    return (
      <div className="form-row setting-test-row">
        <div className="setting-field-actions">{openLink}</div>
        <p className="muted small setting-test-result">
          External mode: Tankarr connects to the Suwayomi server configured above and uses whatever
          extensions are installed there. Switch to Managed to let Tankarr run and maintain Suwayomi
          itself.
        </p>
      </div>
    );
  }

  const busy = action !== null || Boolean(status?.installing);

  return (
    <div className="suwayomi-manager">
      <div className="form-row setting-test-row">
        <div className="setting-field-actions">
          <StatusPill
            kind={
              status?.ready ? "success" : status?.running ? "warn" : status?.installed ? "warn" : status ? "muted" : "muted"
            }
          >
            {status === null
              ? statusError
                ? "Status unavailable"
                : "Checking runtime…"
              : status.installing
                ? status.install_progress ?? "Installing…"
                : status.ready
                  ? `Running${status.version ? ` · ${status.version}` : ""}`
                  : status.running
                    ? "Starting…"
                    : status.installed
                      ? `Stopped${status.version ? ` · ${status.version}` : ""}`
                      : "Not installed"}
          </StatusPill>
          {status?.installed ? (
            <span className="muted small">
              {formatBytes(status.jar_size_bytes)} JAR · heap {status.heap_mb} MiB
              {status.uptime_seconds !== null && status.running ? ` · up ${formatUptime(status.uptime_seconds)}` : ""}
              {status.restarts ? ` · ${status.restarts} restart${status.restarts === 1 ? "" : "s"}` : ""}
              {status.latest_version && status.latest_version !== status.version ? ` · ${status.latest_version} available` : status.latest_version ? " · up to date" : ""}
            </span>
          ) : null}
        </div>
        {status?.installed && !status.extension_store ? (
          <p className="banner banner-warn">
            No extension repository configured: Suwayomi has nothing to install sources from. Enter the
            index URL of a repository you trust in <strong>Extension repository</strong> above and save.
          </p>
        ) : null}
        <p className="muted small setting-test-result">
          Tankarr downloads the official Suwayomi-Server JAR from its GitHub release, verifies the
          published SHA-256 checksum, and runs it inside this container. Its web interface answers on
          the container's port 4567, behind your Tankarr login; publish that port to open it from your
          network (set the public URL under Suwayomi if you map it elsewhere). Extensions come only from
          the repository you configure: Install adds every safe extension it offers for your enabled
          languages, and you stay free to add or remove any extension below.
        </p>
        <div className="setting-field-actions">
          <button
            type="button"
            className="btn btn-primary btn-small"
            disabled={busy || status?.writable === false}
            onClick={() => void install()}
          >
            <Icon name="download" size={14} />
            {action === "install" || status?.installing
              ? "Installing…"
              : status?.installed
                ? "Update / reinstall"
                : "Install Suwayomi"}
          </button>
          <button
            type="button"
            className="btn btn-small"
            disabled={busy || !status?.installed || !status?.running}
            title="Check GitHub for a new server release and update installed extensions now; Tankarr does this every day by itself"
            onClick={() => void updateNow()}
          >
            <Icon name="refresh" size={14} /> {action === "update" ? "Updating…" : status?.update_available ? `Update to ${status.latest_version}` : "Check updates"}
          </button>
          <button
            type="button"
            className="btn btn-small"
            disabled={busy || !status?.installed}
            onClick={() => void restart()}
          >
            <Icon name="refresh" size={14} /> {status?.running ? "Restart" : "Start"}
          </button>
          <button
            type="button"
            className="btn btn-small"
            disabled={busy || !status?.running}
            onClick={() => void stop()}
          >
            <Icon name="close" size={14} /> Stop
          </button>
          {status?.ready && status.exposed ? openLink : null}
          <a className="btn btn-small" href={status?.release_page ?? "https://github.com/Suwayomi/Suwayomi-Server/releases"} target="_blank" rel="noreferrer">
            <Icon name="external" size={14} /> Releases
          </a>
          <button type="button" className="btn btn-small" onClick={() => setShowLog((value) => !value)}>
            <Icon name={showLog ? "chevronDown" : "chevronRight"} size={14} /> Log
          </button>
        </div>
        {status?.writable === false ? (
          <p className="warn-text setting-test-result">
            The Tankarr config directory is not writable; Suwayomi cannot be installed.
          </p>
        ) : null}
        {status?.last_error && !status.ready ? (
          <p className="warn-text setting-test-result">{status.last_error}</p>
        ) : null}
        {actionResult ? <p className="muted small setting-test-result">{actionResult}</p> : null}
        {statusError ? <p className="warn-text setting-test-result">{statusError}</p> : null}
        {showLog ? (
          <pre className="suwayomi-log">
            {(status?.log_tail ?? []).length ? (status?.log_tail ?? []).join("\n") : "No log output yet."}
          </pre>
        ) : null}
      </div>

      <div className="form-row setting-test-row">
        <div className="setting-field-actions suwayomi-language-tabs">
          <span className="muted small">Extensions</span>
          {languageTabs.map((code) => (
            <button
              key={code}
              type="button"
              className={`btn btn-small${language === code ? " btn-primary" : ""}`}
              onClick={() => setLanguage(code)}
            >
              {languageLabel(code)}
            </button>
          ))}
          <button
            type="button"
            className={`btn btn-small${language === "*" ? " btn-primary" : ""}`}
            onClick={() => setLanguage("*")}
          >
            All languages
          </button>
          {otherLanguages.length ? (
            <select
              className="input input-small"
              value=""
              onChange={(event) => {
                const code = event.target.value;
                if (!code) return;
                setExtraLanguages((current) => (current.includes(code) ? current : [...current, code]));
                setLanguage(code);
              }}
            >
              <option value="">Other language…</option>
              {otherLanguages.map((item) => (
                <option key={item.code} value={item.code}>
                  {languageLabel(item.code)} ({item.extensions})
                </option>
              ))}
            </select>
          ) : null}
          <input
            className="input input-small"
            placeholder="Filter by name…"
            value={filter}
            onChange={(event) => setFilter(event.target.value)}
          />
          <button
            type="button"
            className="btn btn-small"
            disabled={extensionsLoading || !status?.ready}
            onClick={() => void loadExtensions(true)}
          >
            <Icon name="refresh" size={14} /> Refresh store
          </button>
        </div>
        {!status?.ready ? (
          <p className="muted small setting-test-result">
            Extensions can be managed once Suwayomi is installed and running.
          </p>
        ) : extensionsError ? (
          <p className="warn-text setting-test-result">{extensionsError}</p>
        ) : extensions === null || extensionsLoading ? (
          <p className="muted small setting-test-result">Loading extensions…</p>
        ) : (
          <>
            <p className="muted small setting-test-result">
              {installedCount} installed · {extensions.length} available for{" "}
              {language === "*" ? "all languages" : languageLabel(language)}. Multi-language
              extensions (MangaDex, MangaFire, Webtoons…) serve every language you enable.
            </p>
            <div className="suwayomi-extension-list">
              {visibleExtensions.map((item) => (
                <div key={item.pkg_name} className={`suwayomi-extension${item.installed ? " installed" : ""}`}>
                  {item.icon_url ? (
                    <img src={item.icon_url} alt="" className="suwayomi-extension-icon" loading="lazy" />
                  ) : (
                    <span className="suwayomi-extension-icon placeholder" />
                  )}
                  <div className="suwayomi-extension-body">
                    <div className="suwayomi-extension-title">
                      <strong>{item.name}</strong>
                      <span className="muted small">{languageLabel(item.language)}{item.version ? ` · ${item.version}` : ""}</span>
                    </div>
                    <div className="suwayomi-extension-badges">
                      {item.nsfw ? <StatusPill kind="warn">NSFW</StatusPill> : null}
                      {item.has_update ? <StatusPill kind="info">Update available</StatusPill> : null}
                      {item.obsolete ? <StatusPill kind="danger">Obsolete</StatusPill> : null}
                    </div>
                  </div>
                  <button
                    type="button"
                    className={`btn btn-small${item.installed ? "" : " btn-primary"}`}
                    disabled={pending.has(item.pkg_name) || (!item.installed && item.obsolete)}
                    onClick={() => void toggleExtension(item)}
                  >
                    {pending.has(item.pkg_name)
                      ? "…"
                      : item.installed
                        ? item.has_update
                          ? "Update"
                          : "Remove"
                        : "Install"}
                  </button>
                </div>
              ))}
              {visibleExtensions.length === 0 ? <p className="muted small">No extension matches.</p> : null}
            </div>
          </>
        )}
      </div>

      <div className="form-row setting-test-row">
        <div className="setting-field-actions">
          <button
            type="button"
            className="btn btn-small"
            disabled={testing || !status?.ready}
            onClick={() => void testSources()}
          >
            <Icon name="check" size={14} />{" "}
            {testing
              ? testProgress
                ? `Testing sources… ${testProgress.done}/${testProgress.total}`
                : "Testing sources…"
              : "Test sources"}
          </button>
          <span className="muted small">
            Searches three well-known titles on every enabled source for{" "}
            {languageLabel(language === "*" ? languages[0] ?? "en" : language)} and reports coverage
            and latency from this network.
          </span>
        </div>
        {testError ? <p className="warn-text setting-test-result">{testError}</p> : null}
        {testResults ? (
          <table className="table suwayomi-test-table">
            <thead>
              <tr>
                <th>Source</th>
                <th>Verdict</th>
                <th>Hits</th>
                <th>Avg</th>
                <th>Detail</th>
              </tr>
            </thead>
            <tbody>
              {testResults.map((row) => (
                <tr key={row.id}>
                  <td>{row.name}</td>
                  <td>
                    <StatusPill kind={VERDICT_KIND[row.verdict]}>{row.verdict}</StatusPill>
                  </td>
                  <td>
                    {row.hits}/{row.probes}
                  </td>
                  <td>{row.average_seconds !== null ? `${row.average_seconds}s` : "—"}</td>
                  <td className="muted small">{row.error ?? ""}</td>
                </tr>
              ))}
              {testResults.length === 0 ? (
                <tr>
                  <td colSpan={5} className="muted">
                    No enabled sources for this language.
                  </td>
                </tr>
              ) : null}
            </tbody>
          </table>
        ) : null}
      </div>
    </div>
  );
}
