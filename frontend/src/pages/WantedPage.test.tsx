import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { api } from "../api";
import { AppActionsContext } from "../components";
import type { WantedEntry } from "../types";
import WantedPage from "./WantedPage";

const english: WantedEntry = {
  manga: {
    id: "english",
    title: "English series",
    provider: "catalogue",
    source_name: null,
    cover_url: null,
    preferred_language: "en",
    monitor_mode: "all",
  },
  chapters: Array.from({ length: 21 }, (_, index) => ({
    id: `expected-${index + 1}`,
    volume: String(index + 1),
    chapter: null,
    title: `Expected volume ${index + 1}`,
    provider: "expected",
    source_name: null,
    publish_at: null,
  })),
  unmapped_expected_count: 0,
  expected_count: 21,
  expected_source: null,
};

const italian: WantedEntry = {
  ...english,
  manga: { ...english.manga, id: "italian", title: "Italian series", preferred_language: "it" },
  chapters: [],
  no_sources: true,
  expected_count: null,
};

afterEach(cleanup);

it("filters wanted rows by reading language before pagination and combines with the item filter", async () => {
  vi.spyOn(api, "wantedSnapshot").mockReturnValue(undefined);
  vi.spyOn(api, "wanted").mockResolvedValue([english, italian]);
  vi.spyOn(api, "monitorStatus").mockResolvedValue({
    enabled: false, interval_seconds: 0, running: false, last_cycle_at: null, last_cycle_error: null,
    wanted_search: { enabled: false, interval_seconds: 0, last_search_at: null, next_search_at: null, last_result: null },
  });
  render(
    <AppActionsContext.Provider value={{ notify: vi.fn(), refreshJobs: vi.fn(), refreshHealth: vi.fn() }}>
      <WantedPage />
    </AppActionsContext.Provider>,
  );

  const language = await screen.findByRole("combobox", { name: "Filter wanted items by language" });
  expect(screen.getByText("22 actionable rows")).toBeTruthy();
  expect(screen.getByRole("option", { name: "Language: Italiano" })).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Next page" }));
  expect(screen.getByRole("link", { name: "Italian series" })).toBeTruthy();

  fireEvent.change(language, { target: { value: "en" } });
  expect(screen.getByText("21 of 22 rows")).toBeTruthy();
  expect(screen.getByText("Expected volume 1")).toBeTruthy();
  expect(screen.queryByText("Expected volume 21")).toBeNull();

  fireEvent.change(language, { target: { value: "it" } });
  expect(screen.getByRole("link", { name: "Italian series" })).toBeTruthy();
  expect(screen.queryByRole("link", { name: "English series" })).toBeNull();

  fireEvent.change(screen.getByRole("combobox", { name: "Filter wanted items by type" }), { target: { value: "volume" } });
  expect(screen.getByText("No wanted items match this filter")).toBeTruthy();
  fireEvent.change(language, { target: { value: "" } });
  expect(screen.getByText("21 of 22 rows")).toBeTruthy();
});
