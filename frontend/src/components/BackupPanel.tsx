import { useEffect, useState } from "react";
import { api } from "../api";
import { Modal, Spinner, formatBytes, formatDate } from "../components";
import { downloadReport } from "../downloadReport";
import type { BackupVerification } from "../operationTypes";
import type { BackupInfo } from "../types";

export default function BackupPanel() {
  const [backups, setBackups] = useState<BackupInfo[] | null>(null);
  const [selected, setSelected] = useState<BackupInfo | null>(null);
  const [verification, setVerification] = useState<BackupVerification | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [restoreError, setRestoreError] = useState<string | null>(null);

  const load = async () => {
    setError(null);
    try { setBackups((await api.systemStatus()).backups); }
    catch (caught) { setError(String(caught)); }
  };
  useEffect(() => { void load(); }, []);

  const backup = async () => {
    setBusy(true);
    setError(null);
    try { setBackups((await api.backupNow()).backups); }
    catch (caught) { setError(String(caught)); }
    finally { setBusy(false); }
  };
  const restore = async (item: BackupInfo) => {
    setSelected(item);
    setVerification(null);
    setRestoreError(null);
    if (!item.name.endsWith(".zip")) return;
    setBusy(true);
    try { setVerification(await api.verifyBackup(item.name)); }
    catch (caught) { setRestoreError(String(caught)); }
    finally { setBusy(false); }
  };
  const download = async () => {
    if (!selected || !verification?.verified) return;
    setBusy(true);
    try { await downloadReport(`/api/system/backups/${encodeURIComponent(selected.name)}/download`, selected.name); }
    catch (caught) { setRestoreError(String(caught)); }
    finally { setBusy(false); }
  };

  return <section className="panel settings-section">
    <h2>Backups</h2>
    <p className="muted">Tankarr backs up daily and before schema migrations. Recovery bundles include settings and credentials; keep them private. Media files are backed up separately.</p>
    <button type="button" className="btn" disabled={busy} onClick={() => void backup()}>Backup now</button>
    {error ? <p className="banner banner-danger" role="alert">{error} <button type="button" className="btn" onClick={() => void load()}>Retry backup list</button></p> : null}
    {!backups && !error ? <Spinner /> : null}
    {backups?.length === 0 ? <p>No backups yet.</p> : null}
    {backups?.length ? <div className="data-table-frame"><table className="table responsive-list-table"><thead><tr><th>Backup</th><th>Created</th><th>Actions</th></tr></thead><tbody>
      {backups.map((item) => <tr key={item.name}>
        <td data-label="Backup"><span className="mono small">{item.name}</span><p>{formatBytes(item.size)}</p></td>
        <td data-label="Created">{formatDate(item.created_at)}</td>
        <td data-label="Actions"><button type="button" className="btn" disabled={busy} aria-label={`Restore ${item.name}`} onClick={() => void restore(item)}>Restore</button></td>
      </tr>)}
    </tbody></table></div> : null}
    {selected ? <Modal title="Restore backup" onClose={() => { if (!busy) setSelected(null); }} footer={<>
      <button type="button" className="btn" disabled={busy} onClick={() => setSelected(null)}>Close</button>
      {verification?.verified ? <button type="button" className="btn btn-primary" disabled={busy} onClick={() => void download()}>Download sensitive backup</button> : null}
    </>}>
      <p className="mono">{selected.name}</p>
      {busy && !verification ? <Spinner /> : null}
      {verification ? <p role="status">{verification.verified ? "Backup integrity verified." : "Backup verification failed. Choose another backup."}</p> : null}
      {restoreError ? <p className="banner banner-danger" role="alert">{restoreError}</p> : null}
      {!selected.name.endsWith(".zip") ? <p>This legacy database snapshot requires administrator recovery. Create a current recovery bundle for the guided restore procedure.</p> : verification?.verified ? <>
        <p>Restore this bundle offline into a new directory. It contains credentials and private application data. Download it to a trusted device.</p>
        <ol>
          <li>Keep a current backup and stop the target Tankarr instance before switching its configuration.</li>
          <li>Restore to a new directory that does not exist yet: <pre style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>python -m tankarr.backups --restore /backups/tankarr-backup.zip --destination /data/tankarr-restored</pre>Replace the bundle path with the downloaded backup.</li>
          <li>Start with <code>TANKARR_DATA_DIR=/data/tankarr-restored</code>, verify the restored configuration and library in safe mode, then explicitly re-enable automation.</li>
        </ol>
        <p className="muted">This dialog never overwrites the running database. Media and the Suwayomi runtime need separate recovery.</p>
      </> : null}
    </Modal> : null}
  </section>;
}
