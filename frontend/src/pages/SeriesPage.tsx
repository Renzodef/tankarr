import type { ReactNode } from "react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ApiError, api } from "../api";
import { LoadError } from "../components/LoadError";
import ChapterMapEditor from "../components/ChapterMapEditor";
import SeriesUnitGroups from "../components/SeriesUnitGroups";
import SeriesAuditDialog from "../components/SeriesAuditDialog";
import TranslationPanel from "../components/TranslationPanel";
import {
  ChapterReleaseSearchModal,
  type ChapterSearchTarget,
} from "../components/ChapterReleaseSearchModal";
import {
  Cover,
  Icon,
  LANGUAGES,
  MONITOR_OPTIONS,
  Modal,
  Spinner,
  StatusPill,
  authorPath,
  artworkThumbnailUrl,
  formatBytes,
  formatDate,
  humanize,
  jobStatusPill,
  languageName,
  monitorLabel,
  navigate,
  providerChainLabel,
  seriesCoverUrl,
  useApp,
} from "../components";
import { additionalContentLabel, libraryStatus, publicationStatus, seriesCounts } from "../seriesStatus";
import { workYears } from "../workYears";
import type {
  CanonicalMetadata,
  ArtworkSelection,
  Chapter,
  ChapterSlot,
  SeriesBookGroup,
  SeriesUnitChapter,
  SeriesUnits,
  DuplicateRetirementResult,
  DeleteResult,
  Job,
  LibraryStatusOverride,
  Manga,
  SeriesCalendar,
  MangaSummary,
  MetadataCorrelationSource,
  MonitorMode,
  PublicationStatusOverride,
  TorrentRelease,
  ReaderBookLink,
  ReaderLink,
  TorrentDownload,
  SeriesUnit,
  UnitCoverage,
} from "../types";

type LogicalChapter = ChapterSlot;

type ActionNotice = {
  kind: "success" | "info" | "error";
  text: string;
};

type MetadataRefreshNotice = {
  kind: "info" | "success" | "error";
  text: string;
};

type FileDeletionTarget =
  | { kind: "chapter"; chapter: Chapter }
  | { kind: "volume"; volume: string; language: string; chapters: Chapter[] }
  | { kind: "duplicates"; chapters: Chapter[]; volume: string };

const ACTIVE_JOB_STATUSES = new Set(["queued", "running", "downloading", "packaging", "importing"]);
const PENDING_ARTWORK_UPLOAD = "__pending-artwork-upload__";
const MAX_ARTWORK_UPLOAD_BYTES = 20 * 1024 * 1024;
const SUPPORTED_ARTWORK_UPLOAD_TYPES = new Set([
  "image/jpeg",
  "image/jpg",
  "image/png",
  "image/webp",
  "image/gif",
  "image/avif",
]);

function preferredRelease(releases: Chapter[]): Chapter {
  return [...releases].sort((a, b) => {
    const versionDelta = (b.version ?? 0) - (a.version ?? 0);
    if (versionDelta) return versionDelta;
    const pagesDelta = (b.pages ?? 0) - (a.pages ?? 0);
    if (pagesDelta) return pagesDelta;
    return (b.publish_at ?? "").localeCompare(a.publish_at ?? "");
  })[0];
}

function coverageLabel(coverage: UnitCoverage | undefined, unit: SeriesUnit): string {
  const entry = coverage?.[unit];
  if (!entry) return "";
  return entry.expected ? `${entry.available}/${entry.expected}` : `${entry.available}`;
}

const CONFIDENCE_LABELS: Record<string, string> = {
  agreed: "agreed",
  lone: "MangaBaka only",
  conflict: "catalogues disagree",
  unconfirmed: "unknown",
};

function describeCount(
  kind: "chapter" | "volume",
  value: number | null | undefined,
  confidence: string | undefined,
  votes: Record<string, number | null> | undefined,
): string {
  const label = CONFIDENCE_LABELS[confidence ?? ""] ?? confidence ?? "unknown";
  const entries = Object.entries(votes ?? {}).filter(([, count]) => count != null) as Array<
    [string, number]
  >;
  const head = value == null ? `${kind}s unknown` : `${value} ${kind}${value === 1 ? "" : "s"}`;
  if (entries.length === 0) return `${head} (${label})`;
  // "Agreed" means two catalogues put it within a chapter or ten percent of
  // each other; say which ones, and name the odd one out instead of listing
  // it as if it agreed too.
  const close = (count: number) =>
    value != null && (Math.abs(count - value) <= 1 || Math.abs(count - value) <= value * 0.1);
  const agreeing = entries.filter(([, count]) => close(count)).map(([source, count]) => `${source} ${count}`);
  const differing = entries.filter(([, count]) => !close(count)).map(([source, count]) => `${source} ${count}`);
  const parts: string[] = [];
  if (agreeing.length) parts.push(`${label}: ${agreeing.join(", ")}`);
  if (differing.length) parts.push(`${differing.join(", ")} differ${differing.length === 1 ? "s" : ""}`);
  return `${head} (${parts.join(" · ")})`;
}

/** One line on how much the catalogues agree about the work's size. */
function describeCorroboration(metadata: CanonicalMetadata): string {
  const confidence = metadata.count_confidence ?? {};
  const votes = metadata.count_votes ?? {};
  const parts = [
    describeCount("chapter", metadata.chapter_count, confidence.chapter, votes.chapter),
    describeCount("volume", metadata.volume_count, confidence.volume, votes.volume),
  ];
  const edition = metadata.managed_edition;
  if (edition?.publisher) {
    const size = edition.volume_count != null ? `${edition.volume_count} vol` : "size unknown";
    parts.push(`English edition: ${edition.publisher}, ${size}${edition.complete ? ", complete" : ""}`);
  }
  // Every edition the catalogues describe (MangaUpdates publisher notes such
  // as "Viz Media — 12 vols, complete"), so the operator can tell whether the
  // books on disk are the canonical run or another edition.
  const known = (metadata.publishers ?? []).flatMap((publisher) => {
    const match = /(\d+)\s*(?:vols?|volumes?)\b/i.exec(publisher.note ?? "");
    if (!match) return [];
    const type = publisher.type ? ` (${publisher.type})` : "";
    return [`${publisher.name}${type} ${match[1]} vol${/complete/i.test(publisher.note ?? "") ? ", complete" : ""}`];
  });
  if (known.length) parts.push(`Editions: ${known.join("; ")}`);
  return `Catalogue corroboration · ${parts.join(" · ")}`;
}

export default function SeriesPage({ id }: { id: string }) {
  const { notify, refreshJobs } = useApp();
  const [manga, setManga] = useState<Manga | null>(null);
  const [units, setUnits] = useState<SeriesUnits | null>(null);
  const [unitsError, setUnitsError] = useState<string | null>(null);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [readerLink, setReaderLink] = useState<ReaderLink | null>(null);
  const [missing, setMissing] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [showEdit, setShowEdit] = useState(false);
  const [showChapterMap, setShowChapterMap] = useState(false);
  const [showAudit, setShowAudit] = useState(false);
  const [showMetadataSources, setShowMetadataSources] = useState(false);
  const [showArtworkChooser, setShowArtworkChooser] = useState(false);
  const [showDelete, setShowDelete] = useState(false);
  const [busy, setBusy] = useState(false);
  const [searchMissingRunning, setSearchMissingRunning] = useState(false);
  const [automaticSearchKey, setAutomaticSearchKey] = useState<string | null>(null);
  const [metadataRefreshing, setMetadataRefreshing] = useState(false);
  const [metadataRefreshNotice, setMetadataRefreshNotice] = useState<MetadataRefreshNotice | null>(null);
  const [descriptionOpen, setDescriptionOpen] = useState(false);
  const [seriesPackSearchOpen, setSeriesPackSearchOpen] = useState(false);
  const [fileDeletion, setFileDeletion] = useState<FileDeletionTarget | null>(null);
  const [chapterSearch, setChapterSearch] = useState<ChapterSearchTarget | null>(null);
  const [calendar, setCalendar] = useState<SeriesCalendar | null>(null);
  const activeJobsRef = useRef(false);
  const mounted = useRef(false);
  const seriesRequest = useRef<{ controller: AbortController; promise: Promise<void> } | null>(null);
  const duplicatePreviewRequest = useRef<AbortController | null>(null);
  const jobsRequest = useRef<{ controller: AbortController; promise: Promise<void> } | null>(null);

  const load = useCallback((fresh = false): Promise<void> => {
    if (!mounted.current) return Promise.resolve();
    // Ordinary polls share pending work. A post-edit refresh supersedes it,
    // and the old response can no longer overwrite the edited snapshot.
    if (!fresh && seriesRequest.current) return seriesRequest.current.promise;
    seriesRequest.current?.controller.abort();
    const controller = new AbortController();
    setLoading(true);
    setLoadError(null);
    setUnitsError(null);
    const isCurrent = () => !controller.signal.aborted && seriesRequest.current?.controller === controller;
    const promise = Promise.allSettled([
      api.manga(id, fresh, controller.signal).then((detail) => {
        if (!isCurrent()) return;
        setManga(detail);
        setMissing(false);
      }).catch((reason: unknown) => {
        if (!isCurrent()) return;
        if (reason instanceof ApiError && reason.status === 404) {
          setMissing(true);
          setManga(null);
        } else {
          setMissing(false);
          setLoadError(reason instanceof Error ? reason.message : String(reason));
        }
      }),
      api.seriesUnits(id, controller.signal).then((grouped) => {
        if (isCurrent()) setUnits(grouped);
      }).catch((reason: unknown) => {
        if (isCurrent()) setUnitsError(reason instanceof Error ? reason.message : String(reason));
      }),
    ]).then(() => undefined)
      .finally(() => {
        if (seriesRequest.current?.controller !== controller) return;
        seriesRequest.current = null;
        if (!controller.signal.aborted) setLoading(false);
      });
    seriesRequest.current = { controller, promise };
    return promise;
  }, [id]);

  const loadJobs = useCallback((): Promise<void> => {
    if (!mounted.current) return Promise.resolve();
    if (jobsRequest.current) return jobsRequest.current.promise;
    const controller = new AbortController();
    const promise = api.jobs([...ACTIVE_JOB_STATUSES], 500, id, controller.signal)
      .then((next) => {
        if (controller.signal.aborted || jobsRequest.current?.controller !== controller) return;
        activeJobsRef.current = next.length > 0;
        setJobs(next);
      })
      .catch(() => {
        // Keep the previous queue state when a series-scoped poll fails.
      })
      .finally(() => {
        if (jobsRequest.current?.controller === controller) jobsRequest.current = null;
      });
    jobsRequest.current = { controller, promise };
    return promise;
  }, [id]);

  useEffect(() => {
    mounted.current = true;
    setFileDeletion(null);
    setShowAudit(false);
    void load();
    void loadJobs();
    return () => {
      mounted.current = false;
      seriesRequest.current?.controller.abort();
      jobsRequest.current?.controller.abort();
      duplicatePreviewRequest.current?.abort();
      seriesRequest.current = null;
      jobsRequest.current = null;
    };
  }, [load, loadJobs]);

  const calendarRevision = manga?.id === id ? manga.downloaded_count : null;
  useEffect(() => {
    let alive = true;
    void api
      .seriesCalendar(id)
      .then((next) => {
        if (alive) setCalendar(next);
      })
      .catch(() => {
        if (alive) setCalendar(null);
      });
    return () => {
      alive = false;
    };
  }, [id, calendarRevision]);

  const readerBookRevision = manga?.id === id ? manga.downloaded_count : null;

  useEffect(() => {
    let active = true;
    setReaderLink(null);
    if (readerBookRevision === null) {
      return () => {
        active = false;
      };
    }
    const refreshReader = () => {
      if (document.hidden) return;
      void api.readerLink(id).then((link) => {
        if (active) setReaderLink(link);
      }).catch(() => {
        // Reading is optional; a failed shortcut must not hide the series.
      });
    };
    refreshReader();
    window.addEventListener("focus", refreshReader);
    document.addEventListener("visibilitychange", refreshReader);
    return () => {
      active = false;
      window.removeEventListener("focus", refreshReader);
      document.removeEventListener("visibilitychange", refreshReader);
    };
  }, [id, readerBookRevision]);

  useEffect(() => {
    let stopped = false;
    let mangaTimer = 0;
    let mangaPolling = false;
    const pollManga = async () => {
      if (stopped || mangaPolling) return;
      mangaPolling = true;
      try {
        if (!document.hidden) await load();
      } finally {
        mangaPolling = false;
      }
      if (stopped) return;
      const delay = document.hidden ? 60000 : activeJobsRef.current ? 10000 : 30000;
      mangaTimer = window.setTimeout(() => void pollManga(), delay);
    };
    const wakePolling = () => {
      if (document.hidden) return;
      window.clearTimeout(mangaTimer);
      void pollManga();
    };
    mangaTimer = window.setTimeout(() => void pollManga(), 30000);
    window.addEventListener("focus", wakePolling);
    document.addEventListener("visibilitychange", wakePolling);
    const jobsTimer = window.setInterval(() => void loadJobs(), 6000);
    return () => {
      stopped = true;
      window.clearTimeout(mangaTimer);
      window.clearInterval(jobsTimer);
      window.removeEventListener("focus", wakePolling);
      document.removeEventListener("visibilitychange", wakePolling);
    };
  }, [load, loadJobs]);

  const chapterJobs = useMemo(() => {
    const map = new Map<string, Job>();
    for (const job of jobs) {
      if (job.manga_id !== id || !ACTIVE_JOB_STATUSES.has(job.status)) continue;
      map.set(job.chapter_id, job);
      if (job.chapter_number !== null) {
        map.set(`chapter:${Number(job.chapter_number)}`, job);
      } else if (job.chapter_volume !== null) {
        map.set(`volume:${Number(job.chapter_volume)}`, job);
      }
    }
    return map;
  }, [jobs, id]);

  const readerBooks = useMemo(
    () => new Map((readerLink?.books ?? []).map((book) => [book.chapter_id, book])),
    [readerLink],
  );

  if (missing) {
    return (
      <div className="page">
        <div className="banner banner-warn">
          <Icon name="alert" /> Series not found — it may have been deleted.
        </div>
      </div>
    );
  }
  if (!manga) {
    return loadError ? (
      <div className="page">
        <LoadError message={loadError} retryLabel="Retry series" retry={() => void load(true)} loading={loading} />
      </div>
    ) : <Spinner />;
  }

  const enriched = manga.metadata;
  const years = workYears(manga);
  const coverUrl = seriesCoverUrl(manga);
  const displayAuthors = enriched?.authors?.length ? enriched.authors : manga.authors;
  const authorEntities = manga.author_entities ?? [];
  const displayDescription = (enriched?.description || manga.description || "").trim();
  const publication = publicationStatus(manga);
  const availability = libraryStatus(manga);
  const counts = seriesCounts(manga);
  const officialPlatforms = manga.official_platforms ?? [];
  const providerCorrelations = (enriched?.provider_correlations ?? []).filter(
    (correlation) => correlation.url && /^https?:\/\//i.test(correlation.url),
  );
  // The spine catalogue (MangaBaka) and anything linked by hand deserve a
  // line each; a secondary catalogue that was down at the last refresh is
  // one quiet note, not an alarm on every series.
  const allMetadataIssues = (manga.metadata_source_status ?? []).filter((source) =>
    ["error", "cached_error", "exact_error", "unavailable"].includes(source.state ?? ""),
  );
  const metadataIssues = allMetadataIssues.filter(
    (source) => source.name === "mangabaka" || source.correlation_origin === "manual",
  );
  const secondaryIssues = allMetadataIssues.filter((source) => !metadataIssues.includes(source));

  async function run<T>(
    action: () => Promise<T>,
    success?: string | ((result: T) => string | ActionNotice),
  ) {
    setBusy(true);
    try {
      const result = await action();
      if (typeof success === "function") {
        const notice = success(result);
        if (typeof notice === "string") notify("success", notice);
        else notify(notice.kind, notice.text);
      } else if (typeof success === "string") notify("success", success);
      await Promise.all([load(true), loadJobs(), refreshJobs()]);
      return result;
    } catch (caught) {
      notify("error", String(caught));
      return null;
    } finally {
      setBusy(false);
    }
  }

  async function searchAllMissing() {
    setSearchMissingRunning(true);
    try {
      await run(
        () => api.searchMissing(id),
        (result) => ({
          kind: result.errors.length ? "info" : "success",
          text: `Release search checked ${result.seen} direct releases across ${result.sources.length} source result${result.sources.length === 1 ? "" : "s"}, found ${result.new} new and queued ${result.queued}.`,
        }),
      );
    } finally {
      setSearchMissingRunning(false);
    }
  }

  async function searchOneMissing(chapter: Pick<LogicalChapter, "key" | "chapter" | "volume">) {
    setAutomaticSearchKey(chapter.key);
    try {
      await run(
        () => api.automaticSearchChapter(id, chapter.chapter, chapter.volume),
        (result) => ({
          kind: result.state === "queued" ? "success" : "info",
          text: result.errors.length
            ? `${result.message} ${result.errors.join(" · ")}`
            : result.message,
        }),
      );
    } finally {
      setAutomaticSearchKey(null);
    }
  }

  async function refreshMetadata(mangaId: string) {
    setBusy(true);
    setMetadataRefreshing(true);
    setMetadataRefreshNotice({
      kind: "info",
      text: "Querying applicable metadata catalogues, rebuilding canonical fields and artwork, then updating portable library metadata and the connected Komga reader. Chapter downloads and reading progress are not changed.",
    });
    try {
      const result = await api.refreshMetadata(mangaId);
      const sourceStatuses = result.metadata.source_status ?? [];
      const matched = sourceStatuses.filter((source) => source.matched).map((source) => source.label);
      const unmatched = sourceStatuses.filter(
        (source) => source.configured && source.supports_series && ["no_match", "ambiguous"].includes(source.state ?? ""),
      );
      const errors = sourceStatuses.filter((source) => ["error", "cached_error"].includes(source.state ?? ""));
      const library = result.library;
      const komga = result.komga;
      const libraryErrors = library.errors ?? [];
      const komgaErrors = komga?.errors ?? [];
      const parts = [
        matched.length
          ? `verified ${matched.join(", ")}`
          : "no external catalogue match was verified",
        unmatched.length
          ? `${unmatched.length} catalogue${unmatched.length === 1 ? "" : "s"} had no safe match`
          : "all applicable catalogues resolved",
        `${library.checked ?? 0} library file${library.checked === 1 ? "" : "s"} checked`,
        `${library.updated ?? 0} portable metadata file${library.updated === 1 ? "" : "s"} changed`,
        `${library.covers_written ?? 0} artwork sidecar${library.covers_written === 1 ? "" : "s"} changed`,
      ];
      if (komga) {
        parts.push(
          komga.synced
            ? `Komga aligned (${komga.books ?? 0} book${komga.books === 1 ? "" : "s"})`
            : `Komga pending${komga.missing_books ? ` · ${komga.missing_books} book${komga.missing_books === 1 ? "" : "s"} not indexed` : ""}`,
          `${komga.artwork_uploaded ?? 0} Komga poster${komga.artwork_uploaded === 1 ? "" : "s"} updated`,
        );
      }
      if (errors.length) parts.push(`${errors.length} source error${errors.length === 1 ? "" : "s"}`);
      if (libraryErrors.length) parts.push(`${libraryErrors.length} portable metadata error${libraryErrors.length === 1 ? "" : "s"}`);
      if (komgaErrors.length) parts.push(`${komgaErrors.length} Komga error${komgaErrors.length === 1 ? "" : "s"}`);
      const hasErrors = errors.length > 0 || libraryErrors.length > 0 || komgaErrors.length > 0;
      const text = `Metadata refresh complete: ${parts.join(" · ")}.`;
      setMetadataRefreshNotice({ kind: hasErrors ? "error" : "success", text });
      notify(hasErrors ? "error" : "success", `Metadata refresh complete · ${matched.length} source${matched.length === 1 ? "" : "s"} verified.`);
      await Promise.all([load(true), loadJobs(), refreshJobs()]);
    } catch (caught) {
      const text = `Metadata refresh failed: ${String(caught)}`;
      setMetadataRefreshNotice({ kind: "error", text });
      notify("error", text);
    } finally {
      setMetadataRefreshing(false);
      setBusy(false);
    }
  }

  async function previewDuplicateRetirement(book: SeriesBookGroup) {
    duplicatePreviewRequest.current?.abort();
    const controller = new AbortController();
    duplicatePreviewRequest.current = controller;
    setBusy(true);
    try {
      const preview = await api.duplicateFiles(id, book.volume, controller.signal);
      if (!mounted.current || controller.signal.aborted || duplicatePreviewRequest.current !== controller) return;
      if (!preview.chapters.length) {
        notify("info", "No duplicate chapter files remain for this book.");
        await load(true);
        return;
      }
      setFileDeletion({ kind: "duplicates", volume: book.volume, chapters: preview.chapters });
    } catch (caught) {
      if (!controller.signal.aborted && mounted.current) notify("error", String(caught));
    } finally {
      if (duplicatePreviewRequest.current === controller) {
        duplicatePreviewRequest.current = null;
        if (mounted.current) setBusy(false);
      }
    }
  }

  const deletionNotice = (result: DeleteResult, subject: string): ActionNotice => {
    const bits: string[] = [];
    if (result.cleanup_pending) return { kind: "success", text: `${subject}. Library cleanup will finish in the background.` };
    if (result.files_deleted) bits.push(`${result.files_deleted} file removed`);
    if (result.chapters_reset) bits.push(`${result.chapters_reset} chapters reset`);
    if (result.quarantine_files_remaining > 0) bits.push("cleanup incomplete — check System");
    return {
      kind: result.quarantine_files_remaining > 0 ? "info" : "success",
      text: `${subject}${bits.length ? `: ${bits.join(" · ")}` : " done"}`,
    };
  };

  return (
    <div className="page">
      {loadError ? (
        <LoadError message={loadError} retryLabel="Retry series" retry={() => void load(true)} loading={loading} hasData />
      ) : null}
      <div className="series-hero">
        {coverUrl ? (
          <div
            className="series-backdrop"
            style={{ backgroundImage: `url(${artworkThumbnailUrl(coverUrl, 640) ?? coverUrl})` }}
          />
        ) : null}
        <div className="series-hero-content">
          <Cover url={coverUrl} title={manga.title} className="series-poster" />
          <div className="series-info">
            <h1>
              {manga.title}
              {years.label ? (
                <span className="muted series-year" title={years.title ?? undefined}> ({years.label})</span>
              ) : null}
            </h1>
            <div className="chip-row">
              <StatusPill kind={publication.kind} title={publication.title}>
                {publication.label}
              </StatusPill>
              {enriched?.classification?.kind && enriched.classification.kind !== "unknown" ? (
                <StatusPill kind="muted" title={enriched.classification.reason}>
                  {humanize(enriched.classification.kind)}
                  {enriched.classification.subtype &&
                  humanize(enriched.classification.subtype) !== humanize(enriched.classification.kind)
                    ? ` · ${humanize(enriched.classification.subtype)}`
                    : ""}
                </StatusPill>
              ) : enriched?.work_type ? (
                <StatusPill kind="muted">{humanize(enriched.work_type)}</StatusPill>
              ) : null}
              <StatusPill kind="muted">{languageName(manga.preferred_language)}</StatusPill>
              <StatusPill kind={manga.monitored ? "success" : "muted"}>
                Monitoring: {monitorLabel(manga.monitor_mode)}
              </StatusPill>
              <StatusPill
                kind="muted"
                title={
                  counts.reference_kind === "available"
                    ? `Progress against the distinct chapters available from the sources, including chapters already in your library.`
                    : counts.reference_unknown
                    ? `No official source is mapped, so the real chapter count is unknown; ${counts.available_count} are offered by the sources so far and Tankarr follows whoever publishes the newest chapter first.`
                    : counts.reference_kind === "official"
                      ? `Chapter count from the official source (${counts.source ?? "official"})`
                      : counts.reference_kind === "verified"
                        ? `Verified published chapters; new releases are still monitored. Source: ${counts.source ?? "recorded verification"}`
                        : undefined
                }
              >
                {counts.downloaded_count} / {counts.reference_unknown ? "?" : counts.total_count} {counts.unit === "issue" ? "issues" : `${counts.unit}s`}
              </StatusPill>
              <StatusPill
                kind="muted"
                title="The same series measured the other way: a shelf of books, and the chapters printed in them. A question mark means no catalogue has said."
              >
                {counts.unit === "volume"
                  ? `${manga.chapter_total_count ?? "?"} chapters`
                  : `${manga.book_total_count ?? "?"} book${manga.book_total_count === 1 ? "" : "s"}`}
              </StatusPill>
              {counts.unit === "chapter" && counts.latest_chapter ? (
                <StatusPill kind="muted">
                  {counts.reference_kind === "official" ? "Latest official chapter" : "Latest available chapter"}: {counts.latest_chapter}
                </StatusPill>
              ) : null}
              {counts.unindexed_missing_count > 0 ? (
                <StatusPill kind="warn" title={`${counts.unindexed_missing_count} expected ${counts.unit}s are not exposed by the current download-provider index.`}>
                  {counts.available_count} indexed
                </StatusPill>
              ) : null}
              <StatusPill kind={availability.kind} title={availability.title}>
                {availability.label}
              </StatusPill>
              {(counts.ignored_missing_count ?? 0) > 0 ? (
                <StatusPill
                  kind="muted"
                  title={
                    manga.library_status_override === "up_to_date"
                      ? "Excluded by the reversible manual library-status override; no file is marked as downloaded."
                      : "Excluded by reversible per-volume monitoring overrides; no file is marked as downloaded."
                  }
                >
                  {counts.ignored_missing_count} {manga.library_status_override === "up_to_date" ? "manually excluded" : "ignored"}
                </StatusPill>
              ) : null}
            </div>
            {additionalContentLabel(counts) ? (
              <p className="muted small">
                Also on disk: {additionalContentLabel(counts)}. Shown separately from the reference chapter count; source numbering is preserved.
              </p>
            ) : null}
            {counts.count_note ? <p className="muted small">{counts.count_note}</p> : null}
            {enriched?.publishers?.length ? (
              <div className="muted small series-publishers">
                Published by{" "}
                {enriched.publishers
                  .filter((item) => item.name)
                  .map((item) => `${item.name}${item.type ? ` (${item.type})` : ""}`)
                  .join(" · ")}
              </div>
            ) : null}
            {authorEntities.length || displayAuthors.length ? (
              <div className="series-authors">
                {authorEntities.length
                  ? authorEntities.map((author, index) => (
                      <span key={author.id}>
                        {index ? ", " : ""}
                        <a href={authorPath(author.id)} title={`Show every work by ${author.name}`}>
                          {author.name}
                        </a>
                      </span>
                    ))
                  : displayAuthors.map((author, index) => (
                      <span key={author}>
                        {index ? ", " : ""}
                        {author}
                      </span>
                    ))}
              </div>
            ) : null}
            {displayDescription ? (
              <div
                className={`series-description ${descriptionOpen ? "open" : ""}`}
                onClick={() => setDescriptionOpen((value) => !value)}
                title="Click to expand"
              >
                {renderDescription(displayDescription)}
              </div>
            ) : null}
            {metadataIssues.map((source) => (
              <div
                key={source.name}
                className={source.state === "ambiguous" ? "warn-text small" : "muted small"}
              >
                <Icon name="alert" size={13} /> {source.label}: {humanize(source.state ?? "error")}
                {source.candidate?.title
                  ? ` · best candidate ${source.candidate.title} (${Math.round((source.confidence ?? 0) * 100)}%) was not linked`
                  : ""}
                {source.error ? ` · ${source.error}` : ""}
              </div>
            ))}
            {secondaryIssues.length > 0 ? (
              <div
                className="muted small"
                title="Ratings and cross-links from these catalogues were not refreshed; MangaBaka remains the reference. Tankarr asks again on the next refresh."
              >
                Secondary catalogues not reached at the last refresh:{" "}
                {secondaryIssues.map((source) => source.label).join(", ")}
              </div>
            ) : null}
            {enriched?.external_sources?.length ? (
              <div className="series-external-links" aria-label="Linked catalogues">
                {enriched.external_sources.map((site) => (
                  <a
                    key={site.source}
                    href={site.url}
                    target="_blank"
                    rel="noreferrer"
                    title={
                      site.rating != null
                        ? `${site.label} · community score ${site.rating.toFixed(1)}/10`
                        : `${site.label} page for this work`
                    }
                  >
                    {site.label}
                    {site.rating != null ? <span className="muted"> {site.rating.toFixed(1)}</span> : null}
                    <Icon name="external" size={12} />
                  </a>
                ))}
              </div>
            ) : providerCorrelations.length ? (
              <div className="series-external-links" aria-label="External links">
                {providerCorrelations.map((correlation) => (
                  <a
                    key={`${correlation.provider}:${correlation.external_id}`}
                    href={correlation.url ?? undefined}
                    target="_blank"
                    rel="noreferrer"
                    title={`${correlation.label} ID ${correlation.external_id}`}
                  >
                    {correlation.label} <Icon name="external" size={12} />
                  </a>
                ))}
              </div>
            ) : null}
            {officialPlatforms.length ? (
              <div className="series-links official-platforms">
                <span className="muted small">Official platforms</span>
                {officialPlatforms.map((platform) => (
                  <a
                    key={platform.host}
                    href={platform.url}
                    target="_blank"
                    rel="noreferrer"
                    title={`${platform.name} · free official platform · extension ${platform.installed ? "installed" : "installs automatically"}${platform.mapped ? " · mapped as a download source" : ""}`}
                  >
                    {platform.name} {platform.mapped ? <Icon name="check" size={12} /> : <Icon name="external" size={12} />}
                  </a>
                ))}
              </div>
            ) : null}
          </div>
        </div>
      </div>

      <div className="toolbar">
        <div className="toolbar-group">
          {manga.provider !== "local" ? (
            <>
              <button
                type="button"
                className="btn"
                disabled={busy}
                aria-busy={metadataRefreshing}
                title="Re-check every download source for new releases and reload the catalogue metadata (cover, description, counts). Never downloads by itself."
                onClick={() =>
                  void run(
                    async () => {
                      const result = await api.refreshManga(manga.id);
                      await refreshMetadata(manga.id);
                      return result;
                    },
                    (result) => `Refreshed: ${result.new} new releases, ${result.queued} queued.`,
                  )
                }
              >
                <Icon name="refresh" /> {metadataRefreshing ? "Refreshing…" : "Refresh"}
              </button>
              <button
                type="button"
                className="btn"
                disabled={busy}
                aria-busy={searchMissingRunning}
                onClick={() => void searchAllMissing()}
              >
                {searchMissingRunning ? <Spinner /> : <Icon name="search" />}
                {searchMissingRunning ? "Searching Missing…" : "Search Missing"}
              </button>
              <button
                type="button"
                className="btn"
                disabled={busy}
                aria-expanded={seriesPackSearchOpen}
                title="Search Prowlarr manually for whole-series and multi-volume packs"
                onClick={() => setSeriesPackSearchOpen((value) => !value)}
              >
                <Icon name="search" /> Series Pack Search
              </button>
            </>
          ) : null}
          <button type="button" className="btn" disabled={busy} onClick={() => setShowEdit(true)}>
            <Icon name="edit" /> Edit
          </button>
          <button type="button" className="btn" disabled={busy} onClick={() => setShowAudit(true)}>Audit files</button>
          <button type="button" className="btn" disabled={busy} onClick={() => setShowChapterMap(true)}>
            <Icon name="library" /> Set book boundaries
          </button>
        </div>
        <div className="toolbar-group">
          {readerLink?.available && readerLink.url ? (
            <div className="series-reader-action">
              <a
                className={`btn ${readerLink.bookmark ? "btn-primary" : "btn-ghost"}`}
                href={readerLink.bookmark?.url ?? readerLink.url}
                target="_blank"
                rel="noreferrer"
              >
                <Icon name={readerLink.bookmark ? "bookmark" : "library"} />
                {readerLink.bookmark ? "Continue from bookmark"
                  : readerLink.reader === "tankarr" ? "Read" : `Read in ${readerLink.label ?? "reader"}`}
              </a>
              {readerLink.reader === "tankarr" ? <small className="muted">
                {readerLink.bookmark
                  ? `${readerLink.bookmark.label} · page ${readerLink.bookmark.page_index + 1}`
                  : "No bookmark saved · use Save bookmark in the reader"}
              </small> : null}
            </div>
          ) : null}
          <button type="button" className="btn btn-danger" disabled={busy} onClick={() => setShowDelete(true)}>
            <Icon name="trash" /> Delete
          </button>
        </div>
      </div>

      {seriesPackSearchOpen ? (
        <SeriesPackSearchPanel
          key={manga.id}
          manga={manga}
          open
          onClose={() => setSeriesPackSearchOpen(false)}
        />
      ) : null}

      {metadataRefreshNotice ? (
        <div
          className={`banner banner-${metadataRefreshNotice.kind === "error" ? "danger" : metadataRefreshNotice.kind}`}
          role="status"
        >
          <Icon name={metadataRefreshNotice.kind === "error" ? "alert" : "info"} />
          {metadataRefreshNotice.text}
        </div>
      ) : null}

      {manga.chapter_index &&
      manga.chapter_index.unresolved_expected_count > 0 &&
      (manga.chapter_index.expected_available_count ?? 0) > 0 ? (
        (() => {
          // A chapter the official platform already lists with a date is not
          // "missing from the sources": it is simply not out yet.
          const announced = (calendar?.expected ?? []).filter((item) => !item.estimated);
          const unit = manga.chapter_index.series_unit ?? "chapters";
          if (announced.length > 0 && manga.chapter_index.unresolved_expected_count <= announced.length) {
            const first = announced[0];
            return (
              <div className="banner banner-info">
                <Icon name="info" />
                The catalogue counts {manga.chapter_index.expected_count} {unit};{" "}
                {manga.chapter_index.expected_available_count} are out. Chapter {first.chapter} is announced by the
                official platform for {calendarDate(first.expected_at)}
                {announced.length > 1 ? ` (and ${announced.length - 1} more after it)` : ""}.
              </div>
            );
          }
          return (
            <div className="banner banner-info">
              <Icon name="info" />
              The catalogue counts {manga.chapter_index.expected_count} {unit}; the sources expose{" "}
              {manga.chapter_index.expected_available_count} of them so far. Tankarr only tracks what a source actually offers.
            </div>
          );
        })()
      ) : null}

      {manga.metadata?.count_confidence ? (
        <p className="muted small series-corroboration">
          {describeCorroboration(manga.metadata)}
        </p>
      ) : null}

      {calendar ? <SeriesCalendarSection calendar={calendar} /> : null}

      {unitsError ? <LoadError message={unitsError} retryLabel="Retry books and chapters" retry={() => void load(true)} loading={loading} hasData={Boolean(units?.manga_id === manga.id)} /> : null}
      {manga.translation_enabled ? <TranslationPanel manga={manga} /> : null}
      {units?.manga_id === manga.id ? <SeriesUnitGroups
        key={manga.id}
        data={units}
        busy={busy}
        monitoringLocked={manga.library_status_override === "up_to_date"}
        readerLabel={readerLink?.label ?? "reader"}
        bookmark={readerLink?.bookmark}
        readerUrl={(releaseId) => readerBooks.get(releaseId)?.url}
        onSetBoundaries={() => setShowChapterMap(true)}
        onSetBookMonitoring={(book, state) => void run(
          () => api.setVolumeMonitoring(manga.id, book.volume, state),
          state === "ignored" ? `Book ${book.volume} ignored: excluded from Wanted and operational Missing.`
            : state === "monitored" ? `Book ${book.volume} explicitly monitored.`
              : `Book ${book.volume} now follows the series monitoring profile.`,
        )}
        onAutomaticSearchBook={(book) => void searchOneMissing({ key: book.key, chapter: null, volume: book.volume })}
        onSearchBook={(book) => setChapterSearch({ key: book.key, chapter: null, volume: book.volume })}
        onDeleteBookFiles={(book) => {
          const chapters = (manga.chapters ?? []).filter((release) => release.downloaded && release.volume === book.volume && release.language === manga.preferred_language);
          if (!chapters.length) { notify("info", "The file list changed. Refresh the series and try again."); void load(true); return; }
          setFileDeletion({ kind: "volume", volume: book.volume, language: manga.preferred_language, chapters });
        }}
        onRetireDuplicates={(book) => void previewDuplicateRetirement(book)}
        renderChapterRows={(chapters, book) => chapters.map((chapter) => {
          const best = chapter.releases.length ? preferredRelease(chapter.releases) : undefined;
          const downloaded = chapter.releases.find((release) => release.id === chapter.open_release_id)
            ?? chapter.releases.find((release) => release.downloaded);
          const activeJob = chapterJobs.get(chapter.key) ?? chapter.releases.map((release) => chapterJobs.get(release.id)).find((job) => job !== undefined);
          return <ChapterRows
            key={chapter.key}
            chapter={chapter}
            best={best}
            downloaded={downloaded}
            readerBook={chapter.open_release_id ? readerBooks.get(chapter.open_release_id) : undefined}
            volumeBook={book?.open_release_id ? readerBooks.get(book.open_release_id) : undefined}
            readerLabel={readerLink?.label ?? "reader"}
            bookmark={readerLink?.bookmark}
            activeJob={activeJob}
            isExpanded={expanded === chapter.key}
            onToggleExpand={() => setExpanded((value) => value === chapter.key ? null : chapter.key)}
            busy={busy}
            automaticSearching={automaticSearchKey === chapter.key}
            onAutomaticSearch={() => void searchOneMissing(chapter)}
            onDeleteFile={(release) => setFileDeletion({ kind: "chapter", chapter: release })}
            onToggleMonitored={(chapterId, monitored) => void run(() => api.setChapterMonitored(manga.id, chapterId, monitored))}
            onInteractiveSearch={() => setChapterSearch({ key: chapter.key, chapter: chapter.chapter, volume: chapter.volume })}
          />;
        })}
      /> : !unitsError ? <Spinner /> : null}

      {chapterSearch ? (
        <ChapterReleaseSearchModal
          manga={manga}
          target={chapterSearch}
          onClose={() => setChapterSearch(null)}
          onQueued={async () => {
            await Promise.all([load(), loadJobs(), refreshJobs()]);
          }}
        />
      ) : null}


      {showAudit ? <SeriesAuditDialog
        mangaId={manga.id}
        title={manga.title}
        client={api}
        onClose={() => setShowAudit(false)}
        onChanged={async () => { await Promise.allSettled([load(true), loadJobs(), refreshJobs()]); }}
      /> : null}
      {showChapterMap ? (
        <ChapterMapEditor
          key={manga.id}
          mangaId={manga.id}
          title={manga.title}
          onClose={() => setShowChapterMap(false)}
          onSaved={async (result) => {
            setManga((current) => current ? { ...current, chapter_index: result.chapter_index } : current);
            setShowChapterMap(false);
            notify("success", result.boundaries.length ? "Book boundaries saved." : "Operator book boundaries removed.");
            await load(true);
          }}
        />
      ) : null}
      {showMetadataSources ? (
        <MetadataSourcesModal
          manga={manga}
          onClose={() => {
            setShowMetadataSources(false);
            setShowEdit(true);
          }}
          onSaved={async () => {
            setShowMetadataSources(false);
            setShowEdit(true);
            await load(true);
          }}
        />
      ) : null}
      {showArtworkChooser ? (
        <ArtworkChooserModal
          manga={manga}
          onClose={() => {
            setShowArtworkChooser(false);
            setShowEdit(true);
          }}
          onSaved={async () => {
            setShowArtworkChooser(false);
            setShowEdit(true);
            await load(true);
          }}
        />
      ) : null}
      {showEdit ? (
        <EditModal
          manga={manga}
          onClose={() => setShowEdit(false)}
          onSaved={async () => {
            setShowEdit(false);
            await Promise.all([load(true), loadJobs(), refreshJobs()]);
          }}
          onOpenCover={() => {
            setShowEdit(false);
            setShowArtworkChooser(true);
          }}
          onOpenMetadata={() => {
            setShowEdit(false);
            setShowMetadataSources(true);
          }}
        />
      ) : null}
      {showDelete ? (
        <DeleteModal
          manga={manga}
          onClose={() => setShowDelete(false)}
          onDeleted={(result, deleteFiles) => {
            if (deleteFiles) {
              const notice = deletionNotice(result, `${manga.title} deleted`);
              notify(notice.kind, notice.text);
            } else {
              notify("success", `${manga.title} removed from Tankarr (files kept).`);
            }
            navigate("/");
          }}
        />
      ) : null}
      {fileDeletion ? (
        <FileDeleteModal
          target={fileDeletion}
          busy={busy}
          onClose={() => setFileDeletion(null)}
          onConfirm={async () => {
            const subject =
              fileDeletion.kind === "chapter"
                ? `Chapter ${fileDeletion.chapter.chapter ?? "special"} file deleted`
                : fileDeletion.kind === "duplicates"
                  ? `Duplicate files for book ${fileDeletion.volume} retired`
                  : `Volume ${fileDeletion.volume} files deleted`;
            const result = await run(
              () =>
                fileDeletion.kind === "chapter"
                  ? api.deleteChapterFile(manga.id, fileDeletion.chapter.id)
                  : fileDeletion.kind === "duplicates"
                    ? api.retireBookDuplicates(
                        manga.id,
                        fileDeletion.volume,
                        fileDeletion.chapters.map((chapter) => chapter.id),
                      )
                  : api.deleteVolumeFiles(
                      manga.id,
                      fileDeletion.volume,
                      fileDeletion.language,
                      fileDeletion.chapters.length,
                      fileDeletion.chapters.map((chapter) => chapter.id),
                    ),
              (deleted) => fileDeletion.kind === "duplicates" ? {
                kind: deleted.cleanup_errors.length ? "info" : "success",
                text: `${(deleted as DuplicateRetirementResult).files_retired} duplicate file${(deleted as DuplicateRetirementResult).files_retired === 1 ? "" : "s"} moved to the recycle bin.${deleted.cleanup_errors.length ? " Some cleanup is pending; check System." : ""}`,
              } : deletionNotice(deleted, subject),
            );
            if (result) setFileDeletion(null);
          }}
        />
      ) : null}
    </div>
  );
}

function correlationOriginLabel(source: MetadataCorrelationSource): string {
  if (source.origin === "manual") return "Manual override";
  if (source.origin === "mangadex") return "Exact ID from MangaDex";
  if (source.origin === "automatic") return "Automatic match";
  return "Not linked";
}

function ArtworkChooserModal({
  manga,
  onClose,
  onSaved,
}: {
  manga: Manga;
  onClose: () => void;
  onSaved: () => Promise<void>;
}) {
  const { notify } = useApp();
  const [selection, setSelection] = useState<ArtworkSelection | null>(null);
  const [chosen, setChosen] = useState<string | null>(null);
  const [initial, setInitial] = useState<string | null>(null);
  const [loadingError, setLoadingError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [pendingUpload, setPendingUpload] = useState<File | null>(null);
  const [pendingPreview, setPendingPreview] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    void api.artworkCandidates(manga.id)
      .then((result) => {
        if (!active) return;
        const value = result.selection_mode === "manual"
          ? result.selected_candidate_id
          : null;
        setSelection(result);
        setChosen(value);
        setInitial(value);
        setLoadingError(null);
      })
      .catch((caught) => {
        if (active) setLoadingError(String(caught));
      });
    return () => {
      active = false;
    };
  }, [manga.id]);

  useEffect(() => () => {
    if (pendingPreview) URL.revokeObjectURL(pendingPreview);
  }, [pendingPreview]);

  const chooseCached = (candidateId: string | null) => {
    setPendingUpload(null);
    setPendingPreview(null);
    setChosen(candidateId);
  };

  const chooseUpload = (file: File | undefined) => {
    if (!file) return;
    if (file.size > MAX_ARTWORK_UPLOAD_BYTES) {
      notify("error", "The cover image exceeds the 20 MiB safety limit.");
      return;
    }
    if (file.type && !SUPPORTED_ARTWORK_UPLOAD_TYPES.has(file.type.toLowerCase())) {
      notify("error", "Choose a JPEG, PNG, WebP, GIF, or AVIF image.");
      return;
    }
    setPendingUpload(file);
    setPendingPreview(URL.createObjectURL(file));
    setChosen(PENDING_ARTWORK_UPLOAD);
  };

  const save = async () => {
    setSaving(true);
    try {
      const isUpload = chosen === PENDING_ARTWORK_UPLOAD && pendingUpload !== null;
      const result = isUpload
        ? await api.uploadArtwork(manga.id, pendingUpload)
        : await api.selectArtwork(manga.id, chosen);
      const komgaErrors = result.komga?.errors?.length ?? 0;
      notify(
        komgaErrors ? "info" : "success",
        isUpload
          ? `Custom cover uploaded and normalized to 1000×1500${komgaErrors ? "; Komga will retry synchronization" : " and mirrored to the library and Komga"}.`
          : chosen
          ? `Cover pinned from ${humanize(result.selection.candidates.find((item) => item.candidate_id === chosen)?.source ?? "metadata")}${komgaErrors ? "; Komga will retry synchronization" : " and mirrored to the library and Komga"}.`
          : `Automatic cover selection restored${komgaErrors ? "; Komga will retry synchronization" : " and mirrored to the library and Komga"}.`,
      );
      await onSaved();
    } catch (caught) {
      notify("error", String(caught));
      setSaving(false);
    }
  };

  return (
    <Modal
      title={`Choose cover · ${manga.title}`}
      onClose={saving ? () => undefined : onClose}
      wide
      footer={
        <>
          <button type="button" className="btn" disabled={saving} onClick={onClose}>
            Cancel
          </button>
          <button
            type="button"
            className="btn btn-primary"
            disabled={
              saving
              || !selection
              || chosen === initial
              || (chosen === PENDING_ARTWORK_UPLOAD && pendingUpload === null)
            }
            onClick={() => void save()}
          >
            <Icon name="check" /> {saving ? "Publishing…" : "Save cover"}
          </button>
        </>
      }
    >
      <p className="muted">
        Tankarr keeps every valid cover returned by the verified metadata sources. Automatic uses
        source authority first and image quality second. A manual choice survives metadata refreshes
        and is published to the filesystem and Komga; reading progress is untouched. Uploaded images
        are fitted to a 1000×1500 JPEG without stretching or cropping.
      </p>
      {loadingError ? (
        <div className="banner banner-danger"><Icon name="alert" /> {loadingError}</div>
      ) : null}
      {!selection && !loadingError ? <Spinner /> : null}
      {selection ? (
        <>
          <label className={`artwork-automatic-choice ${chosen === null ? "selected" : ""}`}>
            <input
              type="radio"
              name="artwork-choice"
              checked={chosen === null}
              disabled={selection.automatic_candidate_id === null}
              onChange={() => chooseCached(null)}
            />
            <span>
              <strong>Automatic (recommended)</strong>
              <span className="muted small">
                {selection.automatic_candidate_id
                  ? "Re-evaluate authority and quality on every metadata refresh."
                  : "Unavailable until Refresh Metadata finds or creates a default cover."}
              </span>
            </span>
          </label>
          <div className="artwork-upload-row">
            <label
              className={`btn artwork-upload-button ${saving ? "disabled" : ""}`}
              aria-disabled={saving}
            >
              <Icon name="upload" /> Upload image
              <input
                type="file"
                hidden
                disabled={saving}
                accept="image/jpeg,image/png,image/webp,image/gif,image/avif"
                onChange={(event) => {
                  chooseUpload(event.currentTarget.files?.[0]);
                  event.currentTarget.value = "";
                }}
              />
            </label>
            <span className="muted small">
              JPEG, PNG, WebP, GIF or AVIF · maximum 20 MiB. The original aspect ratio is preserved;
              margins are added when needed.
            </span>
          </div>
          {pendingPreview || selection.candidates.length ? (
            <div className="artwork-choice-grid">
              {pendingPreview && pendingUpload ? (
                <label
                  className={`artwork-choice ${chosen === PENDING_ARTWORK_UPLOAD ? "selected" : ""}`}
                >
                  <input
                    type="radio"
                    name="artwork-choice"
                    checked={chosen === PENDING_ARTWORK_UPLOAD}
                    onChange={() => setChosen(PENDING_ARTWORK_UPLOAD)}
                  />
                  <img src={pendingPreview} alt="Custom cover preview" />
                  <span className="artwork-choice-details">
                    <strong>Custom upload</strong>
                    <span className="muted small">
                      {pendingUpload.name} · will become 1000×1500
                    </span>
                  </span>
                </label>
              ) : null}
              {selection.candidates.map((candidate) => (
                <label
                  className={`artwork-choice ${chosen === candidate.candidate_id ? "selected" : ""}`}
                  key={candidate.candidate_id}
                >
                  <input
                    type="radio"
                    name="artwork-choice"
                    checked={chosen === candidate.candidate_id}
                    onChange={() => chooseCached(candidate.candidate_id)}
                  />
                  <img
                    src={candidate.image_url}
                    alt={`${humanize(candidate.source)} cover candidate`}
                    loading="lazy"
                  />
                  <span className="artwork-choice-details">
                    <strong>{humanize(candidate.source)}</strong>
                    <span className="muted small">
                      {candidate.source_width}×{candidate.source_height}
                      {candidate.automatic ? " · automatic default" : ""}
                    </span>
                    {/^https?:\/\//i.test(candidate.source_url) ? (
                      <a href={candidate.source_url} target="_blank" rel="noreferrer">
                        Open source <Icon name="external" size={11} />
                      </a>
                    ) : null}
                  </span>
                </label>
              ))}
            </div>
          ) : (
            <div className="banner banner-info">
              <Icon name="info" /> No alternatives are cached yet. Run Refresh Metadata once to
              collect them from the currently verified sources.
            </div>
          )}
        </>
      ) : null}
    </Modal>
  );
}

function MetadataSourcesModal({
  manga,
  onClose,
  onSaved,
}: {
  manga: Manga;
  onClose: () => void;
  onSaved: () => Promise<void>;
}) {
  const { notify } = useApp();
  const [sources, setSources] = useState<MetadataCorrelationSource[] | null>(null);
  const [values, setValues] = useState<Record<string, string>>({});
  const [initialValues, setInitialValues] = useState<Record<string, string>>({});
  const [loadingError, setLoadingError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const loadSources = useCallback(async () => {
    setLoadingError(null);
    try {
      const result = await api.metadataCorrelations(manga.id);
      setSources(result.sources);
      const loadedValues = Object.fromEntries(
        result.sources
          .filter((source) => source.editable)
          .map((source) => [source.source, source.manual_url ?? ""]),
      );
      setValues(loadedValues);
      setInitialValues(loadedValues);
    } catch (caught) {
      setLoadingError(String(caught));
    }
  }, [manga.id]);

  useEffect(() => {
    void loadSources();
  }, [loadSources]);

  const dirtySources = useMemo(
    () =>
      (sources ?? []).filter(
        (source) =>
          source.editable &&
          (values[source.source]?.trim() ?? "") !==
            (initialValues[source.source]?.trim() ?? ""),
      ),
    [initialValues, sources, values],
  );

  const save = async () => {
    if (!sources) return;
    setSaving(true);
    try {
      const updates = Object.fromEntries(
        dirtySources.map((source) => [
          source.source,
          values[source.source]?.trim() || null,
        ]),
      );
      const result = await api.updateMetadataCorrelations(manga.id, updates);
      const pinned = result.correlations.sources.filter(
        (source) => source.origin === "manual",
      );
      notify(
        "success",
        pinned.length
          ? `Metadata sources saved · ${pinned.map((source) => source.label).join(", ")} pinned.`
          : "Manual metadata overrides cleared; automatic and MangaDex identities restored.",
      );
      await onSaved();
    } catch (caught) {
      notify("error", String(caught));
      setSaving(false);
    }
  };

  return (
    <Modal
      title={`Metadata sources · ${manga.title}`}
      onClose={saving ? () => undefined : onClose}
      wide
      footer={
        <>
          <button type="button" className="btn" disabled={saving} onClick={onClose}>
            Cancel
          </button>
          <button
            type="button"
            className="btn btn-primary"
            disabled={saving || !sources || dirtySources.length === 0}
            onClick={() => void save()}
          >
            <Icon name="check" /> {saving ? "Validating & enriching…" : "Save & enrich"}
          </button>
        </>
      }
    >
      <p className="muted">
        Paste an exact MangaBaka series URL or ID. Tankarr pins that identity, then refreshes
        description, creators, counts and artwork from it. Clear the field to return to the
        automatic matcher.
      </p>
      {loadingError ? (
        <div className="banner banner-danger">
          <Icon name="alert" />
          <span>{loadingError}</span>
          <button type="button" className="btn btn-small" onClick={() => void loadSources()}>
            Retry
          </button>
        </div>
      ) : null}
      {!sources && !loadingError ? <Spinner /> : null}
      {sources?.filter((source) => source.editable).map((source) => (
        <div className="metadata-source-editor" key={source.source}>
          <div className="metadata-source-editor-heading">
            <label htmlFor={`metadata-source-${source.source}`}>
              <strong>{source.label}</strong>
            </label>
            <StatusPill kind={source.origin === "manual" ? "provider" : "muted"}>
              {correlationOriginLabel(source)}
            </StatusPill>
          </div>
          <div className="setting-field-actions">
            <input
              id={`metadata-source-${source.source}`}
              name={`metadata-source-${source.source}`}
              className="input"
              type="text"
              inputMode="url"
              autoComplete="off"
              aria-label={`${source.label} exact series URL or ID`}
              disabled={!source.configured && !source.allows_link_only}
              value={values[source.source] ?? ""}
              placeholder="Paste the canonical series URL or provider ID"
              onChange={(event) =>
                setValues((current) => ({
                  ...current,
                  [source.source]: event.target.value,
                }))
              }
            />
            {values[source.source] ? (
              <button
                type="button"
                className="btn btn-small"
                onClick={() =>
                  setValues((current) => ({ ...current, [source.source]: "" }))
                }
              >
                {`Use automatic ${source.label} match`}
              </button>
            ) : null}
          </div>
          <div className="muted small metadata-source-editor-detail">
            {source.effective_url ? (
              <>
                Current: {source.matched_title ? `${source.matched_title} · ` : ""}
                <a href={source.effective_url} target="_blank" rel="noreferrer">
                  {source.external_id} <Icon name="external" size={11} />
                </a>
              </>
            ) : (
              "No verified identity is currently available."
            )}
            {!source.configured && source.unavailable_reason
              ? source.allows_link_only
                ? ` · ${source.unavailable_reason}. The exact link can still be saved.`
                : ` · ${source.unavailable_reason}. Configure this provider in Settings before adding or changing its ID.`
              : ""}
          </div>
        </div>
      ))}
      {sources?.some((source) => !source.editable) ? (
        <div className="metadata-link-only-list">
          <strong>Additional exact links from MangaDex</strong>
          <p className="muted small">
            These catalogues have no supported enrichment API in Tankarr, so they are linked but not
            scraped.
          </p>
          <div className="series-external-links">
            {sources
              .filter((source) => !source.editable && source.effective_url)
              .map((source) => (
                <a
                  key={source.source}
                  href={source.effective_url ?? undefined}
                  target="_blank"
                  rel="noreferrer"
                >
                  {source.label} <Icon name="external" size={12} />
                </a>
              ))}
          </div>
        </div>
      ) : null}
    </Modal>
  );
}

function torrentStatusPill(status: string): { kind: string; label: string } {
  if (status === "completed") return { kind: "info", label: "Downloaded" };
  if (status === "imported") return { kind: "success", label: "Imported" };
  if (status === "review") return { kind: "warn", label: "Review" };
  if (status === "checking") return { kind: "info", label: "Checking" };
  if (status === "adding") return { kind: "muted", label: "Adding" };
  return jobStatusPill(status);
}

function SeriesPackSearchPanel({ manga, open, onClose }: { manga: Manga; open: boolean; onClose: () => void }) {
  const { notify } = useApp();
  const [query, setQuery] = useState(manga.title);
  const [results, setResults] = useState<TorrentRelease[] | null>(null);
  const [downloads, setDownloads] = useState<TorrentDownload[]>([]);
  const [busy, setBusy] = useState(false);
  const [searching, setSearching] = useState(false);

  const loadDownloads = useCallback(async () => {
    try {
      // Only work in progress belongs on the series page: finished and failed
      // grabs are handled automatically and stay in History.
      setDownloads(
        await api.torrents(manga.id, ["adding", "queued", "downloading", "importing"], 100),
      );
    } catch (caught) {
      if (open) notify("error", String(caught));
    }
  }, [manga.id, notify, open]);

  useEffect(() => {
    void loadDownloads();
    const timer = window.setInterval(() => void loadDownloads(), 4000);
    return () => window.clearInterval(timer);
  }, [loadDownloads]);

  const search = async () => {
    setSearching(true);
    setResults(null);
    try {
      const response = await api.searchTorrents(manga.id, query);
      setResults(response.results);
      response.errors.forEach((item) =>
        notify("info", `${item.provider}: ${item.error}`),
      );
    } catch (caught) {
      notify("error", String(caught));
    } finally {
      setSearching(false);
    }
  };

  // Opening the panel is the search: the series title is already the query,
  // like Sonarr's interactive season search. The input stays for refinement.
  const autoSearchedRef = useRef(false);
  useEffect(() => {
    if (!open || autoSearchedRef.current) return;
    if (manga.preferred_language !== "en") return;
    autoSearchedRef.current = true;
    void search();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  const grab = async (release: TorrentRelease) => {
    setBusy(true);
    try {
      await api.grabTorrent(manga.id, release.provider, release.id);
      notify("success", `${release.title} added to qBittorrent.`);
      await loadDownloads();
    } catch (caught) {
      notify("error", String(caught));
    } finally {
      setBusy(false);
    }
  };

  const importDownload = async (download: TorrentDownload) => {
    setBusy(true);
    try {
      const updated = await api.importTorrent(download.id);
      notify(
        updated.status === "failed" ? "error" : "success",
        updated.status === "failed" ? updated.message : "Torrent release imported into the Comics library.",
      );
      await loadDownloads();
    } catch (caught) {
      notify("error", String(caught));
    } finally {
      setBusy(false);
    }
  };

  const retryDownload = async (download: TorrentDownload) => {
    setBusy(true);
    try {
      await api.retryTorrent(download.id);
      notify("success", "Torrent download requeued.");
      await loadDownloads();
    } catch (caught) {
      notify("error", String(caught));
    } finally {
      setBusy(false);
    }
  };

  const discardDownload = async (download: TorrentDownload) => {
    const imported = download.status === "imported";
    if (
      !window.confirm(
        imported
          ? "Remove the torrent and its qBittorrent staging files? The imported CBZ files in the Comics library will be kept."
          : "Remove this torrent and delete its qBittorrent staging files?",
      )
    ) {
      return;
    }
    setBusy(true);
    try {
      await api.discardTorrent(download.id, true);
      notify("success", "Torrent and staging files removed.");
      await loadDownloads();
    } catch (caught) {
      notify("error", String(caught));
    } finally {
      setBusy(false);
    }
  };

  const jobFor = (release: TorrentRelease) =>
    downloads.find(
      (download) =>
        download.source === release.provider && download.source_id === release.id,
    ) ?? null;

  if (!open && downloads.length === 0) return null;
  return (
    <section className="panel nyaa-panel" aria-busy={searching}>
      {open ? (
        <div className="nyaa-header">
          <div>
            <h2>Series Pack Search</h2>
            <div className="search-scope">
              <StatusPill kind="provider">Prowlarr</StatusPill>
              <span className="muted small">Series and multi-volume releases</span>
            </div>
          </div>
          <button type="button" className="btn btn-ghost" onClick={onClose}>
            <Icon name="close" /> Close
          </button>
        </div>
      ) : (
        <div className="nyaa-header">
          <h2>Torrent downloads</h2>
        </div>
      )}

      {downloads.length ? (
        <div className="torrent-downloads">
          {downloads.slice(0, 10).map((download) => {
            const pill = torrentStatusPill(download.status);
            return (
              <div className="torrent-download" key={download.id}>
                <div className="torrent-main">
                  <StatusPill kind="provider">
                    {download.source === "prowlarr"
                      ? `Prowlarr · ${download.indexer || "Indexer"}`
                      : download.indexer || "Torrent"}
                  </StatusPill>
                  <div className="torrent-title" title={download.title}>{download.title}</div>
                  <div className="muted small">
                    {download.volume_hint ? `Volume ${download.volume_hint}` : download.chapter_hint ? `Chapter ${download.chapter_hint}` : "Number pending archive inspection"}
                    {" · "}{formatBytes(download.size_bytes)}{" · "}{download.message}
                  </div>
                  <div className="torrent-progress">
                    <StatusPill kind={pill.kind}>{pill.label}</StatusPill>
                    <div className="torrent-progress-bar">
                      <div className="progress" role="progressbar" aria-valuenow={Math.round(download.progress * 100)}>
                        <div className="progress-fill progress-accent" style={{ width: `${Math.round(download.progress * 100)}%` }} />
                      </div>
                    </div>
                  </div>
                </div>
                <div className="toolbar-group">
                  {download.status === "completed" ? (
                    <button type="button" className="btn btn-primary" disabled={busy} onClick={() => void importDownload(download)}>
                      <Icon name="library" /> Import now
                    </button>
                  ) : null}
                  {download.status === "failed" ? (
                    <button type="button" className="btn" disabled={busy} onClick={() => void retryDownload(download)}>
                      <Icon name="refresh" /> Retry
                    </button>
                  ) : null}
                  {download.status !== "importing" ? (
                    <button type="button" className="btn btn-ghost btn-icon" title="Remove torrent and staging files" disabled={busy} onClick={() => void discardDownload(download)}>
                      <Icon name="trash" size={15} />
                    </button>
                  ) : null}
                </div>
              </div>
            );
          })}
        </div>
      ) : null}

      {open ? (
        <>
          {manga.preferred_language !== "en" ? (
            <div className="banner banner-warn">
              <Icon name="alert" /> Torrent import currently requires an English translation profile so OCR can verify the result.
            </div>
          ) : (
            <form
              className="torrent-search-form"
              onSubmit={(event) => {
                event.preventDefault();
                void search();
              }}
            >
              <input
                className="input input-grow"
                value={query}
                minLength={2}
                maxLength={200}
                disabled={searching}
                onChange={(event) => setQuery(event.target.value)}
                placeholder="Series title or pack query…"
              />
              <button
                type="submit"
                className="btn btn-primary"
                disabled={busy || searching || query.trim().length < 2}
                aria-busy={searching}
              >
                {searching ? <Spinner /> : <Icon name="search" />}
                {searching ? "Searching Prowlarr…" : "Search Prowlarr"}
              </button>
            </form>
          )}

          {searching ? (
            <div className="search-operation-state" role="status" aria-live="polite">
              <Spinner />
              <span>Searching the configured indexers for series and volume packs…</span>
            </div>
          ) : null}

          {results !== null ? (
            results.length ? (
              <div className="torrent-results-scroll">
                <table className="table torrent-results">
                  <thead>
                    <tr>
                      <th>Release</th>
                      <th>Parsed coverage</th>
                      <th>Size</th>
                      <th>Seeds</th>
                      <th>Match</th>
                      <th className="col-actions" aria-label="Actions" />
                    </tr>
                  </thead>
                  <tbody>
                    {results.map((release) => {
                      const current = jobFor(release);
                      const pill = current ? torrentStatusPill(current.status) : null;
                      return (
                        <tr key={`${release.provider}:${release.id}`}>
                          <td>
                            <StatusPill kind="provider">{release.provider_label}</StatusPill>
                            {release.source_url ? (
                              <a className="table-link" href={release.source_url} target="_blank" rel="noreferrer">
                                {release.title} <Icon name="external" size={11} />
                              </a>
                            ) : <div>{release.title}</div>}
                            <div className="muted small">
                              {release.category}{release.trusted ? " · trusted" : ""} · {release.downloads} grabs
                            </div>
                          </td>
                          <td>{release.volume ? `Volume ${release.volume}` : release.chapter ? `Chapter ${release.chapter}` : "Inspect archive"}</td>
                          <td>{release.size}</td>
                          <td>{release.seeders}</td>
                          <td>{release.match_score}%</td>
                          <td className="col-actions">
                            {current && pill ? (
                              <StatusPill kind={pill.kind}>{pill.label}</StatusPill>
                            ) : (
                              <button type="button" className="btn btn-primary" disabled={busy} onClick={() => void grab(release)}>
                                <Icon name="download" /> Grab
                              </button>
                            )}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            ) : (
              <div className="muted small">No matching torrent release was found by the configured indexers.</div>
            )
          ) : null}
        </>
      ) : null}
    </section>
  );
}

function ChapterRows({
  chapter,
  best,
  downloaded,
  readerBook,
  bookmark,
  volumeBook,
  readerLabel,
  activeJob,
  isExpanded,
  onToggleExpand,
  busy,
  automaticSearching,
  onAutomaticSearch,
  onDeleteFile,
  onToggleMonitored,
  onInteractiveSearch,
}: {
  chapter: SeriesUnitChapter;
  best: Chapter | undefined;
  downloaded: Chapter | undefined;
  readerBook: ReaderBookLink | undefined;
  bookmark: ReaderLink["bookmark"];
  volumeBook?: ReaderBookLink;
  readerLabel: string;
  activeJob: Job | undefined;
  isExpanded: boolean;
  onToggleExpand: () => void;
  busy: boolean;
  automaticSearching: boolean;
  onAutomaticSearch: () => void;
  onDeleteFile: (chapter: Chapter) => void;
  onToggleMonitored: (chapterId: string, monitored: boolean) => void;
  onInteractiveSearch: () => void;
}) {
  void isExpanded;
  void onToggleExpand;
  const shown = downloaded ?? best;
  const monitored = chapter.monitored;
  const status = chapter.downloaded && chapter.duplicate_of_volume ? (
    <StatusPill kind="warn">
      <span title={`This chapter file is also inside volume ${chapter.duplicate_of_volume}, which you own. Kept until you delete it.`}>
        Duplicate · v{chapter.duplicate_of_volume}
      </span>
    </StatusPill>
  ) : chapter.downloaded && downloaded?.page_quality?.verdict === "degraded" ? (
    <StatusPill kind="warn">
      <span title={`${downloaded.page_quality.reason}. Tankarr replaces it from another source when one carries the chapter; until then the file is kept.`}>
        Degraded pages
      </span>
    </StatusPill>
  ) : chapter.downloaded ? (
    <StatusPill kind="success">
      <Icon name="check" size={12} /> Downloaded
    </StatusPill>
  ) : chapter.covered_by_volume ? (
    <StatusPill kind="success">
      {volumeBook ? (
        <a
          className="pill-link"
          href={volumeBook.url}
          target="_blank"
          rel="noreferrer"
          title={`This chapter is inside volume ${chapter.covered_by_volume}, which you own. Open the book in ${readerLabel}.`}
        >
          <Icon name="library" size={12} /> In v{chapter.covered_by_volume}
        </a>
      ) : (
        <span title={chapter.coverage_exact ? `Inside owned volume ${chapter.covered_by_volume} (exact chapter map). Open the volume's book to read it.` : `Inside an owned volume block ending at ${chapter.covered_by_volume}; exact volume unknown. Open the volume's book to read it.`}>
          In v{chapter.covered_by_volume}
        </span>
      )}
    </StatusPill>
  ) : chapter.covered_unmapped ? (
    <StatusPill kind="muted">
      <span title="You own volumes that may contain this chapter, but no release map says which. Not downloaded again until the map knows.">Covered · unmapped</span>
    </StatusPill>
  ) : chapter.ignored ? (
    <StatusPill kind="muted">
      {chapter.series_status_ignored ? "Manually excluded" : "Ignored"}
    </StatusPill>
  ) : activeJob || chapter.queue_status ? (
    <StatusPill kind="info">
      {humanize(activeJob?.status ?? chapter.queue_status ?? "queued")}
      {activeJob?.status === "downloading" ? ` ${Math.round(activeJob.progress * 100)}%` : ""}
    </StatusPill>
  ) : (chapter.all_releases_blocked ??
    (chapter.releases.length > 0 && chapter.releases.every((release) => release.blocked))) ? (
    <StatusPill kind="danger">
      <span title="Every known release failed; retry one from History to unblock it.">Blocked</span>
    </StatusPill>
  ) : (
    <StatusPill kind="muted">{chapter.missing ? "Missing" : chapter.special ? "Optional special" : "Not wanted"}</StatusPill>
  );
  return (
    <>
      <tr className={`${downloaded ? "row-downloaded" : ""} ${monitored ? "" : "row-unmonitored"}`}>
        <td className="col-monitor">
          <button
            type="button"
            className={`monitor-toggle ${monitored ? "on" : ""}`}
            disabled={busy || !shown || chapter.ignored}
            title={
              chapter.ignored
                ? "This volume is ignored; return it to Automatic or Monitored first"
                : shown
                ? monitored
                  ? "Monitored — click to unmonitor"
                  : "Unmonitored — click to monitor"
                : "Monitoring follows the series profile until a release is indexed"
            }
            onClick={() => shown && onToggleMonitored(shown.id, !monitored)}
          >
            <Icon name="bookmark" size={15} />
          </button>
        </td>
        <td className="col-chapter">
          <span className="chapter-label">
            {chapter.chapter ? `Chapter ${chapter.chapter}` : chapter.volume ? `Volume ${chapter.volume}` : "Book"}
          </span>
          {downloaded?.pages && downloaded.pages > 0 ? (
            <span className="muted small chapter-subtitle"> · {downloaded.pages} pages</span>
          ) : null}
        </td>
        <td className="col-status">{status}
          {bookmark && chapter.releases.some((release) => release.id === bookmark.chapter_id) ? (
            <a className="bookmark-location" href={bookmark.url} target="_blank" rel="noreferrer"><Icon name="bookmark" size={14} /> Continue · page {bookmark.page_index + 1}</a>
          ) : null}
        </td>
        <td className="col-actions">
          {chapter.downloaded && downloaded ? (
            <div className="chapter-actions">
              {readerBook ? (
                <a
                  className="btn btn-ghost btn-icon"
                  href={readerBook.url}
                  target="_blank"
                  rel="noreferrer"
                  title={`Read this book in ${readerLabel}`}
                  aria-label={`Read this book in ${readerLabel}`}
                >
                  <Icon name="library" size={15} />
                </a>
              ) : null}
              <button
                type="button"
                className="btn btn-ghost btn-icon"
                disabled={busy}
                title="Interactive Search: inspect releases and optionally replace this file"
                aria-label="Interactive Search"
                onClick={onInteractiveSearch}
              >
                <Icon name="user" size={15} />
              </button>
              <button
                type="button"
                className="btn btn-ghost btn-icon"
                disabled={busy}
                title={downloaded.library_path ?? "Delete downloaded file"}
                onClick={() => onDeleteFile(downloaded)}
              >
                <Icon name="trash" size={15} />
              </button>
            </div>
          ) : activeJob || chapter.queue_status || chapter.ignored ? null : (
            <div className="chapter-actions">
              {chapter.chapter || chapter.volume ? (
                <button
                  type="button"
                  className="btn btn-ghost btn-icon"
                  disabled={busy}
                  title="Automatic Search: queue the best verified release"
                  aria-label={automaticSearching ? "Automatic Search in progress" : "Automatic Search"}
                  aria-busy={automaticSearching}
                  onClick={onAutomaticSearch}
                >
                  {automaticSearching ? <Spinner /> : <Icon name="search" size={15} />}
                </button>
              ) : null}
              {chapter.chapter || chapter.volume ? (
                <button
                  type="button"
                  className="btn btn-ghost btn-icon"
                  disabled={busy}
                  title="Interactive Search: inspect every result"
                  aria-label="Interactive Search"
                  onClick={onInteractiveSearch}
                >
                  <Icon name="user" size={15} />
                </button>
              ) : null}
            </div>
          )}
        </td>
      </tr>
    </>
  );
}

export function EditModal({
  manga,
  onClose,
  onSaved,
  onOpenCover,
  onOpenMetadata,
}: {
  manga: Manga;
  onClose: () => void;
  onSaved: () => Promise<void>;
  onOpenCover: () => void;
  onOpenMetadata: () => void;
}) {
  const { notify } = useApp();
  const [title, setTitle] = useState(manga.title);
  const [language, setLanguage] = useState(manga.preferred_language);
  const [translationEnabled, setTranslationEnabled] = useState(Boolean(manga.translation_enabled));
  const [translationLanguages, setTranslationLanguages] = useState(manga.translation_source_languages ?? "");
  const [mode, setMode] = useState<MonitorMode>(manga.monitor_mode);
  const [readerMode, setReaderMode] = useState<"automatic" | "manga" | "webtoon">(
    manga.reader_mode_override ?? "automatic",
  );
  const [readerDirection, setReaderDirection] = useState<"automatic" | "ltr" | "rtl">(
    manga.reader_direction_override ?? "automatic",
  );
  const [statusOverride, setStatusOverride] = useState<PublicationStatusOverride>(
    manga.status_override ?? "automatic",
  );
  const counts = seriesCounts(manga);
  const [libraryStatusOverride, setLibraryStatusOverride] = useState<LibraryStatusOverride>(
    manga.library_status_override ?? "automatic",
  );
  const activeExpectedCount = manga.expected_count_unit_override === counts.unit
    ? manga.expected_count_override : null;
  const [expectedCountMode, setExpectedCountMode] = useState<"automatic" | "manual">(
    activeExpectedCount == null ? "automatic" : "manual",
  );
  const [expectedCount, setExpectedCount] = useState(
    String(Math.max(1, activeExpectedCount ?? counts.total_count)),
  );
  // A work is counted in chapters and bound in books, and an operator may
  // know both. The followed unit takes the total above; the other one is set
  // here, so a chapter series can still say how many books its edition has.
  const [bookCountMode, setBookCountMode] = useState<"automatic" | "manual">(
    manga.edition_book_count == null ? "automatic" : "manual",
  );
  const [bookCount, setBookCount] = useState(
    String(Math.max(1, manga.edition_book_count ?? counts.volume_progress?.expected ?? 1)),
  );
  const [saving, setSaving] = useState(false);
  const [customTitleMode, setCustomTitleMode] = useState(false);
  const automaticAuthors = (manga.metadata?.authors ?? manga.source_authors ?? []).join(", ");
  // Every edition MangaBaka's publisher notes size ("2 Vols - Complete"):
  // picking one fills the manual total, so an omnibus never has to be counted
  // by hand.
  const editions = useMemo(
    () =>
      (manga.metadata?.publishers ?? []).flatMap((publisher) => {
        const match = /(\d+)\s*(?:vols?|volumes?)\b/i.exec(publisher.note ?? "");
        if (!match) return [];
        const volumes = Number(match[1]);
        if (!Number.isFinite(volumes) || volumes < 1) return [];
        const type = publisher.type ? ` (${publisher.type})` : "";
        return [
          {
            key: `${publisher.name}:${publisher.type ?? ""}:${volumes}`,
            volumes,
            label: `${publisher.name}${type} — ${volumes} vol${volumes === 1 ? "" : "s"}${/complete/i.test(publisher.note ?? "") ? ", complete" : ""}`,
          },
        ];
      }),
    [manga.metadata?.publishers],
  );
  const [authors, setAuthors] = useState<string>((manga.authors_override ?? []).join(", "));
  const automaticTitle = manga.metadata_title || manga.source_title;
  const titleChoices = useMemo(() => {
    const choices: Array<{ value: string; label: string }> = [];
    const seen = new Set<string>();
    const add = (value: string | null | undefined, label: string) => {
      const normalized = String(value ?? "").trim().replace(/\s+/g, " ");
      const key = normalized.toLocaleLowerCase();
      if (!normalized || seen.has(key)) return;
      seen.add(key);
      choices.push({ value: normalized, label });
    };
    add(automaticTitle, `${automaticTitle} — Automatic (recommended)`);
    add(manga.source_title, `${manga.source_title} — Original provider title`);
    for (const alias of manga.metadata?.alternate_titles ?? []) {
      add(alias, `${alias} — Alias`);
    }
    return choices;
  }, [automaticTitle, manga.metadata?.alternate_titles, manga.source_title]);
  const selectedKnownTitle = customTitleMode
    ? "__custom__"
    : (titleChoices.find((choice) => choice.value === title)?.value ?? "__custom__");

  const languages = Array.from(
    new Set([manga.preferred_language, ...manga.available_languages, ...LANGUAGES.map(([code]) => code)]),
  );

  const save = async () => {
    const normalizedTitle = title.trim().replace(/\s+/g, " ");
    if (!normalizedTitle) {
      notify("error", "Series title cannot be empty.");
      return;
    }
    const parsedExpectedCount = Number(expectedCount);
    if (
      expectedCountMode === "manual" &&
      (!Number.isInteger(parsedExpectedCount) ||
        parsedExpectedCount < 1 ||
        parsedExpectedCount > 100000)
    ) {
      notify("error", "Managed edition total must be a whole number between 1 and 100000.");
      return;
    }
    if (
      expectedCountMode === "manual" &&
      counts.unit !== "chapter" &&
      parsedExpectedCount < counts.downloaded_count
    ) {
      notify(
        "error",
        `Managed edition total cannot be lower than ${counts.downloaded_count} imported ${counts.unit}${counts.downloaded_count === 1 ? "" : "s"}.`,
      );
      return;
    }
    setSaving(true);
    try {
      const messages: string[] = [];
      let warning = false;
      if (
        normalizedTitle !== manga.title ||
        (manga.title_overridden && normalizedTitle === automaticTitle)
      ) {
        const renamed = await api.renameManga(manga.id, normalizedTitle);
        messages.push(
          `Title set to ${renamed.title}; moved ${renamed.organization.moved} managed file${renamed.organization.moved === 1 ? "" : "s"}.`,
        );
        warning = renamed.warnings.length > 0;
      }
      const changes: Partial<Pick<Manga, "preferred_language" | "monitor_mode">> & {
        translation_enabled?: boolean;
        translation_source_languages?: string;
        assemble_books_automatically?: boolean;
        status_override?: PublicationStatusOverride;
        library_status_override?: LibraryStatusOverride;
        expected_count_override?: number | "automatic";
        edition_book_count?: number | "automatic";
        authors_override?: string[] | "automatic";
        reader_mode?: "automatic" | "manga" | "webtoon";
        reader_direction?: "automatic" | "ltr" | "rtl";
      } = {};
      const editedAuthors = authors
        .split(",")
        .map((name) => name.trim())
        .filter(Boolean);
      const currentAuthors = manga.authors_override ?? [];
      if (editedAuthors.join("|") !== currentAuthors.join("|")) {
        changes.authors_override = editedAuthors.length ? editedAuthors : "automatic";
      }
      if (language !== manga.preferred_language) changes.preferred_language = language;
      if (translationEnabled !== Boolean(manga.translation_enabled)) changes.translation_enabled = translationEnabled;
      if (translationLanguages !== (manga.translation_source_languages ?? "")) changes.translation_source_languages = translationLanguages;
      if (mode !== manga.monitor_mode) changes.monitor_mode = mode;
      if (readerMode !== (manga.reader_mode_override ?? "automatic")) {
        changes.reader_mode = readerMode;
      }
      if (readerDirection !== (manga.reader_direction_override ?? "automatic")) {
        changes.reader_direction = readerDirection;
      }
      if (statusOverride !== (manga.status_override ?? "automatic")) {
        changes.status_override = statusOverride;
      }
      if (libraryStatusOverride !== (manga.library_status_override ?? "automatic")) {
        changes.library_status_override = libraryStatusOverride;
      }
      if (expectedCountMode === "automatic" && activeExpectedCount != null) {
        changes.expected_count_override = "automatic";
      } else if (
        expectedCountMode === "manual" &&
        (manga.expected_count_override !== parsedExpectedCount ||
          manga.expected_count_unit_override !== counts.unit)
      ) {
        changes.expected_count_override = parsedExpectedCount;
      }
      if (bookCountMode === "automatic" && manga.edition_book_count != null) {
        changes.edition_book_count = "automatic";
      } else if (bookCountMode === "manual" && counts.unit !== "volume") {
        const parsedBooks = Number(bookCount);
        if (!Number.isInteger(parsedBooks) || parsedBooks < 1 || parsedBooks > 100000) {
          notify("error", "Books in this edition must be a whole number between 1 and 100000.");
          setSaving(false);
          return;
        }
        if (manga.edition_book_count !== parsedBooks) changes.edition_book_count = parsedBooks;
      }
      if (Object.keys(changes).length) {
        await api.updateManga(manga.id, changes);
        messages.push("Series settings saved.");
      }
      notify(warning ? "info" : "success", messages.join(" ") || "No changes to save.");
      await onSaved();
    } catch (caught) {
      notify("error", String(caught));
      setSaving(false);
    }
  };

  const futureBlocked = !manga.future_monitoring_allowed;

  return (
    <Modal
      title={`Edit ${manga.title}`}
      onClose={onClose}
      footer={
        <>
          <button type="button" className="btn" onClick={onClose}>
            Cancel
          </button>
          <button type="button" className="btn btn-primary" disabled={saving} onClick={() => void save()}>
            <Icon name="check" /> Save
          </button>
        </>
      }
    >
      <div className="form-row">
        <label htmlFor="series-known-title">Choose a known title</label>
        <select
          id="series-known-title"
          className="input"
          value={selectedKnownTitle}
          disabled={saving}
          onChange={(event) => {
            if (event.target.value === "__custom__") {
              setCustomTitleMode(true);
              return;
            }
            setCustomTitleMode(false);
            setTitle(event.target.value);
          }}
        >
          <option value="__custom__">Custom title</option>
          {titleChoices.map((choice) => (
            <option key={choice.value} value={choice.value}>
              {choice.label}
            </option>
          ))}
        </select>
        <p className="muted small">
          Automatic follows Tankarr&apos;s verified English-title priority. Choosing an original name or alias stores
          it as a persistent manual override; choosing Automatic removes that override.
        </p>
      </div>
      <div className="form-row">
        <label htmlFor="series-authors">Creators</label>
        <input
          id="series-authors"
          className="input"
          type="text"
          value={authors}
          disabled={saving}
          placeholder={automaticAuthors || "Follow the catalogue"}
          autoComplete="off"
          onChange={(event) => setAuthors(event.target.value)}
        />
        <p className="muted small">
          Comma separated. Leave empty to follow the catalogue ({automaticAuthors || "no credits"}).
          A correction here is kept through every metadata refresh and is written to the reader&apos;s library.
        </p>
      </div>
      <div className="form-row">
        <p className="muted small">
          Followed in {manga.chapter_index?.series_unit === "volumes" ? "whole books" : "single chapters"}, never both:
          the choice follows the catalogue and what the sources offer
          {manga.chapter_index?.series_unit_reason ? ` (${manga.chapter_index.series_unit_reason})` : ""}.
        </p>
      </div>
      <div className="form-row">
        <p className="muted small">When every chapter in an exact book map is available, create the book and move its chapter files to the recycle bin for the configured retention period. This applies to future completed books too.</p>
      </div>
      <div className="form-row">
        <label>Cover and metadata</label>
        <div className="toolbar-group">
          <button type="button" className="btn" disabled={saving} onClick={onOpenCover}>
            <Icon name="image" /> Choose cover…
          </button>
          <button type="button" className="btn" disabled={saving} onClick={onOpenMetadata}>
            <Icon name="edit" /> Metadata sources…
          </button>
        </div>
      </div>
      <div className="form-row">
        <label htmlFor="series-reader-mode">Reading mode</label>
        <select
          id="series-reader-mode"
          className="input"
          value={readerMode}
          disabled={saving}
          onChange={(event) => setReaderMode(event.target.value as typeof readerMode)}
        >
          <option value="automatic">Automatic (metadata and page shape)</option>
          <option value="manga">Paged</option>
          <option value="webtoon">Webtoon (vertical)</option>
        </select>
        <p className="muted small">
          Automatic uses webtoon evidence or tall pages for vertical scrolling. Paged books follow their origin and the direction below. You can switch temporarily while reading.
        </p>
      </div>
      <div className="form-row">
        <label htmlFor="series-reader-direction">Paged reading direction</label>
        <select
          id="series-reader-direction"
          className="input"
          value={readerDirection}
          disabled={saving}
          onChange={(event) => setReaderDirection(event.target.value as typeof readerDirection)}
        >
          <option value="automatic">Automatic (Japanese right to left; Chinese and Korean left to right)</option>
          <option value="rtl">Right to left</option>
          <option value="ltr">Left to right</option>
        </select>
        <p className="muted small">Applies to paged books in this series. Webtoons always scroll from top to bottom.</p>
      </div>
      <div className="form-row">
        <label htmlFor="series-title">Series title</label>
        <input
          id="series-title"
          className="input"
          value={title}
          maxLength={200}
          autoComplete="off"
          disabled={saving}
          onChange={(event) => {
            setCustomTitleMode(true);
            setTitle(event.target.value);
          }}
        />
        <p className="muted small">
          You can also type any custom title here. A manual choice always wins across metadata and provider refreshes.
          Tankarr renames every managed folder and CBZ, updates embedded ComicInfo metadata, and never overwrites an
          existing destination.
        </p>
        {manga.title_overridden && automaticTitle !== manga.title ? (
          <div className="inline-field-help">
            <span className="muted small">Automatic title: {automaticTitle}</span>
            <button
              type="button"
              className="btn btn-ghost btn-small"
              disabled={saving}
              onClick={() => setTitle(automaticTitle)}
            >
              Use automatic title
            </button>
          </div>
        ) : null}
        {manga.source_title !== automaticTitle ? (
          <div className="inline-field-help">
            <span className="muted small">Original provider title: {manga.source_title}</span>
            <button
              type="button"
              className="btn btn-ghost btn-small"
              disabled={saving}
              onClick={() => setTitle(manga.source_title)}
            >
              Use provider title
            </button>
          </div>
        ) : null}
        {title.trim().replace(/\s+/g, " ") !== manga.title && manga.downloaded_count > 0 ? (
          <div className="banner banner-info small">
            <Icon name="info" /> {manga.downloaded_count} downloaded file{manga.downloaded_count === 1 ? "" : "s"} will be reorganized. The operation can take a little longer while portable metadata is updated.
          </div>
        ) : null}
      </div>
      <div className="form-row">
        <label htmlFor="series-publication-status">Publication status</label>
        <select
          id="series-publication-status"
          className="input"
          value={statusOverride}
          disabled={saving}
          onChange={(event) => setStatusOverride(event.target.value as PublicationStatusOverride)}
        >
          <option value="automatic">Automatic (follow sources)</option>
          <option value="continuing">Continuing</option>
          <option value="hiatus">Hiatus</option>
          <option value="ended">Ended — pause automatic updates</option>
        </select>
        <p className="muted small">
          A manual status takes precedence over the sources. Ended pauses automatic series refreshes,
          metadata updates and scheduled searches. Choose Automatic to resume them with your saved monitoring settings.
          You can still search or refresh this series manually.
        </p>
      </div>
      <div className="form-row">
        <label htmlFor="series-expected-count">Managed edition total</label>
        <select
          id="series-expected-count-mode"
          className="input"
          value={expectedCountMode}
          disabled={saving}
          onChange={(event) => setExpectedCountMode(event.target.value as "automatic" | "manual")}
        >
          <option value="automatic">Automatic (work metadata)</option>
          <option value="manual">Manual edition total</option>
        </select>
        {expectedCountMode === "manual" ? (
          <input
            id="series-expected-count"
            className="input"
            type="number"
            min={counts.unit === "chapter" ? 1 : Math.max(1, counts.downloaded_count)}
            max={100000}
            step={1}
            value={expectedCount}
            disabled={saving}
            onChange={(event) => setExpectedCount(event.target.value)}
          />
        ) : null}
        {counts.unit === "volume" && editions.length > 0 ? (
          <select
            id="series-edition-picker"
            className="input"
            value=""
            disabled={saving}
            aria-label="Use a published edition's volume total"
            onChange={(event) => {
              const chosen = editions.find((edition) => edition.key === event.target.value);
              if (!chosen) return;
              setExpectedCountMode("manual");
              setExpectedCount(String(chosen.volumes));
            }}
          >
            <option value="">Use a published edition's total…</option>
            {editions.map((edition) => (
              <option key={edition.key} value={edition.key}>
                {edition.label}
              </option>
            ))}
          </select>
        ) : null}
        <p className="muted small">
          Automatic uses the original-work catalogue ({counts.automatic_expected_count ?? counts.catalogue_expected_count ?? "unknown"} {counts.unit}s). Use a manual total when your managed edition combines or splits that work differently (a 12-book Viz run of an 18-tankobon work, an 8-book omnibus). It changes the expected slots, never the imported-file count, survives metadata refreshes, and when the series is tracked by chapters it also tells Tankarr which chapters each owned book covers.
          {manga.library_count?.edition_split
            ? ` Now: ${manga.library_count.edition_split.owned_books} of ${manga.library_count.edition_split.books} books cover ${manga.library_count.edition_split.covered_chapters} chapters.`
            : ""}
        </p>
      </div>
      {counts.unit !== "volume" ? (
        <div className="form-row">
          <label htmlFor="series-book-count">Books in this edition</label>
          <select
            id="series-book-count-mode"
            className="input"
            value={bookCountMode}
            disabled={saving}
            onChange={(event) => setBookCountMode(event.target.value as "automatic" | "manual")}
          >
            <option value="automatic">Automatic (work metadata)</option>
            <option value="manual">Manual book total</option>
          </select>
          {bookCountMode === "manual" ? (
            <input
              id="series-book-count"
              className="input"
              type="number"
              min={1}
              max={100000}
              step={1}
              value={bookCount}
              disabled={saving}
              onChange={(event) => setBookCount(event.target.value)}
            />
          ) : null}
          <p className="muted small">
            How many books the edition on disk has, when the series is followed by
            chapters. Automatic uses the catalogue
            ({counts.volume_progress?.expected ?? "unknown"} books). Setting it says which
            chapters each book covers and bounds the shelf; it never changes the
            imported-file count.
          </p>
        </div>
      ) : null}
      <div className="form-row">
        <label htmlFor="series-library-status">Library status</label>
        <select
          id="series-library-status"
          className="input"
          value={libraryStatusOverride}
          disabled={saving}
          onChange={(event) => setLibraryStatusOverride(event.target.value as LibraryStatusOverride)}
        >
          <option value="automatic">Automatic (compare files with expected total)</option>
          <option value="up_to_date">Up to date (manual override)</option>
        </select>
        <p className="muted small">
          Manual Up to date keeps the real {counts.downloaded_count}/{counts.total_count} count visible but excludes absent items from Missing, Wanted and automatic recovery. Selecting Automatic restores normal comparison immediately.
        </p>
      </div>
      <div className="form-row">
        <label>Language</label>
        <select className="input" value={language} onChange={(event) => setLanguage(event.target.value)}>
          {languages.map((code) => (
            <option key={code} value={code}>
              {languageName(code)}
            </option>
          ))}
        </select>
        {language !== manga.preferred_language ? (
          <p className="muted small">
            Changing language re-baselines monitoring and reloads the chapter index for the new language.
          </p>
        ) : null}
      </div>
      <div className="form-row">
        <label htmlFor="series-translation">Translation fallback</label>
        <label className="muted small">
          <input id="series-translation" type="checkbox" checked={translationEnabled} disabled={saving} onChange={(event) => setTranslationEnabled(event.target.checked)} />
          Translate missing books and chapters into the selected language
        </label>
        <p className="muted small">Requires translation to be enabled and an AI provider configured in Settings → Translation. Releases in the requested language have priority.</p>
        {translationEnabled ? <>
          <label htmlFor="series-translation-languages">Source languages, in order</label>
          <input id="series-translation-languages" className="input" value={translationLanguages} disabled={saving} placeholder="Use global settings" onChange={(event) => setTranslationLanguages(event.target.value)} />
          <p className="muted small">Optional override, for example original,ja,fr. Leave empty to use the global language order.</p>
        </> : null}
      </div>
      <div className="form-row">
        <label>Monitor</label>
        <div className="option-cards">
          {MONITOR_OPTIONS.map((option) => {
            const disabled = futureBlocked && (option.value === "all" || option.value === "future");
            return (
              <button
                key={option.value}
                type="button"
                className={`option-card ${mode === option.value ? "selected" : ""}`}
                disabled={disabled}
                title={disabled ? manga.future_monitoring_reason : option.description}
                onClick={() => setMode(option.value)}
              >
                <strong>{option.title}</strong>
                <span>{option.description}</span>
              </button>
            );
          })}
        </div>
      </div>
    </Modal>
  );
}

export function FileDeleteModal({
  target,
  busy,
  onClose,
  onConfirm,
}: {
  target: FileDeletionTarget;
  busy: boolean;
  onClose: () => void;
  onConfirm: () => Promise<void>;
}) {
  const [confirmed, setConfirmed] = useState(false);
  const isChapter = target.kind === "chapter";
  const isDuplicates = target.kind === "duplicates";
  const title = isChapter
    ? `Delete chapter ${target.chapter.chapter ?? "special"} file`
    : isDuplicates
      ? `Retire ${target.chapters.length} duplicate chapter file${target.chapters.length === 1 ? "" : "s"}`
      : `Delete volume ${target.volume} files`;
  const language = isChapter
    ? target.chapter.language
    : isDuplicates
      ? target.chapters[0]?.language ?? ""
      : target.language;

  return (
    <Modal
      title={title}
      onClose={busy ? () => undefined : onClose}
      footer={
        <>
          <button type="button" className="btn" disabled={busy} onClick={onClose}>
            Cancel
          </button>
          <button
            type="button"
            className="btn btn-danger"
            disabled={busy || !confirmed}
            onClick={() => void onConfirm()}
          >
            <Icon name="trash" /> {isDuplicates ? "Move to recycle bin" : `Delete ${isChapter ? "file" : "files"}`}
          </button>
        </>
      }
    >
      <p>
        {isChapter ? (
          <>
            Tankarr will permanently delete the downloaded file for chapter {target.chapter.chapter ?? "special"}
            {target.chapter.volume ? `, volume ${target.chapter.volume}` : ""} in {languageName(language)}.
          </>
        ) : isDuplicates ? (
          <>
            Tankarr will move {target.chapters.length} chapter file{target.chapters.length === 1 ? "" : "s"}
            {" "}({formatChapterList(target.chapters)}) to the recycle bin. Their content is already inside book {target.volume}, which you own. The book files are kept.
          </>
        ) : (
          <>
            Tankarr will permanently delete the files for {target.chapters.length} downloaded chapter
            {target.chapters.length === 1 ? "" : "s"} in volume {target.volume} ({languageName(language)}).
          </>
        )}
      </p>
      {isChapter && target.chapter.library_path ? (
        <p className="muted small deletion-path">{target.chapter.library_path}</p>
      ) : null}
      {!isChapter ? <ul className="deletion-path small" aria-label="Reviewed files">{target.chapters.map((chapter) => <li key={chapter.id}>{chapter.library_path ?? chapter.title ?? chapter.id}<br /><span className="muted">File ID: {chapter.id}</span></li>)}</ul> : null}
      <div className="banner banner-warn">
        <Icon name="alert" /> {isDuplicates ? "These files leave the library and are kept in the recycle bin for the configured retention period before automatic cleanup." : "The chapter records remain in Tankarr, but this deletion cannot be undone. Monitored chapters can be downloaded again later."}
      </div>
      <label className="checkbox-row">
        <input
          type="checkbox"
          checked={confirmed}
          disabled={busy}
          onChange={(event) => setConfirmed(event.target.checked)}
        />
        {isDuplicates ? "I reviewed these files and confirm moving them to the recycle bin" : "I understand and confirm this permanent file deletion"}
      </label>
    </Modal>
  );
}

export function DeleteModal({
  manga,
  onClose,
  onDeleted,
}: {
  manga: Manga;
  onClose: () => void;
  onDeleted: (result: DeleteResult, deleteFiles: boolean) => void;
}) {
  const { notify } = useApp();
  const [deleteFiles, setDeleteFiles] = useState(true);
  const [confirming, setConfirming] = useState(false);

  const remove = async () => {
    setConfirming(true);
    try {
      const result = await api.deleteManga(manga.id, deleteFiles);
      onDeleted(result, deleteFiles);
    } catch (caught) {
      notify("error", String(caught));
      setConfirming(false);
    }
  };

  return (
    <Modal
      title={`Delete ${manga.title}`}
      onClose={onClose}
      footer={
        <>
          <button type="button" className="btn" disabled={confirming} onClick={onClose}>
            Cancel
          </button>
          <button type="button" className="btn btn-danger" disabled={confirming} onClick={() => void remove()}>
            <Icon name="trash" /> Delete
          </button>
        </>
      }
    >
      <p>The series and its chapter index will be removed from Tankarr immediately.</p>
      <label className="checkbox-row">
        <input type="checkbox" checked={deleteFiles} disabled={confirming}
          onChange={(event) => setDeleteFiles(event.target.checked)} />
        Permanently delete this series’ managed library files in every language
      </label>
      <p className="muted">
        {deleteFiles
          ? "File removal and reader synchronization continue in the background, including after a restart."
          : "Files will stay on disk and remain available in your reader."}
      </p>
    </Modal>
  );
}


function formatChapterList(chapters: Chapter[]): string {
  const labels = chapters
    .map((chapter) => chapter.chapter)
    .filter((value): value is string => Boolean(value));
  if (labels.length === 0) return `${chapters.length} chapters`;
  if (labels.length <= 6) return labels.map((label) => `c${label}`).join(", ");
  return `c${labels[0]}–c${labels[labels.length - 1]}`;
}


const DESCRIPTION_INLINE = /\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)|\*\*([^*]+)\*\*|\*([^*\n]+)\*|__([^_]+)__|(?:^|(?<=\s))_([^_\n]+)_(?=\s|$)/g;

function renderInline(text: string, keyPrefix: string): ReactNode[] {
  const nodes: ReactNode[] = [];
  let last = 0;
  for (const match of text.matchAll(DESCRIPTION_INLINE)) {
    const index = match.index ?? 0;
    if (index > last) nodes.push(text.slice(last, index));
    const key = `${keyPrefix}-${index}`;
    if (match[1] && match[2]) {
      nodes.push(
        <a key={key} href={match[2]} target="_blank" rel="noreferrer" onClick={(event) => event.stopPropagation()}>
          {match[1]}
        </a>,
      );
    } else if (match[3] || match[5]) {
      nodes.push(<strong key={key}>{match[3] ?? match[5]}</strong>);
    } else {
      nodes.push(<em key={key}>{match[4] ?? match[6]}</em>);
    }
    last = index + match[0].length;
  }
  if (last < text.length) nodes.push(text.slice(last));
  return nodes;
}

function renderDescription(text: string): ReactNode[] {
  // Markdown-lite as MangaBaka writes it: paragraphs, "• " bullets,
  // **bold**, *italic* and [text](https://…) links. No raw HTML.
  return text.split(/\n\s*\n/).map((paragraph, index) => (
    <p key={index} className="series-description-paragraph">
      {paragraph.split("\n").map((line, lineIndex, lines) => (
        <span key={lineIndex}>
          {renderInline(line, `${index}-${lineIndex}`)}
          {lineIndex < lines.length - 1 ? <br /> : null}
        </span>
      ))}
    </p>
  ));
}

function calendarTime(value: string): string {
  const moment = new Date(value);
  if (Number.isNaN(moment.getTime())) return "";
  return new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit" }).format(moment);
}

function calendarDate(value: string): string {
  const [year, month, day] = value.slice(0, 10).split("-").map(Number);
  const date = new Date(year, month - 1, day, 12);
  const thisYear = new Date().getFullYear();
  return new Intl.DateTimeFormat(undefined, {
    weekday: "short",
    day: "numeric",
    month: "short",
    ...(year !== thisYear ? { year: "numeric" } : {}),
  }).format(date);
}

const RECENT_RELEASE_WINDOW_DAYS = 90;

function SeriesCalendarSection({ calendar }: { calendar: SeriesCalendar }) {
  const { expected, cadence, reason } = calendar;
  // A finished work has no calendar; a paused one gets one line; a running
  // work gets the section only when it has a rhythm to show. Old scanlation
  // dates are not "recent releases".
  if (reason === "ended") return null;
  if (reason === "paused") {
    return (
      <p className="muted small series-calendar">
        Publication is paused: no upcoming chapter is expected until it resumes.
      </p>
    );
  }
  const cutoff = Date.now() - RECENT_RELEASE_WINDOW_DAYS * 24 * 3600 * 1000;
  const recent = calendar.recent.filter((item) => {
    const [year, month, day] = item.released_at.slice(0, 10).split("-").map(Number);
    return new Date(year, month - 1, day, 12).getTime() >= cutoff;
  });
  if (expected.length === 0 && recent.length === 0) return null;
  return (
    <section className="series-calendar" aria-label="Release calendar">
      <h2>Calendar</h2>
      {expected.length > 0 ? (
        <ul className="series-calendar-list">
          {expected.map((item) => (
            <li key={item.chapter} className={item.overdue_days > 0 ? "overdue" : undefined}>
              <strong>Chapter {item.chapter}</strong> · {item.estimated ? "expected" : "announced"}{" "}
              {calendarDate(item.expected_at)}
              {!item.estimated && item.published_at ? ` · ${calendarTime(item.published_at)}` : ""}
              <span className="muted small">
                {" "}
                {!item.estimated ? "· date listed by the official platform" : null}
                {item.overdue_days > 0
                  ? ` · ${item.overdue_days} day${item.overdue_days === 1 ? "" : "s"} overdue`
                  : ""}
                {item.downloaded
                  ? " · downloaded"
                  : item.official
                    ? " · available on the official platform"
                    : item.available
                      ? " · listed by a source"
                      : ""}
              </span>
            </li>
          ))}
        </ul>
      ) : null}
      {recent.length > 0 ? (
        <p className="muted small">
          Recent releases:{" "}
          {recent
            .map((item) => `ch. ${item.chapter} (${calendarDate(item.released_at)})`)
            .join(", ")}
          {calendar.source ? ` · from ${calendar.source}` : ""}
        </p>
      ) : null}
      {cadence ? (
        <p className="muted small">
          Expected dates follow the {cadence.label} rhythm of the release history (last: chapter{" "}
          {cadence.last_chapter} on {calendarDate(cadence.last_release_at)}). A date the official
          platform already lists is shown as announced; a skipped week shows as overdue.
        </p>
      ) : null}
    </section>
  );
}
