import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import { Spinner, StatusPill, navigate } from "../components";
import type { PreflightReport } from "../operationTypes";
import { msg, t } from "../i18n";

const STEPS = [
  { title: msg("Library"), tab: "general", description: msg("Check the library directory, its identity and available space. Set the library path with TANKARR_LIBRARY_DIR in the environment, then restart Tankarr."), matches: (tab: string) => !["reader", "sources", "indexers"].includes(tab) },
  { title: msg("Reader"), tab: "reader", description: msg("Choose a reader for your library. You can continue without one and open the files independently."), matches: (tab: string) => tab === "reader" },
  { title: msg("Suwayomi sources"), tab: "sources", description: msg("Configure Suwayomi and choose the sources and languages Tankarr should search."), matches: (tab: string) => tab === "sources" },
  { title: msg("Optional indexers"), tab: "indexers", description: msg("Configure indexers and download clients if you use them. These integrations are optional."), matches: (tab: string) => tab === "indexers" },
];

export default function SetupPage({ onFinish }: { onFinish: (skipped?: boolean) => void }) {
  const [step, setStep] = useState(0);
  const [report, setReport] = useState<PreflightReport | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const revision = useRef(0);

  useEffect(() => {
    const current = ++revision.current;
    void api.preflight().then((result) => {
      if (current === revision.current) setReport(result);
    }).catch((caught: unknown) => {
      if (current === revision.current) setError(String(caught));
    });
    return () => { revision.current += 1; };
  }, []);

  const check = async (connections = false) => {
    const current = ++revision.current;
    setBusy(true);
    setError(null);
    try {
      const result = await api.preflight(connections);
      if (current === revision.current) setReport(result);
    } catch (caught) { if (current === revision.current) setError(String(caught)); }
    finally { if (current === revision.current) setBusy(false); }
  };

  const complete = async () => {
    const current = ++revision.current;
    setBusy(true);
    setError(null);
    try {
      const latest = await api.preflight();
      if (current !== revision.current) return;
      setReport(latest);
      if (!latest.ready) {
        setError(t("A required check needs attention. Review it before completing setup."));
        return;
      }
      await api.putSettings({ setup_completed_at: new Date().toISOString() });
      if (current !== revision.current) return;
      onFinish();
      navigate("/");
    } catch (caught) { if (current === revision.current) setError(String(caught)); }
    finally { if (current === revision.current) setBusy(false); }
  };

  const selected = STEPS[step];
  const checks = report?.checks.filter((item) => selected.matches(item.settings_tab ?? "general")) ?? [];
  return <div className="page">
    <div className="toolbar">
      <h1 className="page-title">{t("Setup")}</h1>
      <button type="button" className="btn" onClick={() => { ++revision.current; onFinish(true); navigate("/"); }}>{t("Skip for now")}</button>
    </div>
    <p className="muted">{t("Check your saved configuration before adding series. Local checks run automatically; connection checks run only when requested.")}</p>
    <nav className="toolbar-group" aria-label={t("Setup steps")}>
      {STEPS.map((item, index) => <button key={item.title} type="button" className={`btn ${step === index ? "btn-primary" : ""}`} aria-current={step === index ? "step" : undefined} onClick={() => setStep(index)}>{index + 1}. {t(item.title)}</button>)}
    </nav>
    <section className="panel settings-section">
      <h2>{t(selected.title)}</h2>
      <p>{t(selected.description)}</p>
      <p><a href={`#/settings?tab=${selected.tab}`}>{t("Open {section} settings", { section: t(selected.title).toLocaleLowerCase() })}</a></p>
      {!report && !error ? <Spinner /> : null}
      {report && !checks.length ? <p className="muted">{t("No checks for this optional integration. Configure it in Settings if needed.")}</p> : null}
      <ul className="alert-list">{checks.map((item) => <li key={item.id} className="alert-row">
        <StatusPill kind={item.status === "ok" ? "success" : item.status === "error" ? "danger" : "warn"}>{item.status === "ok" ? t("Passed") : item.status === "error" ? t("Required") : t("Optional")}</StatusPill>
        <div><strong>{item.label}</strong><p>{item.detail}</p>{item.settings_tab && item.settings_tab !== selected.tab ? <a href={`#/settings?tab=${item.settings_tab}`}>{t("Open settings")}</a> : null}</div>
      </li>)}</ul>
      <div className="toolbar-group">
        <button type="button" className="btn" disabled={busy} onClick={() => void check()}>{t("Refresh local checks")}</button>
        <button type="button" className="btn" disabled={busy} onClick={() => void check(true)}>{t("Run connection checks")}</button>
      </div>
      {error ? <p className="banner banner-danger" role="alert">{error}</p> : null}
      {report ? <p role="status">{report.ready ? t("Required checks passed. Warnings do not prevent completing setup.") : t("Some required checks need attention before completing setup.")}</p> : null}
      <div className="toolbar-group">
        <button type="button" className="btn" disabled={step === 0} onClick={() => setStep((current) => current - 1)}>{t("Back")}</button>
        {step < STEPS.length - 1 ? <button type="button" className="btn btn-primary" onClick={() => setStep((current) => current + 1)}>{t("Next")}</button> : <button type="button" className="btn btn-primary" disabled={busy || !report?.ready} onClick={() => void complete()}>{busy ? t("Saving…") : t("Complete setup")}</button>}
      </div>
    </section>
  </div>;
}
