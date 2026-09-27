import { Component, type ReactNode } from "react";

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
        <h1 className="page-title">This page could not be loaded</h1>
        <p className="muted">
          The connection may have been interrupted, or Tankarr was updated while this tab was open.
          Reload to try again.
        </p>
        <div className="toolbar-group">
          <button type="button" className="btn btn-primary" onClick={() => window.location.reload()}>
            Reload app
          </button>
          <a className="btn" href="#/">Back to library</a>
        </div>
      </section>
    );
  }
}
