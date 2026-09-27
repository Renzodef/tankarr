import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api";
import type { PreflightReport } from "./operationTypes";
import type { SettingsPayload } from "./types";

const SKIPPED_KEY = "tankarr.setup.skipped";

export function needsSetup(settings: SettingsPayload, report: PreflightReport, skipped: boolean) {
  return !skipped && settings.setup_completed_at?.value == null && !report.ready;
}

function wasSkipped() {
  try { return window.sessionStorage.getItem(SKIPPED_KEY) === "1"; }
  catch { return false; }
}

export function useSetupGate() {
  const [required, setRequired] = useState(false);
  const dismissed = useRef(false);
  useEffect(() => {
    let active = true;
    if (wasSkipped()) return;
    void Promise.all([api.getSettings(), api.preflight()]).then(([settings, report]) => {
      if (active && !dismissed.current) setRequired(needsSetup(settings, report, false));
    }).catch(() => {
      // An unavailable check must not lock the operator out of Settings.
    });
    return () => { active = false; };
  }, []);
  const finish = useCallback((skipped = false) => {
    dismissed.current = true;
    if (skipped) {
      try { window.sessionStorage.setItem(SKIPPED_KEY, "1"); } catch { /* Session-only state still works. */ }
    }
    setRequired(false);
  }, []);
  return { required, finish };
}
