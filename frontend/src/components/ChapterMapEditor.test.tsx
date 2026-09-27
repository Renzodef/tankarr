import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, ApiError } from "../api";
import type { ChapterIndex, ChapterMapPreview, ChapterMapState } from "../types";
import ChapterMapEditor from "./ChapterMapEditor";

const boundaries = [{ volume: "12", first_chapter: "55" }, { volume: "13", first_chapter: "60" }];
const index: ChapterIndex = { slots: [], unit: "chapter", numbering_mode: "global", sequence_end: 85, sequence_basis: "catalogue", expected_count: 85, expected_source: "catalogue", unresolved_expected_count: 0, mapped_missing_count: 2, raw_missing_count: 2, ignored_missing_count: 0 };
const state: ChapterMapState = { boundaries, suggestions: [{ volume: "12", first_chapter: "50" }], last_known_chapter: "85", warnings: [], revision: "original-revision" };
const preview: ChapterMapPreview = {
  boundaries,
  intervals: [
    { volume: "12", first_chapter: "55", last_chapter: "59", chapters: ["55", "55.5", "56", "57", "58", "59"] },
    { volume: "13", first_chapter: "60", last_chapter: "85", chapters: ["60", "85"] },
  ],
  warnings: [], revision: state.revision, confirmation_snapshot: "preview-snapshot", chapter_index: index,
};

beforeEach(() => {
  vi.spyOn(api, "chapterMap").mockResolvedValue(state);
  vi.spyOn(api, "previewChapterMap").mockResolvedValue(preview);
  vi.spyOn(api, "saveChapterMap").mockResolvedValue(preview);
  vi.spyOn(api, "previewChapterMapRemoval").mockResolvedValue({ ...preview, boundaries: [], intervals: [], confirmation_snapshot: "remove-snapshot" });
  vi.spyOn(api, "removeChapterMap").mockResolvedValue({ ...preview, boundaries: [], intervals: [] });
});
afterEach(cleanup);

function editor(onSaved = vi.fn(), onClose = vi.fn()) {
  return render(<ChapterMapEditor mangaId="series-one" title="Example series" onSaved={onSaved} onClose={onClose} />);
}

async function loaded() { return screen.findByRole("textbox", { name: "First chapter in row 1" }); }
async function previewChanges() {
  fireEvent.click(screen.getByRole("button", { name: "Preview changes" }));
  return screen.findByRole("button", { name: "Confirm and save boundaries" });
}

describe("initial boundary draft", () => {
  it("prefers saved operator boundaries over catalogue suggestions", async () => {
    editor();
    expect((await loaded() as HTMLInputElement).value).toBe("55");
    expect(screen.getByText("Operator boundaries")).toBeTruthy();
    expect(screen.queryByText("Catalogue suggestions — not verified")).toBeNull();
  });

  it("prepopulates unverified suggestions only when the operator map is absent", async () => {
    vi.mocked(api.chapterMap).mockResolvedValue({ ...state, boundaries: [] });
    editor();
    expect((await loaded() as HTMLInputElement).value).toBe("50");
    expect(screen.getByText("Catalogue suggestions — not verified")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Preview removing operator map" })).toBeNull();
    expect(api.saveChapterMap).not.toHaveBeenCalled();
  });

  it("starts empty without saved boundaries or hints and supports adding and removing rows", async () => {
    vi.mocked(api.chapterMap).mockResolvedValue({ ...state, boundaries: [], suggestions: [] });
    editor();
    await screen.findByText("No book boundaries yet");
    expect(screen.queryAllByRole("textbox")).toHaveLength(0);
    fireEvent.click(screen.getByRole("button", { name: "Add book boundary" }));
    expect(screen.getAllByRole("textbox")).toHaveLength(2);
    fireEvent.click(screen.getByRole("button", { name: "Remove boundary row 1" }));
    fireEvent.click(screen.getByRole("button", { name: "Preview changes" }));
    await screen.findByRole("alert");
    expect(api.previewChapterMap).not.toHaveBeenCalled();
  });

  it("shows saved warnings and supports retrying an unavailable initial map", async () => {
    vi.mocked(api.chapterMap).mockRejectedValueOnce(new Error("Temporarily unavailable")).mockResolvedValueOnce({ ...state, warnings: ["New chapters extend beyond the saved operator map."] });
    editor();
    await screen.findByRole("alert");
    fireEvent.click(screen.getByRole("button", { name: "Retry loading boundaries" }));
    await screen.findByText("New chapters extend beyond the saved operator map.");
  });
});

describe("validation and preview", () => {
  it.each([
    ["Book number in row 1", "0.5", "at least 1"],
    ["First chapter in row 1", "NaN", "0 or greater"],
    ["First chapter in row 1", "-1", "0 or greater"],
    ["Book number in row 2", "012.0", "book numbers must increase"],
    ["First chapter in row 2", "55.0", "first chapters must increase"],
  ])("rejects invalid %s = %s before sending a preview", async (label, value, message) => {
    editor();
    await loaded();
    fireEvent.change(screen.getByRole("textbox", { name: label }), { target: { value } });
    fireEvent.click(screen.getByRole("button", { name: "Preview changes" }));
    expect((await screen.findByRole("alert")).textContent).toContain(message);
    expect(api.previewChapterMap).not.toHaveBeenCalled();
    expect(api.saveChapterMap).not.toHaveBeenCalled();
  });

  it("sends exact normalized decimals, preserves zero and does not save a preview", async () => {
    editor();
    await loaded();
    fireEvent.change(screen.getByRole("textbox", { name: "Book number in row 1" }), { target: { value: "001.50" } });
    fireEvent.change(screen.getByRole("textbox", { name: "First chapter in row 1" }), { target: { value: "00.0" } });
    fireEvent.change(screen.getByRole("textbox", { name: "First chapter in row 2" }), { target: { value: "055.50000000000000001" } });
    await previewChanges();
    expect(api.previewChapterMap).toHaveBeenCalledWith("series-one", [{ volume: "1.5", first_chapter: "0" }, { volume: "13", first_chapter: "55.50000000000000001" }], expect.any(AbortSignal));
    expect(api.saveChapterMap).not.toHaveBeenCalled();
    expect(screen.getByText(/Book 13 ends at chapter 85, the last canonical chapter known for this preview/)).toBeTruthy();
    expect(screen.getByText("55, 55.5, 56, 57, 58, 59")).toBeTruthy();
  });

  it("ignores a delayed preview after the draft changes", async () => {
    let resolve!: (value: ChapterMapPreview) => void;
    vi.mocked(api.previewChapterMap).mockReturnValueOnce(new Promise((done) => { resolve = done; }));
    editor();
    await loaded();
    fireEvent.click(screen.getByRole("button", { name: "Preview changes" }));
    fireEvent.change(screen.getByRole("textbox", { name: "First chapter in row 1" }), { target: { value: "55.5" } });
    await act(async () => resolve(preview));
    expect(screen.queryByRole("button", { name: "Confirm and save boundaries" })).toBeNull();
    expect((screen.getByRole("textbox", { name: "First chapter in row 1" }) as HTMLInputElement).value).toBe("55.5");
    expect(api.saveChapterMap).not.toHaveBeenCalled();
  });

  it("does not overwrite a new series with an old GET response", async () => {
    let resolve!: (value: ChapterMapState) => void;
    vi.mocked(api.chapterMap).mockReturnValueOnce(new Promise((done) => { resolve = done; })).mockResolvedValueOnce({ ...state, boundaries: [{ volume: "1", first_chapter: "7" }] });
    const view = editor();
    view.rerender(<ChapterMapEditor mangaId="series-two" title="Another series" onSaved={vi.fn()} onClose={vi.fn()} />);
    await loaded();
    await act(async () => resolve(state));
    expect((screen.getByRole("textbox", { name: "First chapter in row 1" }) as HTMLInputElement).value).toBe("7");
  });
});

describe("confirmed map mutations", () => {
  it("saves only the previewed boundaries with the confirmation snapshot and returns the index", async () => {
    const saved = vi.fn();
    editor(saved);
    await loaded();
    fireEvent.click(await previewChanges());
    await waitFor(() => expect(saved).toHaveBeenCalledWith(preview));
    expect(api.saveChapterMap).toHaveBeenCalledWith("series-one", boundaries, "preview-snapshot", expect.any(AbortSignal));
    expect(api.removeChapterMap).not.toHaveBeenCalled();
  });

  it("requires a fresh preview after a 409 without losing the draft", async () => {
    vi.mocked(api.saveChapterMap).mockRejectedValueOnce(new ApiError("The chapter catalogue changed", 409));
    const saved = vi.fn();
    editor(saved);
    await loaded();
    fireEvent.click(await previewChanges());
    expect((await screen.findByRole("alert")).textContent).toContain("Preview again before confirming");
    expect(screen.queryByRole("button", { name: "Confirm and save boundaries" })).toBeNull();
    expect((screen.getByRole("textbox", { name: "First chapter in row 1" }) as HTMLInputElement).value).toBe("55");
    expect(saved).not.toHaveBeenCalled();
  });

  it("previews operator removal and restores other sources only after explicit confirmation", async () => {
    const saved = vi.fn();
    editor(saved);
    await loaded();
    fireEvent.click(screen.getByRole("button", { name: "Preview removing operator map" }));
    const confirm = await screen.findByRole("button", { name: "Confirm removal" });
    expect(api.removeChapterMap).not.toHaveBeenCalled();
    expect(screen.getByText(/return to an even estimate; catalogue suggestions remain unconfirmed/)).toBeTruthy();
    expect(screen.getByText("No operator book assignments remain in this preview.")).toBeTruthy();
    fireEvent.click(confirm);
    await waitFor(() => expect(saved).toHaveBeenCalledTimes(1));
    expect(api.removeChapterMap).toHaveBeenCalledWith("series-one", "remove-snapshot", expect.any(AbortSignal));
    expect(api.saveChapterMap).not.toHaveBeenCalled();
  });

  it("cancels an unconfirmed removal without mutating the map", async () => {
    const close = vi.fn();
    editor(vi.fn(), close);
    await loaded();
    fireEvent.click(screen.getByRole("button", { name: "Preview removing operator map" }));
    await screen.findByRole("button", { name: "Confirm removal" });
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(close).toHaveBeenCalledTimes(1);
    expect(api.removeChapterMap).not.toHaveBeenCalled();
  });

  it("does not publish a stale save result after unmounting", async () => {
    let resolve!: (value: ChapterMapPreview) => void;
    vi.mocked(api.saveChapterMap).mockReturnValueOnce(new Promise((done) => { resolve = done; }));
    const saved = vi.fn();
    const view = editor(saved);
    await loaded();
    fireEvent.click(await previewChanges());
    view.unmount();
    await act(async () => resolve(preview));
    expect(saved).not.toHaveBeenCalled();
  });
});
