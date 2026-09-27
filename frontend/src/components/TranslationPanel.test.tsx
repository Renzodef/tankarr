import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import TranslationPanel from "./TranslationPanel";
import { api } from "../api";
import type { Manga } from "../types";

vi.mock("../api", () => ({ api: { translations: vi.fn(), searchTranslations: vi.fn(), retryTranslation: vi.fn(), cancelTranslation: vi.fn() } }));
vi.mock("../components", () => ({ LANGUAGES: [["en", "English"], ["ja", "Japanese"]], Spinner: () => null, languageName: (language: string) => language, useApp: () => ({ notify: vi.fn() }) }));
const manga = { id: "work", preferred_language: "en", translation_enabled: true } as Manga;
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
