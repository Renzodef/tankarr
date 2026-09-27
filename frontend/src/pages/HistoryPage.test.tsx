import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { api } from "../api";
import { AppContext } from "../components";
import type { Job, TorrentDownload } from "../types";
import HistoryPage from "./HistoryPage";

const job: Job = {
  id: 7, manga_id: "alpha", manga_title: "Alpha", manga_cover_url: null, manga_source_name: null, manga_source_id: null,
  chapter_id: "chapter-two", chapter_volume: null, chapter_number: "2", chapter_title: "Chapter 2", chapter_groups: [], chapter_provider: "mangadex", chapter_source_name: null, chapter_source_url: null,
  requested_language: "en", status: "failed", progress: 0, message: "Chapter download failed.", result_path: null, language_evidence: {}, created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-02T00:00:00Z",
};
const torrent: TorrentDownload = {
  id: 7, manga_id: "beta", manga_title: "Beta", manga_cover_url: null, manga_source_name: null,
  title: "Beta release", status: "imported", imported_paths: ["/srv/library/Beta/Example v12.cbz"], progress: 1, size_bytes: 512, source: "prowlarr", protocol: "torrent", message: "Imported one book.",
  source_id: "fixture-release", info_hash: "fixture-hash", language: "en", category: "manga", indexer: "Example indexer", seeders: 1, leechers: 0, trusted: false, remake: false, volume_hint: "12", chapter_hint: null, publish_at: null, source_url: "", qbit_state: null, content_path: null,
  language_evidence: { import_decisions: { imported_paths: ["/srv/library/Beta/Example v12.cbz"], already_owned_paths: ["Example v01.cbz"], skipped: [{ path: "extras.cbz", reason: "unnumbered" }] } }, client_url: null, review_reason: null,
  created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-03T00:00:00Z",
};
const notify = vi.fn();
beforeEach(() => {
  notify.mockReset();
  vi.spyOn(api, "jobs").mockResolvedValue([job]);
  vi.spyOn(api, "torrents").mockResolvedValue([torrent]);
  vi.spyOn(api, "retryJob").mockResolvedValue(job);
  vi.spyOn(api, "retryTorrent").mockResolvedValue(torrent);
  vi.spyOn(api, "deleteJob").mockResolvedValue(job);
  vi.spyOn(api, "discardTorrent").mockResolvedValue({ id: 7, discarded: true, torrent_files_deleted: false });
});
afterEach(cleanup);
function history() { return render(<AppContext.Provider value={{ health: null, notify, refreshJobs: vi.fn().mockResolvedValue(undefined), refreshHealth: vi.fn().mockResolvedValue(undefined) }}><HistoryPage /></AppContext.Provider>); }
function row(series: string) { return screen.getByRole("link", { name: series }).closest("tr")!; }
function seriesOrder() { return screen.getAllByRole("row").slice(1).map((item) => within(item).getByRole("link").textContent); }

it("merges both histories with overlapping IDs and bounded status-filtered requests", async () => {
  const errors = vi.spyOn(console, "error").mockImplementation(() => undefined);
  history();
  await screen.findByRole("link", { name: "Beta" });
  expect(screen.getByRole("link", { name: "Alpha" })).toBeTruthy();
  expect(seriesOrder()).toEqual(["Beta", "Alpha"]);
  expect(screen.getByText("2 history records")).toBeTruthy();
  expect(api.jobs).toHaveBeenCalledWith(["completed", "failed"], 500);
  expect(api.torrents).toHaveBeenCalledWith(undefined, ["imported", "failed"], 500);
  expect(errors.mock.calls.flat().join(" ")).not.toContain("same key");
});

it("retries jobs and torrents through their own APIs even when IDs overlap", async () => {
  vi.mocked(api.torrents).mockResolvedValue([{ ...torrent, status: "failed" }]);
  history();
  await screen.findByRole("link", { name: "Beta" });
  const releaseRetry = within(row("Beta")).getByTitle("Retry download");
  fireEvent.click(releaseRetry);
  await waitFor(() => expect((releaseRetry as HTMLButtonElement).disabled).toBe(false));
  expect(api.retryTorrent).toHaveBeenCalledWith(7);
  expect(api.retryJob).not.toHaveBeenCalled();
  fireEvent.click(within(row("Alpha")).getByTitle("Retry download"));
  await waitFor(() => expect(api.retryJob).toHaveBeenCalledWith(7, { overrideQuality: false }));
  expect(api.retryTorrent).toHaveBeenCalledOnce();
});

it("keeps torrent history informational and removes only provider job records", async () => {
  history();
  await screen.findByRole("link", { name: "Beta" });
  expect(within(row("Beta")).queryByTitle("Remove from history")).toBeNull();
  fireEvent.click(within(row("Alpha")).getByTitle("Remove from history"));
  await waitFor(() => expect(api.deleteJob).toHaveBeenCalledWith(7));
  expect(api.discardTorrent).not.toHaveBeenCalled();
});

it("preserves state, series, item and direction filters across both sources", async () => {
  history();
  await screen.findByRole("link", { name: "Beta" });
  fireEvent.change(screen.getByLabelText("Filter history by state"), { target: { value: "completed" } });
  expect(seriesOrder()).toEqual(["Beta"]);
  fireEvent.change(screen.getByLabelText("Filter history by state"), { target: { value: "failed" } });
  expect(seriesOrder()).toEqual(["Alpha"]);
  fireEvent.change(screen.getByLabelText("Filter history by state"), { target: { value: "all" } });
  fireEvent.change(screen.getByLabelText("Sort history"), { target: { value: "series" } });
  expect(seriesOrder()).toEqual(["Alpha", "Beta"]);
  fireEvent.click(screen.getByRole("button", { name: "Sort descending" }));
  expect(seriesOrder()).toEqual(["Beta", "Alpha"]);
  fireEvent.change(screen.getByLabelText("Sort history"), { target: { value: "item" } });
  expect(seriesOrder()).toEqual(["Alpha", "Beta"]);
  fireEvent.change(screen.getByLabelText("Filter history by series or pattern"), { target: { value: "EXAMPLE V12" } });
  expect(seriesOrder()).toEqual(["Beta"]);
  fireEvent.change(screen.getByLabelText("Filter history by series or pattern"), { target: { value: "alpha" } });
  expect(seriesOrder()).toEqual(["Alpha"]);
});

it("shows the files imported, already owned and skipped without a confirmation flow", async () => {
  history();
  const summary = await screen.findByText("Files: 1 imported · 1 already owned · 1 skipped");
  const details = summary.closest("details")!;
  details.open = true;
  fireEvent(details, new Event("toggle", { bubbles: true }));
  expect(screen.getByText("Example v12.cbz")).toBeTruthy();
  expect(screen.getByText("Example v01.cbz")).toBeTruthy();
  expect(screen.getByText(/No book or chapter number/)).toBeTruthy();
  expect(screen.queryByRole("button", { name: /Confirm English|Choose files/ })).toBeNull();
});

it("paginates the merged history as one list", async () => {
  vi.mocked(api.jobs).mockResolvedValue(Array.from({ length: 10 }, (_, i) => ({ ...job, id: i, manga_title: `Chapter series ${i}` })));
  vi.mocked(api.torrents).mockResolvedValue(Array.from({ length: 11 }, (_, i) => ({ ...torrent, id: i, manga_title: `Release series ${i}` })));
  history();
  await screen.findByText("21 history records");
  expect(screen.getAllByRole("row")).toHaveLength(21);
  fireEvent.click(screen.getByRole("button", { name: "Next page" }));
  expect(screen.getAllByRole("row")).toHaveLength(2);
});

it("keeps available chapter history visible when release history fails", async () => {
  vi.mocked(api.torrents).mockRejectedValue(new Error("Release database unavailable"));
  history();
  await screen.findByRole("link", { name: "Alpha" });
  expect(screen.getByText(/Release history unavailable/)).toBeTruthy();
  expect(screen.getByRole("button", { name: "Retry history" })).toBeTruthy();
});

it("does not offer a chapter quality override for a torrent with a similar error", async () => {
  vi.mocked(api.jobs).mockResolvedValue([{ ...job, message: "DegradedPagesError: chapter shorter than expected" }]);
  vi.mocked(api.torrents).mockResolvedValue([{ ...torrent, status: "failed", message: "DegradedPagesError: import rejected" }]);
  history();
  await screen.findByRole("link", { name: "Beta" });
  expect(within(row("Beta")).queryByTitle(/Download anyway/)).toBeNull();
  expect(within(row("Alpha")).getByTitle(/Download anyway/)).toBeTruthy();
});

it("ignores a pending history response after unmount", async () => {
  let finish!: (value: TorrentDownload[]) => void;
  const pending = new Promise<TorrentDownload[]>((resolve) => { finish = resolve; });
  vi.mocked(api.torrents).mockReturnValue(pending);
  const { unmount } = history();
  unmount();
  await act(async () => { finish([torrent]); await pending; });
  expect(notify).not.toHaveBeenCalled();
  expect(screen.queryByRole("link", { name: "Beta" })).toBeNull();
});
