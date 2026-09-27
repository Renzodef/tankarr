import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { api } from "../api";
import { AppContext } from "../components";
import * as download from "../downloadReport";
import SettingsPage from "../pages/SettingsPage";
import type { BackupInfo, SystemStatus } from "../types";
import BackupPanel from "./BackupPanel";

const backup: BackupInfo = { name: "tankarr-example.zip", size: 512, created_at: "2026-01-01T00:00:00Z" };
beforeEach(() => {
  window.history.replaceState(null, "", "#/settings?tab=data");
  vi.spyOn(api, "systemStatus").mockResolvedValue({ backups: [backup] } as SystemStatus);
  vi.spyOn(api, "getSettings").mockResolvedValue({});
  vi.spyOn(api, "verifyBackup").mockResolvedValue({ verified: true, format: "tankarr-application-v1", contains_secrets: true, files: ["tankarr.sqlite3"], excluded: ["media"] });
  vi.spyOn(download, "downloadReport").mockResolvedValue();
});
afterEach(cleanup);

it("Settings has a Data tab and no Tools panels or library-list controls", async () => {
  render(<AppContext.Provider value={{ health: null, notify: vi.fn(), refreshHealth: vi.fn(), refreshJobs: vi.fn() }}><SettingsPage /></AppContext.Provider>);
  await screen.findByRole("button", { name: "Backup now" });
  const navigation = screen.getByRole("navigation", { name: "Settings sections" });
  expect(within(navigation).getByRole("button", { name: "Data" })).toBeTruthy();
  expect(within(navigation).queryByRole("button", { name: /tools/i })).toBeNull();
  expect(screen.queryByText("Library lists")).toBeNull();
  expect(screen.queryByText("Acquisition decisions & reader alignment")).toBeNull();
});

it("Restore verifies first and offers offline instructions with an explicit download", async () => {
  render(<BackupPanel />);
  fireEvent.click(await screen.findByRole("button", { name: `Restore ${backup.name}` }));
  const dialog = screen.getByRole("dialog", { name: "Restore backup" });
  await within(dialog).findByText("Backup integrity verified.");
  expect(api.verifyBackup).toHaveBeenCalledWith(backup.name);
  expect(dialog.textContent).toContain("--destination /data/tankarr-restored");
  expect(dialog.textContent).toContain("never overwrites the running database");
  expect(download.downloadReport).not.toHaveBeenCalled();
  fireEvent.click(within(dialog).getByRole("button", { name: "Download sensitive backup" }));
  await waitFor(() => expect(download.downloadReport).toHaveBeenCalledWith(`/api/system/backups/${backup.name}/download`, backup.name));
});

it("does not offer a download when verification fails", async () => {
  vi.mocked(api.verifyBackup).mockRejectedValue(new Error("Invalid manifest"));
  render(<BackupPanel />);
  fireEvent.click(await screen.findByRole("button", { name: `Restore ${backup.name}` }));
  await screen.findByRole("alert");
  expect(screen.queryByRole("button", { name: "Download sensitive backup" })).toBeNull();
  expect(download.downloadReport).not.toHaveBeenCalled();
});

it("Backup now refreshes the displayed list from the creation response", async () => {
  const newest = { ...backup, name: "tankarr-latest.zip" };
  vi.spyOn(api, "backupNow").mockResolvedValue({ name: newest.name, size: newest.size, backups: [newest, backup] });
  render(<BackupPanel />);
  await screen.findByRole("button", { name: `Restore ${backup.name}` });
  fireEvent.click(screen.getByRole("button", { name: "Backup now" }));
  await screen.findByRole("button", { name: `Restore ${newest.name}` });
  expect(api.backupNow).toHaveBeenCalledTimes(1);
});
