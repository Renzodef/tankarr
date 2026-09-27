import { useEffect, useState } from "react";
import { api } from "../api";
import { Modal, Spinner, formatDate, humanize, seriesPath } from "../components";
import type { WantedRecovery } from "../types";
import type { AcquisitionPreview } from "../operationTypes";
import { AcquisitionChoices } from "./AcquisitionChoices";

export function WantedExplanation({ mangaId, title, item, recovery, blockReason, busy, onClose, onSearch, onInteractive }: {
  mangaId: string;
  title: string;
  item: string;
  recovery?: WantedRecovery;
  blockReason?: string | null;
  busy: boolean;
  onClose: () => void;
  onSearch: () => void;
  onInteractive?: () => void;
}) {
  const [preview, setPreview] = useState<AcquisitionPreview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    setError(null);
    void api.previewAcquisition(mangaId, undefined, controller.signal)
      .then((next) => { if (!controller.signal.aborted) setPreview(next); })
      .catch((caught: unknown) => { if (!controller.signal.aborted) setError(String(caught)); });
    return () => controller.abort();
  }, [mangaId, attempt]);
  return <Modal title="Why still wanted?" onClose={onClose} wide footer={<>
    <button type="button" className="btn" onClick={onClose}>Close</button>
    {onInteractive ? <button type="button" className="btn" disabled={busy} onClick={onInteractive}>Inspect search results</button> : null}
    <button type="button" className="btn btn-primary" disabled={busy} onClick={onSearch}>Search now</button>
  </>}>
    <h3>{title} · {item}</h3>
    <p>{recovery?.summary ?? "No recovery evidence has been recorded yet."}</p>
    {blockReason ? <p className="banner banner-warn">{blockReason}</p> : null}
    <p>{recovery?.actionable_reason ?? "Inspect the recorded evidence or run a search. A missing result does not prove that the work is permanently unavailable."}</p>
    <p>Last checked: {recovery?.checked_at ? formatDate(recovery.checked_at) : "No recorded check"}.</p>
    {recovery?.next_eligible_at ? <p>Eligible again after {formatDate(recovery.next_eligible_at)}. This is the cooldown deadline, not a guaranteed download or search time.</p> : <p className="muted">No per-item cooldown deadline is currently available. The Wanted header shows the next global recovery cycle.</p>}
    {recovery?.channels.length ? <div className="data-table-frame"><table className="table responsive-list-table">
      <thead><tr><th>Channel</th><th>Outcome</th><th>Checked</th><th>Evidence & next action</th></tr></thead>
      <tbody>{recovery.channels.map((channel, index) => <tr key={`${channel.channel}:${index}`}>
        <td data-label="Channel">{humanize(channel.channel)}</td><td data-label="Outcome">{humanize(channel.outcome)}</td>
        <td data-label="Checked">{channel.checked_at || channel.at ? formatDate(channel.checked_at ?? channel.at) : "Not recorded"}</td>
        <td data-label="Evidence & next action"><p>{channel.detail || "No additional detail was recorded."}</p>{channel.actionable_reason ? <p>{channel.actionable_reason}</p> : null}{channel.next_eligible_at ? <p>Eligible after {formatDate(channel.next_eligible_at)}</p> : null}</td>
      </tr>)}</tbody>
    </table></div> : null}
    <h3>Candidate decisions for this series</h3>
    <p className="muted">Read-only preview using the saved acquisition policy; it does not queue or replace files.</p>
    {error ? <p className="banner banner-danger" role="alert">{error} <button type="button" className="btn" onClick={() => setAttempt((value) => value + 1)}>Retry candidate preview</button></p> : null}
    {preview ? <AcquisitionChoices preview={preview} /> : !error ? <Spinner /> : null}
    <p><a href={seriesPath(mangaId)}>Open series</a> · <a href="#/settings?tab=sources">Acquisition settings</a></p>
  </Modal>;
}
