import { StatusPill } from "../components";
import type { LibraryAlignment, MetadataStatus } from "../types";

export default function SystemIntegrationHealth({ alignment, ntfyConfigured, metadata }: {
  alignment: LibraryAlignment | null | undefined;
  ntfyConfigured: boolean;
  metadata: MetadataStatus;
}) {
  const readerNames: Record<string, string> = { stump: "Stump", komga: "Komga", kavita: "Kavita", url: "Reader" };
  const readerName = alignment?.reader_label || readerNames[alignment?.reader ?? ""] || "Reader";
  const readerError = alignment?.error || alignment?.metadata_sync_error;
  const noReader = alignment?.configured === false && !alignment.reader_independent;

  return <>
    <h2>Integrations</h2>
    <dl className="kv">
      <dt>Library reader</dt>
      <dd>
        {readerError ? <>
          <StatusPill kind="danger">{readerName} · Error</StatusPill>
          <p className="warn-text small">{readerError}</p>
        </> : noReader ? <StatusPill kind="muted">No reader configured</StatusPill>
          : alignment?.ready || alignment?.reader_independent ? <StatusPill kind="success">{readerName} · OK</StatusPill>
          : <StatusPill kind="muted">{readerName} · Waiting</StatusPill>}
      </dd>
      <dt>ntfy</dt>
      <dd><StatusPill kind={ntfyConfigured ? "success" : "muted"}>{ntfyConfigured ? "OK" : "Not configured"}</StatusPill></dd>
      <dt>Metadata</dt>
      <dd>
        {!metadata.enabled ? <StatusPill kind="muted">Disabled</StatusPill>
          : metadata.last_cycle_error ? <>
            <StatusPill kind="danger">Error</StatusPill>
            <p className="warn-text small">{metadata.last_cycle_error}</p>
          </> : metadata.running ? <StatusPill kind="muted">Refreshing…</StatusPill>
          : <StatusPill kind="success">OK</StatusPill>}
      </dd>
    </dl>
  </>;
}
