import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { api } from "../api";
import type { ReaderBookmark } from "../types";
import BookmarksPage from "./BookmarksPage";

afterEach(() => { cleanup(); vi.restoreAllMocks(); });

const saved: ReaderBookmark = {
  manga_id: "series-1", manga_title: "Reader Example", manga_cover_url: null,
  chapter_id: "book-2", label: "Volume 2", page_index: 41,
  updated_at: "2026-01-03T12:00:00Z", url: "#/reader/book-2?page=42",
};

it("lists saved positions and opens the exact bookmarked page", async () => {
  vi.spyOn(api, "readerBookmarks").mockResolvedValue([saved]);
  render(<BookmarksPage />);
  const resume = await screen.findByRole("link", { name: "Continue reading" });
  expect(resume.getAttribute("href")).toBe("#/reader/book-2?page=42");
  expect(screen.getByText("Volume 2 · page 42")).toBeTruthy();
  expect(screen.getByRole("link", { name: "Reader Example" }).getAttribute("href")).toBe("#/series/series-1");
});

it("removes a saved position and shows the empty state", async () => {
  vi.spyOn(api, "readerBookmarks").mockResolvedValue([saved]);
  const remove = vi.spyOn(api, "clearReaderBookmark").mockResolvedValue(undefined);
  render(<BookmarksPage />);
  fireEvent.click(await screen.findByRole("button", { name: "Remove bookmark for Reader Example" }));
  await waitFor(() => expect(remove).toHaveBeenCalledWith("series-1"));
  expect(await screen.findByText("No bookmarks saved")).toBeTruthy();
});

it("sorts bookmarks by saved date or series title in either direction", async () => {
  const bookmarks = [
    { ...saved, manga_id: "zebra", manga_title: "Zebra", updated_at: "2026-01-02T12:00:00Z" },
    { ...saved, manga_id: "mango", manga_title: "Mango", updated_at: "2026-01-01T12:00:00Z" },
    { ...saved, manga_id: "alpha", manga_title: "Alpha", updated_at: "2026-01-03T12:00:00Z" },
  ];
  vi.spyOn(api, "readerBookmarks").mockResolvedValue(bookmarks);
  render(<BookmarksPage />);

  const titles = () => screen.getAllByRole("article").map((article) =>
    article.querySelector(".bookmark-series-title")?.textContent,
  );
  await screen.findByRole("link", { name: "Alpha" });
  expect(titles()).toEqual(["Alpha", "Zebra", "Mango"]);

  fireEvent.click(screen.getByRole("button", { name: "Sort ascending" }));
  expect(titles()).toEqual(["Mango", "Zebra", "Alpha"]);

  fireEvent.change(screen.getByLabelText("Sort bookmarks by"), { target: { value: "title" } });
  expect(titles()).toEqual(["Alpha", "Mango", "Zebra"]);

  fireEvent.click(screen.getByRole("button", { name: "Sort descending" }));
  expect(titles()).toEqual(["Zebra", "Mango", "Alpha"]);

  fireEvent.change(screen.getByLabelText("Sort bookmarks by"), { target: { value: "updated" } });
  expect(titles()).toEqual(["Alpha", "Zebra", "Mango"]);
});
