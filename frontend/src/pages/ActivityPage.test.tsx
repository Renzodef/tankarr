import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { api } from "../api";
import { AppContext } from "../components";
import type { TorrentDownload } from "../types";
import ActivityPage from "./ActivityPage";

const release: TorrentDownload = {
  id: 7, manga_id: "series-one", manga_title: "Example series", manga_cover_url: null,
  manga_source_name: null, title: "Example release", status: "completed", imported_paths: [],
  progress: 1, size_bytes: 512, source: "prowlarr", protocol: "torrent", message: "Downloaded",
  source_id: "fixture-release", info_hash: "fixture-hash", language: "en", category: "manga",
  indexer: null, seeders: 1, leechers: 0, trusted: false, remake: false, volume_hint: null,
  chapter_hint: null, publish_at: null, source_url: "", qbit_state: null, content_path: null,
  language_evidence: {}, client_url: null, review_reason: null,
  created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z",
};
const notify = vi.fn();
beforeEach(() => {
  notify.mockReset();
  vi.spyOn(api, "jobsSummary").mockResolvedValue({ active: 0, counts: {}, series: [] });
  vi.spyOn(api, "jobs").mockResolvedValue([]);
  vi.spyOn(api, "torrents").mockResolvedValue([release]);
});
afterEach(cleanup);

function activity() {
  render(<AppContext.Provider value={{ health: null, notify, refreshJobs: vi.fn(), refreshHealth: vi.fn() }}><ActivityPage /></AppContext.Provider>);
}

it.each(["language", "content"] as const)("does not ask the operator to decide a release's %s review", async (reason) => {
  vi.mocked(api.torrents).mockResolvedValue([{ ...release, review_reason: reason }]);
  activity();
  await screen.findByRole("link", { name: "Example series" });
  fireEvent.click(screen.getByTitle("Show items"));
  expect(screen.queryByRole("button", { name: "Confirm English" })).toBeNull();
  expect(screen.queryByRole("button", { name: "Choose files" })).toBeNull();
  expect(screen.queryByRole("option", { name: /Needs review/ })).toBeNull();
  expect(screen.getByTitle("Import now")).toBeTruthy();
});

it("does not present legacy review downloads as active decisions", async () => {
  vi.mocked(api.torrents).mockResolvedValue([{ ...release, status: "review", review_reason: "content" }]);
  activity();
  await screen.findByText("The queue is empty");
  expect(screen.queryByRole("button", { name: "Choose files" })).toBeNull();
  expect(screen.queryByRole("option", { name: /Needs review/ })).toBeNull();
});

it("reports a rejected import as a failure and keeps staging removal explicit", async () => {
  vi.spyOn(api, "importTorrent").mockResolvedValue({ ...release, status: "failed", message: "No unambiguous edition found; staging retained." });
  vi.spyOn(api, "discardTorrent").mockResolvedValue({ id: 7, discarded: true, torrent_files_deleted: true });
  vi.spyOn(window, "confirm").mockReturnValue(false);
  activity();
  await screen.findByRole("link", { name: "Example series" });
  fireEvent.click(screen.getByTitle("Show items"));
  fireEvent.click(screen.getByTitle("Import now"));
  await waitFor(() => expect(notify).toHaveBeenCalledWith("error", "No unambiguous edition found; staging retained."));
  fireEvent.click(screen.getByTitle("Remove release and staging files"));
  expect(window.confirm).toHaveBeenCalledTimes(1);
  expect(api.discardTorrent).not.toHaveBeenCalled();
});
