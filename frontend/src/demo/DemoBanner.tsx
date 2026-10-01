import { t } from "../i18n";

/** The strip above the online demo: a fictional library, nothing is saved. */
export default function DemoBanner({ error }: { error?: string | null }) {
  const install = new URL("../installation/", document.baseURI).toString();
  return (
    <div className="demo-banner" role="note">
      <span>
        <strong>{t("Demo")}</strong>{" "}
        {error ?? t("You are looking at a demo of Tankarr: nothing is downloaded and changes are not saved.")}
      </span>
      <a href={install}>{t("Install Tankarr")}</a>
    </div>
  );
}
