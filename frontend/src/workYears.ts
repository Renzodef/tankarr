import type { CanonicalMetadata } from "./types";

type WorkYearInput = {
  year?: number | null;
  publication_year?: number | null;
  preferred_language?: string | null;
  metadata?: Pick<CanonicalMetadata, "year" | "work" | "editions"> | null;
};

export type WorkYears = {
  original: number | null;
  publication: number | null;
  sort: number | null;
  label: string | null;
  title: string | null;
};

function validYear(value: unknown): number | null {
  if (typeof value !== "number" || !Number.isInteger(value)) return null;
  return value >= 1000 && value <= 9999 ? value : null;
}

function editionPublicationYear(work: WorkYearInput): number | null {
  const editions = (work.metadata?.editions ?? []).filter(
    (edition) => validYear(edition.publication_year) !== null,
  );
  if (!editions.length) return null;

  const language = work.preferred_language?.trim().toLocaleLowerCase();
  const matching = language
    ? editions.filter((edition) => edition.language?.toLocaleLowerCase() === language)
    : editions;
  const candidates = matching.length
    ? matching
    : editions.filter((edition) => !edition.language);
  if (!candidates.length) return null;
  return Math.min(...candidates.map((edition) => validYear(edition.publication_year)!));
}

export function workYears(work: WorkYearInput): WorkYears {
  const original =
    validYear(work.metadata?.work?.year) ??
    validYear(work.metadata?.year) ??
    validYear(work.year);
  const rawPublication =
    editionPublicationYear(work) ?? validYear(work.publication_year);
  const publication = rawPublication === original ? null : rawPublication;
  const sort = original ?? rawPublication;

  if (original && publication) {
    return {
      original,
      publication,
      sort,
      label: `${original} · pub. ${publication}`,
      title: `Original work: ${original} · Publication: ${publication}`,
    };
  }
  const onlyYear = original ?? rawPublication;
  return {
    original,
    publication,
    sort,
    label: onlyYear ? String(onlyYear) : null,
    title: onlyYear
      ? original
        ? `Original work: ${onlyYear}`
        : `Publication: ${onlyYear}`
      : null,
  };
}
