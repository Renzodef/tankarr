export type MonitorMode = "all" | "future" | "existing" | "none";
export type VolumeMonitorState = "automatic" | "monitored" | "ignored";
export type PublicationStatusOverride = "automatic" | "continuing" | "hiatus" | "ended";
export type LibraryStatusOverride = "automatic" | "up_to_date";

export type PublicationSummary = {
  status: "continuing" | "ended" | "unknown";
  source: "manual" | "metadata" | "provider" | null;
  overridden: boolean;
  raw_status: string | null;
  /** The work is continuing but the catalogue says publication is paused. */
  paused?: boolean;
  /** Who reported the pause: mangabaka, myanimelist, mangaupdates, official, manual. */
  pause_sources?: string[];
};

export type LibraryCountSummary = {
  unit: "chapter" | "volume" | "issue" | "book";
  available_count: number;
  downloaded_count: number;
  expected_count: number | null;
  total_count: number;
  missing_count: number;
  raw_missing_count?: number;
  ignored_missing_count?: number;
  indexed_missing_count: number;
  unindexed_missing_count: number;
  volume_progress?: { owned: number; expected: number } | null;
  chapter_progress?: { owned: number; expected: number } | null;
  reference_kind?: "official" | "catalogue" | "manual" | "verified" | "available" | "none";
  latest_chapter?: string | null;
  reference_unknown?: boolean;
  short_of_books?: boolean;
  additional_content?: { prologues?: number; extras?: number };
  special_count?: number;
  special_downloaded_count?: number;
  source: string | null;
  metadata_field: string;
  count_conflict: boolean;
  catalogue_expected_count?: number | null;
  catalogue_source?: string | null;
  segmentation_differs?: boolean;
  count_note?: string | null;
  edition_split?: {
    books: number;
    chapters_per_book: number;
    owned_books: number;
    covered_chapters: number;
    duplicate_chapters: number;
  } | null;
  expected_count_overridden?: boolean;
  automatic_expected_count?: number | null;
  automatic_expected_source?: string | null;
  library_status_overridden?: boolean;
};

export type AuthStatus = {
  configured: boolean;
  method: "forms" | "basic";
  authenticated: boolean;
  username: string | null;
};

export type CountConfidence = "agreed" | "lone" | "conflict" | "unconfirmed";

export type ManagedEdition = {
  publisher: string;
  volume_count: number | null;
  complete: boolean;
  note: string;
  source: string;
};

export type CanonicalMetadata = {
  publishers?: { name: string; type?: string; note?: string }[];
  /** How many independent catalogues back each count (MangaBaka is the spine). */
  count_confidence?: { chapter?: CountConfidence; volume?: CountConfidence; status?: CountConfidence };
  count_votes?: {
    chapter?: Record<string, number | null>;
    volume?: Record<string, number | null>;
  };
  /** The English edition MangaBaka's publisher note describes, when it does. */
  managed_edition?: ManagedEdition | null;
  official_links?: { name?: string; url: string; language?: string; type?: string }[];
  title: string;
  title_selection_version?: number;
  title_selection?: {
    source: string;
    language: string | null;
    localized: boolean;
    strategy?:
      | "localized"
      | "verified_provider_alias"
      | "catalogue_primary"
      | "provider_fallback";
    priority: number;
  };
  alternate_titles: string[];
  description: string;
  authors: string[];
  creators: { name: string; role: string }[];
  creator_links?: {
    name: string;
    role: string;
    source: string;
    label: string;
    external_id: string;
    url: string;
  }[];
  genres: string[];
  tags: string[];
  publisher: string | null;
  year: number | null;
  status: string | null;
  work_type: string | null;
  content_kind?: "manga" | "webtoon" | "comic" | "book" | "unknown";
  classification?: {
    kind: "manga" | "webtoon" | "comic" | "book" | "unknown";
    subtype: string | null;
    source: string | null;
    confidence: number;
    reason: string;
  };
  original_language: string | null;
  translation_language: string;
  reading_direction: string | null;
  volume_count: number | null;
  chapter_count: number | null;
  issue_count?: number | null;
  book_count?: number | null;
  rating: number | null;
  external_ids: Record<string, string>;
  links: { label: string; url: string }[];
  provider_correlations: {
    provider: string;
    label: string;
    external_id: string;
    url: string | null;
  }[];
  work?: {
    title: string;
    display_title: string;
    alternate_titles: string[];
    authors: string[];
    creators: { name: string; role: string }[];
    creator_links?: {
      name: string;
      role: string;
      source: string;
      label: string;
      external_id: string;
      url: string;
    }[];
    year: number | null;
    status: string | null;
    work_type: string | null;
    content_kind?: "manga" | "webtoon" | "comic" | "book" | "unknown";
    original_language: string | null;
    volume_count: number | null;
    chapter_count: number | null;
    external_ids: Record<string, string>;
    links: { label: string; url: string }[];
  };
  editions?: {
    source: string;
    external_id: string;
    title: string;
    alternate_titles: string[];
    language: string | null;
    publisher: string | null;
    publication_year: number | null;
    volume_count: number | null;
    issue_count: number | null;
    description: string;
    links: { label: string; url: string }[];
  }[];
  provenance: Record<string, string>;
  matched_sources: string[];
  external_sources?: {
    source: string;
    label: string;
    url: string;
    rating: number | null;
  }[];
  cover_url: string | null;
  artwork_source: string | null;
  artwork_source_url: string | null;
  artwork_source_width?: number | null;
  artwork_source_height?: number | null;
  artwork_normalized_width?: number | null;
  artwork_normalized_height?: number | null;
  artwork_score?: number | null;
  artwork_candidates_evaluated?: number | null;
  artwork_candidate_id?: string | null;
  artwork_automatic_candidate_id?: string | null;
  artwork_selection_mode?: "automatic" | "manual" | null;
};

export type ArtworkCandidate = {
  candidate_id: string;
  source: string;
  source_url: string;
  image_url: string;
  artwork_sha256: string;
  source_width: number;
  source_height: number;
  normalized_width: number;
  normalized_height: number;
  score: number;
  source_priority: number;
  automatic: boolean;
  selected: boolean;
};

export type ArtworkSelection = {
  manga_id: string;
  selection_mode: "automatic" | "manual";
  selected_candidate_id: string | null;
  automatic_candidate_id: string | null;
  candidates: ArtworkCandidate[];
};

export type ArtworkSelectionUpdate = {
  selection: ArtworkSelection;
  metadata: { data: CanonicalMetadata };
  library: { updated?: number; errors?: { error?: string }[] };
  komga: {
    synced: boolean;
    artwork_uploaded?: number;
    errors?: { target_id?: string; error?: string }[];
  } | null;
};

export type VolumeMetadata = {
  volume_key: string;
  artwork_url: string | null;
  artwork_path: string | null;
  artwork_sha256: string | null;
  artwork_media_type: string | null;
  updated_at: string;
  data: {
    number: string;
    title: string;
    cover_url: string | null;
    artwork_source: string | null;
    artwork_source_width?: number | null;
    artwork_source_height?: number | null;
    artwork_normalized_width?: number | null;
    artwork_normalized_height?: number | null;
  };
};

export type MetadataSourceStatus = {
  name: string;
  label: string;
  configured: boolean;
  supports_series: boolean;
  supports_volumes: boolean;
  supports_author_lookup?: boolean;
  automatic_matching?: boolean;
  allows_link_only?: boolean;
  unavailable_reason: string | null;
  state?: "matched" | "cached" | "cached_error" | "no_match" | "ambiguous" | "error" | "unavailable" | "volume_only" | "not_applicable" | "exact_link" | "exact_error" | "manual_only";
  matched?: boolean;
  external_id?: string;
  confidence?: number;
  reason?: string;
  candidate?: {
    external_id: string;
    title: string;
    title_relation: string;
  } | null;
  runner_up_confidence?: number | null;
  margin?: number | null;
  error?: string;
  correlation_origin?: "manual" | "mangadex";
};

export type MetadataCorrelationSource = {
  source: string;
  label: string;
  editable: boolean;
  configured: boolean;
  allows_link_only: boolean;
  supports_enrichment: boolean;
  manual_url: string | null;
  provider_url: string | null;
  automatic_url: string | null;
  effective_url: string | null;
  external_id: string | null;
  origin: "manual" | "mangadex" | "automatic" | null;
  matched_title: string | null;
  unavailable_reason: string | null;
};

export type MetadataCorrelations = {
  manga_id: string;
  sources: MetadataCorrelationSource[];
};

export type MetadataCorrelationsUpdate = {
  correlations: MetadataCorrelations;
  refresh: MetadataRefreshResult;
};

export type MetadataStatus = {
  enabled: boolean;
  running: boolean;
  monitor_active?: boolean;
  current_manga_id: string | null;
  last_cycle_at: string | null;
  last_cycle_error: string | null;
  refresh_interval_hours: number;
  coverage: {
    series_total: number;
    series_enriched: number;
    series_with_external_metadata: number;
    series_without_external_metadata: number;
    series_with_artwork: number;
    series_synced: number;
    series_errors: number;
    volumes_enriched: number;
    volumes_with_artwork: number;
    source_records: number;
    sync_errors: number;
  };
  sources: MetadataSourceStatus[];
};

export type MetadataRefreshResult = {
  manga_id: string;
  metadata: {
    data: CanonicalMetadata;
    source_status: MetadataSourceStatus[];
    last_enriched_at: string;
  };
  library: {
    reader_independent: boolean;
    checked: number;
    updated: number;
    covers_written: number;
    series_covers_written?: number;
    book_covers_written?: number;
    covers_unchanged?: number;
    errors?: { error?: string }[];
  };
  komga?: {
    configured?: boolean;
    synced: boolean;
    series?: number;
    books?: number;
    updated?: number;
    artwork_uploaded?: number;
    missing_books?: number;
    errors?: { target_id?: string; error?: string }[];
    reason?: string;
    forced?: boolean;
  };
};

export type Chapter = {
  selection?: { selected: boolean; reasons: string[]; source_class: string; source_priority: number; next_retry_at: number | null };
  /** "volume" for a whole book listed among the releases, else a chapter. */
  release_unit?: string | null;
  id: string;
  manga_id: string;
  volume: string | null;
  chapter: string | null;
  source_chapter?: string | null;
  edition_chapter?: string | null;
  canonical_chapter?: string | null;
  primary_chapter?: string | null;
  numbering_status?: "mapped" | "provisional" | "unmapped" | "ambiguous" | "pending_evidence";
  numbering_method?: string;
  numbering_confidence?: number;
  numbering_evidence?: Record<string, unknown>;
  numbering_version?: number;
  title: string;
  language: string;
  provider: string;
  groups: string[];
  source_key?: string | null;
  source_name?: string | null;
  publish_at: string | null;
  source_url: string;
  pages: number | null;
  version: number | null;
  monitored: boolean;
  downloaded: boolean;
  library_path: string | null;
  queue_job_id?: number | null;
  queue_status?: string | null;
  blocked?: boolean;
  block_reason?: string | null;
  /** What the page-quality audit decided about the imported file. */
  page_quality?: {
    verdict: "ok" | "degraded" | "unknown" | string;
    reason: string;
    /** Set on a fragment of a split chapter: the chapter it belongs to. */
    whole_chapter?: string;
  } | null;
};

export type ChapterSlot = {
  key: string;
  volume: string | null;
  chapter: string | null;
  expected: boolean;
  special: boolean;
  evidence: string;
  volume_inferred: boolean;
  split_parts: string[];
  releases: Chapter[];
  release_count?: number;
  all_releases_blocked?: boolean;
  available: boolean;
  downloaded: boolean;
  monitored: boolean;
  volume_monitor_state: VolumeMonitorState;
  ignored: boolean;
  series_status_ignored?: boolean;
  covered_by_volume?: string | null;
  coverage_exact?: boolean;
  covered_unmapped?: boolean;
  duplicate_of_volume?: string | null;
  queue_status: string | null;
  searchable: boolean;
};

export type ChapterIndex = {
  slots: ChapterSlot[];
  unit: "chapter" | "volume";
  numbering_mode: "global" | "volume_scoped";
  sequence_end: number | null;
  sequence_basis: string | null;
  expected_count: number | null;
  expected_source: string | null;
  unresolved_expected_count: number;
  mapped_missing_count: number;
  raw_missing_count: number;
  ignored_missing_count: number;
  covered_count?: number;
  covered_unmapped_count?: number;
  duplicate_count?: number;
  owned_volumes?: string[];
  special_count?: number;
  special_downloaded_count?: number;
  special_monitored?: boolean;
  expected_volume_count?: number | null;
  owned_volume_count?: number;
  expected_available_count?: number;
  expected_satisfied_count?: number;
  series_unit?: SeriesUnit;
  series_unit_reason?: string;
  series_unit_override?: boolean;
  unit_coverage?: UnitCoverage;
  dropped_releases?: Record<string, number>;
};

export type ChapterMapBoundary = { volume: string; first_chapter: string };

export type ChapterMapState = {
  boundaries: ChapterMapBoundary[];
  suggestions: ChapterMapBoundary[];
  last_known_chapter: string | null;
  warnings: string[];
  revision: string;
};

export type ChapterMapPreview = {
  boundaries: ChapterMapBoundary[];
  intervals: { volume: string; first_chapter: string; last_chapter: string; chapters: string[] }[];
  warnings: string[];
  revision: string;
  confirmation_snapshot: string;
  chapter_index: ChapterIndex;
};

export type SeriesUnit = "chapters" | "volumes";
export type SeriesUnitChoice = SeriesUnit | "automatic";
export type UnitCoverage = {
  chapters: { available: number; expected: number | null };
  volumes: { available: number; expected: number | null };
};

export type VolumeMonitorOverride = {
  volume_key: string;
  state: Exclude<VolumeMonitorState, "automatic">;
  created_at: string;
  updated_at: string;
};

export type ReleaseSourceMapping = {
  manga_id: string;
  provider: string;
  provider_manga_id: string;
  title: string;
  source_url: string | null;
  source_name: string | null;
  language: string;
  match_confidence: number;
  match_reason: string;
  verified_by: "automatic" | "manual" | string;
  enabled: boolean;
  last_checked_at: string | null;
  last_error: string | null;
};

export type DirectSourceSearchStatus = {
  provider: string;
  label: string;
  state: "matched" | "no_match" | "ambiguous" | "error" | "disabled";
  provider_manga_id?: string;
  title?: string;
  releases?: number;
  new?: number;
  confidence?: number;
  reason?: string;
  error?: string;
};

export type MangaSummary = {
  id: string;
  provider: string;
  title: string;
  source_title: string;
  metadata_title: string | null;
  title_source: "manual" | "metadata" | "provider";
  title_override: string | null;
  title_overridden: boolean;
  alternate_titles?: string[];
  native_title?: string | null;
  work_type?: string | null;
  volume_count?: number | null;
  chapter_count?: number | null;
  latest_release_chapter?: number | null;
  rating?: number | null;
  genres?: string[];
  external_id?: string;
  in_library?: boolean;
  library_manga_id?: string | null;
  /** Library state of the matching series, present when in_library. */
  library?: {
    publication?: PublicationSummary | null;
    library_count?: LibraryCountSummary | null;
    library_status_override?: LibraryStatusOverride | null;
    status_override?: PublicationStatusOverride | null;
    preferred_language?: string | null;
    monitor_mode?: MonitorMode | null;
    effective_series_unit?: string | null;
    chapter_count?: number | null;
    downloaded_count?: number | null;
  } | null;
  description: string;
  cover_url: string | null;
  artwork_url: string | null;
  artwork_sha256: string | null;
  authors: string[];
  author_entities?: AuthorCredit[];
  authors_override?: string[] | null;
  authors_overridden?: boolean;
  source_authors?: string[];
  original_language: string | null;
  preferred_language: string;
  status: string | null;
  status_override: Exclude<PublicationStatusOverride, "automatic"> | null;
  status_overridden: boolean;
  library_status_override: Exclude<LibraryStatusOverride, "automatic"> | null;
  library_status_overridden: boolean;
  expected_count_override: number | null;
  expected_count_unit_override: LibraryCountSummary["unit"] | null;
  edition_book_count: number | null;
  publication?: PublicationSummary;
  year: number | null;
  publication_year?: number | null;
  last_chapter: string | null;
  last_volume: string | null;
  available_languages: string[];
  external_correlations?: {
    source: string;
    label: string;
    external_id: string;
    url: string;
  }[];
  source_url?: string;
  source_name?: string | null;
  source_id?: string | null;
  metadata?: CanonicalMetadata;
  metadata_last_enriched_at?: string | null;
};

export type AuthorCredit = {
  id: string;
  name: string;
  credited_names: string[];
  roles: string[];
};

export type Manga = MangaSummary & {
  translation_enabled?: boolean;
  translation_source_languages?: string;
  assemble_books_automatically?: boolean;
  created_at: string;
  monitor_mode: MonitorMode;
  series_unit_override?: SeriesUnit | null;
  reader_mode_override?: "manga" | "webtoon" | null;
  reader_direction_override?: "ltr" | "rtl" | null;
  effective_series_unit?: SeriesUnit;
  book_total_count?: number | null;
  chapter_total_count?: number | null;
  future_monitoring_allowed: boolean;
  future_monitoring_reason: string;
  monitor_initialized: boolean;
  monitored: boolean;
  last_checked_at: string | null;
  last_check_error: string | null;
  chapter_count: number;
  downloaded_count: number;
  numbered_chapter_count: number;
  numbered_volume_count: number;
  library_count?: LibraryCountSummary;
  chapters?: Chapter[];
  chapter_index?: ChapterIndex;
  official_platforms?: { name: string; url: string; host: string; pkg_name: string; free: boolean; installed: boolean; mapped: boolean }[];
  release_sources?: ReleaseSourceMapping[];
  volume_metadata?: VolumeMetadata[];
  volume_monitor_overrides?: VolumeMonitorOverride[];
  metadata_last_synced_at?: string | null;
  metadata_last_error?: string | null;
  metadata_source_status?: MetadataSourceStatus[];
};

export type SeriesRenameResult = {
  manga: Manga;
  previous_title: string;
  title: string;
  sidecars_moved: number;
  organization: LibraryOrganization;
  metadata: {
    reader_independent: boolean;
    checked: number;
    updated: number;
    errors?: { error?: string }[];
  } | null;
  warnings: string[];
};

export type ChapterCompletenessSource = {
  name: string;
  label: string;
  state: "matched" | "no_match" | "ambiguous" | "error" | "unavailable";
  matched: boolean;
  chapter_count: number | null;
  publication_status: string | null;
  confidence: number | null;
  reason: string;
};

export type ChapterCompleteness = {
  status: "complete" | "incomplete" | "unknown" | "conflict";
  expected_chapter_count: number | null;
  available_chapter_count: number;
  covered_chapter_count: number | null;
  missing_chapter_count: number | null;
  missing_chapter_ranges: string[];
  provider_claimed_chapter_count: number | null;
  provider_label: string;
  metadata_sources: ChapterCompletenessSource[];
  checked_source_count: number;
  matched_source_count: number;
  verified_source_count: number;
  verification_basis: "provider_final" | "metadata_total" | null;
  numbering_disagreement: boolean;
  message: string;
};

export type MangaPreview = MangaSummary & {
  chapter_count: number;
  latest_available_chapter: string | null;
  future_monitoring_allowed: boolean;
  future_monitoring_reason: string;
  chapter_completeness: ChapterCompleteness;
  suggested_unit?: SeriesUnit;
};

export type WorkSource = {
  provider: string;
  id: string;
  title: string;
  source_name: string | null;
  match_confidence: number;
};

export type WorkGroup = MangaSummary & { sources: WorkSource[] };

export type SearchResponse = {
  results: MangaSummary[];
  works?: WorkGroup[];
  errors: { provider: string; error: string }[];
  releases: TorrentRelease[];
  release_matches: Record<string, ProwlarrSeriesReleaseMatch[]>;
};

export type ProwlarrSeriesReleaseMatch = {
  provider: "prowlarr" | "internetarchive";
  release_id: string;
  score: number;
};

export type CancelJobsResponse = {
  removed: Job[];
  failed: { id: number; error: string }[];
};

export type KomgaScanResult = {
  requested?: boolean;
  triggered: boolean;
  configured?: boolean;
  library_id?: string | null;
  purged?: boolean;
  pending_operations?: number;
  completed_operations?: number;
  error?: string;
  reason?: string;
};

export type MangaDeletionLanguagePreview = {
  language: string;
  chapters: number;
  chapter_releases: number;
  downloaded_chapters: number;
  files: number;
  existing_files: number;
  missing_files: number;
};

export type MangaDeletionPreview = {
  manga_id: string;
  title: string;
  snapshot: string;
  chapters: number;
  chapter_releases: number;
  downloaded_chapters: number;
  files: number;
  existing_files: number;
  missing_files: number;
  languages: MangaDeletionLanguagePreview[];
};

export type DeletionRecovery = {
  library_available: boolean;
  rolled_back: number;
  purged: number;
  files_deleted: number;
  warnings: string[];
  scan_required: boolean;
  recovery_blocked?: boolean;
  komga_scan?: KomgaScanResult;
};

export type LibraryOrganization = {
  naming_version: number;
  naming_format: string;
  chapters_scanned: number;
  downloaded_files: number;
  dry_run: boolean;
  planned_moves: number;
  planned_adoptions: number;
  hashes_to_record: number;
  moved: number;
  adopted: number;
  unchanged: number;
  normalized_records: number;
  hashes_recorded: number;
  directories_removed: number;
  scan_required: boolean;
  deferred: boolean;
  warnings: string[];
  organization_blocked: boolean;
  komga_scan?: KomgaScanResult;
};

export type DeleteResult = {
  cleanup_pending?: boolean;
  deletion_id?: string | null;
  manga_id: string;
  files_deleted: number;
  files_missing: number;
  directories_removed: number;
  cleanup_errors: string[];
  quarantine_files_remaining: number;
  quarantine_path: string | null;
  jobs_deleted: number;
  komga_scan: KomgaScanResult;
  deleted?: boolean;
  delete_files?: boolean;
  chapters_deleted?: number;
  chapter_id?: string;
  chapters_reset?: number;
  volume?: string;
  language?: string;
  chapters_matched?: number;
};

export type Job = {
  retry_count?: number;
  next_retry_at?: number;
  failure_code?: string;
  failure_scope?: string;
  id: number;
  manga_id: string;
  manga_title: string | null;
  manga_cover_url: string | null;
  manga_source_name: string | null;
  manga_source_id: string | null;
  chapter_id: string;
  chapter_volume: string | null;
  chapter_number: string | null;
  chapter_title: string | null;
  chapter_groups: string[];
  chapter_provider: string | null;
  chapter_source_name: string | null;
  chapter_source_url: string | null;
  requested_language: string;
  status: string;
  progress: number;
  message: string;
  result_path: string | null;
  language_evidence: Record<string, unknown>;
  created_at: string;
  updated_at: string;
};

export type TorrentRelease = {
  id: string;
  provider: "prowlarr" | "internetarchive";
  provider_label: string;
  indexer: string;
  indexer_id: number | null;
  title: string;
  language: string;
  category_id: string;
  category_ids: Array<string | number>;
  category: string;
  size: string;
  size_bytes: number;
  seeders: number;
  leechers: number;
  downloads: number;
  comments: number;
  trusted: boolean;
  remake: boolean;
  info_hash: string;
  publish_at: string | null;
  source_url: string;
  volume: string | null;
  chapter: string | null;
  match_score: number;
  download: Pick<TorrentDownload, "id" | "status" | "progress" | "message"> | null;
};

export type TorrentSearchResponse = {
  provider: "aggregate";
  providers: Record<string, number>;
  errors: Array<{ provider: string; error: string }>;
  query: string;
  results: TorrentRelease[];
};

export type ChapterReleaseSearchResponse = {
  manga_id: string;
  chapter: string | null;
  volume: string | null;
  direct_releases: Chapter[];
  direct_sources: DirectSourceSearchStatus[];
  torrent: TorrentSearchResponse;
};

export type ChapterAutomaticSearchResponse = {
  manga_id: string;
  chapter: string | null;
  volume: string | null;
  state:
    | "queued"
    | "already_queued"
    | "downloaded"
    | "blocked"
    | "not_monitored"
    | "no_match";
  queued: number;
  grabbed: number;
  message: string;
  errors: string[];
};

export type TorrentDownload = {
  id: number;
  manga_id: string;
  manga_title: string | null;
  manga_cover_url: string | null;
  manga_source_name: string | null;
  source: "nyaa" | "prowlarr";
  protocol?: "torrent" | "usenet" | "http";
  client_id?: string | null;
  source_id: string;
  info_hash: string;
  title: string;
  language: "en";
  category: string;
  indexer: string | null;
  size_bytes: number;
  seeders: number;
  leechers: number;
  trusted: boolean;
  remake: boolean;
  volume_hint: string | null;
  chapter_hint: string | null;
  publish_at: string | null;
  source_url: string;
  status: string;
  progress: number;
  qbit_state: string | null;
  content_path: string | null;
  imported_paths: string[];
  language_evidence: Record<string, unknown>;
  message: string;
  client_url: string | null;
  review_reason: "language" | "content" | null;
  created_at: string;
  updated_at: string;
};

export type TorrentStatus = {
  enabled: boolean;
  configured: boolean;
  sources: Record<string, boolean>;
  running: boolean;
  import_mount: boolean;
  category: string;
  active: number;
  review: number;
  orphan_sweep?: {
    at: string;
    seen: number;
    removed: number;
    names: string[];
    foreign: number;
    foreign_names: string[];
  } | null;
  last_poll_at: string | null;
  last_error: string | null;
};

export type ProviderInfo = {
  name: string;
  label: string;
  search_mode: "title" | "url";
};

export type LibraryAlignment = {
  ready: boolean;
  configured?: boolean;
  reader?: string;
  reader_label?: string;
  reader_independent?: boolean;
  triggered?: boolean;
  expected_books?: number;
  matched_expected_books?: number;
  active_books?: number;
  missing_books?: number;
  stale_books?: number;
  scan_job?: {
    id?: string;
    status?: string;
    created_at?: string | null;
    completed_at?: string | null;
    ms_elapsed?: number;
  } | null;
  refreshed_at?: string;
  refresh_reason?: string;
  metadata_sync?: {
    eligible?: number;
    synced?: number;
    updated?: number;
    artwork_uploaded?: number;
    complete?: boolean;
    errors?: { manga_id?: string; error?: string }[];
  };
  metadata_sync_error?: string;
  error?: string;
};

export type KomgaRefreshStatus = {
  enabled: boolean;
  interval_minutes: number;
  due: boolean;
  next_due_in_seconds: number | null;
  last_result: (LibraryAlignment & {
    refresh_reason?: string;
    refreshed_at?: string;
    attempted_at?: string;
  }) | null;
};

export type Health = {
  status: string;
  version: string;
  provider: string;
  providers: ProviderInfo[];
  default_language: string;
  search_languages: string[];
  library_dir: string;
  library_alignment: LibraryAlignment | null;
  komga_refresh: KomgaRefreshStatus;
  ntfy_configured: boolean;
  notifications?: Record<string, boolean>;
  library:
    | { available: true; root: string }
    | { available: false; reason: string };
  deletion_recovery: DeletionRecovery | null;
  library_organization: LibraryOrganization | null;
  download_worker: {
    running: boolean;
    current_job_id: number | null;
    current_job_ids: number[];
    queued_in_memory: number;
    pipeline_depth: number;
    pipeline_maximum: number;
    active_series: number;
    active_sources: number;
    adaptive: {
      effective: number;
      maximum: number;
      configured_maximum: number;
      reason: string;
      cpu_limit_cores: number;
      cpu_usage_percent: number | null;
      cpu_pressure_avg10: number;
      io_pressure_avg10: number;
      memory_working_set_mib: number | null;
      memory_limit_mib: number | null;
      memory_headroom_mib: number | null;
      network_mib_per_second: number | null;
      temperature_c: number | null;
      source_latency_ms: number | null;
      source_rate_wait_ms: number | null;
      source_failure_pressure: number;
    };
    error: string | null;
  };
  download_tuning: {
    effective: number | null;
    maximum: number;
    target_requests_per_second: number;
    latency_ewma_ms: number | null;
    rate_wait_ewma_ms: number | null;
    page_kib_ewma: number | null;
    failure_pressure: number;
  } | null;
  monitor: MonitorStatus;
  metadata: MetadataStatus;
  torrents: TorrentStatus;
};

export type MonitorStatus = {
  enabled: boolean;
  interval_seconds: number;
  running: boolean;
  last_cycle_at: string | null;
  last_cycle_error: string | null;
  wanted_search: {
    enabled: boolean;
    interval_seconds: number;
    last_search_at: string | null;
    next_search_at: string | null;
    last_result: WantedSearchResult | null;
    running?: boolean;
    started_at?: string | null;
    current_manga_id?: string | null;
    progress?: { processed: number; total: number };
    last_error?: string | null;
  };
};

export type WantedSearchResult = {
  trigger: "manual" | "scheduled";
  series: number;
  missing: number;
  discovered: number;
  searched?: number;
  queued: number;
  upgraded?: number;
  aligned?: { deleted: number; series: number; enabled: boolean };
  quality?: {
    measured: number;
    degraded?: number;
    replaced: number;
    kept?: number;
    enabled: boolean;
  };
  errors: { manga_id: string; error: string }[];
};

export type InternetArchiveProbe = {
  ok: boolean;
  enabled: boolean;
  error?: string;
  latency_ms: number;
  probe_title?: string;
  items?: number;
  releases?: number;
  sample?: string[];
  user_agent: string;
};

/** Library files on disk that no Tankarr release claims. */
export type LibraryOrphans = {
  available: boolean;
  reason?: string;
  count: number;
  bytes?: number;
  truncated?: boolean;
  folders: { folder: string; files: number; bytes: number; sample: string[] }[];
};

export type WantedRecoveryChannel = {
  channel: "sources" | "indexer_chapter" | "indexer_book" | string;
  outcome: string;
  detail: string;
  at: string;
  checked_at?: string | null;
  next_eligible_at?: string | null;
  actionable_reason?: string;
};

/** What each recovery channel answered for one wanted slot. */
export type WantedRecovery = {
  verdict: "recovering" | "needs_review" | "exhausted" | "unsearched";
  summary: string;
  channels: WantedRecoveryChannel[];
  checked_at?: string | null;
  next_eligible_at?: string | null;
  actionable_reason?: string;
};

export type WantedChapter = Pick<
  Chapter,
  | "id"
  | "volume"
  | "chapter"
  | "title"
  | "provider"
  | "source_name"
  | "publish_at"
> & {
  queue_status?: string | null;
  blocked?: boolean;
  block_reason?: string | null;
  slot_key?: string;
  recovery?: WantedRecovery;
};

export type WantedEntry = {
  manga: {
    id: string;
    title: string;
    provider: string;
    source_name: string | null;
    cover_url: string | null;
    preferred_language: string;
    monitor_mode: MonitorMode;
  };
  chapters: WantedChapter[];
  unmapped_expected_count: number;
  /** How many of the unmapped items are locked at the official source. */
  unmapped_locked_count?: number;
  /** No installed source lists this work and no catalogue knows its size. */
  no_sources?: boolean;
  /** The series-level verdict, only for a `no_sources` entry. */
  recovery?: WantedRecovery;
  expected_count: number | null;
  expected_source: string | null;
};

export type SystemLogs = {
  level: string;
  directory: string;
  files: { name: string; size: number; modified: string }[];
};

export type DiskInfo = {
  path: string;
  total: number | null;
  free: number | null;
  used: number | null;
};

export type ProviderProbe = {
  name: string;
  label: string;
  search_mode: "title" | "url";
  ok: boolean;
  latency_ms: number | null;
  error?: string;
  supported_sites?: number;
};

export type SuwayomiSource = {
  id: string;
  name: string;
  language: string;
  content_warning: string;
  selected: boolean;
  allowed: boolean;
  enabled: boolean;
};

export type ProwlarrIndexer = {
  id: number;
  name: string;
  enabled: boolean;
  compatible: boolean;
  selected: boolean;
  protocol: string;
  priority: number;
  category_ids: number[];
};

export type ProwlarrCategory = {
  id: number;
  name: string;
  indexer_ids: number[];
  selected: boolean;
};

export type ProwlarrProbe = {
  ok: boolean;
  error?: string;
  version?: string;
  instance_name?: string;
  enabled_indexers?: number;
  compatible_indexers?: number;
  indexers?: ProwlarrIndexer[];
  categories?: ProwlarrCategory[];
};

export type BackupInfo = { name: string; size: number; created_at: string };

export type SystemStatus = {
  alerts?: {
    level: "danger" | "warn" | "info";
    key: string;
    title: string;
    detail: string;
    href?: string;
    signature?: string;
  }[];
  version: string;
  update?: {
    enabled: boolean;
    current: string;
    latest: string | null;
    update_available: boolean;
    url: string | null;
    checked_at: string | null;
    error: string | null;
  };
  python: string;
  platform: string;
  started_at: string;
  database_path: string;
  database_size: number;
  storage: { data: DiskInfo; library: DiskInfo };
  totals: {
    series: number;
    monitored: number;
    chapters: number;
    downloaded: number;
    jobs: number;
    failed_jobs: number;
  };
  providers: ProviderInfo[];
  suwayomi?: SuwayomiRuntimeStatus | null;
  library_alignment: LibraryAlignment | null;
  komga_refresh: KomgaRefreshStatus;
  ntfy_configured: boolean;
  notifications?: Record<string, boolean>;
  monitor: MonitorStatus;
  metadata: MetadataStatus;
  torrents: TorrentStatus;
  library:
    | { available: true; root: string }
    | { available: false; reason: string };
  backups: BackupInfo[];
};

export type CalendarRelease = Chapter & {
  manga_title: string;
  manga_cover_url: string | null;
  manga_monitor_mode: MonitorMode;
  availability_status: CalendarAvailabilityStatus;
};

export type CalendarAvailabilityStatus =
  | "expected"
  | "early_available"
  | "official_available"
  | "downloaded";

export type ExpectedRelease = {
  manga_id: string;
  manga_title: string;
  manga_cover_url: string | null;
  chapter: string;
  expected_at: string;
  cadence_days: number;
  cadence_label: string;
  last_chapter: string;
  last_release_at: string;
  availability_status: CalendarAvailabilityStatus;
  available: boolean;
  downloaded: boolean;
  /** Always true: the date projects the work's own rhythm, no publisher announces one. */
  estimated?: boolean;
  /** Days since the chapter was first expected and did not appear. */
  overdue_days?: number;
};

export type SeriesCalendarExpected = {
  chapter: string;
  expected_at: string;
  /** The platform's exact moment for an announced episode (ISO, with zone). */
  published_at?: string | null;
  cadence_days: number;
  cadence_label: string;
  overdue_days: number;
  estimated: boolean;
  available: boolean;
  downloaded: boolean;
  official: boolean;
};

export type SeriesCalendar = {
  manga_id: string;
  source: string | null;
  history_count: number;
  recent: { chapter: string; released_at: string }[];
  cadence: {
    days: number;
    label: string;
    last_chapter: string;
    last_release_at: string;
  } | null;
  expected: SeriesCalendarExpected[];
  reason: "paused" | "ended" | "no_history" | "no_rhythm" | null;
};

export type CalendarResponse = {
  releases: CalendarRelease[];
  expected: ExpectedRelease[];
};

export type SettingView = {
  value: string | number | boolean | null;
  secret: boolean;
  overridden: boolean;
};

export type SettingsPayload = Record<string, SettingView>;

export type ReaderBookLink = {
  chapter_id: string;
  book_id: string;
  url: string;
};

export type ReaderBookmark = {
  manga_id: string;
  manga_title: string;
  manga_cover_url: string | null;
  chapter_id: string;
  label: string;
  page_index: number;
  updated_at: string;
  url: string;
};

export type ReaderLink = {
  reader: "tankarr" | "komga" | "kavita" | "stump" | "url" | "none";
  label?: string;
  configured: boolean;
  available: boolean;
  url?: string;
  series_id?: string;
  title?: string;
  matched_books?: number;
  books?: ReaderBookLink[];
  bookmark?: {
    chapter_id: string;
    page_index: number;
    label: string;
    url: string;
  } | null;
  reason?: string;
};

export type ReaderBook = {
  release_id: string;
  page_version?: string;
  manga_id: string;
  series_title: string;
  book_title: string;
  display_mode: "manga" | "webtoon";
  display_mode_source: "series" | "global" | "metadata" | "page_shape" | "automatic";
  reading_direction: "rtl" | "ltr";
  page_count: number;
  page_index: number;
  bookmarked: boolean;
  bookmark_page_index: number | null;
  previous_release_id: string | null;
  next_release_id: string | null;
};

export type KomgaConnectionTest = {
  ok: boolean;
  error?: string;
  auth_method?: "api_key" | "basic";
  library_id?: string;
  library_name?: string;
  series_count?: number;
  book_count?: number;
};

export type ImportItem = {
  path: string;
  kind: "archive" | "folder";
  size: number;
  images: number;
  series_hint: string;
  volume: string | null;
  chapter: string | null;
  chapter_title: string;
};

export type ImportGroup = {
  key: string;
  title: string;
  authors: string[];
  items: ImportItem[];
  total_size: number;
  upload_id?: string;
  target_manga_id?: string;
  language?: string;
  unit?: SeriesUnit;
  set_series_unit?: boolean;
};

export type ImportScan = {
  root: string;
  upload_id?: string;
  groups: ImportGroup[];
  skipped: { path: string; reason: string }[];
  rar_supported: boolean;
  pdf_supported?: boolean;
};

export type ImportState = {
  running: boolean;
  total?: number;
  done?: number;
  imported?: number;
  series?: number;
  current?: string;
  errors?: { group: string; path?: string; error: string }[];
  finished?: boolean;
};

export type SuwayomiRuntimeStatus = {
  managed: boolean;
  installed: boolean;
  installing: boolean;
  install_progress: string | null;
  version: string | null;
  installed_at: string | null;
  jar_size_bytes: number;
  running: boolean;
  ready: boolean;
  pid: number | null;
  uptime_seconds: number | null;
  restarts: number;
  consecutive_crashes: number;
  last_exit_code: number | null;
  last_error: string | null;
  heap_mb: number;
  challenged_sources?: string[];
  exposed: boolean;
  extension_store: string | null;
  latest_version?: string | null;
  update_available?: boolean;
  last_update_check_at?: string | null;
  last_extension_refresh_at?: string | null;
  last_extension_updates?: string[];
  maintenance?: { last_run_at: string | null; result: Record<string, unknown> | null } | null;
  url: string;
  data_dir: string;
  log_tail: string[];
  release_page: string;
  writable: boolean;
  mode: string;
  enabled: boolean;
  languages: string[];
  public_url: string | null;
  external_url: string;
  default_extensions?: string[];
};

export type SuwayomiExtension = {
  pkg_name: string;
  name: string;
  language: string;
  version: string;
  nsfw: boolean;
  installed: boolean;
  has_update: boolean;
  obsolete: boolean;
  icon_url: string | null;
};

export type SuwayomiSourceTest = {
  id: string;
  name: string | null;
  language: string | null;
  hits: number;
  probes: number;
  average_seconds: number | null;
  error: string | null;
  verdict: "good" | "partial" | "slow" | "empty" | "unreachable";
};

export type QueueSeriesSummary = {
  manga_id: string;
  manga_title: string | null;
  manga_cover_url: string | null;
  queued: number;
  working: number;
  current: Job | null;
};

export type MatchReview = {
  id: number;
  manga_id: string;
  manga_title: string | null;
  manga_cover_url: string | null;
  kind: "source" | "release";
  provider: string;
  candidate_id: string;
  title: string;
  source_name: string | null;
  source_url: string | null;
  confidence: number;
  reason: string;
  created_at: string;
  // What the decision rests on: for a release, the copy being offered.
  payload?: {
    size?: string;
    seeders?: number;
    volume?: string | null;
    chapter?: string | null;
    protocol?: "torrent" | "usenet" | "http";
    indexer?: string;
    [key: string]: unknown;
  } | null;
};

export type AuthorPage = {
  id: string;
  author: string;
  aliases: string[];
  source: "mangabaka";
  source_url: string | null;
  source_urls: string[];
  source_pages: { name: string; url: string }[];
  works: MangaSummary[];
  work_count: number;
  library_manga_count: number;
  last_refreshed_at: string | null;
  next_refresh_at: string | null;
  last_refresh_error: string | null;
  merged_from: {
    id: string;
    name: string;
    reason: string;
    evidence: Record<string, unknown>;
    merged_at: string;
  }[];
  redirected_from: string | null;
};

export type TorrentContentFile = {
  path: string;
  size: number;
  pages: number;
  volume: string | null;
  chapter: string | null;
  numbered: boolean;
  contested: boolean;
  already_owned: boolean;
};

export type TorrentContents = {
  id: number;
  manga_id: string;
  title: string;
  files: TorrentContentFile[];
  skipped: { path?: string; reason?: string }[];
};

export type SeriesUnitFile = { id: string; title: string; provider: string; source_name: string | null; language: string; pages: number | null; library_path: string | null };
export type SeriesUnitChapter = ChapterSlot & { files: SeriesUnitFile[]; open_release_id: string | null; pages: number | null; missing: boolean; content_verdict?: "inside" | "outside" | "ambiguous" };
export type SeriesBookGroup = {
  key: string; volume: string; expected: boolean; owned: boolean; monitored: boolean; ignored: boolean;
  volume_monitor_state: VolumeMonitorState; status: "owned" | "covered_by_chapters" | "missing" | "unmapped";
  pages: number | null; files: SeriesUnitFile[]; open_release_id: string | null; suspect: boolean;
  retirement_note: string | null; exact: boolean; chapter_range: { first: string; last: string } | null;
  map_sources?: string[];
  chapters: SeriesUnitChapter[]; chapter_count: number; downloaded_chapter_count: number;
  missing_chapter_count: number; duplicate_file_count: number; duplicate_release_ids: string[];
  covered_by_chapters: boolean; can_assemble: boolean; collapsed: boolean;
  // The form layer (series_form): an estimated range, the completion plan and its wording.
  estimated?: boolean; plan_state?: "book" | "assemble" | "partial" | "missing"; plan_text?: string; redundant_book?: boolean;
};
export type SeriesMapConfidence = { level: "exact" | "partial" | "estimated" | "none"; estimated_books: number; unknown_books?: number; sources: string[]; hint_sources: string[] };
export type SeriesUnits = {
  completeness?: { indexed_chapters: number; indexed_chapters_on_disk: number; owned_books: number; expected_books: number | null; edition_files_complete: boolean; estimated_groups: number; manual_boundaries: boolean };
  manga_id: string; mode: "grouped" | "flat"; series_unit: "chapters" | "volumes";
  edition_book_count: number | null; expected_book_count: number | null; warning: string | null;
  hints: { volumes: string[]; chapters: string[]; source: string }[];
  books: SeriesBookGroup[]; unassigned_chapters: SeriesUnitChapter[];
  // The form of the series: a list of books or a list of chapters, never both.
  form?: "volumes" | "chapters"; form_reason?: string; uniform?: "books" | "chapters" | "mixed";
  preference?: "volumes" | "chapters"; map_confidence?: SeriesMapConfidence;
  expected_chapter_count?: number | null; exact_sources?: string[];
  // What the pages said where they disagreed with the map, one line each.
  content_notes?: string[];
};


export type DuplicateRetirementResult = DeleteResult & { files_retired: number; chapters_matched: number; volumes?: string[] };

export type BookAssemblyPreview = {
  manga_id: string;
  volume: string;
  filename: string;
  pages: number;
  chapters: { chapter: string; id: string; pages: number; source: string; source_chapter?: string | null }[];
  confirmation_snapshot: string;
};
export type BookAssemblyResult = BookAssemblyPreview & {
  chapter: Chapter;
  retirement: DuplicateRetirementResult;
  reader: Record<string, unknown>;
};
export type BooksAssemblyPreview = {
  books: BookAssemblyPreview[];
  errors: { volume: string; message: string }[];
  confirmation_snapshot: string;
};
export type BooksAssemblyResult = {
  assembled: BookAssemblyResult[];
  errors: { volume: string; message: string }[];
  remaining: string[];
};

export type SeriesAuditFile = {
  id: string; unit: "volume" | "chapter"; number: string | null; title: string;
  provider: string; provider_manga_id: string | null; source_name: string | null; language: string;
  pages: number | null; verdict: Record<string, unknown> | null; size_bytes: number | null;
  first_thumbnail_url: string | null; last_thumbnail_url: string | null;
  thumbnail_status: "pending" | "ready" | "unavailable"; last_page_black: boolean | null;
  anomalies: { code: string; label: string }[]; can_retire: boolean; can_reject_source: boolean;
};
export type SeriesAuditSource = { provider: string; provider_manga_id: string; source_name: string | null; language: string; file_ids: string[]; can_reject: boolean };
export type SeriesAuditReport = { manga_id: string; revision: string; files: SeriesAuditFile[]; sources: SeriesAuditSource[] };
export type SeriesAuditSourceKey = Pick<SeriesAuditSource, "provider" | "provider_manga_id">;
export type SeriesAuditPreview = {
  manga_id: string; action: "retire" | "reject_source"; chapter_ids: string[]; source: SeriesAuditSourceKey | null;
  revision: string; files: SeriesAuditFile[]; confirmation_snapshot: string; releases_to_remove?: number;
};
export type SeriesAuditResult = {
  manga_id: string; files_retired: number; files_deleted: number; chapters_reset: number;
  releases_removed: number; source_removed: boolean; cleanup_errors: string[]; cleanup_warning?: string | null;
};
