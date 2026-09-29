import type { AcquisitionPreview } from "../operationTypes";
import { StatusPill } from "../components";
import { t } from "../i18n";

export function AcquisitionChoices({ preview }: { preview: AcquisitionPreview }) {
  return <div>
    <p>{preview.explanation}</p>
    {(preview.warnings ?? []).map((warning, index) => <p key={index} className="banner banner-warn">{warning}</p>)}
    {preview.truncated ? <p className="muted">{preview.total_candidates != null ? t("Showing {shown} of {total} candidates.", { shown: preview.candidates.length, total: preview.total_candidates }) : t("Showing the first {shown} candidates.", { shown: preview.candidates.length })}</p> : null}
    {preview.candidates.length === 0 ? <p className="muted">{t("No candidate releases were returned for this preview.")}</p> : (
      <div className="data-table-frame"><table className="table responsive-list-table">
        <thead><tr><th>{t("Candidate")}</th><th>{t("Decision")}</th><th>{t("Evidence")}</th></tr></thead>
        <tbody>{preview.candidates.map((candidate, index) => <tr key={`${candidate.id}:${index}`}>
          <td data-label={t("Candidate")}><strong>{candidate.title}</strong></td>
          <td data-label={t("Decision")}><StatusPill kind={candidate.selected ? "success" : "muted"}>{candidate.selected ? t("Selected") : t("Not selected")}</StatusPill></td>
          <td data-label={t("Evidence")}>{[...(candidate.reasons ?? []), ...(candidate.rejections ?? [])].map((reason, reasonIndex) => <p key={reasonIndex}>{reason}</p>)}</td>
        </tr>)}</tbody>
      </table></div>
    )}
  </div>;
}
