import type {
  InternetArchiveProbe,
  LibraryOrphans,
  ArtworkSelection,
  ArtworkSelectionUpdate,
  BackupInfo,
  AuthStatus,
  CalendarRelease,
  CalendarResponse,
  PublicationStatusOverride,
  SeriesCalendar,
  CancelJobsResponse,
  Chapter,
  ChapterAutomaticSearchResponse,
  ChapterReleaseSearchResponse,
  ChapterMapBoundary,
  ChapterMapPreview,
  ChapterMapState,
  SeriesUnits,
  DuplicateRetirementResult,
  BookAssemblyPreview,
  BookAssemblyResult,
  BooksAssemblyPreview,
  BooksAssemblyResult,
  SeriesAuditReport,
  SeriesAuditPreview,
  SeriesAuditResult,
  SeriesAuditSourceKey,
  DeleteResult,
  ImportGroup,
  ImportScan,
  ImportState,
  Health,
  Job,
  LibraryAlignment,
  LibraryOrganization,
  KomgaConnectionTest,
  Manga,
  MangaDeletionPreview,
  MangaPreview,
  MetadataRefreshResult,
  MetadataCorrelations,
  MetadataCorrelationsUpdate,
  MetadataStatus,
  MonitorMode,
  TorrentSearchResponse,
  ProviderProbe,
  ProwlarrProbe,
  ReaderLink,
  ReaderBookmark,
  ReaderBook,
  SearchResponse,
  SeriesRenameResult,
  SettingsPayload,
  SuwayomiSource,
  SuwayomiExtension,
  SuwayomiRuntimeStatus,
  SuwayomiSourceTest,
  SystemStatus,
  TorrentDownload,
  WantedEntry,
  WantedSearchResult,
  MonitorStatus,
  SeriesUnitChoice,
  QueueSeriesSummary,
  MatchReview,
  AuthorPage,
  TorrentContents,
} from "./types";
import type { AcquisitionPreview, BackupVerification, LibraryListItem, LibraryListPreview, MaintenanceStatus, PreflightReport, ReaderAlignment, RepairPreview } from "./operationTypes";

export type SuwayomiTestEvent =
  | { type: "start"; language: string; count: number }
  | ({ type: "result" } & SuwayomiSourceTest)
  | { type: "done" };

export type ReaderDiscoveryMatch = {
  kind: "stump" | "komga" | "kavita";
  label: string;
  internal_url: string;
  browser_url: string | null;
  browser_port: number;
  credentials: "accepted" | "required" | "rejected";
  detail: string;
};

export type ReaderDiscovery = {
  found: boolean;
  selected: ReaderDiscoveryMatch | null;
  matches: ReaderDiscoveryMatch[];
  checked: number;
  message: string;
};

export class ApiError extends Error {
  constructor(message: string, public readonly status: number, public readonly detail?: unknown) {
    super(message);
    this.name = "ApiError";
  }
}

// Keep navigation snapshots in memory, without retaining every series or
// calendar range visited during a long-running session.
const RESPONSE_CACHE_LIMIT = 32;
const responseCache = new Map<string, { etag: string | null; value: unknown }>();
const pendingReads = new Map<string, Promise<unknown>>();
const cacheRequests = new Map<string, number>();
let cacheEpoch = 0;
let requestSequence = 0;

export type LibrarySnapshot = {
  data: Manga[] | null;
  error: string | null;
  loading: boolean;
  updatedAt: number;
};

let librarySnapshot: LibrarySnapshot = { data: null, error: null, loading: false, updatedAt: 0 };
const libraryListeners = new Set<() => void>();
let libraryPending: {
  epoch: number;
  fresh: boolean;
  controller: AbortController;
  promise: Promise<Manga[]>;
} | null = null;

function publishLibrary(snapshot: LibrarySnapshot) {
  librarySnapshot = snapshot;
  libraryListeners.forEach((listener) => listener());
}

function invalidateSnapshots(clearLibrary = false) {
  const revalidateLibrary = librarySnapshot.data !== null || librarySnapshot.loading;
  cacheEpoch += 1;
  responseCache.clear();
  cacheRequests.clear();
  libraryPending?.controller.abort();
  libraryPending = null;
  publishLibrary({
    data: clearLibrary ? null : librarySnapshot.data,
    error: null,
    loading: false,
    updatedAt: 0,
  });
  // A mounted search box and Library use the same snapshot. Successful edits
  // update both, including when the edit happened on a different route.
  if (!clearLibrary && revalidateLibrary && libraryListeners.size) {
    void loadLibrary(true).catch(() => undefined);
  }
}

async function performRequest<T>(
  path: string,
  options?: RequestInit,
  cacheKey?: string,
): Promise<T> {
  const epoch = cacheEpoch;
  const sequence = ++requestSequence;
  if (cacheKey) cacheRequests.set(cacheKey, sequence);
  const cached = cacheKey ? responseCache.get(cacheKey) : undefined;
  const headers = new Headers(options?.headers);
  if (!headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  if (cached?.etag) headers.set("If-None-Match", cached.etag);
  const method = options?.method?.toUpperCase() ?? "GET";
  const controller = method === "GET" ? new AbortController() : null;
  const abort = () => controller?.abort(options?.signal?.reason);
  if (options?.signal?.aborted) abort();
  else options?.signal?.addEventListener("abort", abort, { once: true });
  const boundedRead = Boolean(cacheKey) || /^\/api\/(auth\/status|system\/health|health|jobs\/summary|monitor\/status)$/.test(path);
  const timeout = controller && boundedRead ? window.setTimeout(() => controller.abort(), 30_000) : undefined;
  try {
    let response: Response;
    try {
      response = await fetch(path, {
        ...options,
        credentials: "same-origin",
        headers,
        signal: controller?.signal ?? options?.signal,
      });
    } catch (caught) {
      if (options?.signal?.aborted) throw caught;
      if (controller?.signal.aborted) throw new Error("The request timed out. Please retry.");
      const detail = caught instanceof Error ? caught.message : String(caught);
      throw new Error(
        `Tankarr API is unreachable from this page (${detail}). Reload the app and verify the Tankarr URL or reverse proxy.`,
      );
    }
    if (response.status === 304 && cached) return cached.value as T;
    if (!response.ok) {
      if (response.status === 401) {
        invalidateSnapshots(true);
        window.dispatchEvent(new CustomEvent("tankarr:authentication-required"));
      }
      let detail = `${response.status} ${response.statusText}`;
      let structuredDetail: unknown;
      try {
        const body = await response.json();
        structuredDetail = body.detail;
        if (typeof body.detail === "string") detail = body.detail;
        else if (body.detail && typeof body.detail.message === "string") {
          const warning = Array.isArray(body.detail.warnings) && body.detail.warnings.length
            ? `: ${String(body.detail.warnings[0])}`
            : "";
          detail = `${body.detail.message}${warning}`;
        }
      } catch {
        // Keep the HTTP error when the body is not JSON.
      }
      throw new ApiError(detail, response.status, structuredDetail);
    }
    const value = response.status === 204 ? undefined as T : (await response.json()) as T;
    const etag = cacheKey ? response.headers.get("ETag") : null;
    if (cacheKey && epoch === cacheEpoch && cacheRequests.get(cacheKey) === sequence) {
      responseCache.delete(cacheKey);
      responseCache.set(cacheKey, { etag, value });
      while (responseCache.size > RESPONSE_CACHE_LIMIT) {
        responseCache.delete(responseCache.keys().next().value!);
      }
    }
    if (method !== "GET" && method !== "HEAD") {
      if (path.startsWith("/api/auth/")) invalidateSnapshots(true);
      else if (
        /^\/api\/(manga|chapters|jobs|torrents|reviews|wanted|monitor|library|import|metadata)(\/|\?|$)/.test(path) &&
        !path.startsWith("/api/import/uploads") &&
        !/\/(delete-preview|chapters\/search|preview)$/.test(path.split("?")[0])
      ) {
        invalidateSnapshots();
      }
    }
    return value;
  } finally {
    if (timeout !== undefined) window.clearTimeout(timeout);
    options?.signal?.removeEventListener("abort", abort);
    if (cacheKey && cacheRequests.get(cacheKey) === sequence) cacheRequests.delete(cacheKey);
  }
}

function request<T>(path: string, options?: RequestInit, cacheKey?: string): Promise<T> {
  // Explicitly cancellable requests have their own lifetime; other concurrent
  // reads (the header and Library, or overlapping badge refreshes) share work.
  if ((options?.method ?? "GET") !== "GET" || options?.signal) {
    return performRequest<T>(path, options, cacheKey);
  }
  const key = `${cacheEpoch}:${path}`;
  const pending = pendingReads.get(key);
  if (pending) return pending as Promise<T>;
  const promise = performRequest<T>(path, options, cacheKey).finally(() => {
    if (pendingReads.get(key) === promise) pendingReads.delete(key);
  });
  pendingReads.set(key, promise);
  return promise;
}

function loadLibrary(fresh = false): Promise<Manga[]> {
  if (libraryPending?.epoch === cacheEpoch && (!fresh || libraryPending.fresh)) {
    return libraryPending.promise;
  }
  libraryPending?.controller.abort();
  const epoch = cacheEpoch;
  const controller = new AbortController();
  publishLibrary({ ...librarySnapshot, loading: true, error: null });
  let loaded = false;
  const promise = request<Manga[]>(fresh ? "/api/manga?fresh=true" : "/api/manga?cached=true", { signal: controller.signal }, "/api/manga")
    .then((data) => {
      if (epoch !== cacheEpoch || libraryPending?.promise !== promise) return librarySnapshot.data ?? [];
      loaded = true;
      publishLibrary({ data, error: null, loading: false, updatedAt: Date.now() });
      return data;
    }, (caught: unknown) => {
      if (epoch === cacheEpoch && libraryPending?.promise === promise) {
        publishLibrary({
          ...librarySnapshot,
          loading: false,
          error: caught instanceof Error ? caught.message : String(caught),
        });
      }
      throw caught;
    })
    .finally(() => {
      if (libraryPending?.promise === promise) libraryPending = null;
      if (loaded && !fresh && epoch === cacheEpoch && libraryListeners.size && !libraryPending) {
        void loadLibrary(true).catch(() => undefined);
      }
    });
  libraryPending = { epoch, fresh, controller, promise };
  return promise;
}

export const libraryStore = {
  getSnapshot: () => librarySnapshot,
  subscribe: (listener: () => void) => {
    libraryListeners.add(listener);
    return () => { libraryListeners.delete(listener); };
  },
  revalidate: () => loadLibrary(librarySnapshot.data !== null),
  refresh: () => loadLibrary(true),
};

export const api = {
  translations: (mangaId: string) => request<{
    enabled: boolean;
    jobs: Array<{ id: string; slot_key: string; target_language: string; status: string; message: string; source: { language: string } }>;
  }>(`/api/translations?manga_id=${encodeURIComponent(mangaId)}`),
  searchTranslations: (mangaId: string) => request<{ translations_queued?: number }>(`/api/manga/${encodeURIComponent(mangaId)}/translations/search`, { method: "POST" }),
  retryTranslation: (id: string) => request(`/api/translations/${encodeURIComponent(id)}/retry`, { method: "POST" }),
  cancelTranslation: (id: string) => request(`/api/translations/${encodeURIComponent(id)}/cancel`, { method: "POST" }),
  uploadTranslation: (mangaId: string, file: File, language: string, unit: "volume" | "chapter", number: string) => {
    const query = new URLSearchParams({ source_language: language, [unit]: number });
    return request(`/api/manga/${encodeURIComponent(mangaId)}/translations/upload?${query}`, { method: "POST", headers: { "Content-Type": "application/zip" }, body: file });
  },
  authStatus: () => request<AuthStatus>("/api/auth/status"),
  login: (username: string, password: string, rememberMe: boolean) =>
    request<AuthStatus>("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({ username, password, remember_me: rememberMe }),
    }),
  logout: () =>
    request<{ authenticated: false }>("/api/auth/logout", { method: "POST" }),
  health: () => request<Health>("/api/system/health"),
  search: (query: string, language: string, provider: string) =>
    request<SearchResponse>(
      `/api/search?q=${encodeURIComponent(query)}&language=${encodeURIComponent(language)}&provider=${encodeURIComponent(provider)}`,
    ),
  library: loadLibrary,
  manga: (id: string, fresh = false, signal?: AbortSignal) => {
    const path = `/api/manga/${encodeURIComponent(id)}?compact=true`;
    return request<Manga>(fresh ? `${path}&fresh=true` : path, signal ? { signal } : undefined, path);
  },
  readerLink: (id: string) =>
    request<ReaderLink>(`/api/manga/${encodeURIComponent(id)}/reader-link`),
  readerBookmarks: () => request<ReaderBookmark[]>("/api/reader/bookmarks"),
  readerBook: (id: string) =>
    request<ReaderBook>(`/api/reader/books/${encodeURIComponent(id)}`),
  saveReaderBookmark: (mangaId: string, releaseId: string, pageIndex: number) =>
    request<{ chapter_id: string; page_index: number }>(
      `/api/reader/series/${encodeURIComponent(mangaId)}/bookmark`,
      { method: "PUT", body: JSON.stringify({ release_id: releaseId, page_index: pageIndex }) },
    ),
  clearReaderBookmark: (mangaId: string) =>
    request<void>(`/api/reader/series/${encodeURIComponent(mangaId)}/bookmark`, { method: "DELETE" }),
  previewManga: (id: string, language: string, provider: string) =>
    request<MangaPreview>(
      `/api/manga/${encodeURIComponent(id)}/preview?language=${encodeURIComponent(language)}&provider=${encodeURIComponent(provider)}`,
    ),
  authorPage: (id: string) => request<AuthorPage>(`/api/authors/${encodeURIComponent(id)}`),
  refreshAuthor: (id: string) =>
    request<AuthorPage>(`/api/authors/${encodeURIComponent(id)}/refresh`, { method: "POST" }),
  addManga: (
    mangaId: string,
    provider: string,
    language: string,
    monitorMode: MonitorMode,
    options: { seriesUnit?: SeriesUnitChoice; searchNow?: boolean } = {},
  ) =>
    request<Manga & { queued: number }>("/api/manga", {
      method: "POST",
      body: JSON.stringify({
        manga_id: mangaId,
        provider,
        language,
        monitor_mode: monitorMode,
        series_unit: options.seriesUnit ?? "automatic",
        search_now: options.searchNow ?? true,
      }),
    }),
  removeReleaseSource: (mangaId: string, provider: string, providerMangaId: string) =>
    request<{ manga_id: string; provider: string; provider_manga_id: string; releases_forgotten: number }>(
      `/api/manga/${encodeURIComponent(mangaId)}/release-sources/${encodeURIComponent(provider)}/${encodeURIComponent(providerMangaId)}`,
      { method: "DELETE" },
    ),
  addReleaseSource: (mangaId: string, provider: string, providerMangaId: string) =>
    request<{ releases: number; new: number; queued: number }>(
      `/api/manga/${encodeURIComponent(mangaId)}/release-sources`,
      {
        method: "POST",
        body: JSON.stringify({ provider, provider_manga_id: providerMangaId }),
      },
    ),
  updateManga: (
    id: string,
    changes: Partial<Pick<Manga, "preferred_language" | "monitor_mode">> & {
      assemble_books_automatically?: boolean;
      translation_enabled?: boolean;
      translation_source_languages?: string;
      status_override?: PublicationStatusOverride;
      library_status_override?: "automatic" | "up_to_date";
      expected_count_override?: number | "automatic";
      edition_book_count?: number | "automatic";
      series_unit?: SeriesUnitChoice;
      reader_mode?: "automatic" | "manga" | "webtoon";
      reader_direction?: "automatic" | "ltr" | "rtl";
      authors_override?: string[] | "automatic";
    },
  ) =>
    request<Manga>(`/api/manga/${encodeURIComponent(id)}`, {
      method: "PATCH",
      body: JSON.stringify(changes),
    }),
  renameManga: (id: string, title: string) =>
    request<SeriesRenameResult>(`/api/manga/${encodeURIComponent(id)}/rename`, {
      method: "POST",
      body: JSON.stringify({ title }),
    }),
  searchMissing: (id: string) =>
    request<{
      manga_id: string;
      queued: number;
      seen: number;
      new: number;
      sources: Array<{ state: string; label: string }>;
      errors: Array<{ provider: string; error: string }>;
    }>(`/api/manga/${encodeURIComponent(id)}/search`, { method: "POST" }),
  searchChapterReleases: (
    id: string,
    chapter: string | null,
    volume: string | null,
  ) =>
    request<ChapterReleaseSearchResponse>(
      `/api/manga/${encodeURIComponent(id)}/chapters/search`,
      {
        method: "POST",
        body: JSON.stringify({ chapter, volume }),
      },
    ),
  automaticSearchChapter: (
    id: string,
    chapter: string | null,
    volume: string | null,
  ) =>
    request<ChapterAutomaticSearchResponse>(
      `/api/manga/${encodeURIComponent(id)}/chapters/search/automatic`,
      {
        method: "POST",
        body: JSON.stringify({ chapter, volume }),
      },
    ),
  refreshManga: (id: string) =>
    request<{ seen: number; new: number; queued: number; language: string }>(
      `/api/manga/${encodeURIComponent(id)}/refresh`,
      { method: "POST" },
    ),
  refreshMetadata: (id: string) =>
    request<MetadataRefreshResult>(
      `/api/manga/${encodeURIComponent(id)}/metadata/refresh`,
      { method: "POST" },
    ),
  metadataCorrelations: (id: string) =>
    request<MetadataCorrelations>(
      `/api/manga/${encodeURIComponent(id)}/metadata/correlations`,
    ),
  updateMetadataCorrelations: (
    id: string,
    correlations: Record<string, string | null>,
  ) =>
    request<MetadataCorrelationsUpdate>(
      `/api/manga/${encodeURIComponent(id)}/metadata/correlations`,
      {
        method: "PUT",
        body: JSON.stringify({ correlations }),
      },
    ),
  artworkCandidates: (id: string) =>
    request<ArtworkSelection>(
      `/api/manga/${encodeURIComponent(id)}/artwork-candidates`,
    ),
  selectArtwork: (id: string, candidateId: string | null) =>
    request<ArtworkSelectionUpdate>(
      `/api/manga/${encodeURIComponent(id)}/artwork-selection`,
      {
        method: "PUT",
        body: JSON.stringify({ candidate_id: candidateId }),
      },
    ),
  uploadArtwork: (id: string, file: File) =>
    request<ArtworkSelectionUpdate>(
      `/api/manga/${encodeURIComponent(id)}/artwork-upload`,
      {
        method: "POST",
        headers: { "Content-Type": file.type || "application/octet-stream" },
        body: file,
      },
    ),
  metadataStatus: () => request<MetadataStatus>("/api/metadata/status"),
  refreshAllMetadata: (force = true) =>
    request<{ started: boolean; force?: boolean; reason?: string }>(
      `/api/metadata/refresh?force=${String(force)}`,
      { method: "POST" },
    ),
  download: (chapterId: string, replace = false) =>
    request<Job>(`/api/chapters/${encodeURIComponent(chapterId)}/download`, {
      method: "POST",
      body: JSON.stringify({ force: false, replace }),
    }),
  searchTorrents: (mangaId: string, query?: string) => {
    const parameters = new URLSearchParams();
    if (query?.trim()) parameters.set("q", query.trim());
    const suffix = parameters.size ? `?${parameters}` : "";
    return request<TorrentSearchResponse>(
      `/api/manga/${encodeURIComponent(mangaId)}/torrent-search${suffix}`,
    );
  },
  grabTorrent: (
    mangaId: string,
    provider: "prowlarr" | "internetarchive",
    releaseId: string,
  ) =>
    request<TorrentDownload>(`/api/manga/${encodeURIComponent(mangaId)}/torrents`, {
      method: "POST",
      body: JSON.stringify({ provider, release_id: releaseId }),
    }),
  grabDiscoveredTorrent: (
    mangaId: string,
    provider: "prowlarr" | "internetarchive",
    releaseId: string,
  ) =>
    request<TorrentDownload>(
      `/api/manga/${encodeURIComponent(mangaId)}/torrents/discovered`,
      {
        method: "POST",
        body: JSON.stringify({ provider, release_id: releaseId }),
      },
    ),
  dismissAlert: (key: string, signature: string) =>
    request<{ key: string; dismissed: boolean }>(
      `/api/system/alerts/${encodeURIComponent(key)}/dismiss`,
      { method: "POST", body: JSON.stringify({ signature }) },
    ),
  torrents: (mangaId?: string, statuses?: string[], limit = 200) => {
    const parameters = new URLSearchParams({ limit: String(limit) });
    if (mangaId) parameters.set("manga_id", mangaId);
    (statuses ?? []).forEach((status) => parameters.append("status", status));
    return request<TorrentDownload[]>(`/api/torrents?${parameters}`);
  },
  torrentContents: (downloadId: number) =>
    request<TorrentContents>(`/api/torrents/${downloadId}/contents`),
  importTorrent: (
    downloadId: number,
    options: {
      confirmLanguage?: boolean;
      skipUnnumbered?: boolean;
      selectedPaths?: string[];
      assigned?: { path: string; volume?: string; chapter?: string }[];
    } = {},
  ) =>
    request<TorrentDownload>(`/api/torrents/${downloadId}/import`, {
      method: "POST",
      body: JSON.stringify({
        confirm_language: options.confirmLanguage ?? false,
        skip_unnumbered: options.skipUnnumbered ?? false,
        selected_paths: options.selectedPaths ?? null,
        assigned: options.assigned ?? null,
      }),
    }),
  retryTorrent: (downloadId: number) =>
    request<TorrentDownload>(`/api/torrents/${downloadId}/retry`, { method: "POST" }),
  discardTorrent: (downloadId: number, deleteFiles = true) =>
    request<{ id: number; discarded: boolean; torrent_files_deleted: boolean }>(
      `/api/torrents/${downloadId}?delete_files=${String(deleteFiles)}`,
      { method: "DELETE" },
    ),
  deleteMangaPreview: (mangaId: string, signal?: AbortSignal) =>
    request<MangaDeletionPreview>(`/api/manga/${encodeURIComponent(mangaId)}/delete-preview`, {
      signal,
    }),
  deleteManga: (mangaId: string, deleteFiles: boolean, confirmationSnapshot?: string) => {
    const parameters = new URLSearchParams({ delete_files: String(deleteFiles) });
    if (confirmationSnapshot) parameters.set("confirmation_snapshot", confirmationSnapshot);
    return request<DeleteResult>(`/api/manga/${encodeURIComponent(mangaId)}?${parameters}`, {
      method: "DELETE",
    });
  },
  deleteChapterFile: (mangaId: string, chapterId: string) =>
    request<DeleteResult>(
      `/api/manga/${encodeURIComponent(mangaId)}/chapters/${encodeURIComponent(chapterId)}/file`,
      { method: "DELETE" },
    ),
  deleteDuplicateFiles: (mangaId: string, expectedChapterIds: string[]) => {
    const parameters = new URLSearchParams();
    for (const id of expectedChapterIds) parameters.append("expected_chapter_id", id);
    return request<DeleteResult & { chapters_matched: number; volumes?: string[] }>(
      `/api/manga/${encodeURIComponent(mangaId)}/duplicates/files?${parameters}`,
      { method: "DELETE" },
    );
  },
  deleteVolumeFiles: (
    mangaId: string,
    volume: string,
    language: string,
    expectedDownloadedCount: number,
    expectedChapterIds: string[],
  ) => {
    const parameters = new URLSearchParams({
      language,
      expected_downloaded_count: String(expectedDownloadedCount),
    });
    [...expectedChapterIds].sort().forEach((chapterId) => parameters.append("expected_chapter_id", chapterId));
    return request<DeleteResult>(
      `/api/manga/${encodeURIComponent(mangaId)}/volumes/${encodeURIComponent(volume)}/files?${parameters}`,
      { method: "DELETE" },
    );
  },
  reviews: () => request<MatchReview[]>("/api/reviews"),
  acceptReview: (id: number) => request<{ review: number; resolution: string }>(`/api/reviews/${id}/accept`, { method: "POST" }),
  rejectReview: (id: number) => request<MatchReview>(`/api/reviews/${id}/reject`, { method: "POST" }),
  jobsSummary: () =>
    request<{ counts: Record<string, number>; active: number; series: QueueSeriesSummary[] }>("/api/jobs/summary"),
  jobs: (statuses?: string[], limit = 200, mangaId?: string, signal?: AbortSignal) => {
    const parameters = new URLSearchParams({ limit: String(limit) });
    (statuses ?? []).forEach((status) => parameters.append("status", status));
    if (mangaId) parameters.set("manga_id", mangaId);
    return request<Job[]>(`/api/jobs?${parameters}`, signal ? { signal } : undefined);
  },
  retryJob: (jobId: number, options: { overrideQuality?: boolean } = {}) =>
    request<Job>(
      `/api/jobs/${jobId}/retry${options.overrideQuality ? "?override_quality=true" : ""}`,
      { method: "POST" },
    ),
  deleteJob: (jobId: number) => request<Job>(`/api/jobs/${jobId}`, { method: "DELETE" }),
  cancelJobs: (jobIds: number[]) =>
    request<CancelJobsResponse>("/api/jobs/cancel", {
      method: "POST",
      body: JSON.stringify({ job_ids: jobIds }),
    }),
  wantedSnapshot: () => responseCache.get("/api/wanted?compact=true")?.value as WantedEntry[] | undefined,
  wanted: (fresh = false, signal?: AbortSignal) => {
    const path = "/api/wanted?compact=true";
    return request<WantedEntry[]>(fresh ? `${path}&fresh=true` : `${path}&cached=true`, signal ? { signal } : undefined, path);
  },
  searchWanted: () => request<WantedSearchResult>("/api/wanted/search", { method: "POST" }),
  monitorStatus: (signal?: AbortSignal) => request<MonitorStatus>("/api/monitor/status", signal ? { signal } : undefined),
  systemStatus: () => request<SystemStatus>("/api/system/status"),
  refreshKomga: () =>
    request<LibraryAlignment>("/api/system/komga/refresh", { method: "POST" }),
  providersStatus: () => request<ProviderProbe[]>("/api/providers/status"),
  calendar: (start: string, end: string) => {
    const parameters = new URLSearchParams({ start, end });
    const path = `/api/calendar?${parameters}`;
    return request<CalendarResponse>(path, undefined, path);
  },
  seriesCalendar: (mangaId: string) =>
    request<SeriesCalendar>(`/api/manga/${encodeURIComponent(mangaId)}/calendar`),
  setChapterMonitored: (mangaId: string, chapterId: string, monitored: boolean) =>
    request<{ chapter_id: string; releases_updated: number }>(
      `/api/manga/${encodeURIComponent(mangaId)}/chapters/${encodeURIComponent(chapterId)}/monitored`,
      { method: "PATCH", body: JSON.stringify({ monitored }) },
    ),
  setVolumeMonitoring: (
    mangaId: string,
    volume: string,
    state: "automatic" | "monitored" | "ignored",
  ) =>
    request<{ volume: string; state: "automatic" | "monitored" | "ignored" }>(
      `/api/manga/${encodeURIComponent(mangaId)}/volumes/${encodeURIComponent(volume)}/monitoring`,
      { method: "PATCH", body: JSON.stringify({ state }) },
    ),
  libraryOrphans: () => request<LibraryOrphans>("/api/library/orphans"),
  deleteLibraryOrphans: (folders?: string[]) =>
    request<{ deleted: number; folders: string[]; errors: string[] }>(
      "/api/library/orphans/delete",
      { method: "POST", body: JSON.stringify({ folders: folders ?? null }) },
    ),
  getSettings: () => request<SettingsPayload>("/api/settings"),
  seriesAudit: (mangaId: string, signal?: AbortSignal) => request<SeriesAuditReport>(`/api/manga/${encodeURIComponent(mangaId)}/audit`, { signal }),
  previewAuditRetirement: (mangaId: string, chapterIds: string[], revision: string, signal?: AbortSignal) => request<SeriesAuditPreview>(`/api/manga/${encodeURIComponent(mangaId)}/audit/retire?dry_run=true`, { method: "POST", body: JSON.stringify({ chapter_ids: chapterIds, revision }), signal }),
  retireAuditFiles: (mangaId: string, chapterIds: string[], revision: string, snapshot: string, signal?: AbortSignal) => request<SeriesAuditResult>(`/api/manga/${encodeURIComponent(mangaId)}/audit/retire?${new URLSearchParams({ confirmation_snapshot: snapshot })}`, { method: "POST", body: JSON.stringify({ chapter_ids: chapterIds, revision }), signal }),
  previewAuditSourceRejection: (mangaId: string, source: SeriesAuditSourceKey, revision: string, signal?: AbortSignal) => request<SeriesAuditPreview>(`/api/manga/${encodeURIComponent(mangaId)}/audit/reject-source?dry_run=true`, { method: "POST", body: JSON.stringify({ ...source, revision }), signal }),
  rejectAuditSource: (mangaId: string, source: SeriesAuditSourceKey, revision: string, snapshot: string, signal?: AbortSignal) => request<SeriesAuditResult>(`/api/manga/${encodeURIComponent(mangaId)}/audit/reject-source?${new URLSearchParams({ confirmation_snapshot: snapshot })}`, { method: "POST", body: JSON.stringify({ ...source, revision }), signal }),
  previewBookAssembly: (mangaId: string, volume: string, signal?: AbortSignal) => request<BookAssemblyPreview>(`/api/manga/${encodeURIComponent(mangaId)}/volumes/${encodeURIComponent(volume)}/assemble?dry_run=true`, { method: "POST", signal }),
  assembleBook: (mangaId: string, volume: string, snapshot: string, signal?: AbortSignal) => request<BookAssemblyResult>(`/api/manga/${encodeURIComponent(mangaId)}/volumes/${encodeURIComponent(volume)}/assemble?${new URLSearchParams({ confirmation_snapshot: snapshot })}`, { method: "POST", signal }),
  previewBooksAssembly: (mangaId: string, signal?: AbortSignal) => request<BooksAssemblyPreview>(`/api/manga/${encodeURIComponent(mangaId)}/assemble?dry_run=true`, { method: "POST", signal }),
  assembleBooks: (mangaId: string, snapshot: string, signal?: AbortSignal) => request<BooksAssemblyResult>(`/api/manga/${encodeURIComponent(mangaId)}/assemble?${new URLSearchParams({ confirmation_snapshot: snapshot })}`, { method: "POST", signal }),
  seriesUnits: (mangaId: string, signal?: AbortSignal) => request<SeriesUnits>(`/api/manga/${encodeURIComponent(mangaId)}/units`, { signal }),
  duplicateFiles: (mangaId: string, volume: string, signal?: AbortSignal) => request<{ manga_id: string; chapters: Chapter[]; count: number }>(`/api/manga/${encodeURIComponent(mangaId)}/duplicates?${new URLSearchParams({ volume })}`, { signal }),
  retireBookDuplicates: (mangaId: string, volume: string, expectedChapterIds: string[]) => {
    const parameters = new URLSearchParams({ volume, recycle: "true" });
    for (const id of expectedChapterIds) parameters.append("expected_chapter_id", id);
    return request<DuplicateRetirementResult>(`/api/manga/${encodeURIComponent(mangaId)}/duplicates/files?${parameters}`, { method: "DELETE" });
  },
  chapterMap: (mangaId: string, signal?: AbortSignal) => request<ChapterMapState>(`/api/manga/${encodeURIComponent(mangaId)}/chapter-map`, { signal }),
  previewChapterMap: (mangaId: string, boundaries: ChapterMapBoundary[], signal?: AbortSignal) => request<ChapterMapPreview>(`/api/manga/${encodeURIComponent(mangaId)}/chapter-map?dry_run=true`, { method: "PUT", body: JSON.stringify({ boundaries, mode: "replace" }), signal }),
  saveChapterMap: (mangaId: string, boundaries: ChapterMapBoundary[], snapshot: string, signal?: AbortSignal) => request<ChapterMapPreview>(`/api/manga/${encodeURIComponent(mangaId)}/chapter-map?confirmation_snapshot=${encodeURIComponent(snapshot)}`, { method: "PUT", body: JSON.stringify({ boundaries, mode: "replace" }), signal }),
  previewChapterMapRemoval: (mangaId: string, signal?: AbortSignal) => request<ChapterMapPreview>(`/api/manga/${encodeURIComponent(mangaId)}/chapter-map?dry_run=true`, { method: "DELETE", signal }),
  removeChapterMap: (mangaId: string, snapshot: string, signal?: AbortSignal) => request<ChapterMapPreview>(`/api/manga/${encodeURIComponent(mangaId)}/chapter-map?confirmation_snapshot=${encodeURIComponent(snapshot)}`, { method: "DELETE", signal }),
  putSettings: (changes: Record<string, string | number | null>) =>
    request<{ applied: string[]; settings: SettingsPayload }>("/api/settings", {
      method: "PUT",
      body: JSON.stringify(changes),
    }),
  testReader: (changes: Record<string, string>) =>
    request<{ ok: boolean; detail?: string; error?: string }>("/api/settings/test/reader", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(changes),
    }),
  discoverReader: () =>
    request<ReaderDiscovery>("/api/settings/discover/reader", { method: "POST" }),
  suwayomiStatus: () => request<SuwayomiRuntimeStatus>("/api/system/suwayomi"),
  suwayomiInstall: () =>
    request<SuwayomiRuntimeStatus>("/api/system/suwayomi/install", { method: "POST" }),
  suwayomiRestart: () =>
    request<SuwayomiRuntimeStatus>("/api/system/suwayomi/restart", { method: "POST" }),
  suwayomiStop: () =>
    request<SuwayomiRuntimeStatus>("/api/system/suwayomi/stop", { method: "POST" }),
  suwayomiLanguages: () =>
    request<{ languages: { code: string; extensions: number }[] }>("/api/system/suwayomi/languages"),
  suwayomiUpdateNow: () =>
    request<SuwayomiRuntimeStatus>("/api/system/suwayomi/update", { method: "POST" }),
  suwayomiExtensions: (language: string, refresh = false) =>
    request<{ extensions: SuwayomiExtension[]; count: number }>(
      `/api/system/suwayomi/extensions?language=${encodeURIComponent(language)}&refresh=${String(refresh)}`,
    ),
  suwayomiSetExtension: (pkgName: string, installed: boolean) =>
    request<{ pkg_name: string; installed: boolean; has_update: boolean }>(
      "/api/system/suwayomi/extensions",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ pkg_name: pkgName, installed }),
      },
    ),
  // Probes stream back one NDJSON line per source the moment each finishes,
  // so the operator watches the ranking build instead of waiting for all.
  suwayomiTestSourcesStream: async (
    language: string,
    onEvent: (event: SuwayomiTestEvent) => void,
  ): Promise<void> => {
    const response = await fetch("/api/system/suwayomi/test", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ language }),
    });
    if (!response.ok || !response.body) {
      if (response.status === 401) {
        window.dispatchEvent(new CustomEvent("tankarr:authentication-required"));
      }
      let detail = `${response.status} ${response.statusText}`;
      try {
        const body = await response.json();
        if (typeof body.detail === "string") detail = body.detail;
      } catch {
        // Keep the HTTP error when the body is not JSON.
      }
      throw new Error(detail);
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let index = buffer.indexOf("\n");
      while (index >= 0) {
        const line = buffer.slice(0, index).trim();
        buffer = buffer.slice(index + 1);
        if (line) onEvent(JSON.parse(line) as SuwayomiTestEvent);
        index = buffer.indexOf("\n");
      }
    }
  },
  testQbittorrent: (changes: Record<string, string>) =>
    request<{ ok: boolean; error?: string; version?: string; api_version?: string }>(
      "/api/settings/test/qbittorrent",
      { method: "POST", body: JSON.stringify(changes) },
    ),
  testSabnzbd: (changes: Record<string, string>) =>
    request<{ ok: boolean; error?: string; version?: string }>("/api/settings/test/sabnzbd", {
      method: "POST",
      body: JSON.stringify(changes),
    }),
  testNtfy: (changes: Record<string, string>) =>
    request<{ ok: boolean; error?: string; status_code?: number; topic?: string }>(
      "/api/settings/test/ntfy",
      { method: "POST", body: JSON.stringify(changes) },
    ),
  testDownloadProvider: (provider: string, changes: Record<string, string>) =>
    request<{
      ok: boolean;
      provider: string;
      label?: string;
      error?: string;
      language?: string;
      sources?: number;
      source_details?: SuwayomiSource[];
      supported_sites?: number;
    }>(`/api/settings/test/provider/${encodeURIComponent(provider)}`, {
      method: "POST",
      body: JSON.stringify(changes),
    }),
  testInternetArchive: (changes: Record<string, string>) =>
    request<InternetArchiveProbe>("/api/settings/test/internet-archive", {
      method: "POST",
      body: JSON.stringify(changes),
    }),
  testProwlarr: (changes: Record<string, string>) =>
    request<ProwlarrProbe>("/api/settings/test/prowlarr", {
      method: "POST",
      body: JSON.stringify(changes),
    }),
  testKomga: (changes: Record<string, string>) =>
    request<KomgaConnectionTest>("/api/settings/test/komga", {
      method: "POST",
      body: JSON.stringify(changes),
    }),
  testMetadataSource: (source: string, changes: Record<string, string>) =>
    request<{ ok: boolean; error?: string; source?: string; results?: number }>(
      `/api/settings/test/metadata/${encodeURIComponent(source)}`,
      { method: "POST", body: JSON.stringify(changes) },
    ),
  importScan: () => request<ImportScan>("/api/import/scan"),
  importCreateUpload: () =>
    request<{ upload_id: string }>("/api/import/uploads", { method: "POST" }),
  importUploadFile: (uploadId: string, path: string, file: File) =>
    request<{ path: string; size: number }>(
      `/api/import/uploads/${encodeURIComponent(uploadId)}/files?path=${encodeURIComponent(path)}`,
      {
        method: "PUT",
        headers: { "Content-Type": file.type || "application/octet-stream" },
        body: file,
      },
    ),
  importScanUpload: (uploadId: string) =>
    request<ImportScan>(`/api/import/uploads/${encodeURIComponent(uploadId)}/scan`),
  importDeleteUpload: (uploadId: string) =>
    request<void>(`/api/import/uploads/${encodeURIComponent(uploadId)}`, {
      method: "DELETE",
    }),
  importStart: (groups: ImportGroup[], language: string) =>
    request<ImportState>("/api/import", {
      method: "POST",
      body: JSON.stringify({
        groups: groups.map((group) => ({
          key: group.key,
          title: group.title,
          authors: group.authors,
          upload_id: group.upload_id,
          target_manga_id: group.target_manga_id,
          language: group.language,
          unit: group.unit,
          set_series_unit: group.set_series_unit ?? false,
          items: group.items.map((item) => ({
            path: item.path,
            volume: item.volume,
            chapter: item.chapter,
            chapter_title: item.chapter_title,
          })),
        })),
        language,
      }),
    }),
  importStatus: () => request<ImportState>("/api/import/status"),
  backupNow: () =>
    request<{ name: string; size: number; backups: BackupInfo[] }>("/api/system/backup", { method: "POST" }),
  runMonitor: () => request<{ checked: number; queued: number }>("/api/monitor/run", { method: "POST" }),
  organizeLibrary: () => request<LibraryOrganization>("/api/library/organize", { method: "POST" }),
  preflight: (connections = false) => request<PreflightReport>("/api/system/preflight", connections ? { method: "POST" } : undefined),
  previewLibraryList: (items: LibraryListItem[]) => request<LibraryListPreview>("/api/library/list/preview", { method: "POST", body: JSON.stringify({ items }) }),
  importLibraryList: (token: string) => request<{ imported: number; skipped: number; errors: string[] }>("/api/library/list/import", { method: "POST", body: JSON.stringify({ token }) }),
  maintenanceStatus: () => request<MaintenanceStatus>("/api/system/maintenance"),
  applyMaintenanceRepair: (revision: string) => request<Record<string, unknown>>("/api/system/maintenance/repair/apply", { method: "POST", body: JSON.stringify({ revision }) }),
  previewRepair: () => request<RepairPreview>("/api/library/repair/preview", { method: "POST" }),
  applyRepair: (token: string) => request<Record<string, unknown>>("/api/library/repair/apply", { method: "POST", body: JSON.stringify({ token }) }),
  previewAcquisition: (mangaId: string, preferences?: Record<string, string | number | boolean>, signal?: AbortSignal) => request<AcquisitionPreview>("/api/acquisition/preview", { method: "POST", body: JSON.stringify({ manga_id: mangaId, preferences }), signal }),
  readerAlignment: (mangaId: string) => request<ReaderAlignment>(`/api/system/reader/alignment?manga_id=${encodeURIComponent(mangaId)}`),
  verifyBackup: (name: string) => request<BackupVerification>(`/api/system/backups/${encodeURIComponent(name)}/verify`, { method: "POST" }),
};
