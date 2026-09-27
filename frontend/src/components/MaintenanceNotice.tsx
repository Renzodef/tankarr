import { formatDate, humanize } from "../components";
import type { MaintenanceStatus } from "../operationTypes";

export function maintenanceErrors(status: MaintenanceStatus) {
  return Object.entries(status.jobs).filter(([name, job]) => name !== "repair" && job.status === "error");
}

export default function MaintenanceNotice({ status }: { status: MaintenanceStatus }) {
  return <>{maintenanceErrors(status).map(([name, job]) => <p key={name} className="banner banner-danger" role="alert">
    {humanize(name)} job: {job.error ?? "The scheduled job failed."}{job.retry_after ? ` Next retry: ${formatDate(job.retry_after)}.` : ""}
  </p>)}</>;
}
