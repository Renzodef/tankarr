import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { api } from "../api";
import { AppContext } from "../components";
import type { DeleteResult, Manga } from "../types";
import { DeleteModal } from "./SeriesPage";

afterEach(() => { cleanup(); vi.restoreAllMocks(); });

it.each([true, false])("removes immediately without a library preview (delete files: %s)", async (deleteFiles) => {
  const preview = vi.spyOn(api, "deleteMangaPreview").mockImplementation(() => new Promise(() => {}));
  const result = { deleted: true, cleanup_pending: deleteFiles } as DeleteResult;
  const remove = vi.spyOn(api, "deleteManga").mockResolvedValue(result);
  const deleted = vi.fn();
  render(<AppContext.Provider value={{ health: null, notify: vi.fn(), refreshJobs: vi.fn().mockResolvedValue(undefined), refreshHealth: vi.fn().mockResolvedValue(undefined) }}>
    <DeleteModal manga={{ id: "example", title: "Example" } as Manga} onClose={vi.fn()} onDeleted={deleted} />
  </AppContext.Provider>);
  if (!deleteFiles) fireEvent.click(screen.getByRole("checkbox"));
  const button = screen.getByRole("button", { name: "Delete" }) as HTMLButtonElement;
  expect(button.disabled).toBe(false);
  fireEvent.click(button);
  await waitFor(() => expect(deleted).toHaveBeenCalledWith(result, deleteFiles));
  expect(remove).toHaveBeenCalledWith("example", deleteFiles);
  expect(preview).not.toHaveBeenCalled();
});
