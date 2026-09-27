import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { api } from "../api";
import { AppContext } from "../components";
import type { CalendarRelease, CalendarResponse } from "../types";
import CalendarPage from "./CalendarPage";

const notify = vi.fn();
let sequence = 0;
let payload: CalendarResponse;
beforeEach(() => {
  vi.useFakeTimers();
  // Keep each test's module-level navigation cache in a different window.
  vi.setSystemTime(new Date(2026, 0, 8 + sequence++ * 35, 12));
  vi.spyOn(document, "hidden", "get").mockReturnValue(false);
  notify.mockReset();
  payload = {
    releases: [{
      id: "old-upload", manga_id: "paused-work", manga_title: "Paused work",
      chapter: "205", volume: null, title: "Chapter 205", publish_at: new Date().toISOString(),
      availability_status: "early_available",
    } as CalendarRelease],
    expected: [],
  };
  vi.spyOn(api, "calendar").mockResolvedValue(payload);
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
});
async function calendar() {
  let view!: ReturnType<typeof render>;
  await act(async () => {
    view = render(<AppContext.Provider value={{ health: null, notify, refreshJobs: vi.fn().mockResolvedValue(undefined), refreshHealth: vi.fn().mockResolvedValue(undefined) }}><CalendarPage /></AppContext.Provider>);
  });
  return view;
}

it("removes obsolete release cards while the same week remains open", async () => {
  await calendar();
  expect(screen.getByText("Paused work")).toBeTruthy();
  vi.mocked(api.calendar).mockResolvedValue({ releases: [], expected: [] });
  await act(async () => { await vi.advanceTimersByTimeAsync(120000); });
  expect(api.calendar).toHaveBeenCalledTimes(2);
  expect(screen.queryByText("Paused work")).toBeNull();
  expect(screen.getByText("0 releases this week")).toBeTruthy();
});

it("revalidates a cached week when returning to the page before its TTL", async () => {
  const first = await calendar();
  first.unmount();
  vi.mocked(api.calendar).mockResolvedValue({ releases: [], expected: [] });
  await calendar();
  expect(api.calendar).toHaveBeenCalledTimes(2);
  expect(screen.queryByText("Paused work")).toBeNull();
});

it("waits while hidden and refreshes when the tab becomes visible", async () => {
  await calendar();
  vi.spyOn(document, "hidden", "get").mockReturnValue(true);
  vi.mocked(api.calendar).mockResolvedValue({ releases: [], expected: [] });
  await act(async () => { await vi.advanceTimersByTimeAsync(240000); });
  expect(api.calendar).toHaveBeenCalledTimes(1);
  vi.spyOn(document, "hidden", "get").mockReturnValue(false);
  await act(async () => { fireEvent(document, new Event("visibilitychange")); });
  expect(api.calendar).toHaveBeenCalledTimes(2);
  expect(screen.queryByText("Paused work")).toBeNull();
});

it("deduplicates wake events and stops polling after leaving the page", async () => {
  const view = await calendar();
  let resolve!: (value: CalendarResponse) => void;
  vi.mocked(api.calendar).mockImplementation(() => new Promise((done) => { resolve = done; }));
  await act(async () => {
    fireEvent(window, new Event("focus"));
    fireEvent(document, new Event("visibilitychange"));
    await vi.advanceTimersByTimeAsync(120000);
  });
  expect(api.calendar).toHaveBeenCalledTimes(2);
  view.unmount();
  await act(async () => {
    resolve({ releases: [], expected: [] });
    fireEvent(window, new Event("focus"));
    await vi.advanceTimersByTimeAsync(240000);
  });
  expect(api.calendar).toHaveBeenCalledTimes(2);
  expect(notify).not.toHaveBeenCalled();
});
