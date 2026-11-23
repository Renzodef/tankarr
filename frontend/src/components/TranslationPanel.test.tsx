import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import TranslationPanel from "./TranslationPanel";
import { api } from "../api";
import type { Manga } from "../types";

vi.mock("../api", () => ({ api: { translations: vi.fn(), searchTranslations: vi.fn(), retryTranslation: vi.fn(), cancelTranslation: vi.fn() } }));
vi.mock("../components", () => ({ LANGUAGES: [["en", "English"], ["ja", "Japanese"]], Spinner: () => null, languageName: (language: string) => language, useApp: () => ({ notify: vi.fn() }) }));
const manga = { id: "work", preferred_language: "en" } as Manga;
beforeEach(() => { vi.clearAllMocks(); });
afterEach(cleanup);

test("global pause keeps the queue visible and disables new work", async () => {
  vi.mocked(api.translations).mockResolvedValue({ enabled: false, jobs: [] });
  render(<TranslationPanel manga={manga} />);
  await screen.findByRole("status");
  expect((screen.getByText("Search missing translations") as HTMLButtonElement).disabled).toBe(true);
  expect((screen.getByText("Translate a source CBZ") as HTMLButtonElement).disabled).toBe(true);
});

test("search asks the normal acquisition path before fallback", async () => {
  vi.mocked(api.translations).mockResolvedValue({ enabled: true, jobs: [] });
  vi.mocked(api.searchTranslations).mockResolvedValue({ translations_queued: 1 });
  render(<TranslationPanel manga={manga} />);
  await waitFor(() => expect((screen.getByText("Search missing translations") as HTMLButtonElement).disabled).toBe(false));
  fireEvent.click(screen.getByText("Search missing translations"));
  await waitFor(() => expect(api.searchTranslations).toHaveBeenCalledWith("work"));
});

test("failed jobs show their source language and can be retried", async () => {
  vi.mocked(api.translations).mockResolvedValue({ enabled: true, jobs: [{ id: "job", slot_key: "volume:2", source: { language: "ja" }, target_language: "en", status: "failed", message: "Validation failed" }] });
  render(<TranslationPanel manga={manga} />);
  fireEvent.click(await screen.findByText("Retry"));
  expect(screen.getByText("ja → en")).toBeTruthy();
  await waitFor(() => expect(api.retryTranslation).toHaveBeenCalledWith("job"));
});

test("fulfilled series keeps cancelled and failed attempts under closed history", async () => {
  vi.mocked(api.translations).mockResolvedValue({
    enabled: true,
    jobs: [
      { id: "cancelled", slot_key: "volume:41", source: { language: "zh" }, target_language: "en", status: "cancelled", message: "A release is already in the library" },
      { id: "failed", slot_key: "volume:63", source: { language: "zh" }, target_language: "en", status: "failed", message: "Earlier attempt failed" },
    ],
  });
  const completeSeries = {
    ...manga,
    status: "ended",
    library_count: { expected_count: 72, total_count: 72, downloaded_count: 72, missing_count: 0 },
  } as Manga;
  render(<TranslationPanel manga={completeSeries} />);

  const cancelled = await screen.findByText("A release is already in the library");
  const history = screen.getByText("History").closest("details");
  expect(history?.open).toBe(false);
  expect(cancelled.closest("details")).toBe(history);
  expect(screen.getByText("Earlier attempt failed").closest("details")).toBe(history);
});

test("keeps active jobs and failures for missing books in the main table", async () => {
  vi.mocked(api.translations).mockResolvedValue({
    enabled: true,
    jobs: [
      { id: "running", slot_key: "volume:9", source: { language: "zh" }, target_language: "en", status: "processing", message: "Processing pages" },
      { id: "failed", slot_key: "volume:26", source: { language: "zh" }, target_language: "en", status: "failed", message: "Missing result" },
      { id: "cancelled", slot_key: "volume:25", source: { language: "zh" }, target_language: "en", status: "cancelled", message: "A release is already in the library" },
    ],
  });
  const incompleteSeries = {
    ...manga,
    status: "ended",
    library_count: { expected_count: 37, total_count: 37, downloaded_count: 31, missing_count: 6 },
  } as Manga;
  render(<TranslationPanel manga={incompleteSeries} />);

  expect((await screen.findByText("Processing pages")).closest("details")).toBeNull();
  expect(screen.getByText("Missing result").closest("details")).toBeNull();
  expect(screen.getByText("A release is already in the library").closest("details")?.open).toBe(false);
});
