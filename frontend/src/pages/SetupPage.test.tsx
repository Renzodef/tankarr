import { act, cleanup, fireEvent, render, renderHook, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../api";
import { legacyToolsRedirect, parseRoute, useHashRoute } from "../components";
import type { PreflightReport } from "../operationTypes";
import { needsSetup, useSetupGate } from "../useSetupGate";
import { useUnsavedChangesGuard } from "../useUnsavedChangesGuard";
import SetupPage from "./SetupPage";

const pendingSettings = { setup_completed_at: { value: null, secret: false, overridden: false } };
const failed: PreflightReport = { ready: false, checks: [{ id: "library_identity", label: "Library identity", status: "error", detail: "Library mount unavailable", settings_tab: "general" }] };
const ready: PreflightReport = { ready: true, checks: [{ id: "reader", label: "Reader", status: "warning", detail: "No reader selected", settings_tab: "reader" }] };

beforeEach(() => {
  window.sessionStorage.clear();
  window.history.replaceState(null, "", "#/setup");
  vi.spyOn(api, "preflight").mockResolvedValue(ready);
  vi.spyOn(api, "getSettings").mockResolvedValue(pendingSettings);
  vi.spyOn(api, "putSettings").mockResolvedValue({ applied: ["setup_completed_at"], settings: pendingSettings });
});
afterEach(cleanup);

describe("first-run setup", () => {
  it("requires setup only for an incomplete installation with required errors", () => {
    expect(needsSetup(pendingSettings, failed, false)).toBe(true);
    expect(needsSetup(pendingSettings, ready, false)).toBe(false);
    expect(needsSetup(pendingSettings, failed, true)).toBe(false);
    expect(needsSetup({ setup_completed_at: { ...pendingSettings.setup_completed_at, value: "2026-01-01T00:00:00Z" } }, failed, false)).toBe(false);
  });

  it("loads the gate and remembers Skip for now across remounts in this session", async () => {
    vi.mocked(api.preflight).mockResolvedValue(failed);
    const initial = renderHook(useSetupGate);
    await waitFor(() => expect(initial.result.current.required).toBe(true));
    act(() => initial.result.current.finish(true));
    expect(initial.result.current.required).toBe(false);
    initial.unmount();
    const next = renderHook(useSetupGate);
    expect(next.result.current.required).toBe(false);
    expect(api.getSettings).toHaveBeenCalledTimes(1);
    expect(api.putSettings).not.toHaveBeenCalled();
  });

  it("does not reinstate the gate when an older startup check finishes after skip", async () => {
    let resolve!: (value: PreflightReport) => void;
    vi.mocked(api.preflight).mockReturnValue(new Promise((done) => { resolve = done; }));
    const gate = renderHook(useSetupGate);
    act(() => gate.result.current.finish(true));
    await act(async () => resolve(failed));
    expect(gate.result.current.required).toBe(false);
  });

  it("runs only local checks on mount and connection probes on explicit request", async () => {
    render(<SetupPage onFinish={vi.fn()} />);
    await screen.findByText(/Required checks passed/);
    expect(api.preflight).toHaveBeenCalledExactlyOnceWith();
    fireEvent.click(screen.getByRole("button", { name: "Run connection checks" }));
    await waitFor(() => expect(api.preflight).toHaveBeenLastCalledWith(true));
    expect(screen.queryByRole("textbox")).toBeNull();
  });

  it("keeps the latest connection result when the initial local request is delayed", async () => {
    let resolve!: (value: PreflightReport) => void;
    vi.mocked(api.preflight).mockReturnValueOnce(new Promise((done) => { resolve = done; })).mockResolvedValue(failed);
    render(<SetupPage onFinish={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Run connection checks" }));
    await screen.findByText("Library mount unavailable");
    await act(async () => resolve(ready));
    expect(screen.getByText("Library mount unavailable")).toBeTruthy();
    expect(screen.queryByText(/Required checks passed/)).toBeNull();
  });

  it("allows skipping even when local checks cannot load", async () => {
    vi.mocked(api.preflight).mockRejectedValue(new Error("Unavailable"));
    const finish = vi.fn();
    render(<SetupPage onFinish={finish} />);
    await screen.findByRole("alert");
    fireEvent.click(screen.getByRole("button", { name: "Skip for now" }));
    expect(finish).toHaveBeenCalledWith(true);
    expect(api.putSettings).not.toHaveBeenCalled();
  });

  it("completes with warnings and saves a timestamp after checking the latest configuration", async () => {
    const finish = vi.fn();
    render(<SetupPage onFinish={finish} />);
    await screen.findByText(/Required checks passed/);
    fireEvent.click(screen.getByRole("button", { name: "4. Optional indexers" }));
    fireEvent.click(screen.getByRole("button", { name: "Complete setup" }));
    await waitFor(() => expect(api.putSettings).toHaveBeenCalledWith({ setup_completed_at: expect.stringMatching(/^\d{4}-\d{2}-\d{2}T/) }));
    expect(api.preflight).toHaveBeenCalledTimes(2);
    expect(finish).toHaveBeenCalledWith();
  });

  it("does not save completion if required checks fail during confirmation", async () => {
    vi.mocked(api.preflight).mockResolvedValueOnce(ready).mockResolvedValue(failed);
    render(<SetupPage onFinish={vi.fn()} />);
    await screen.findByText(/Required checks passed/);
    fireEvent.click(screen.getByRole("button", { name: "4. Optional indexers" }));
    fireEvent.click(screen.getByRole("button", { name: "Complete setup" }));
    await screen.findByRole("alert");
    expect(api.putSettings).not.toHaveBeenCalled();
    expect((screen.getByRole("button", { name: "Complete setup" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it.each(["check", "save"])("does not complete or change navigation after Skip during the final %s", async (phase) => {
    let resolve!: () => void;
    if (phase === "check") {
      vi.mocked(api.preflight).mockResolvedValueOnce(ready).mockReturnValueOnce(new Promise((done) => { resolve = () => done(ready); }));
    } else {
      vi.mocked(api.putSettings).mockReturnValueOnce(new Promise((done) => { resolve = () => done({ applied: ["setup_completed_at"], settings: pendingSettings }); }));
    }
    const finish = vi.fn();
    render(<SetupPage onFinish={finish} />);
    await screen.findByText(/Required checks passed/);
    fireEvent.click(screen.getByRole("button", { name: "4. Optional indexers" }));
    fireEvent.click(screen.getByRole("button", { name: "Complete setup" }));
    await waitFor(() => expect(phase === "check" ? api.preflight : api.putSettings).toHaveBeenCalledTimes(phase === "check" ? 2 : 1));
    fireEvent.click(screen.getByRole("button", { name: "Skip for now" }));
    window.history.replaceState(null, "", "#/series/series-one");
    await act(async () => resolve());
    expect(finish).toHaveBeenCalledExactlyOnceWith(true);
    expect(window.location.hash).toBe("#/series/series-one");
    if (phase === "check") expect(api.putSettings).not.toHaveBeenCalled();
  });
});

describe("legacy Tools routes", () => {
  it("replaces the old Tools hash with Data in browser history", () => {
    window.history.replaceState(null, "", "#/settings?tab=tools");
    const route = renderHook(useHashRoute);
    expect(route.result.current).toEqual({ page: "settings" });
    expect(window.location.hash).toBe("#/settings?tab=data");
  });

  it("redirects a series deep link and preserves its encoded ID", () => {
    expect(legacyToolsRedirect("#/settings?tab=tools&manga_id=series%2Fone")).toBe("#/series/series%2Fone");
    expect(parseRoute("#/settings?tab=tools&manga_id=series%2Fone")).toEqual({ page: "series", id: "series/one" });
    expect(parseRoute("#/setup")).toEqual({ page: "setup" });
    expect(legacyToolsRedirect("#/settings?tab=sources")).toBeNull();
  });

  it("preserves unsaved settings when a legacy series redirect is declined", () => {
    window.history.replaceState(null, "", "#/settings?tab=sources");
    vi.spyOn(window, "confirm").mockReturnValue(false);
    const route = renderHook(() => {
      const current = useHashRoute();
      useUnsavedChangesGuard(true);
      return current;
    });
    act(() => {
      window.history.replaceState(null, "", "#/settings?tab=tools&manga_id=series-one");
      window.dispatchEvent(new HashChangeEvent("hashchange"));
    });
    expect(window.confirm).toHaveBeenCalledTimes(1);
    expect(window.location.hash).toBe("#/settings?tab=sources");
    expect(route.result.current).toEqual({ page: "settings" });
  });
});
