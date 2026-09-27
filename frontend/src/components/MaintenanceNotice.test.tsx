import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";
import type { MaintenanceStatus } from "../operationTypes";
import MaintenanceNotice, { maintenanceErrors } from "./MaintenanceNotice";

const job = { status: "ok" as const, last_attempt_at: null, last_success_at: null, retry_after: null, error: null };
const status: MaintenanceStatus = {
  jobs: { backup: job, repair: job, recycle: job },
  repair: { revision: "old-review", generated_at: "2026-01-01T00:00:00Z", actions: [{ id: "one", kind: "move", path: "Series/chapter.cbz", reason: "Normalize filename", safe: true }], total_actions: 33, truncated: false, warnings: [], can_apply: true },
};
afterEach(cleanup);

it("does not show repair proposals, paths, or confirmation buttons from old servers", () => {
  const { container } = render(<MaintenanceNotice status={status} />);
  expect(container.textContent).toBe("");
  expect(screen.queryByRole("button")).toBeNull();
  expect(maintenanceErrors(status)).toEqual([]);
});

it("keeps repair retries and blockers silent", () => {
  const pending: MaintenanceStatus = { ...status,
    jobs: { ...status.jobs, repair: { ...job, status: "error", error: "Repair deferred" } },
    repair: { ...status.repair, warnings: ["File changed"], can_apply: false },
  };
  const { container } = render(<MaintenanceNotice status={pending} />);
  expect(container.textContent).toBe("");
  expect(maintenanceErrors(pending)).toEqual([]);
});

it("still reports unrelated backup failures", () => {
  render(<MaintenanceNotice status={{ ...status, jobs: { ...status.jobs, backup: { ...job, status: "error", error: "Backup failed" } } }} />);
  expect(screen.getByRole("alert").textContent).toContain("Backup failed");
  expect(screen.queryByText("Library repair")).toBeNull();
});
