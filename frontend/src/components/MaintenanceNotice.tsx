import { formatDate, humanize } from "../components";
import type { MaintenanceStatus } from "../operationTypes";
import { t } from "../i18n";

export function maintenanceErrors(status: MaintenanceStatus) {
  return Object.entries(status.jobs).filter(([name, job]) => name !== "repair" && job.status === "error");
}

export default function MaintenanceNotice({ status }: { status: MaintenanceStatus }) {
  return <>{maintenanceErrors(status).map(([name, job]) => <p key={name} className="banner banner-danger" role="alert">
    {t("{job} job: {error}", { job: humanize(name), error: job.error ?? t("The scheduled job failed.") })}{job.retry_after ? " " + t("Next retry: {date}.", { date: formatDate(job.retry_after) }) : ""}
  </p>)}</>;
}
