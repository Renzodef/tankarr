import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import { Modal, Spinner } from "../components";
import { t, tn } from "../i18n";
import type { ChapterRecoveryPreview } from "../types";

export default function ChapterRecoveryDialog({ mangaId, onClose, onQueued }: {
  mangaId: string;
  onClose: () => void;
  onQueued: () => Promise<void>;
}) {
  const [preview, setPreview] = useState<ChapterRecoveryPreview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [queueing, setQueueing] = useState(false);
  const alive = useRef(false);
  useEffect(() => {
    alive.current = true;
    const controller = new AbortController();
    setPreview(null);
    setLoading(true);
    void api.previewChapterRecovery(mangaId, controller.signal).then(result => {
      if (!controller.signal.aborted) setPreview(result);
    }).catch(caught => {
      if (!controller.signal.aborted) setError(String(caught));
    }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => { alive.current = false; controller.abort(); };
  }, [mangaId]);
  const queue = async () => {
    if (!preview || queueing) return;
    setQueueing(true);
    try {
      await api.recoverChapters(mangaId, preview.confirmation_snapshot);
      await onQueued();
      if (alive.current) onClose();
    } catch (caught) {
      if (alive.current) { setPreview(null); setError(String(caught)); }
    } finally { if (alive.current) setQueueing(false); }
  };
  return <Modal title={t("Download chapters anyway")} onClose={() => { if (!queueing) onClose(); }} footer={<>
    <button className="btn" disabled={queueing} onClick={onClose}>{t("Cancel")}</button>
    <button className="btn btn-primary" disabled={loading || queueing || !preview?.chapters.length} onClick={() => void queue()}>{t("Confirm chapter downloads")}</button>
  </>}>
    <p>{t("The series stays in volume format. Chapters are kept until confirmed book boundaries are available. Automatic assembly follows the global setting.")}</p>
    <p>{t("Only currently mapped, eligible releases are included. This action does not change sources, language or monitoring settings.")}</p>
    {loading || queueing ? <Spinner /> : null}
    {error ? <p role="alert" className="banner banner-error">{error}</p> : null}
    {preview ? <>
      <p>{tn(preview.chapters.length, "{count} chapter to download", "{count} chapters to download")}</p>
      <ul>{preview.chapters.map(row => <li key={row.id}>{row.chapter} · {row.source}</li>)}</ul>
    </> : null}
  </Modal>;
}
