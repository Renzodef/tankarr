import { StrictMode } from "react";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { api } from "../api";
import { AppContext } from "../components";
import type { Manga, TorrentSearchResponse, TorrentDownload } from "../types";
import SeriesPage, { SeriesPackSearchPanel } from "./SeriesPage";

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: Error) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
const manga = (id: string) => ({ id, title: `Series ${id}`, authors: [], description: "", preferred_language: "en", provider: "catalogue", chapter_count: 0, downloaded_count: 0, chapters: [], monitored: true, monitor_mode: "all", available_languages: ["en"] } as unknown as Manga);
const empty = { results: [], errors: [] } as unknown as TorrentSearchResponse;
function setup() {
  vi.spyOn(api, "manga").mockImplementation(async (id) => manga(id));
  vi.spyOn(api, "seriesUnits").mockImplementation(async (id) => ({ manga_id: id, mode: "flat", series_unit: "chapters", form: "chapters", edition_book_count: null, expected_book_count: null, warning: null, hints: [], books: [], unassigned_chapters: [] }));
  vi.spyOn(api, "jobs").mockResolvedValue([]);
  vi.spyOn(api, "readerLink").mockRejectedValue(new Error("Unavailable"));
  vi.spyOn(api, "torrents").mockResolvedValue([]);
  return { health: null, notify: vi.fn(), refreshJobs: vi.fn().mockResolvedValue(undefined), refreshHealth: vi.fn().mockResolvedValue(undefined) };
}
afterEach(() => { cleanup(); vi.useRealTimers(); vi.restoreAllMocks(); });

it.each(["response", "error"])("closes a pending search immediately and ignores its late %s after reopening", async (late) => {
  const context = setup();
  const old = deferred<TorrentSearchResponse>();
  const current = deferred<TorrentSearchResponse>();
  const search = vi.spyOn(api, "searchTorrents").mockReturnValueOnce(old.promise).mockReturnValueOnce(current.promise);
  render(<AppContext.Provider value={context}><SeriesPage id="one" /></AppContext.Provider>);
  await screen.findByRole("heading", { name: "Series one" });
  fireEvent.click(screen.getByRole("button", { name: "Series Pack Search" }));
  expect(screen.getAllByRole("heading", { name: "Series Pack Search" })).toHaveLength(1);
  const oldSignal = search.mock.calls[0][2]!;
  fireEvent.click(screen.getByRole("button", { name: "Close" }));
  expect(screen.queryByRole("heading", { name: "Series Pack Search" })).toBeNull();
  expect(oldSignal.aborted).toBe(true);
  fireEvent.click(screen.getByRole("button", { name: "Series Pack Search" }));
  await act(async () => {
    if (late === "error") old.reject(new Error("Old search failed"));
    else old.resolve({ ...empty, errors: [{ provider: "old", error: "Obsolete error" }] });
  });
  expect(context.notify).not.toHaveBeenCalled();
  expect(screen.getAllByRole("heading", { name: "Series Pack Search" })).toHaveLength(1);
  expect(screen.getByText("Searching the configured indexers for series and volume packs…")).toBeTruthy();
  await act(async () => { current.resolve(empty); });
  expect(screen.getByText("No matching torrent release was found by the configured indexers.")).toBeTruthy();
});

it("closes and aborts the old panel when navigating to another series", async () => {
  const context = setup();
  const old = deferred<TorrentSearchResponse>();
  const search = vi.spyOn(api, "searchTorrents").mockReturnValue(old.promise);
  const page = (id: string) => <AppContext.Provider value={context}><SeriesPage id={id} /></AppContext.Provider>;
  const view = render(page("one"));
  await screen.findByRole("heading", { name: "Series one" });
  fireEvent.click(screen.getByRole("button", { name: "Series Pack Search" }));
  view.rerender(page("two"));
  await screen.findByRole("heading", { name: "Series two" });
  expect(search.mock.calls[0][2]!.aborted).toBe(true);
  expect(screen.queryByRole("heading", { name: "Series Pack Search" })).toBeNull();
  await act(async () => { old.resolve(empty); });
  expect(search).toHaveBeenCalledTimes(1);
});

it("restarts an aborted automatic search during StrictMode effect replay", async () => {
  const context = setup();
  const pending = deferred<TorrentSearchResponse>();
  const search = vi.spyOn(api, "searchTorrents").mockReturnValueOnce(pending.promise).mockResolvedValue(empty);
  render(<StrictMode><AppContext.Provider value={context}><SeriesPackSearchPanel manga={manga("one")} open onClose={vi.fn()} /></AppContext.Provider></StrictMode>);
  await screen.findByText("No matching torrent release was found by the configured indexers.");
  expect(search).toHaveBeenCalledTimes(2);
  expect(search.mock.calls[0][2]!.aborted).toBe(true);
  await act(async () => { pending.resolve(empty); });
  expect(context.notify).not.toHaveBeenCalled();
});

it("lets a slow download-status request finish across poll intervals and aborts it on close", async () => {
  vi.useFakeTimers();
  const context = setup();
  const pending = deferred<TorrentDownload[]>();
  const downloads = vi.mocked(api.torrents).mockReturnValue(pending.promise);
  vi.spyOn(api, "searchTorrents").mockResolvedValue(empty);
  const view = render(<AppContext.Provider value={context}><SeriesPackSearchPanel manga={manga("one")} open onClose={vi.fn()} /></AppContext.Provider>);
  await act(async () => { await vi.advanceTimersByTimeAsync(8000); });
  expect(downloads).toHaveBeenCalledTimes(1);
  expect(downloads.mock.calls[0][3]!.aborted).toBe(false);
  view.unmount();
  expect(downloads.mock.calls[0][3]!.aborted).toBe(true);
  await act(async () => { pending.resolve([]); });
  expect(context.notify).not.toHaveBeenCalled();
});
