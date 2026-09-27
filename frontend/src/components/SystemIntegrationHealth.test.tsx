import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";
import type { LibraryAlignment, MetadataStatus } from "../types";
import SystemIntegrationHealth from "./SystemIntegrationHealth";

const metadata: MetadataStatus = {
  enabled: true, running: false, current_manga_id: null, last_cycle_at: null,
  last_cycle_error: null, refresh_interval_hours: 24, sources: [],
  coverage: {
    series_total: 164, series_enriched: 160, series_with_external_metadata: 160,
    series_without_external_metadata: 4, series_with_artwork: 163, series_synced: 160,
    series_errors: 0, volumes_enriched: 350, volumes_with_artwork: 318, source_records: 0,
    sync_errors: 0,
  },
};
afterEach(cleanup);

function integrations(alignment: LibraryAlignment | null, currentMetadata = metadata) {
  return render(<SystemIntegrationHealth alignment={alignment} ntfyConfigured metadata={currentMetadata} />);
}

it("shows a restarting reader as waiting with its configured name", () => {
  const view = integrations({ reader: "stump", configured: true, ready: false });
  expect(screen.getByText("Stump · Waiting")).toBeTruthy();
  expect(view.container.textContent).not.toMatch(/Komga|blocked|Error/);
  expect(view.container.querySelector(".pill-danger")).toBeNull();
});

it("reports healthy integrations without turning catalogue gaps into tasks", () => {
  const view = integrations({ reader_label: "Stump", configured: true, ready: true, expected_books: 20, matched_expected_books: 18, missing_books: 2, stale_books: 1 });
  expect(screen.getByText("Stump · OK")).toBeTruthy();
  expect(screen.getAllByText("OK")).toHaveLength(2);
  expect(view.container.textContent).not.toMatch(/coverage|unmatched|artwork|behind|163|318|18\/20/i);
  expect(screen.queryByRole("button", { name: /sync/i })).toBeNull();
});

it("shows reader and metadata error details only for reported failures", () => {
  integrations({ reader: "kavita", configured: true, ready: false, error: "Authentication rejected" }, { ...metadata, last_cycle_error: "Catalogue connection failed" });
  expect(screen.getByText("Kavita · Error")).toBeTruthy();
  expect(screen.getByText("Authentication rejected")).toBeTruthy();
  expect(screen.getByText("Catalogue connection failed")).toBeTruthy();
});

it("uses a neutral label while the reader name is unavailable", () => {
  const view = integrations({ configured: true, ready: false });
  expect(screen.getByText("Reader · Waiting")).toBeTruthy();
  expect(view.container.textContent).not.toContain("Komga");
});

it("keeps a reader that scans independently healthy without synchronization chores", () => {
  const view = integrations({ reader: "stump", reader_independent: true, configured: false, ready: true });
  expect(screen.getByText("Stump · OK")).toBeTruthy();
  expect(view.container.textContent).not.toMatch(/scan|sync/i);
});

it("distinguishes disabled integrations from errors", () => {
  render(<SystemIntegrationHealth alignment={{ ready: false, configured: false }} ntfyConfigured={false} metadata={{ ...metadata, enabled: false }} />);
  expect(screen.getByText("No reader configured")).toBeTruthy();
  expect(screen.getByText("Not configured")).toBeTruthy();
  expect(screen.getByText("Disabled")).toBeTruthy();
});
