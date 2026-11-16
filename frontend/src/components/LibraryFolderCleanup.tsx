import { useState } from "react";
import { api } from "../api";
import { Icon, useApp } from "../components";
import { t } from "../i18n";

export default function LibraryFolderCleanup() {
  const { notify } = useApp();
  const [report, setReport] = useState<Awaited<ReturnType<typeof api.libraryFolderCleanup>> | null>(null);
  const [busy, setBusy] = useState(false);

  async function run(remove = false) {
    setBusy(true);
    try {
      if (remove && report?.empty_folders.length) {
        const { removed, skipped } = await api.deleteEmptyLibraryFolders(report.empty_folders);
        notify("success", t("Empty folders removed: {removed}. Skipped: {skipped}.", {
          removed: removed.length, skipped: skipped.length,
        }));
      }
      setReport(await api.libraryFolderCleanup());
    } catch (caught) {
      notify("error", String(caught));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="panel">
      <h2>{t("Library folder cleanup")}</h2>
      <button type="button" className="btn" disabled={busy} onClick={() => void run()}>
        {t("Check library folders")}
      </button>
      {report && (
        <>
          <p>{t("Empty folders: {empty}. Folders to review: {review}.", {
            empty: report.empty_count, review: report.review_count,
          })}</p>
          {report.empty_folders.length > 0 && (
            <>
              <ul>{report.empty_folders.map((folder) => <li key={folder}>{folder}</li>)}</ul>
              <button type="button" className="btn btn-danger" disabled={busy} onClick={() => void run(true)}>
                <Icon name="trash" /> {t("Remove listed empty folders")}
              </button>
            </>
          )}
          {report.review_folders.length > 0 && (
            <>
              <p>{t("Check these folders for loose files, nested books or storage errors.")}</p>
              <ul>{report.review_folders.map((folder) => <li key={folder}>{folder}</li>)}</ul>
            </>
          )}
        </>
      )}
    </section>
  );
}
