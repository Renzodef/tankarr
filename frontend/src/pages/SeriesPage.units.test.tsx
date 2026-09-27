import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { api } from "../api";
import { AppContext } from "../components";
import type { Chapter, Manga, SeriesUnits } from "../types";
import SeriesPage, { FileDeleteModal } from "./SeriesPage";

const release = { id: "chapter-file-one", chapter: "1", volume: "1", language: "en", title: "Example chapter", library_path: "/srv/library/example/chapter-1.cbz" } as Chapter;
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("shows the scoped files and requires confirmation before retiring duplicates", () => {
  const confirm = vi.fn();
  render(<FileDeleteModal target={{ kind: "duplicates", volume: "1", chapters: [release] }} busy={false} onClose={vi.fn()} onConfirm={confirm} />);
  expect(screen.getByText("/srv/library/example/chapter-1.cbz")).toBeTruthy();
  expect(screen.getByText("File ID: chapter-file-one")).toBeTruthy();
  const submit = screen.getByRole("button", { name: "Move to recycle bin" }) as HTMLButtonElement;
  expect(submit.disabled).toBe(true);
  fireEvent.click(submit);
  expect(confirm).not.toHaveBeenCalled();
  fireEvent.click(screen.getByLabelText("I reviewed these files and confirm moving them to the recycle bin"));
  fireEvent.click(submit);
  expect(confirm).toHaveBeenCalledOnce();
  expect(screen.getByText(/configured retention period before automatic cleanup/)).toBeTruthy();
});

it("retains the existing permanent confirmation and full file list for volume deletion", () => {
  render(<FileDeleteModal target={{ kind: "volume", volume: "1", language: "en", chapters: [release] }} busy={false} onClose={vi.fn()} onConfirm={vi.fn()} />);
  expect(screen.getByRole("button", { name: "Delete files" }).hasAttribute("disabled")).toBe(true);
  expect(screen.getByLabelText("I understand and confirm this permanent file deletion")).toBeTruthy();
  expect(screen.getByText("File ID: chapter-file-one")).toBeTruthy();
  expect(screen.queryByRole("button", { name: "Move to recycle bin" })).toBeNull();
});

it("previews a specific book and sends only reviewed IDs with recycle enabled", async () => {
  const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify({ chapters: [release], count: 1 }), { status: 200 }));
  vi.stubGlobal("fetch", fetcher);
  await api.duplicateFiles("series/one", "1.5");
  const previewUrl = new URL(fetcher.mock.calls[0][0], "https://tankarr.example");
  expect(previewUrl.pathname).toBe("/api/manga/series%2Fone/duplicates");
  expect(previewUrl.searchParams.get("volume")).toBe("1.5");
  fetcher.mockResolvedValue(new Response(JSON.stringify({ files_retired: 2 }), { status: 200 }));
  await api.retireBookDuplicates("series/one", "1.5", ["file-one", "file-two"]);
  const [path, options] = fetcher.mock.calls[1];
  const url = new URL(path, "https://tankarr.example");
  expect(url.searchParams.get("volume")).toBe("1.5");
  expect(url.searchParams.get("recycle")).toBe("true");
  expect(url.searchParams.getAll("expected_chapter_id")).toEqual(["file-one", "file-two"]);
  expect(options.method).toBe("DELETE");
});

it("ignores a late units response after navigating to another series", async () => {
  let oldResponse!: (value: SeriesUnits) => void;
  const oldUnits = new Promise<SeriesUnits>((resolve) => { oldResponse = resolve; });
  const unitData = (id: string, warning: string): SeriesUnits => ({ manga_id: id, mode: "flat", series_unit: "chapters", edition_book_count: null, expected_book_count: null, warning, hints: [], books: [], unassigned_chapters: [] });
  vi.spyOn(api, "seriesUnits").mockImplementation((id) => id === "old" ? oldUnits : Promise.resolve(unitData(id, "New series boundaries")));
  vi.spyOn(api, "manga").mockImplementation(async (id) => ({ id, title: `Series ${id}`, authors: [], description: "", preferred_language: "en", provider: "local", chapter_count: 0, downloaded_count: 0, chapters: [], monitored: true, monitor_mode: "all", available_languages: ["en"] } as unknown as Manga));
  vi.spyOn(api, "jobs").mockResolvedValue([]);
  vi.spyOn(api, "seriesCalendar").mockRejectedValue(new Error("Unavailable"));
  vi.spyOn(api, "readerLink").mockRejectedValue(new Error("Unavailable"));
  const context = { health: null, notify: vi.fn(), refreshJobs: vi.fn().mockResolvedValue(undefined), refreshHealth: vi.fn().mockResolvedValue(undefined) };
  const page = (id: string) => <AppContext.Provider value={context}><SeriesPage id={id} /></AppContext.Provider>;
  const view = render(page("old"));
  // The header must become usable before the slower book grouping responds.
  expect(await screen.findByRole("heading", { name: "Series old" })).toBeTruthy();
  view.rerender(page("new"));
  await screen.findByText(/New series boundaries/);
  await act(async () => { oldResponse(unitData("old", "Old series boundaries")); await oldUnits; });
  expect(screen.getByRole("heading", { name: "Series new" })).toBeTruthy();
  expect(screen.queryByText(/Old series boundaries/)).toBeNull();
});

it("reports the actual book search outcome instead of a generic success", async () => {
  const units: SeriesUnits = {
    manga_id: "example", mode: "flat", series_unit: "volumes", edition_book_count: 1, expected_book_count: 1,
    warning: null, hints: [], unassigned_chapters: [], books: [{ key: "volume:1", volume: "1", expected: true, owned: false, monitored: true, ignored: false, volume_monitor_state: "automatic", status: "missing", pages: null, files: [], open_release_id: null, suspect: false, retirement_note: null, exact: false, chapter_range: null, chapters: [], chapter_count: 0, downloaded_chapter_count: 0, missing_chapter_count: 0, duplicate_file_count: 0, duplicate_release_ids: [], covered_by_chapters: false, can_assemble: false, collapsed: false }],
  };
  vi.spyOn(api, "seriesUnits").mockResolvedValue(units);
  vi.spyOn(api, "manga").mockResolvedValue({ id: "example", title: "Example", authors: [], description: "", preferred_language: "en", provider: "local", chapter_count: 0, downloaded_count: 0, chapters: [], monitored: true, monitor_mode: "all", available_languages: ["en"] } as unknown as Manga);
  vi.spyOn(api, "jobs").mockResolvedValue([]);
  vi.spyOn(api, "seriesCalendar").mockRejectedValue(new Error("Unavailable"));
  vi.spyOn(api, "readerLink").mockRejectedValue(new Error("Unavailable"));
  vi.spyOn(api, "automaticSearchChapter").mockResolvedValue({ manga_id: "example", chapter: null, volume: "1", state: "no_match", queued: 0, grabbed: 0, message: "No matching releases found.", errors: [] });
  const context = { health: null, notify: vi.fn(), refreshJobs: vi.fn().mockResolvedValue(undefined), refreshHealth: vi.fn().mockResolvedValue(undefined) };
  render(<AppContext.Provider value={context}><SeriesPage id="example" /></AppContext.Provider>);
  fireEvent.click(await screen.findByRole("button", { name: "Automatically search book 1" }));
  await waitFor(() => expect(context.notify).toHaveBeenCalledWith("info", "No matching releases found."));
  expect(api.automaticSearchChapter).toHaveBeenCalledWith("example", null, "1");
});

it("opens the series audit only from its explicit action", async () => {
  vi.spyOn(api, "seriesUnits").mockResolvedValue({ manga_id: "example", mode: "flat", series_unit: "chapters", edition_book_count: null, expected_book_count: null, warning: null, hints: [], books: [], unassigned_chapters: [] });
  vi.spyOn(api, "manga").mockResolvedValue({ id: "example", title: "Example", authors: [], description: "", preferred_language: "en", provider: "local", chapter_count: 0, downloaded_count: 0, chapters: [], monitored: true, monitor_mode: "all", available_languages: ["en"] } as unknown as Manga);
  vi.spyOn(api, "jobs").mockResolvedValue([]);
  vi.spyOn(api, "seriesCalendar").mockRejectedValue(new Error("Unavailable"));
  vi.spyOn(api, "readerLink").mockRejectedValue(new Error("Unavailable"));
  vi.spyOn(api, "seriesAudit").mockResolvedValue({ manga_id: "example", revision: "a".repeat(64), files: [], sources: [] });
  const context = { health: null, notify: vi.fn(), refreshJobs: vi.fn().mockResolvedValue(undefined), refreshHealth: vi.fn().mockResolvedValue(undefined) };
  render(<AppContext.Provider value={context}><SeriesPage id="example" /></AppContext.Provider>);
  const action = await screen.findByRole("button", { name: "Audit files" });
  expect(api.seriesAudit).not.toHaveBeenCalled();
  fireEvent.click(action);
  await screen.findByRole("dialog", { name: "Audit files · Example" });
  expect(await screen.findByText("No downloaded files to audit.")).toBeTruthy();
  expect(api.seriesAudit).toHaveBeenCalledWith("example", expect.any(AbortSignal));
});


it("shows the bookmarked book and page, and refreshes after returning from another tab", async () => {
  vi.spyOn(api, "seriesUnits").mockResolvedValue({ manga_id: "example", mode: "flat", series_unit: "chapters", edition_book_count: null, expected_book_count: null, warning: null, hints: [], books: [], unassigned_chapters: [] });
  vi.spyOn(api, "manga").mockResolvedValue({ id: "example", title: "Example", authors: [], description: "", preferred_language: "en", provider: "local", chapter_count: 1, downloaded_count: 1, chapters: [], monitored: true, monitor_mode: "all", available_languages: ["en"] } as unknown as Manga);
  vi.spyOn(api, "jobs").mockResolvedValue([]);
  vi.spyOn(api, "seriesCalendar").mockRejectedValue(new Error("Unavailable"));
  const link = { reader: "tankarr" as const, configured: true, available: true, url: "#/reader/book-1", bookmark: null };
  const reader = vi.spyOn(api, "readerLink").mockResolvedValue(link);
  const context = { health: null, notify: vi.fn(), refreshJobs: vi.fn().mockResolvedValue(undefined), refreshHealth: vi.fn().mockResolvedValue(undefined) };
  const page = <AppContext.Provider value={context}><SeriesPage id="example" /></AppContext.Provider>;
  const view = render(page);
  await screen.findByText("No bookmark saved · use Save bookmark in the reader");
  const read = screen.getByRole("link", { name: "Read" });
  expect(read.getAttribute("target")).toBe("_blank");
  expect(read.getAttribute("rel")).toBe("noreferrer");
  expect(screen.queryByRole("link", { name: "Continue from bookmark" })).toBeNull();
  reader.mockResolvedValue({ ...link, bookmark: { chapter_id: "book-2", label: "Volume 2", page_index: 41, url: "#/reader/book-2?page=42" } });
  fireEvent.focus(window);
  const resume = await screen.findByRole("link", { name: "Continue from bookmark" });
  expect(resume.getAttribute("href")).toBe("#/reader/book-2?page=42");
  expect(resume.getAttribute("target")).toBe("_blank");
  expect(resume.getAttribute("rel")).toBe("noreferrer");
  expect(screen.getByText("Volume 2 · page 42")).toBeTruthy();
  view.unmount();
  render(page);
  expect((await screen.findByRole("link", { name: "Continue from bookmark" })).getAttribute("href")).toBe("#/reader/book-2?page=42");
});
