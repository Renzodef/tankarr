import type { AcquisitionPreview } from "../operationTypes";
import { StatusPill } from "../components";

export function AcquisitionChoices({ preview }: { preview: AcquisitionPreview }) {
  return <div>
    <p>{preview.explanation}</p>
    {(preview.warnings ?? []).map((warning, index) => <p key={index} className="banner banner-warn">{warning}</p>)}
    {preview.truncated ? <p className="muted">Showing {preview.candidates.length} of {preview.total_candidates ?? "more"} candidates.</p> : null}
    {preview.candidates.length === 0 ? <p className="muted">No candidate releases were returned for this preview.</p> : (
      <div className="data-table-frame"><table className="table responsive-list-table">
        <thead><tr><th>Candidate</th><th>Decision</th><th>Evidence</th></tr></thead>
        <tbody>{preview.candidates.map((candidate, index) => <tr key={`${candidate.id}:${index}`}>
          <td data-label="Candidate"><strong>{candidate.title}</strong></td>
          <td data-label="Decision"><StatusPill kind={candidate.selected ? "success" : "muted"}>{candidate.selected ? "Selected" : "Not selected"}</StatusPill></td>
          <td data-label="Evidence">{[...(candidate.reasons ?? []), ...(candidate.rejections ?? [])].map((reason, reasonIndex) => <p key={reasonIndex}>{reason}</p>)}</td>
        </tr>)}</tbody>
      </table></div>
    )}
  </div>;
}
