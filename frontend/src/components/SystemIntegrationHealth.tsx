import { StatusPill } from "../components";
import type { LibraryAlignment, MetadataStatus } from "../types";
import { t } from "../i18n";

const CHANNEL_LABELS: Record<string, string> = { ntfy: "ntfy", webhook: "Webhook", discord: "Discord", telegram: "Telegram", apprise: "Apprise" };

export default function SystemIntegrationHealth({ alignment, ntfyConfigured, notifications, metadata }: {
  alignment: LibraryAlignment | null | undefined;
  ntfyConfigured: boolean;
  notifications?: Record<string, boolean>;
  metadata: MetadataStatus;
}) {
  const channels = Object.entries(notifications ?? {}).filter(([, configured]) => configured).map(([name]) => CHANNEL_LABELS[name] ?? name);
  const notifying = ntfyConfigured || channels.length > 0;
  const readerNames: Record<string, string> = { stump: "Stump", komga: "Komga", kavita: "Kavita", url: t("Reader") };
  const readerName = alignment?.reader_label || readerNames[alignment?.reader ?? ""] || t("Reader");
  const readerError = alignment?.error || alignment?.metadata_sync_error;
  const noReader = alignment?.configured === false && !alignment.reader_independent;

  return <>
    <h2>{t("Integrations")}</h2>
    <dl className="kv">
      <dt>{t("Library reader")}</dt>
      <dd>
        {readerError ? <>
          <StatusPill kind="danger">{t("{reader} · Error", { reader: readerName })}</StatusPill>
          <p className="warn-text small">{readerError}</p>
        </> : noReader ? <StatusPill kind="muted">{t("No reader configured")}</StatusPill>
          : alignment?.ready || alignment?.reader_independent ? <StatusPill kind="success">{t("{reader} · OK", { reader: readerName })}</StatusPill>
          : <StatusPill kind="muted">{t("{reader} · Waiting", { reader: readerName })}</StatusPill>}
      </dd>
      <dt>{t("Notifications")}</dt>
      <dd><StatusPill kind={notifying ? "success" : "muted"}>{notifying ? (channels.length ? channels.join(", ") : "OK") : t("Not configured")}</StatusPill></dd>
      <dt>{t("Metadata")}</dt>
      <dd>
        {!metadata.enabled ? <StatusPill kind="muted">{t("Disabled")}</StatusPill>
          : metadata.last_cycle_error ? <>
            <StatusPill kind="danger">{t("Error")}</StatusPill>
            <p className="warn-text small">{metadata.last_cycle_error}</p>
          </> : metadata.running ? <StatusPill kind="muted">{t("Refreshing…")}</StatusPill>
          : <StatusPill kind="success">OK</StatusPill>}
      </dd>
    </dl>
  </>;
}
