import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { api } from "../api";
import { AppContext } from "../components";
import type { Manga } from "../types";
import { EditModal } from "./SeriesPage";

afterEach(cleanup);
it("submits a main-chapter total below the count including decimals for server validation", async () => {
  const item = {
    id: "edition", title: "Example", authors: [], preferred_language: "en", monitor_mode: "all",
    available_languages: ["en"], downloaded_count: 47, chapter_count: 45,
    expected_count_override: 45, expected_count_unit_override: "chapter",
    library_count: { unit: "chapter", downloaded_count: 45, total_count: 45 },
  } as unknown as Manga;
  vi.spyOn(api, "updateManga").mockResolvedValue(item);
  const saved = vi.fn().mockResolvedValue(undefined);
  render(<AppContext.Provider value={{ health: null, notify: vi.fn(), refreshJobs: vi.fn().mockResolvedValue(undefined), refreshHealth: vi.fn().mockResolvedValue(undefined) }}><EditModal manga={item} onClose={vi.fn()} onSaved={saved} onOpenCover={vi.fn()} onOpenMetadata={vi.fn()} /></AppContext.Provider>);
  const input = screen.getByLabelText("Managed edition total") as HTMLInputElement;
  fireEvent.change(input, { target: { value: "44" } });
  expect(input.validity.valid).toBe(true);
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
  await waitFor(() => expect(saved).toHaveBeenCalledOnce());
  expect(api.updateManga).toHaveBeenCalledWith("edition", { expected_count_override: 44 });
});

it("does not reuse a chapter override as a volume total or clear it on an unrelated save", async () => {
  const item = {
    id: "books", title: "Example", authors: [], preferred_language: "en", monitor_mode: "all",
    available_languages: ["en"], downloaded_count: 3, chapter_count: 30,
    expected_count_override: 27, expected_count_unit_override: "chapter",
    library_count: { unit: "volume", downloaded_count: 3, total_count: 3 },
  } as unknown as Manga;
  const update = vi.spyOn(api, "updateManga").mockResolvedValue(item);
  update.mockClear();
  const saved = vi.fn().mockResolvedValue(undefined);
  render(<AppContext.Provider value={{ health: null, notify: vi.fn(), refreshJobs: vi.fn().mockResolvedValue(undefined), refreshHealth: vi.fn().mockResolvedValue(undefined) }}><EditModal manga={item} onClose={vi.fn()} onSaved={saved} onOpenCover={vi.fn()} onOpenMetadata={vi.fn()} /></AppContext.Provider>);
  const mode = document.querySelector("#series-expected-count-mode") as HTMLSelectElement;
  expect(mode.value).toBe("automatic");
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
  await waitFor(() => expect(saved).toHaveBeenCalledOnce());
  expect(update).not.toHaveBeenCalled();
  cleanup();
  render(<AppContext.Provider value={{ health: null, notify: vi.fn(), refreshJobs: vi.fn().mockResolvedValue(undefined), refreshHealth: vi.fn().mockResolvedValue(undefined) }}><EditModal manga={item} onClose={vi.fn()} onSaved={saved} onOpenCover={vi.fn()} onOpenMetadata={vi.fn()} /></AppContext.Provider>);
  fireEvent.change(document.querySelector("#series-expected-count-mode")!, { target: { value: "manual" } });
  expect((screen.getByLabelText("Managed edition total") as HTMLInputElement).value).toBe("3");
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
  await waitFor(() => expect(update).toHaveBeenCalledWith("books", { expected_count_override: 3 }));
});
