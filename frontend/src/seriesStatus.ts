import type { LibraryCountSummary, Manga } from "./types";
import { msg, t, tn } from "./i18n";

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
  const manualTitle = manual ? t("Manually set.") + " " : "";

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
      label: t("Ended"),
      kind: "success",
      title: `${manualTitle}${t("No new chapters are expected from this work.")}`,
    };
  }
  const paused =
    manga.publication?.paused ?? ["hiatus", "on_hiatus", "paused"].includes(value);
  if (paused) {
    const names: Record<string, string> = {
      mangabaka: "MangaBaka",
      myanimelist: "MyAnimeList",
      mangaupdates: "MangaUpdates",
      official: t("the official platform"),
      manual: t("you"),
      metadata: t("the catalogue"),
      provider: t("the source"),
    };
    const reporters = (manga.publication?.pause_sources ?? [])
      .map((source) => names[source] ?? source)
      .join(", ");
    return {
      key: "hiatus",
      label: t("Hiatus"),
      kind: "warn",
      title: `${manualTitle}${t("Publication is paused according to {reporters}. Monitoring continues and the work is rechecked on every refresh; no new chapters are expected until it resumes.", { reporters: reporters || t("the catalogue") })}`,
    };
  }
  if (["ongoing", "continuing", "publishing", "releasing"].includes(value)) {
    return {
      key: "continuing",
      label: t("Continuing"),
      kind: "info",
      title: `${manualTitle}${t("Future chapters may still be released.")}`,
    };
  }
  return {
    key: "unknown",
    label: t("Status unknown"),
    kind: "muted",
    title: t("Tankarr cannot yet determine whether future chapters are expected."),
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
    chapter: [msg("chapter"), msg("chapters")],
    volume: [msg("volume"), msg("volumes")],
    issue: [msg("issue"), msg("issues")],
    book: [msg("book"), msg("books")],
  } as const;
  return t(labels[unit][count === 1 ? 0 : 1]);
}

export function libraryStatus(manga: Manga): SeriesStatusBadge {
  const summary = seriesCounts(manga);
  const total = summary.total_count;
  const downloaded = summary.downloaded_count;
  const missing = summary.missing_count;
  const noun = countNoun(summary.unit, total);
  const source = summary.source ? " " + t("according to {source}", { source: summary.source }) : "";
  const counted = { downloaded, total, noun, source };
  const count = summary.reference_kind === "available"
    ? t("{downloaded} of {total} available {noun}{source}", counted)
    : summary.expected_count !== null
      ? t("{downloaded} of {total} expected {noun}{source}", counted)
      : t("{downloaded} of {total} currently indexed {noun}{source}", counted);

  if (manga.library_status_override === "up_to_date" && total > 0) {
    const suppressed = summary.raw_missing_count ?? Math.max(0, total - downloaded);
    return {
      key: "up_to_date",
      label: t("Manual up to date"),
      kind: "success",
      title: suppressed
        ? t("Manually marked up to date. {downloaded} of {total} {noun} are imported; {suppressed} remain absent and are excluded from Missing and Wanted until Automatic is restored.", { downloaded, total, noun, suppressed })
        : t("Manually marked up to date. All {total} {noun} are already imported.", { total, noun }),
    };
  }

  if (total === 0) {
    return {
      key: "missing",
      label: t("Missing"),
      kind: "warn",
      title: t("No {noun} are currently available from the configured sources.", { noun: countNoun(summary.unit, 0) }),
    };
  }
  if (missing > 0) {
    return {
      key: "missing",
      label: tn(missing, "{count} missing", "{count} missing"),
      kind: "warn",
      title: t("{count} imported; {missing} still missing from the managed library.", { count, missing }) + (
        summary.unindexed_missing_count
          ? " " + t("{indexed} can be matched to the current provider index; {unindexed} are not exposed there.", { indexed: summary.indexed_missing_count, unindexed: summary.unindexed_missing_count })
          : ""
      ),
    };
  }
  if (summary.short_of_books) {
    // The catalogues agree on the books; a book holds several chapters, so a
    // library with no more chapters than the work has books is short however
    // few chapters the sources index (Moonlight Mile: 24 indexed, 24 books).
    const books = summary.volume_progress?.expected ?? 0;
    return {
      key: "missing",
      label: t("{count} indexed", { count: downloaded }),
      kind: "warn",
      title: t("{count}. How many {noun} the work has is not corroborated, but the catalogues agree it runs to {books} books: {downloaded} {noun} cannot be all of it. The sources index no more; the rest has to come from elsewhere.", { count, noun, books, downloaded }),
    };
  }
  if (summary.reference_unknown) {
    return {
      key: "unknown",
      label: t("Indexed chapters present"),
      kind: "info",
      title: t("All {total} currently indexed {noun} are imported. The complete chapter count and numbering have not been verified.", { total, noun }),
    };
  }
  return {
    key: "up_to_date",
    label: t("Up to date"),
    kind: "success",
    title: summary.reference_kind === "available"
      ? t("All {total} available {noun} are imported. Up to date with the known releases; publication may continue.", { total, noun })
      : summary.expected_count !== null
        ? t("All {total} expected {noun} are imported.", { total, noun })
        : t("All {total} currently indexed {noun} are imported.", { total, noun }),
  };
}

/** Owned material outside the reference sequence, without renumbering it. */
export function additionalContentLabel(counts: LibraryCountSummary): string {
  const prologues = counts.additional_content?.prologues ?? 0;
  const extras = counts.additional_content?.extras ?? 0;
  return [
    prologues ? tn(prologues, "{count} prologue", "{count} prologues") : null,
    extras ? tn(extras, "{count} extra", "{count} extras") : null,
  ].filter(Boolean).join(" + ");
}
