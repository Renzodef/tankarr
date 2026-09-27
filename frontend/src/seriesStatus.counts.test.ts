import { describe, expect, it } from "vitest";

import { additionalContentLabel, libraryStatus } from "./seriesStatus";
import type { LibraryCountSummary, Manga } from "./types";

function series(count: Partial<LibraryCountSummary>): Manga {
  const library_count = {
    unit: "chapter",
    available_count: 24,
    downloaded_count: 24,
    expected_count: null,
    total_count: 24,
    missing_count: 0,
    indexed_missing_count: 0,
    unindexed_missing_count: 0,
    source: "kitsu",
    metadata_field: "chapter_count",
    count_conflict: false,
    ...count,
  } as LibraryCountSummary;
  return { chapter_count: 24, downloaded_count: 24, library_count } as unknown as Manga;
}

describe("libraryStatus", () => {
  it("does not call a work up to date when its own books disprove it", () => {
    // Moonlight Mile: one catalogue says 206 chapters and is not corroborated,
    // but two agree the work runs to 24 books. 24 indexed chapters cannot be
    // all of a 24-book work.
    const badge = libraryStatus(
      series({
        reference_unknown: true,
        short_of_books: true,
        volume_progress: { owned: 0, expected: 24 },
      }),
    );

    expect(badge.key).toBe("missing");
    expect(badge.label).toBe("24 indexed");
    expect(badge.title).toContain("24 books");
  });

  it("still calls a finished library up to date", () => {
    const badge = libraryStatus(series({ expected_count: 24 }));

    expect(badge.key).toBe("up_to_date");
  });
});

it("distinguishes a complete index from a verified complete work", () => {
  const badge = libraryStatus(series({ reference_unknown: true }));
  expect(badge.key).toBe("unknown");
  expect(badge.label).toBe("Indexed chapters present");
});

it("shows prologues separately while preserving the reference count", () => {
  const manga = series({
    expected_count: 24,
    reference_unknown: false,
    additional_content: { prologues: 2, extras: 1 },
  });
  expect(additionalContentLabel(manga.library_count!)).toBe("2 prologues + 1 extra");
  expect(libraryStatus(manga).label).toBe("Up to date");
  expect(manga.library_count!.total_count).toBe(24);
});

it("calls a continuing available index up to date without claiming completion", () => {
  const manga = series({
    reference_kind: "available", reference_unknown: false,
    expected_count: 65, total_count: 65, available_count: 65, downloaded_count: 65,
    catalogue_expected_count: 70,
  });
  expect(libraryStatus(manga).label).toBe("Up to date");
  expect(libraryStatus(manga).title).toContain("65 available chapters");
  expect(libraryStatus(manga).title).toContain("publication may continue");
});
