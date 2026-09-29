import { Component, type ReactNode } from "react";
import { t } from "../i18n";

export class RouteErrorBoundary extends Component<
  { children: ReactNode },
  { failed: boolean }
> {
  state = { failed: false };

  static getDerivedStateFromError() {
    return { failed: true };
  }

  render() {
    if (!this.state.failed) return this.props.children;
    return (
      <section className="page" role="alert">
        <h1 className="page-title">{t("This page could not be loaded")}</h1>
        <p className="muted">
          {t("The connection may have been interrupted, or Tankarr was updated while this tab was open. Reload to try again.")}
        </p>
        <div className="toolbar-group">
          <button type="button" className="btn btn-primary" onClick={() => window.location.reload()}>
            {t("Reload app")}
          </button>
          <a className="btn" href="#/">{t("Back to library")}</a>
        </div>
      </section>
    );
  }
}
