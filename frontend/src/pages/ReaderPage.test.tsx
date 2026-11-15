import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
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

it("warms only four distant pages of a long webtoon, streaming one at a time", async () => {
  Object.defineProperty(HTMLElement.prototype, "scrollIntoView", { configurable: true, value: vi.fn() });
  const requested: string[] = [];
  let finishFirstPage: () => void = () => {};
  const firstPage = new Promise<void>((resolve) => { finishFirstPage = resolve; });
  vi.stubGlobal("fetch", vi.fn(async (url: string) => {
    requested.push(url);
    return new Response(new ReadableStream({ async start(controller) {
      controller.enqueue(new Uint8Array([1]));
      if (url.endsWith("/3")) await firstPage;
      controller.close();
    } }));
  }));
  vi.spyOn(api, "readerBook").mockResolvedValue({
    release_id: "book-1", manga_id: "series-1", series_title: "Example", book_title: "Chapter 1",
    display_mode: "webtoon", display_mode_source: "automatic", reading_direction: "ltr",
    page_count: 200, page_index: 0, bookmarked: false, bookmark_page_index: null,
    previous_release_id: null, next_release_id: null,
  });

  render(<ReaderPage id="book-1" initialPage={null} />);
  const first = await screen.findByAltText("Page 1 of 200");
  expect(requested).toEqual([]);
  fireEvent.load(first);
  await waitFor(() => expect(requested).toHaveLength(1), { timeout: 3000 });
  finishFirstPage();
  await waitFor(() => expect(requested).toHaveLength(4));
  expect(requested).toEqual([3, 4, 5, 6].map(index => `/api/reader/books/book-1/pages/${index}`));
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 50)); });
  expect(requested).toHaveLength(4);
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
    page_count: 10, page_index: 0, bookmarked: false, bookmark_page_index: null,
    previous_release_id: null, next_release_id: null,
  });

  const view = render(<ReaderPage id="book-1" initialPage={null} />);
  const first = await screen.findByAltText("Page 1 of 10");
  fireEvent.load(first);
  await waitFor(() => expect(signals).toHaveLength(1), { timeout: 3000 });
  expect(signals[0].aborted).toBe(false);
  fireEvent.click(screen.getByRole("button", { name: "Next page" }));
  expect(signals[0].aborted).toBe(true);
  fireEvent.load(await screen.findByAltText("Page 2 of 10"));
  await waitFor(() => expect(signals).toHaveLength(2), { timeout: 3000 });
  view.unmount();
  expect(signals[1].aborted).toBe(true);
});

it("does not wrap prefetch to the start or fetch distant pages in data-saving mode", async () => {
  const fetchMock = vi.fn();
  const imageMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
  vi.stubGlobal("Image", class { set src(value: string) { imageMock(value); } });
  vi.spyOn(api, "readerBook").mockResolvedValue({
    release_id: "book-1", manga_id: "series-1", series_title: "Example", book_title: "Volume 1",
    display_mode: "manga", display_mode_source: "automatic", reading_direction: "ltr",
    page_count: 200, page_index: 199, bookmarked: false, bookmark_page_index: null,
    previous_release_id: null, next_release_id: null,
  });
  const view = render(<ReaderPage id="book-1" initialPage={null} />);
  fireEvent.load(await screen.findByAltText("Page 200 of 200"));
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 1100)); });
  expect(fetchMock).not.toHaveBeenCalled();
  view.unmount(); imageMock.mockClear();

  Object.defineProperty(navigator, "connection", { configurable: true, value: { saveData: true } });
  try {
    render(<ReaderPage id="book-1" initialPage={0} />);
    fireEvent.load(await screen.findByAltText("Page 1 of 200"));
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 1100)); });
    expect(fetchMock).not.toHaveBeenCalled();
    expect(imageMock).not.toHaveBeenCalled();
  } finally { Reflect.deleteProperty(navigator, "connection"); }
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
  // The counter is in the DOM one commit before the effect that attaches the
  // keyboard listener has run; flush it, or the key press can arrive first.
  await act(async () => {});
  expect((screen.getByRole("slider") as HTMLInputElement).dir).toBe("ltr");
  expect(screen.getByRole("button", { name: "Pages · LTR" })).toBeTruthy();
  fireEvent.keyDown(document.body, { key: "ArrowRight" });
  expect(screen.getByText("2 / 3")).toBeTruthy();
  fireEvent.keyDown(document.body, { key: "ArrowLeft" });
  expect(screen.getByText("1 / 3")).toBeTruthy();
});
