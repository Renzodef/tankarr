import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { api, ApiError } from "../api";
import { AppContext } from "../components";
import { EditModal } from "../pages/SeriesPage";
import type { BookAssemblyPreview, BookAssemblyResult, BooksAssemblyResult, Manga } from "../types";
import AssembleBooksDialog from "./AssembleBooksDialog";

const snapshot = "a".repeat(64);
const preview: BookAssemblyPreview = { manga_id: "example", volume: "1", filename: "Example - Book 1.cbz", pages: 80, confirmation_snapshot: snapshot, chapters: [
  { chapter: "0", id: "chapter-zero", pages: 20, source: "Source One" },
  { chapter: "1", source_chapter: "1.1", id: "chapter-part-one", pages: 30, source: "Source Two" },
  { chapter: "1", source_chapter: "1.2", id: "chapter-part-two", pages: 30, source: "Source Two" },
] };
const completed = { ...preview, chapter: { id: "new-book" }, retirement: { files_retired: 3, cleanup_errors: [] }, reader: {} } as unknown as BookAssemblyResult;
const confirmLabel = "I reviewed the preview and confirm creating these books and moving their chapter files to the recycle bin";

beforeEach(() => {
  vi.spyOn(api, "previewBookAssembly").mockResolvedValue(preview);
  vi.spyOn(api, "assembleBook").mockResolvedValue(completed);
  vi.spyOn(api, "previewBooksAssembly").mockResolvedValue({ books: [preview], errors: [], confirmation_snapshot: snapshot });
  vi.spyOn(api, "assembleBooks").mockResolvedValue({ assembled: [completed], errors: [], remaining: [] });
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

function dialog(volume: string | null = "1", onChanged = vi.fn(), onClose = vi.fn()) {
  return { ...render(<AssembleBooksDialog mangaId="example" title="Example" volume={volume} onChanged={onChanged} onClose={onClose} />), onChanged, onClose };
}
async function confirm() {
  fireEvent.click(await screen.findByLabelText(confirmLabel));
  fireEvent.click(screen.getByRole("button", { name: "Confirm assembly" }));
}

it("previews ordered chapters, split parts, pages and filename before explicit confirmation", async () => {
  const { onChanged } = dialog();
  expect(await screen.findByText(/Book 1 · 3 chapter files · 80 pages · Example - Book 1.cbz/)).toBeTruthy();
  expect(screen.getByText("(source chapter 1.1)")).toBeTruthy();
  expect(screen.getByText("(source chapter 1.2)")).toBeTruthy();
  expect(screen.getByText("Source One")).toBeTruthy();
  expect((screen.getByRole("button", { name: "Confirm assembly" }) as HTMLButtonElement).disabled).toBe(true);
  expect(api.assembleBook).not.toHaveBeenCalled();
  await confirm();
  await screen.findByText("1 book assembled.");
  expect(api.assembleBook).toHaveBeenCalledWith("example", "1", snapshot, expect.any(AbortSignal));
  expect(onChanged).toHaveBeenCalledWith({ assembled: [completed], errors: [], remaining: [] });
  expect(screen.getByText(/3 chapter files moved to the recycle bin/)).toBeTruthy();
  expect(screen.queryByRole("button", { name: "Confirm assembly" })).toBeNull();
});

it("requires a new preview and new confirmation after a stale snapshot", async () => {
  vi.mocked(api.assembleBook).mockRejectedValue(new ApiError("The book map changed.", 409, { missing_chapters: ["5", "5.5"] }));
  dialog();
  await confirm();
  expect(await screen.findByRole("alert")).toHaveProperty("textContent", expect.stringContaining("Missing chapters: 5, 5.5."));
  expect(screen.queryByLabelText(confirmLabel)).toBeNull();
  const nextSnapshot = "b".repeat(64);
  vi.mocked(api.previewBookAssembly).mockResolvedValue({ ...preview, confirmation_snapshot: nextSnapshot });
  vi.mocked(api.assembleBook).mockResolvedValue(completed);
  fireEvent.click(screen.getByRole("button", { name: "Preview again" }));
  await screen.findByLabelText(confirmLabel);
  expect((screen.getByRole("button", { name: "Confirm assembly" }) as HTMLButtonElement).disabled).toBe(true);
  await confirm();
  await screen.findByText("1 book assembled.");
  expect(api.assembleBook).toHaveBeenLastCalledWith("example", "1", nextSnapshot, expect.any(AbortSignal));
});

it("shows missing chapters when the initial preview cannot assemble the book", async () => {
  vi.mocked(api.previewBookAssembly).mockRejectedValue(new ApiError("Every mapped chapter needs a file.", 409, { missing_chapters: ["12", "13"] }));
  dialog();
  expect(await screen.findByRole("alert")).toHaveProperty("textContent", expect.stringContaining("Missing chapters: 12, 13."));
  expect(api.assembleBook).not.toHaveBeenCalled();
  expect(screen.queryByRole("button", { name: "Confirm assembly" })).toBeNull();
});

it("rejects an empty bulk preview instead of sending an empty confirmation", async () => {
  vi.mocked(api.previewBooksAssembly).mockResolvedValue({ books: [], errors: [], confirmation_snapshot: snapshot });
  dialog(null);
  expect(await screen.findByRole("alert")).toHaveProperty("textContent", expect.stringContaining("No complete books"));
  expect(api.assembleBooks).not.toHaveBeenCalled();
});

it("ignores an older preview when the selected book changes", async () => {
  let resolveOld!: (value: BookAssemblyPreview) => void;
  const old = new Promise<BookAssemblyPreview>((resolve) => { resolveOld = resolve; });
  vi.mocked(api.previewBookAssembly).mockReturnValueOnce(old).mockResolvedValue({ ...preview, volume: "2", filename: "Example - Book 2.cbz" });
  const { rerender } = dialog();
  rerender(<AssembleBooksDialog mangaId="example" title="Example" volume="2" onChanged={vi.fn()} onClose={vi.fn()} />);
  await screen.findByText(/Book 2 · 3 chapter files/);
  await act(async () => { resolveOld(preview); await old; });
  expect(screen.queryByText(/Book 1 · 3 chapter files/)).toBeNull();
  expect(vi.mocked(api.previewBookAssembly).mock.calls[0][2]?.aborted).toBe(true);
});

it("cancels a pending preview on close without performing assembly", async () => {
  let resolvePreview!: (value: BookAssemblyPreview) => void;
  const pending = new Promise<BookAssemblyPreview>((resolve) => { resolvePreview = resolve; });
  vi.mocked(api.previewBookAssembly).mockReturnValue(pending);
  const { onClose } = dialog();
  fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
  expect(onClose).toHaveBeenCalledOnce();
  await act(async () => { resolvePreview(preview); await pending; });
  expect(screen.queryByRole("button", { name: "Confirm assembly" })).toBeNull();
  expect(api.assembleBook).not.toHaveBeenCalled();
});

it("prevents repeated confirmation and ignores completion after unmount", async () => {
  let resolveAssembly!: (value: BookAssemblyResult) => void;
  const pending = new Promise<BookAssemblyResult>((resolve) => { resolveAssembly = resolve; });
  vi.mocked(api.assembleBook).mockReturnValue(pending);
  const { onChanged, unmount } = dialog();
  await confirm();
  expect((screen.getByRole("button", { name: "Cancel" }) as HTMLButtonElement).disabled).toBe(true);
  expect((screen.getByRole("button", { name: "Assembling…" }) as HTMLButtonElement).disabled).toBe(true);
  expect(api.assembleBook).toHaveBeenCalledOnce();
  unmount();
  await act(async () => { resolveAssembly(completed); await pending; });
  expect(onChanged).not.toHaveBeenCalled();
});

it("keeps partial bulk results visible and previews remaining books afresh", async () => {
  const second = { ...preview, volume: "2", filename: "Example - Book 2.cbz" };
  vi.mocked(api.previewBooksAssembly).mockResolvedValueOnce({ books: [preview, second], errors: [{ volume: "3", message: "Chapter 9 is missing." }], confirmation_snapshot: snapshot }).mockResolvedValue({ books: [second], errors: [], confirmation_snapshot: "c".repeat(64) });
  const partial: BooksAssemblyResult = { assembled: [completed], errors: [{ volume: "2", message: "Source file changed." }], remaining: ["2"] };
  vi.mocked(api.assembleBooks).mockResolvedValue(partial);
  const { onChanged } = dialog(null);
  expect(await screen.findByText("Book 3 cannot be assembled: Chapter 9 is missing.")).toBeTruthy();
  await confirm();
  expect(await screen.findByText("Book 2: Source file changed.")).toBeTruthy();
  expect(screen.getByText("Books remaining: 2.")).toBeTruthy();
  expect(onChanged).toHaveBeenCalledWith(partial);
  expect(api.assembleBooks).toHaveBeenCalledWith("example", snapshot, expect.any(AbortSignal));
  expect(api.assembleBook).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "Preview remaining books" }));
  await screen.findByLabelText(confirmLabel);
  expect((screen.getByRole("button", { name: "Confirm assembly" }) as HTMLButtonElement).disabled).toBe(true);
  expect(screen.queryByText(/Book 1 · 3 chapter files/)).toBeNull();
});

it("uses separate single and bulk POST previews and preserves structured conflicts", async () => {
  vi.restoreAllMocks();
  const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify(preview), { status: 200 }));
  vi.stubGlobal("fetch", fetcher);
  await api.previewBookAssembly("series/one", "1.5");
  expect(fetcher.mock.calls[0][0]).toBe("/api/manga/series%2Fone/volumes/1.5/assemble?dry_run=true");
  expect(fetcher.mock.calls[0][1].method).toBe("POST");
  fetcher.mockResolvedValue(new Response(JSON.stringify({ books: [preview], errors: [], confirmation_snapshot: snapshot }), { status: 200 }));
  await api.previewBooksAssembly("series/one");
  expect(fetcher.mock.calls[1][0]).toBe("/api/manga/series%2Fone/assemble?dry_run=true");
  fetcher.mockResolvedValue(new Response(JSON.stringify({ detail: { message: "Source changed.", missing_chapters: ["1.5"] } }), { status: 409 }));
  await expect(api.assembleBook("series/one", "1.5", snapshot)).rejects.toMatchObject({ status: 409, detail: { missing_chapters: ["1.5"] } });
  expect(new URL(fetcher.mock.calls[2][0], "https://tankarr.example").searchParams.get("confirmation_snapshot")).toBe(snapshot);
});
