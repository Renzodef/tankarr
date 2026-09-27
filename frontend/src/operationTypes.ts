import type { ReaderLink } from "./types";

export type PreflightReport = {
  ready: boolean;
  checks: { id: string; label: string; status: "ok" | "warning" | "error"; detail: string; settings_tab?: string }[];
};

export type LibraryListItem = {
  provider?: string;
  source_id?: string;
  manga_id?: string;
  title: string;
  language?: string;
  monitor_mode?: string;
};

export type LibraryListPreview = {
  items: { key: string; title: string; status: "ready" | "existing" | "ambiguous" | "invalid"; reason: string }[];
  token?: string;
};

export type RepairPreview = {
  actions: { id: string; kind: string; path?: string; destination?: string; reason: string; safe: boolean }[];
  warnings: string[];
  token?: string;
};

export type AcquisitionPreview = {
  candidates: { id: string; title: string; source?: string; selected: boolean; reasons: string[]; rejections?: string[] }[];
  explanation: string;
  warnings?: string[];
  truncated?: boolean;
  total_candidates?: number;
};

export type ReaderAlignment = ReaderLink & {
  managed_books: number;
  unmatched_books: number;
  match_method: string;
};

export type BackupVerification = {
  verified: boolean;
  format: string;
  contains_secrets: boolean;
  files: string[];
  excluded: string[];
};

export type MaintenanceStatus = {
  jobs: Record<"backup" | "repair" | "recycle", {
    status: "pending" | "running" | "ok" | "skipped" | "error";
    last_attempt_at: string | null;
    last_success_at: string | null;
    retry_after: string | null;
    error: string | null;
  }>;
  repair: {
    revision: string | null;
    generated_at: string | null;
    actions: RepairPreview["actions"];
    total_actions: number;
    truncated: boolean;
    warnings: string[];
    can_apply: boolean;
  };
};
