import { useState } from "react";

type SkipDecision = { path: string; reason: string; detail?: string };
type Decisions = { imported: string[]; alreadyOwned: string[]; skipped: SkipDecision[] };

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}
function paths(value: unknown): string[] {
  return Array.isArray(value) ? [...new Set(value.filter((item): item is string => typeof item === "string" && Boolean(item.trim())))] : [];
}
function decisions(evidence: Record<string, unknown>, importedPaths: readonly string[]): Decisions {
  const imported = record(evidence.import_decisions);
  const skipped = Array.isArray(imported.skipped) ? imported.skipped : evidence.skip_decisions;
  return {
    imported: Array.isArray(imported.imported_paths) ? paths(imported.imported_paths) : paths(importedPaths),
    alreadyOwned: paths(imported.already_owned_paths),
    skipped: Array.isArray(skipped) ? skipped.flatMap((value) => {
      const entry = record(value);
      return typeof entry.path === "string" && entry.path.trim() ? [{ path: entry.path, reason: typeof entry.reason === "string" ? entry.reason : "Not imported", ...(typeof entry.detail === "string" ? { detail: entry.detail } : {}) }] : [];
    }) : [],
  };
}
function reasonLabel(reason: string) {
  const labels: Record<string, string> = {
    unnumbered: "No book or chapter number",
    ambiguous_numbering: "Ambiguous book or chapter numbering",
    already_owned: "Already owned",
    owned_conflict: "Already owned",
    identical_copy: "Identical duplicate",
    ambiguous_editions: "Multiple editions for the same book",
  };
  return labels[reason] ?? reason.replaceAll("_", " ");
}
function basename(path: string) { return path.replaceAll("\\", "/").split("/").filter(Boolean).at(-1) ?? path; }

export function importDecisionsSearchText(evidence: Record<string, unknown>, importedPaths: readonly string[] = []) {
  const result = decisions(evidence, importedPaths);
  return [...result.imported, ...result.alreadyOwned, ...result.skipped.map((item) => `${item.path} ${reasonLabel(item.reason)} ${item.detail ?? ""}`)].join(" ");
}

export default function ImportDecisionsDetails({ evidence, importedPaths = [] }: {
  evidence: Record<string, unknown>; importedPaths?: readonly string[];
}) {
  const [open, setOpen] = useState(false);
  const result = decisions(evidence, importedPaths);
  const counts = [result.imported.length ? `${result.imported.length} imported` : "", result.alreadyOwned.length ? `${result.alreadyOwned.length} already owned` : "", result.skipped.length ? `${result.skipped.length} skipped` : ""].filter(Boolean);
  if (!counts.length) return null;
  return <details onToggle={(event) => setOpen(event.currentTarget.open)}>
    <summary className="link-button">Files: {counts.join(" · ")}</summary>
    {open ? <div style={{ whiteSpace: "normal", overflowWrap: "anywhere" }}>
      {result.imported.length ? <><p><strong>Imported</strong></p><ul>{result.imported.map((path) => <li key={path}><span title={path}>{basename(path)}</span></li>)}</ul></> : null}
      {result.alreadyOwned.length ? <><p><strong>Already owned</strong></p><ul>{result.alreadyOwned.map((path) => <li key={path}><span title={path}>{basename(path)}</span></li>)}</ul></> : null}
      {result.skipped.length ? <><p><strong>Skipped</strong></p><ul>{result.skipped.map((item, index) => <li key={`${item.path}:${index}`}><span title={item.path}>{basename(item.path)}</span> — {reasonLabel(item.reason)}{item.detail ? `: ${item.detail}` : ""}</li>)}</ul></> : null}
    </div> : null}
  </details>;
}
