import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { api } from "../api";
import ChapterRecoveryDialog from "./ChapterRecoveryDialog";

afterEach(() => { cleanup(); vi.restoreAllMocks(); });

it("previews before downloading and requires an explicit confirmation", async () => {
  vi.spyOn(api, "previewChapterRecovery").mockResolvedValue({ chapters: [{ id: "c1", chapter: "1", source: "Example" }], confirmation_snapshot: "signed-plan" });
  vi.spyOn(api, "recoverChapters").mockResolvedValue({ queued: 1 });
  const changed = vi.fn().mockResolvedValue(undefined);
  const close = vi.fn();
  render(<ChapterRecoveryDialog mangaId="example" onClose={close} onQueued={changed} />);
  await screen.findByText("1 · Example");
  expect(api.recoverChapters).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "Confirm chapter downloads" }));
  await waitFor(() => expect(close).toHaveBeenCalledOnce());
  expect(api.recoverChapters).toHaveBeenCalledWith("example", "signed-plan");
  expect(changed).toHaveBeenCalledOnce();
});

it("invalidates the preview after a rejected confirmation", async () => {
  vi.spyOn(api, "previewChapterRecovery").mockResolvedValue({ chapters: [{ id: "c1", chapter: "1", source: "Example" }], confirmation_snapshot: "old-plan" });
  vi.spyOn(api, "recoverChapters").mockRejectedValue(new Error("Queue changed"));
  render(<ChapterRecoveryDialog mangaId="example" onClose={vi.fn()} onQueued={vi.fn()} />);
  await screen.findByText("1 · Example");
  fireEvent.click(screen.getByRole("button", { name: "Confirm chapter downloads" }));
  await screen.findByRole("alert");
  expect((screen.getByRole("button", { name: "Confirm chapter downloads" }) as HTMLButtonElement).disabled).toBe(true);
});
