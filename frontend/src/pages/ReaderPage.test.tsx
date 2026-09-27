import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { api } from "../api";
import type { ReaderBook } from "../types";
import ReaderPage from "./ReaderPage";

afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

it("opens a manga manifest that predates reading direction metadata", async () => {
  vi.spyOn(api, "readerBook").mockResolvedValue({
    release_id: "book-1", manga_id: "series-1", series_title: "Example", book_title: "Volume 1",
    display_mode: "manga", display_mode_source: "automatic", page_count: 2, page_index: 0,
    bookmarked: false, bookmark_page_index: null, previous_release_id: null, next_release_id: null,
  } as ReaderBook);
  render(<ReaderPage id="book-1" initialPage={null} />);
  expect(await screen.findByRole("button", { name: "Pages · RTL" })).toBeTruthy();
  expect((screen.getByRole("slider") as HTMLInputElement).dir).toBe("rtl");
});

it("preloads nearby pages after the visible page loads", async () => {
  const requests: Array<{ url: string; priority: string }> = [];
  class PreloadImage {
    fetchPriority = "";
    set src(value: string) { requests.push({ url: value, priority: this.fetchPriority }); }
  }
  vi.stubGlobal("Image", PreloadImage);
  vi.spyOn(api, "readerBook").mockResolvedValue({
    release_id: "book-1", page_version: "newhash", manga_id: "series-1", series_title: "Example", book_title: "Volume 1",
    display_mode: "manga", display_mode_source: "automatic", reading_direction: "rtl", page_count: 5,
    page_index: 0, bookmarked: false, bookmark_page_index: null,
    previous_release_id: null, next_release_id: null,
  });

  render(<ReaderPage id="book-1" initialPage={null} />);
  const first = await screen.findByAltText("Page 1 of 5");
  expect(first.getAttribute("src")).toBe("/api/reader/books/book-1/pages/0?v=newhash");
  expect(first.getAttribute("fetchpriority")).toBe("high");
  expect(requests).toEqual([]);
  fireEvent.load(first);
  expect(requests).toEqual([
    { url: "/api/reader/books/book-1/pages/1?v=newhash", priority: "high" },
    { url: "/api/reader/books/book-1/pages/2?v=newhash", priority: "low" },
  ]);
  fireEvent.click(screen.getByRole("button", { name: "Next page" }));
  fireEvent.load(await screen.findByAltText("Page 2 of 5"));
  expect(requests).toEqual([
    { url: "/api/reader/books/book-1/pages/1?v=newhash", priority: "high" },
    { url: "/api/reader/books/book-1/pages/2?v=newhash", priority: "low" },
    { url: "/api/reader/books/book-1/pages/3?v=newhash", priority: "low" },
    { url: "/api/reader/books/book-1/pages/0?v=newhash", priority: "low" },
  ]);
});

it("warms the whole webtoon after a visible page loads, one page at a time", async () => {
  Object.defineProperty(HTMLElement.prototype, "scrollIntoView", { configurable: true, value: vi.fn() });
  const requested: Array<{ url: string; priority: RequestPriority | undefined }> = [];
  let finishFirstPage: () => void = () => {};
  const firstPage = new Promise<void>((resolve) => { finishFirstPage = resolve; });
  vi.stubGlobal("fetch", vi.fn(async (url: string, options?: RequestInit) => {
    requested.push({ url, priority: options?.priority });
    return { ok: true, arrayBuffer: async () => {
      if (url.endsWith("/0")) await firstPage;
      return new ArrayBuffer(1);
    } };
  }));
  vi.spyOn(api, "readerBook").mockResolvedValue({
    release_id: "book-1", manga_id: "series-1", series_title: "Example", book_title: "Chapter 1",
    display_mode: "webtoon", display_mode_source: "automatic", reading_direction: "ltr",
    page_count: 3, page_index: 0, bookmarked: false, bookmark_page_index: null,
    previous_release_id: null, next_release_id: null,
  });

  render(<ReaderPage id="book-1" initialPage={null} />);
  const first = await screen.findByAltText("Page 1 of 3");
  expect(requested).toEqual([]);
  fireEvent.load(first);
  await waitFor(() => expect(requested).toHaveLength(1), { timeout: 3000 });
  expect(requested).toHaveLength(1);
  finishFirstPage();
  await waitFor(() => expect(requested).toHaveLength(3));
  expect(requested).toEqual([0, 1, 2].map((index) => ({
    url: `/api/reader/books/book-1/pages/${index}`, priority: "low",
  })));
  expect(screen.queryByRole("button", { name: /preload/i })).toBeNull();
});

it("reprioritizes background warming when the page changes and stops on close", async () => {
  const signals: AbortSignal[] = [];
  vi.stubGlobal("fetch", vi.fn((_url: string, options: RequestInit) => {
    signals.push(options.signal as AbortSignal);
    return new Promise(() => {});
  }));
  vi.spyOn(api, "readerBook").mockResolvedValue({
    release_id: "book-1", manga_id: "series-1", series_title: "Example", book_title: "Volume 1",
    display_mode: "manga", display_mode_source: "automatic", reading_direction: "rtl",
    page_count: 3, page_index: 0, bookmarked: false, bookmark_page_index: null,
    previous_release_id: null, next_release_id: null,
  });

  const view = render(<ReaderPage id="book-1" initialPage={null} />);
  const first = await screen.findByAltText("Page 1 of 3");
  fireEvent.load(first);
  await waitFor(() => expect(signals).toHaveLength(1), { timeout: 3000 });
  expect(signals[0].aborted).toBe(false);
  fireEvent.click(screen.getByRole("button", { name: "Next page" }));
  expect(signals[0].aborted).toBe(true);
  fireEvent.load(await screen.findByAltText("Page 2 of 3"));
  await waitFor(() => expect(signals).toHaveLength(2), { timeout: 3000 });
  view.unmount();
  expect(signals[1].aborted).toBe(true);
});

it("stores reading state only when the bookmark button is clicked", async () => {
  vi.spyOn(api, "readerBook").mockResolvedValue({
    release_id: "book-1",
    manga_id: "series-1",
    series_title: "Example",
    book_title: "Volume 1",
    display_mode: "manga",
    display_mode_source: "automatic",
    reading_direction: "rtl",
    page_count: 2,
    page_index: 0,
    bookmarked: false,
    bookmark_page_index: null,
    previous_release_id: null,
    next_release_id: null,
  });
  const save = vi.spyOn(api, "saveReaderBookmark").mockResolvedValue({
    chapter_id: "book-1",
    page_index: 1,
  });
  const clear = vi.spyOn(api, "clearReaderBookmark").mockResolvedValue(undefined);

  render(<ReaderPage id="book-1" initialPage={null} />);
  await screen.findByText("1 / 2");
  fireEvent.click(screen.getByRole("button", { name: "Next page" }));
  expect(await screen.findByText("2 / 2")).toBeTruthy();
  expect(save).not.toHaveBeenCalled();
  expect(clear).not.toHaveBeenCalled();

  fireEvent.click(screen.getByRole("button", { name: "Save bookmark" }));
  await waitFor(() => expect(save).toHaveBeenCalledWith("series-1", "book-1", 1));
  fireEvent.click(screen.getByRole("button", { name: "Remove bookmark" }));
  await waitFor(() => expect(clear).toHaveBeenCalledWith("series-1"));
});


it("resumes the saved page, keeps it when paging, and uses the manga direction", async () => {
  vi.spyOn(api, "readerBook").mockResolvedValue({
    release_id: "book-2", manga_id: "series-1", series_title: "Example", book_title: "Volume 2",
    display_mode: "manga", display_mode_source: "automatic", reading_direction: "rtl", page_count: 8, page_index: 4,
    bookmarked: true, bookmark_page_index: 4, previous_release_id: "book-1", next_release_id: null,
  });
  const save = vi.spyOn(api, "saveReaderBookmark");
  render(<ReaderPage id="book-2" initialPage={null} />);
  await screen.findByText("5 / 8");
  expect(screen.getByRole("status").textContent).toBe("Bookmark saved · Volume 2 · page 5");
  const slider = screen.getByRole("slider") as HTMLInputElement;
  expect(slider.dir).toBe("rtl");
  expect(slider.getAttribute("aria-orientation")).toBe("horizontal");
  fireEvent.change(slider, { target: { value: "7" } });
  expect(screen.getByText("7 / 8")).toBeTruthy();
  expect(screen.getByRole("status").textContent).toContain("page 5");
  expect(save).not.toHaveBeenCalled();
  // Inputs retain native keyboard handling, without the reader also moving.
  const key = new KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true, cancelable: true });
  slider.dispatchEvent(key);
  expect(key.defaultPrevented).toBe(false);
  expect(screen.getByText("7 / 8")).toBeTruthy();
});

it("uses a top-to-bottom slider for webtoons and aligns the saved page after images load", async () => {
  const scroll = vi.fn();
  Object.defineProperty(HTMLElement.prototype, "scrollIntoView", { configurable: true, value: scroll });
  vi.spyOn(api, "readerBook").mockResolvedValue({
    release_id: "book-1", manga_id: "series-1", series_title: "Example", book_title: "Chapter 1",
    display_mode: "webtoon", display_mode_source: "automatic", reading_direction: "ltr", page_count: 3, page_index: 2,
    bookmarked: true, bookmark_page_index: 2, previous_release_id: null, next_release_id: null,
  });
  render(<ReaderPage id="book-1" initialPage={null} />);
  await screen.findByText("3 / 3");
  const slider = screen.getByRole("slider") as HTMLInputElement;
  expect(slider.dir).toBe("ltr");
  expect(slider.style.writingMode).toBe("vertical-lr");
  expect(slider.getAttribute("aria-orientation")).toBe("vertical");
  fireEvent.load(screen.getByAltText("Page 1 of 3"));
  expect(scroll.mock.instances.at(-1)).toBe(screen.getByAltText("Page 3 of 3"));
  fireEvent.click(screen.getByRole("button", { name: "Webtoon · Vertical" }));
  expect(slider.dir).toBe("ltr");
  expect(slider.getAttribute("aria-orientation")).toBe("horizontal");
});

it("uses left-to-right paging for a Chinese book", async () => {
  vi.spyOn(api, "readerBook").mockResolvedValue({
    release_id: "book-1", manga_id: "series-1", series_title: "Example Manhua", book_title: "Volume 1",
    display_mode: "manga", display_mode_source: "metadata", reading_direction: "ltr", page_count: 3, page_index: 0,
    bookmarked: false, bookmark_page_index: null, previous_release_id: null, next_release_id: null,
  });
  render(<ReaderPage id="book-1" initialPage={null} />);
  await screen.findByText("1 / 3");
  expect((screen.getByRole("slider") as HTMLInputElement).dir).toBe("ltr");
  expect(screen.getByRole("button", { name: "Pages · LTR" })).toBeTruthy();
  fireEvent.keyDown(document.body, { key: "ArrowRight" });
  expect(screen.getByText("2 / 3")).toBeTruthy();
  fireEvent.keyDown(document.body, { key: "ArrowLeft" });
  expect(screen.getByText("1 / 3")).toBeTruthy();
});
