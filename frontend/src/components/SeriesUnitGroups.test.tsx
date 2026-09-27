import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import type { SeriesBookGroup, SeriesUnitChapter, SeriesUnits } from "../types";
import SeriesUnitGroups from "./SeriesUnitGroups";

function chapter(number: string, values: Partial<SeriesUnitChapter> = {}): SeriesUnitChapter {
  return { key: `chapter:${number}`, chapter: number, volume: null, expected: true, special: false, evidence: "exact_map", volume_inferred: false, split_parts: [], releases: [], available: true, downloaded: false, monitored: true, queue_status: null, searchable: true, covered_by_volume: null, covered_by_chapters: false, coverage_exact: false, covered_unmapped: false, duplicate_of_volume: null, volume_monitor_state: "automatic", ignored: false, files: [], open_release_id: null, pages: null, missing: true, ...values } as SeriesUnitChapter;
}
function book(volume: string, chapters: SeriesUnitChapter[], values: Partial<SeriesBookGroup> = {}): SeriesBookGroup {
  const downloaded = chapters.filter((item) => item.downloaded).length;
  return { key: `book:${volume}`, volume, expected: true, owned: false, monitored: true, ignored: false, volume_monitor_state: "automatic", status: "missing", pages: null, files: [], open_release_id: null, suspect: false, retirement_note: null, exact: true, chapter_range: chapters.length ? { first: chapters[0].chapter as string, last: chapters[chapters.length - 1].chapter as string } : null, chapters, chapter_count: chapters.length, downloaded_chapter_count: downloaded, missing_chapter_count: chapters.length - downloaded, duplicate_file_count: 0, duplicate_release_ids: [], covered_by_chapters: false, can_assemble: false, collapsed: false, ...values };
}
function data(books: SeriesBookGroup[], values: Partial<SeriesUnits> = {}): SeriesUnits {
  return { manga_id: "example-series", mode: "grouped", series_unit: "chapters", edition_book_count: null, expected_book_count: books.length, warning: null, hints: [], books, unassigned_chapters: [], form: "volumes", uniform: "mixed", preference: "volumes", ...values };
}
function view(groups: SeriesUnits, bookmark?: import("../types").ReaderLink["bookmark"]) {
  const actions = {
    bookmark, busy: false, readerLabel: "Stump", readerUrl: (id: string) => id === "owned-book" ? "https://reader.example/book/owned-book" : undefined,
    onSetBookMonitoring: vi.fn(), onAutomaticSearchBook: vi.fn(), onSearchBook: vi.fn(), onDeleteBookFiles: vi.fn(), onRetireDuplicates: vi.fn(), onSetBoundaries: vi.fn(),
    renderChapterRows: (chapters: SeriesUnitChapter[]) => chapters.map((item) => <tr key={item.key}><td>{`Chapter ${item.chapter}`}</td></tr>),
  };
  return { ...render(<SeriesUnitGroups data={groups} {...actions} />), actions };
}
afterEach(cleanup);

it.each(["chapters", "volumes"] as const)("separates chapter coverage from edition ownership in %s form", (form) => {
  view(data([book("1", [chapter("1", { downloaded: true })], { estimated: true })], {
    form,
    completeness: { indexed_chapters: 65, indexed_chapters_on_disk: 65, owned_books: 0, expected_books: 10, edition_files_complete: false, estimated_groups: 10, manual_boundaries: false },
  }));
  const summary = screen.getByLabelText("Library completeness").textContent;
  expect(summary).toContain("65/65 on disk");
  expect(summary).toContain("Edition files: 0/10 books");
  expect(summary).not.toMatch(/estimated/i);
  expect(screen.queryByText(/All expected book files present/)).toBeNull();
});

it("does not label unmapped chapters as absent when the edition files are owned", () => {
  view(data([book("1", [], { owned: true, estimated: true })], {
    uniform: "books",
    completeness: { indexed_chapters: 10, indexed_chapters_on_disk: 0, owned_books: 1, expected_books: 1, edition_files_complete: true, estimated_groups: 1, manual_boundaries: false },
  }));
  const summary = screen.getByLabelText("Library completeness").textContent;
  expect(summary).toContain("10 chapters indexed");
  expect(summary).toContain("coverage inside the owned books is not fully mapped");
  expect(summary).toContain("Edition files: 1/1 books");
  expect(summary).not.toContain("0/10");
  expect(screen.getByText(/All expected book files present/)).toBeTruthy();
});

it("counts chapters mapped into owned books as covered without separate chapter files", () => {
  const owned = book("1", [chapter("1", { missing: false, covered_by_volume: "1" }), chapter("2", { missing: false, covered_by_volume: "1" })], {
    owned: true, status: "owned", open_release_id: "owned-book", plan_state: "book",
  });
  view(data([owned], {
    uniform: "books",
    completeness: { indexed_chapters: 2, indexed_chapters_on_disk: 0, owned_books: 1, expected_books: 1, edition_files_complete: true, estimated_groups: 0, manual_boundaries: true },
  }));
  const summary = screen.getByLabelText("Library completeness").textContent;
  expect(summary).toContain("2 chapters indexed; covered by the owned books");
  expect(summary).not.toContain("not fully mapped");
});

it("keeps a readable volume visible when a webtoon is presented as chapters", () => {
  const owned = book("1", [], { owned: true, status: "owned", pages: 811, open_release_id: "owned-book" });
  view(data([owned], { form: "chapters", uniform: "chapters", unassigned_chapters: [chapter("1")] }));
  const shelf = screen.getByRole("region", { name: "Readable books" });
  expect(within(shelf).getByRole("link", { name: "Read book 1 in Stump" }).getAttribute("href")).toBe("https://reader.example/book/owned-book");
  expect(screen.getByRole("region", { name: "Chapters" })).toBeTruthy();
});

it("shows one row per book with its plan, keeps owned books closed and opens the rest", () => {
  const owned = book("1", [chapter("1", { downloaded: true, missing: false })], { owned: true, status: "owned", pages: 208, plan_state: "book", plan_text: "208 pages" });
  const partial = book("2", [chapter("2", { downloaded: true, missing: false }), chapter("3")], { plan_state: "partial", plan_text: "1 of 2 chapters · search the book, then the missing chapters" });
  view(data([owned, partial]));
  expect(screen.getAllByRole("heading", { level: 3 }).map((item) => item.textContent)).toEqual(["Book 1", "Book 2"]);
  expect(screen.queryByText("Chapter 1")).toBeNull();
  expect(screen.getByText("Chapter 3")).toBeTruthy();
  expect(within(screen.getByRole("region", { name: "Book 1" })).getByText("Book")).toBeTruthy();
  expect(within(screen.getByRole("region", { name: "Book 2" })).getByText("In part")).toBeTruthy();
  expect(screen.getByText(/1 of 2 chapters · search the book/)).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Show separate chapter releases for book 1" }));
  expect(screen.getByText("Chapter 1")).toBeTruthy();
});

it("keeps inferred boundaries quiet and offers the map editor", () => {
  const { actions } = view(data([book("37", [chapter("171"), chapter("174")], { estimated: true, plan_state: "missing" })], { map_confidence: { level: "partial", estimated_books: 1, sources: ["operator"], hint_sources: [] } }));
  expect(screen.queryByText(/estimated/i)).toBeNull();
  expect(screen.getByText(/1 book laid out between known neighbours/)).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Correct the map" }));
  expect(actions.onSetBoundaries).toHaveBeenCalledOnce();
});

it("shows a complete book collection with its inferred layout, never as a problem", () => {
  const books = Array.from({ length: 10 }, (_, i) => book(String(i + 1), [chapter(String(i * 5 + 1))], {
    owned: true, status: "owned", estimated: i > 0, plan_state: "book", pages: 294 - i,
    open_release_id: "owned-book",
  }));
  view(data(books, { uniform: "books", map_confidence: { level: "partial", estimated_books: 9, sources: ["mangaupdates"], hint_sources: [] } }));
  expect(screen.getByText(/All expected book files present/).textContent).toBe("10 books on disk · All expected book files present");
  expect(screen.getByText(/Book boundaries:/)).toBeTruthy();
  expect(screen.queryByRole("button", { name: "Correct the map" })).toBeNull();
  // Every book shows where its chapters fall; the nine laid out between
  // known neighbours say so and stay books on a complete shelf.
  expect(screen.getAllByText(/ch\. /)).toHaveLength(10);
  expect(screen.queryByText(/estimated/i)).toBeNull();
  expect(screen.getByRole("button", { name: "Edit book boundaries" })).toBeTruthy();
  expect(screen.queryByRole("button", { name: /Expand book/ })).toBeNull();
  const first = within(screen.getByRole("region", { name: "Book 1" }));
  expect(first.getByText(/294 pages/).getAttribute("title")).toBe("ch. 1–1 · catalogue boundaries · 294 pages");
  expect(first.getByRole("link", { name: "Read book 1 in Stump" })).toBeTruthy();
});

it("retains actual chapter releases and duplicate actions in an owned book", () => {
  const owned = book("1", [chapter("1"), chapter("2", { downloaded: true, missing: false })], {
    owned: true, status: "owned", estimated: true, plan_state: "book",
    duplicate_file_count: 1, duplicate_release_ids: ["duplicate"],
  });
  const { actions } = view(data([owned], { uniform: "books" }));
  fireEvent.click(screen.getByRole("button", { name: "Show separate chapter releases for book 1" }));
  expect(screen.queryByText("Chapter 1")).toBeNull();
  expect(screen.getByText("Chapter 2")).toBeTruthy();
  expect(screen.getByText("Book on disk. Separate chapter releases:")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Retire duplicate chapters in book 1" }));
  expect(actions.onRetireDuplicates).toHaveBeenCalledWith(owned);
});

it("keeps missing books and their map visible when the collection is incomplete", () => {
  view(data([book("1", [], { owned: true }), book("2", [chapter("5")], { estimated: true })], {
    map_confidence: { level: "partial", estimated_books: 1, sources: ["operator"], hint_sources: [] },
  }));
  expect(screen.queryByText(/All expected book files present/)).toBeNull();
  expect(screen.getByRole("button", { name: "Correct the map" })).toBeTruthy();
  expect(screen.getByRole("button", { name: "Automatically search book 2" })).toBeTruthy();
  expect(screen.getByText("Chapter 5")).toBeTruthy();
});

it("puts the primary action on the row and the rest behind the menu", () => {
  const assemble = book("1", [chapter("1", { downloaded: true, missing: false })], { covered_by_chapters: true, can_assemble: true, status: "covered_by_chapters", plan_state: "assemble" });
  const owned = book("2", [chapter("3")], { owned: true, status: "owned", open_release_id: "owned-book", duplicate_file_count: 2, duplicate_release_ids: ["file-one", "file-two"], files: [{ id: "owned-book", title: "Book 2", provider: "manual", source_name: null, language: "en", library_path: "/library/book-2.cbz", pages: 190 }], plan_state: "book" });
  const missing = book("3", [chapter("4")], { plan_state: "missing" });
  const { actions } = view(data([assemble, owned, missing]));
  expect(screen.queryByRole("button", { name: "Assemble book 1" })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Automatically search book 1" }));
  expect(actions.onAutomaticSearchBook).toHaveBeenCalledWith(assemble);
  expect(screen.getByRole("link", { name: "Read book 2 in Stump" }).getAttribute("href")).toBe("https://reader.example/book/owned-book");
  fireEvent.change(screen.getByLabelText("Monitoring for book 2"), { target: { value: "ignored" } });
  expect(actions.onSetBookMonitoring).toHaveBeenCalledWith(owned, "ignored");
  fireEvent.click(screen.getByRole("button", { name: "Retire duplicate chapters in book 2" }));
  expect(actions.onRetireDuplicates).toHaveBeenCalledWith(owned);
  fireEvent.click(screen.getByRole("button", { name: "Interactive search for book 2" }));
  expect(actions.onSearchBook).toHaveBeenCalledWith(owned);
  fireEvent.click(screen.getByRole("button", { name: "Delete files for book 2" }));
  expect(actions.onDeleteBookFiles).toHaveBeenCalledWith(owned);
  fireEvent.click(screen.getByRole("button", { name: "Automatically search book 3" }));
  expect(actions.onAutomaticSearchBook).toHaveBeenCalledWith(missing);
});

it("filters to problems only and explains an all-clear series", () => {
  const owned = book("1", [chapter("1", { downloaded: true, missing: false })], { owned: true, status: "owned", plan_state: "book" });
  const covered = book("2", [chapter("2", { downloaded: true, missing: false })], { covered_by_chapters: true, plan_state: "assemble" });
  const missing = book("3", [chapter("3")], { plan_state: "missing" });
  view(data([owned, covered, missing]));
  fireEvent.click(screen.getByRole("button", { name: "Problems only" }));
  expect(screen.queryByRole("region", { name: "Book 1" })).toBeNull();
  expect(screen.queryByRole("region", { name: "Book 2" })).toBeNull();
  expect(screen.getByRole("region", { name: "Book 3" })).toBeTruthy();
  cleanup();
  view(data([owned, covered]));
  expect(screen.queryByRole("button", { name: "Problems only" })).toBeNull();
  fireEvent.change(screen.getByLabelText("Filter series units"), { target: { value: "problems" } });
  expect(screen.getByText(/No problems: every book is on disk or completes from its chapters/)).toBeTruthy();
});

it("reads a running series as a single chapter list", () => {
  view(data([book("1", [chapter("1", { downloaded: true, missing: false })])], { form: "chapters", form_reason: "running series: chapters until it ends", unassigned_chapters: [chapter("2"), chapter("3", { downloaded: true, missing: false })] }));
  expect(screen.queryByRole("heading", { level: 3 })).toBeNull();
  expect(screen.getByText(/2 of 3 chapters on disk · running series/)).toBeTruthy();
  const table = screen.getByRole("region", { name: "Chapters" });
  expect(within(table).getAllByText(/^Chapter \d+$/).map((item) => item.textContent)).toEqual(["Chapter 1", "Chapter 2", "Chapter 3"]);
  fireEvent.change(screen.getByLabelText("Filter series units"), { target: { value: "missing" } });
  expect(within(screen.getByRole("region", { name: "Chapters" })).getAllByText(/^Chapter \d+$/).map((item) => item.textContent)).toEqual(["Chapter 2"]);
});

it("keeps unassigned chapters visible without claiming they are extras", () => {
  const redundant = book("1", [chapter("1", { downloaded: true, missing: false })], { owned: true, status: "owned", plan_state: "book", redundant_book: true, plan_text: "Book on disk; the series completes from chapters, this book is redundant" });
  view(data([redundant], { uniform: "chapters", unassigned_chapters: [chapter("9", { downloaded: true, missing: false })] }));
  expect(screen.getByText(/This book is redundant/)).toBeTruthy();
  expect(screen.getByText(/form: /).textContent).toContain("all chapters");
  expect(within(screen.getByRole("region", { name: "Chapters without a book assignment" })).getByText("Chapter 9")).toBeTruthy();
  expect(screen.queryByText(/extras beyond the last book/)).toBeNull();
  expect(screen.getByText(/does not establish which book/)).toBeTruthy();
});

it("reverses books and chapters together and lists everything at once", () => {
  view(data([book("1", [chapter("1"), chapter("1.5")]), book("1.5", [chapter("2")])]));
  fireEvent.change(screen.getByLabelText("Order series units"), { target: { value: "descending" } });
  expect(screen.getAllByRole("heading", { level: 3 }).map((item) => item.textContent)).toEqual(["Book 1.5", "Book 1"]);
  expect(within(screen.getByRole("region", { name: "Book 1" })).getAllByRole("cell").map((item) => item.textContent)).toEqual(["Chapter 1.5", "Chapter 1"]);
  cleanup();
  view(data(Array.from({ length: 29 }, (_, i) => book(String(i + 1), [])), { edition_book_count: 10 }));
  expect(screen.getAllByRole("heading", { level: 3 })).toHaveLength(29);
  expect(screen.getByText(/Managed edition: 10 books/)).toBeTruthy();
  cleanup();
  view(data([book("1", Array.from({ length: 400 }, (_, i) => chapter(String(i + 1))))]));
  expect(screen.getAllByText(/^Chapter \d+$/)).toHaveLength(400);
  expect(screen.queryByRole("button", { name: /more chapters/ })).toBeNull();
});

it("shows an owned book's range without repeating how it was laid out", () => {
  view(data([
    book("1", [chapter("1"), chapter("7")], { owned: true, pages: 200, plan_state: "book" }),
    book("2", [chapter("8"), chapter("15")], { owned: true, pages: 200, estimated: true, plan_state: "book" }),
  ]));
  expect(screen.getByText(/ch\. 1–7 · 200 pages/)).toBeTruthy();
  expect(screen.getByText(/ch\. 8–15 · 200 pages/)).toBeTruthy();
  expect(screen.queryByText(/estimated/i)).toBeNull();
});

it("labels books placed by the sources' tags and by their pages", () => {
  view(data([
    book("1", [chapter("1"), chapter("2")], { owned: true, pages: 200, plan_state: "book", estimated: true, map_sources: ["operator", "source_tags"] }),
    book("2", [chapter("3"), chapter("4")], { owned: true, pages: 200, plan_state: "book", map_sources: ["operator", "content"] }),
  ], { content_notes: ["Chapter 5: its pages are not in book 2; the file is kept."] }));
  expect(screen.getByText(/ch\. 1–2 · 200 pages/)).toBeTruthy();
  expect(screen.getByText(/ch\. 3–4 · 200 pages/)).toBeTruthy();
  expect(screen.getByText(/Chapter 5: its pages are not in book 2; the file is kept\./)).toBeTruthy();
});

it("labels a printed table of contents as read from the book", () => {
  view(data([book("1", [chapter("1"), chapter("7")], { owned: true, pages: 200, plan_state: "book", map_sources: ["ocr"] })]));
  expect(screen.getByTitle(/ch\. 1–7 · read from book/)).toBeTruthy();
});


it("marks the bookmarked book while it is collapsed and links to its saved page", () => {
  const owned = book("2", [chapter("9", { downloaded: true })], { owned: true, open_release_id: "owned-book", plan_state: "book" });
  view(data([owned]), { chapter_id: "owned-book", label: "Volume 2", page_index: 41, url: "#/reader/owned-book?page=42" });
  const row = within(screen.getByRole("region", { name: "Book 2" }));
  expect(row.getByRole("button", { name: "Show separate chapter releases for book 2" }).getAttribute("aria-expanded")).toBe("false");
  expect(row.getByRole("link", { name: "Volume 2 · page 42 · Continue" }).getAttribute("href")).toBe("#/reader/owned-book?page=42");
});

it("keeps missing boundary evidence distinct from statistical estimates", () => {
  view(data([book("1", [chapter("1")]), book("2", [])], {
    map_confidence: { level: "partial", estimated_books: 0, unknown_books: 1, sources: ["operator"], hint_sources: [] },
  }));
  expect(screen.getByText(/1 book has no verified chapter assignment/)).toBeTruthy();
  expect(screen.queryByText(/split evenly/)).toBeNull();
});

it("opens manual boundaries from a running chapter shelf", () => {
  const { actions } = view(data([book("1", [chapter("1")]), book("2", [chapter("2")])], { form: "chapters" }));
  fireEvent.click(screen.getByRole("button", { name: "Edit book boundaries" }));
  expect(actions.onSetBoundaries).toHaveBeenCalledOnce();
  expect(screen.getByRole("region", { name: "Series chapters" })).toBeTruthy();
});

it("keeps owned volume rows consistent with and without indexed chapter releases", () => {
  const release = { id: "source-chapter", downloaded: false } as SeriesUnitChapter["releases"][number];
  const indexed = book("1", [chapter("1", { releases: [release], covered_by_volume: "1", missing: false })], { owned: true, status: "owned" });
  const volumeOnly = book("2", [], { owned: true, status: "owned" });
  view(data([indexed, volumeOnly], { uniform: "books", series_unit: "volumes" }));
  expect(screen.queryByRole("button", { name: /Expand book|Collapse book/ })).toBeNull();
  expect(screen.queryByText("Chapter 1")).toBeNull();
  fireEvent.change(screen.getByLabelText("Filter series units"), { target: { value: "monitored" } });
  expect(screen.queryByText("Chapter 1")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Show separate chapter releases for book 1" }));
  expect(screen.getByText("Chapter 1")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Hide separate chapter releases for book 1" }));
  expect(screen.queryByText("Chapter 1")).toBeNull();
  expect(within(screen.getByRole("region", { name: "Book 2" })).queryByRole("button", { name: /separate chapter releases/ })).toBeNull();
});
