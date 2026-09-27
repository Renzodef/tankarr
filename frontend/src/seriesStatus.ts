import type { LibraryCountSummary, Manga } from "./types";

export type SeriesStatusBadge = {
  key:
    | "continuing"
    | "hiatus"
    | "ended"
    | "unknown"
    | "up_to_date"
    | "missing";
  label: string;
  kind: "success" | "info" | "warn" | "danger" | "muted";
  title: string;
};

export function publicationStatus(manga: Manga): SeriesStatusBadge {
  const manual = manga.status_override;
  const value = (
    manga.publication?.status || manual || manga.metadata?.status || manga.status || ""
  )
    .trim()
    .toLocaleLowerCase();
  const manualTitle = manual ? "Manually set. " : "";

  if (
    [
      "ended",
      "completed",
      "complete",
      "finished",
      "abandoned",
      "dropped",
      "cancelled",
      "canceled",
      "discontinued",
    ].includes(value)
  ) {
    return {
      key: "ended",
      // A finished work is good news, not a caveat: the library can be
      // complete and stay complete.
      label: "Ended",
      kind: "success",
      title: `${manualTitle}No new chapters are expected from this work.`,
    };
  }
  const paused =
    manga.publication?.paused ?? ["hiatus", "on_hiatus", "paused"].includes(value);
  if (paused) {
    const names: Record<string, string> = {
      mangabaka: "MangaBaka",
      myanimelist: "MyAnimeList",
      mangaupdates: "MangaUpdates",
      official: "the official platform",
      manual: "you",
      metadata: "the catalogue",
      provider: "the source",
    };
    const reporters = (manga.publication?.pause_sources ?? [])
      .map((source) => names[source] ?? source)
      .join(", ");
    return {
      key: "hiatus",
      label: "Hiatus",
      kind: "warn",
      title: `${manualTitle}Publication is paused according to ${reporters || "the catalogue"}. Monitoring continues and the work is rechecked on every refresh; no new chapters are expected until it resumes.`,
    };
  }
  if (["ongoing", "continuing", "publishing", "releasing"].includes(value)) {
    return {
      key: "continuing",
      label: "Continuing",
      kind: "info",
      title: `${manualTitle}Future chapters may still be released.`,
    };
  }
  return {
    key: "unknown",
    label: "Status unknown",
    kind: "muted",
    title: "Tankarr cannot yet determine whether future chapters are expected.",
  };
}

export function seriesCounts(manga: Manga): LibraryCountSummary {
  const available = Math.max(0, manga.chapter_count);
  const downloaded = Math.max(0, manga.downloaded_count);
  return (
    manga.library_count ?? {
      unit: "chapter",
      available_count: available,
      downloaded_count: downloaded,
      expected_count: null,
      total_count: available,
      missing_count: Math.max(0, available - downloaded),
      indexed_missing_count: Math.max(0, available - downloaded),
      unindexed_missing_count: 0,
      source: null,
      metadata_field: "chapter_count",
      count_conflict: false,
      catalogue_expected_count: null,
      catalogue_source: null,
      segmentation_differs: false,
      count_note: null,
    }
  );
}

export function countNoun(unit: LibraryCountSummary["unit"], count: number) {
  const labels = {
    chapter: ["chapter", "chapters"],
    volume: ["volume", "volumes"],
    issue: ["issue", "issues"],
    book: ["book", "books"],
  } as const;
  return labels[unit][count === 1 ? 0 : 1];
}

export function libraryStatus(manga: Manga): SeriesStatusBadge {
  const summary = seriesCounts(manga);
  const total = summary.total_count;
  const downloaded = summary.downloaded_count;
  const missing = summary.missing_count;
  const noun = countNoun(summary.unit, total);
  const source = summary.source ? ` according to ${summary.source}` : "";
  const expected = summary.reference_kind === "available"
    ? " available"
    : summary.expected_count !== null ? " expected" : " currently indexed";
  const count = `${downloaded} of ${total}${expected} ${noun}${source}`;

  if (manga.library_status_override === "up_to_date" && total > 0) {
    const suppressed = summary.raw_missing_count ?? Math.max(0, total - downloaded);
    return {
      key: "up_to_date",
      label: "Manual up to date",
      kind: "success",
      title: suppressed
        ? `Manually marked up to date. ${downloaded} of ${total} ${noun} are imported; ${suppressed} remain absent and are excluded from Missing and Wanted until Automatic is restored.`
        : `Manually marked up to date. All ${total} ${noun} are already imported.`,
    };
  }

  if (total === 0) {
    return {
      key: "missing",
      label: "Missing",
      kind: "warn",
      title: `No ${countNoun(summary.unit, 0)} are currently available from the configured sources.`,
    };
  }
  if (missing > 0) {
    return {
      key: "missing",
      label: `${missing} missing`,
      kind: "warn",
      title: `${count} imported; ${missing} still missing from the managed library.${
        summary.unindexed_missing_count
          ? ` ${summary.indexed_missing_count} can be matched to the current provider index; ${summary.unindexed_missing_count} are not exposed there.`
          : ""
      }`,
    };
  }
  if (summary.short_of_books) {
    // The catalogues agree on the books; a book holds several chapters, so a
    // library with no more chapters than the work has books is short however
    // few chapters the sources index (Moonlight Mile: 24 indexed, 24 books).
    const books = summary.volume_progress?.expected ?? 0;
    return {
      key: "missing",
      label: `${downloaded} indexed`,
      kind: "warn",
      title: `${count}. How many ${noun} the work has is not corroborated, but the catalogues agree it runs to ${books} books: ${downloaded} ${noun} cannot be all of it. The sources index no more; the rest has to come from elsewhere.`,
    };
  }
  if (summary.reference_unknown) {
    return {
      key: "unknown",
      label: "Indexed chapters present",
      kind: "info",
      title: `All ${total} currently indexed ${noun} are imported. The complete chapter count and numbering have not been verified.`,
    };
  }
  return {
    key: "up_to_date",
    label: "Up to date",
    kind: "success",
    title: summary.reference_kind === "available"
      ? `All ${total} available ${noun} are imported. Up to date with the known releases; publication may continue.`
      : `All ${total}${summary.expected_count !== null ? " expected" : " currently indexed"} ${noun} are imported.`,
  };
}

/** Owned material outside the reference sequence, without renumbering it. */
export function additionalContentLabel(counts: LibraryCountSummary): string {
  return ([['prologues', 'prologue'], ['extras', 'extra']] as const)
    .flatMap(([key, noun]) => {
      const count = counts.additional_content?.[key] ?? 0;
      return count ? [`${count} ${noun}${count === 1 ? '' : 's'}`] : [];
    }).join(' + ');
}
