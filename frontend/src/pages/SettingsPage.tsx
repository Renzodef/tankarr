import { lazy, Suspense, useEffect, useState } from "react";
import { api, type ReaderDiscoveryMatch } from "../api";
import { SuwayomiManager } from "../components/SuwayomiManager";
import { Icon, LANGUAGES, Spinner, StatusPill, useApp } from "../components";
import { useUnsavedChangesGuard } from "../useUnsavedChangesGuard";
import type {
  ProwlarrCategory,
  ProwlarrIndexer,
  SettingsPayload,
  SuwayomiSource,
} from "../types";

type FieldDef = {
  key: string;
  label: string;
  hint?: string;
  helpUrl?: string;
  helpLabel?: string;
  testSource?: string;
  readOnly?: boolean;
  kind:
    | "text"
    | "url"
    | "password"
    | "number"
    | "language"
    | "languages"
    | "boolean"
    | "select"
    | "suwayomi_sources"
    | "prowlarr_indexers"
    | "prowlarr_categories";
  options?: [string, string][];
  visibleWhen?: { key: string; equals: string };
  advanced?: boolean;
  displayDivisor?: number;
};

type DownloadProvider = "suwayomi";

type SettingsTab = "general" | "sources" | "indexers" | "reader" | "metadata" | "notifications" | "security" | "data" | "translation";

const BackupPanel = lazy(() => import("../components/BackupPanel"));

const TABS: { id: SettingsTab; label: string; icon: "settings" | "search" | "download" | "external" | "library" | "alert" | "gears" }[] = [
  { id: "general", label: "General", icon: "settings" },
  { id: "sources", label: "Sources", icon: "search" },
  { id: "translation", label: "Translation", icon: "library" },
  { id: "indexers", label: "Indexers & torrents", icon: "download" },
  { id: "reader", label: "Reader", icon: "external" },
  { id: "metadata", label: "Metadata", icon: "library" },
  { id: "notifications", label: "Notifications", icon: "alert" },
  { id: "security", label: "Security", icon: "gears" },
  { id: "data", label: "Data", icon: "gears" },
];

type SectionDef = {
  title: string;
  group: SettingsTab;
  description?: string;
  fields: FieldDef[];
  providerTest?: {
    name: DownloadProvider;
    label: string;
    keys: string[];
  };
  prowlarrTest?: { keys: string[] };
  internetArchiveTest?: { keys: string[] };
  komgaTest?: { keys: string[] };
};

type CatalogOption = {
  id: string;
  label: string;
  detail: string;
  defaultSelected: boolean;
  disabled?: boolean;
  warning?: string;
};

function formatBytes(value: number) {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KiB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MiB`;
}

function browserRootForReader(match: ReaderDiscoveryMatch) {
  if (match.browser_url) return match.browser_url;
  try {
    const browser = new URL(window.location.origin);
    const service = new URL(match.internal_url);
    browser.protocol = service.protocol;
    browser.port = String(match.browser_port);
    browser.pathname = "/";
    browser.search = "";
    browser.hash = "";
    return browser.origin;
  } catch {
    return match.internal_url;
  }
}

function csvValues(value: string) {
  return value
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
}

function CatalogSelector({
  value,
  options,
  emptyMeansAll,
  emptyMessage,
  selectLabel = "Select available",
  id,
  labelId,
  hintId,
  onChange,
}: {
  value: string;
  options: CatalogOption[];
  emptyMeansAll: boolean;
  emptyMessage: string;
  selectLabel?: string;
  id?: string;
  labelId?: string;
  hintId?: string;
  onChange: (value: string) => void;
}) {
  if (!options.length) return <p className="muted small">{emptyMessage}</p>;

  const explicit = csvValues(value);
  const automatic = emptyMeansAll && explicit.length === 0;
  const selected = new Set(
    automatic
      ? options.filter((option) => option.defaultSelected).map((option) => option.id)
      : explicit,
  );
  const toggle = (id: string, checked: boolean) => {
    if (options.find((option) => option.id === id)?.disabled) return;
    const next = new Set(selected);
    if (checked) next.add(id);
    else next.delete(id);
    if (next.size === 0) return;
    onChange(
      options
        .map((option) => option.id)
        .filter((optionId) => next.has(optionId))
        .join(","),
    );
  };

  return (
    <div id={id} className="connection-catalog" role="group" aria-labelledby={labelId} aria-describedby={hintId}>
      <div className="connection-catalog-toolbar">
        <span className="muted small">
          {automatic ? "Automatic selection" : `${selected.size} selected`}
        </span>
        <div className="toolbar-group">
          {emptyMeansAll ? (
            <button type="button" className="btn btn-small" onClick={() => onChange("")}>
              Use all automatically
            </button>
          ) : null}
          <button
            type="button"
            className="btn btn-small"
            onClick={() =>
              onChange(
                options
                  .filter((option) => option.defaultSelected)
                  .map((option) => option.id)
                  .join(","),
              )
            }
          >
            {selectLabel}
          </button>
        </div>
      </div>
      <div className="connection-checklist">
        {options.map((option) => (
          <label
            key={option.id}
            className={`connection-option ${selected.has(option.id) ? "selected" : ""} ${option.disabled ? "disabled" : ""}`}
          >
            <input
              type="checkbox"
              checked={selected.has(option.id)}
              disabled={option.disabled}
              onChange={(event) => toggle(option.id, event.target.checked)}
            />
            <span className="connection-option-copy">
              <strong>{option.label}</strong>
              <span>{option.detail}</span>
              {option.warning ? <span className="warn-text">{option.warning}</span> : null}
            </span>
          </label>
        ))}
      </div>
    </div>
  );
}

const SECTIONS: SectionDef[] = [
  {
    title: "Translation fallback",
    group: "translation",
    description: "Translate missing books or chapters from another language. Tankarr tries the series language first. Enable fallback here and separately in each series, then enter your AI provider settings. By default, basic OCR and lettering run on this computer, one job at a time. Docker includes the required tools; other installations need Tesseract and DejaVu fonts. The AI provider may charge for translation. Existing files remain available when fallback is switched off.",
    fields: [
      { key: "translation_enabled", label: "Enable translation fallback", kind: "boolean" },
      { key: "translation_source_languages", label: "Source languages, in order", kind: "text", hint: "Use language codes separated by commas, for example original,ja,en,fr. Original uses the work’s original language when known. The requested language is skipped." },
      { key: "translation_ai_url", label: "AI provider API base URL", kind: "url", hint: "A provider with an OpenAI-compatible chat API, including its API path when required." },
      { key: "translation_ai_model", label: "AI model", kind: "text", hint: "Model identifier supplied by your AI provider." },
      { key: "translation_ai_api_key", label: "AI provider API key", kind: "password", hint: "Used to translate recognized dialogue with your provider. If an external processor is configured, it also receives this key. Stored with Tankarr’s other integration secrets." },
      { key: "translation_processor_url", label: "External processor URL (optional)", kind: "url", advanced: true, hint: "Leave empty to process on this computer. For advanced OCR and lettering, enter the HTTP(S) address of a compatible external processor. This overrides local processing." },
      { key: "translation_processor_token", label: "External processor access token (optional)", kind: "password", advanced: true, hint: "Only needed if your external processor requires authentication. This is not your AI provider key and is unused for local processing." },
    ],
  },
  {
    title: "Retention",
    group: "data",
    fields: [
      { key: "backup_retention_count", label: "Backups to keep", kind: "number", hint: "Number of automatic recovery bundles to retain (default 7)." },
      { key: "recycle_bin_retention_days", label: "Recycle bin retention (days)", kind: "number", hint: "Retired files are retained for this many days before nightly cleanup (default 7)." },
    ],
  },
  {
    title: "Security",
    group: "security",
    fields: [
      {
        key: "auth_method",
        label: "Authentication method",
        hint: "Forms shows the Tankarr login page. Basic uses the browser's credential prompt and remains available to API clients.",
        kind: "select",
        options: [
          ["forms", "Forms (Login Page)"],
          ["basic", "Basic (Browser Prompt)"],
        ],
      },
      {
        key: "auth_username",
        label: "Username",
        hint: "Changing the username signs out existing browser sessions.",
        kind: "text",
      },
      {
        key: "auth_password",
        label: "Password",
        hint: "At least 8 characters. Leave the masked value unchanged to keep the current password.",
        kind: "password",
      },
    ],
  },
  {
    title: "General",
    group: "general",
    fields: [
      {
        key: "search_languages",
        label: "Search languages",
        hint: "Choose which translation languages appear when adding a series. English is enabled by default.",
        kind: "languages",
      },
      { key: "default_language", label: "Default language", kind: "language" },
      {
        key: "monitor_enabled",
        label: "Release monitoring",
        hint: "Poll monitored remote series for newly published chapters.",
        kind: "boolean",
      },
      {
        key: "monitor_interval_seconds",
        advanced: true,
        label: "Monitor interval (seconds)",
        hint: "How often monitored series are checked for new releases (min 60).",
        kind: "number",
      },
      {
        key: "wanted_search_enabled",
        label: "Scheduled Wanted recovery",
        hint: "Periodically retry monitored releases that are still missing from the library.",
        kind: "boolean",
      },
      {
        key: "wanted_search_interval_seconds",
        advanced: true,
        label: "Wanted recovery interval (seconds)",
        hint: "Separate from release monitoring. Default 21600 means every 6 hours (min 900).",
        kind: "number",
      },
      {
        key: "download_concurrency",
        advanced: true,
        label: "Maximum page concurrency",
        hint: "Adaptive ceiling (1–12). Tankarr uses latency, source errors, memory pressure and temperature to choose the effective value.",
        kind: "number",
      },
      {
        key: "download_pipeline_max",
        advanced: true,
        label: "Maximum simultaneous chapters",
        hint: "0 is automatic. Tankarr increases or reduces parallel series from live CPU, working-set memory and source/network pressure; a positive value only sets a ceiling.",
        kind: "number",
      },
      {
        key: "import_max_expanded_bytes",
        advanced: true,
        label: "Expanded archive size limit (MiB)",
        hint: "Maximum uncompressed size accepted for an import. 1024 MiB = 1 GiB; the default 16384 MiB is 16 GiB. Existing books are not changed.",
        kind: "number",
        displayDivisor: 1024 * 1024,
      },
      {
        key: "import_max_pages",
        advanced: true,
        label: "Maximum pages per import",
        hint: "Reject oversized imports before accepting more than this number of pages. Default: 20000 pages.",
        kind: "number",
      },
      {
        key: "import_subprocess_memory_mb",
        advanced: true,
        label: "Import subprocess memory limit (MiB)",
        hint: "Memory ceiling for an import subprocess, not a new concurrency target. Default: 1024 MiB (1 GiB).",
        kind: "number",
      },
      {
        key: "import_disk_reserve_bytes",
        advanced: true,
        label: "Import free-space reserve (MiB)",
        hint: "Keep this much disk space in reserve when estimating expansion and packaging. Default: 512 MiB. Imports may be refused when space is insufficient.",
        kind: "number",
        displayDivisor: 1024 * 1024,
      },
    ],
  },
  {
    title: "Download sources",
    group: "sources",
    description: "Works are added from the MangaBaka catalogue; chapters come from the Suwayomi sources and Prowlarr indexers. Choose a source preference profile; automatic selection follows it for every matching chapter. Optional source lists let you refine the order.",
    fields: [
      {
        key: "release_acquisition_policy",
        label: "New release policy",
        hint: "This chooses the file only after every candidate has been mapped to the same canonical chapter. It never changes numbering or calendar dates.",
        kind: "select",
        options: [
          ["prefer_official", "Prefer official when available"],
          ["first_available", "First available"],
          ["official_only", "Official only"],
        ],
      },
      {
        key: "duplicate_cleanup_enabled",
        label: "Remove duplicate chapter files automatically",
        hint: "When the volume↔chapter map proves a chapter file's content is inside a book already on disk, the chapter file is quarantined and removed on the monitor's cycle (up to 10 series per cycle). Off: duplicates are only reported on the series page.",
        kind: "boolean",
      },
      {
        key: "release_preference_profile",
        label: "Source preference profile",
        hint: "Balanced keeps the normal new-release/backlog ordering. Official prioritizes publishers; curated prioritizes curated scans. Language and numbering rules always apply. Explicit source lists take precedence.",
        kind: "select",
        options: [["balanced", "Balanced"], ["official", "Official sources"], ["curated", "Curated scans"]],
      },
      {
        key: "source_priority_fresh", label: "Preferred sources for new chapters",
        hint: "Optional ordered source names, separated by commas, for example suwayomi:mangaplus,suwayomi:weebcentral. Empty uses the profile.", kind: "text",
      },
      {
        key: "source_priority_backfill", label: "Preferred sources for backlog and upgrades",
        hint: "Optional ordered source names. Unlisted sources remain available as fallbacks.", kind: "text",
      },
      {
        key: "source_upgrade_enabled", label: "Upgrade to preferred sources automatically",
        hint: "Replace an owned chapter only with a strictly preferred source in the same language and numbering. The old file stays until the replacement is verified. Equal preferences, age and size never trigger replacement. When enabled, this profile also governs official upgrades.", kind: "boolean",
      },
      {
        key: "official_upgrade_enabled",
        label: "Upgrade to official releases",
        hint: "Independent from the acquisition policy: replace a non-official file when the same canonical chapter later appears on the publisher platform (up to 20 per series per cycle).",
        kind: "boolean",
      },
      {
        key: "preferred_unit",
        label: "Preferred unit",
        hint: "Tankarr follows whatever the sources can deliver in full: chapters or whole books. When both can complete a finished work, this preference decides; a running work follows chapters unless only books exist.",
        kind: "select",
        options: [
          ["volumes", "Whole volumes (books)"],
          ["chapters", "Chapters"],
        ],
      },
      {
        key: "special_chapters_outside_books",
        label: "Show specials no book contains",
        hint: "A half chapter, an omake, or a second cut of a story a source published on its own: content that was never bound into a volume. Off by default, so a shelf of books carries no rows for it. Either way it is never counted as missing.",
        kind: "boolean",
      },
    ],
  },
  {
    title: "Suwayomi",
    group: "sources",
    description: "Suwayomi is Tankarr's source engine: it runs Mihon/Tachiyomi-compatible extensions from the repository you configure, so Tankarr never re-implements a manga site. In Managed mode Tankarr installs and supervises the official server inside its own container; Tankarr keeps owning monitoring, downloads, CBZ creation, and library import.",
    fields: [
      {
        key: "suwayomi_enabled",
        label: "Enabled",
        hint: "Disabling stops Suwayomi searches and downloads immediately. Existing Suwayomi series cannot refresh or download until it is enabled again.",
        kind: "boolean",
      },
      {
        key: "suwayomi_mode",
        label: "Mode",
        hint: "Managed: Tankarr downloads, verifies and runs the official Suwayomi-Server JAR inside its own container. External: connect to a Suwayomi server you run yourself.",
        kind: "select",
        options: [
          ["managed", "Managed by Tankarr (recommended)"],
          ["external", "External server"],
        ],
      },
      {
        key: "suwayomi_managed_heap_mb",
        advanced: true,
        label: "Java heap (MiB)",
        hint: "Maximum heap for the managed server (128–1024). With the full language catalogue installed, 512 MiB is a safer floor; raise it if the log shows OutOfMemoryError.",
        kind: "number",
        visibleWhen: { key: "suwayomi_mode", equals: "managed" },
      },
      {
        key: "suwayomi_extension_store",
        label: "Extension repository",
        hint: "Index URL of a Mihon/Tachiyomi-compatible extension repository, usually ending in index.min.json. Tankarr ships no repository and recommends none: enter the one you trust. Applied when the managed server restarts.",
        kind: "url",
        visibleWhen: { key: "suwayomi_mode", equals: "managed" },
      },
      {
        key: "suwayomi_url",
        label: "Server URL",
        hint: "Internal service root, for example http://suwayomi:4567. Do not append /api/graphql.",
        kind: "text",
        visibleWhen: { key: "suwayomi_mode", equals: "external" },
      },
      {
        key: "suwayomi_username",
        label: "Username",
        hint: "The login already configured in Suwayomi. Changing this does not create or modify a Suwayomi account.",
        kind: "text",
        visibleWhen: { key: "suwayomi_mode", equals: "external" },
      },
      {
        key: "suwayomi_password",
        label: "Password",
        hint: "Stored in /config/metadata.env with mode 0600 and returned only as a masked value.",
        kind: "password",
        visibleWhen: { key: "suwayomi_mode", equals: "external" },
      },
      {
        key: "suwayomi_auto_install_official",
        label: "Auto-install official extensions",
        hint: "When a work's catalogue record names a free official platform (MANGA Plus, WEBTOON, Tapas, Comikey, Manga UP!), Tankarr installs its extension in the managed runtime and maps it as a download source.",
        kind: "boolean",
        visibleWhen: { key: "suwayomi_mode", equals: "managed" },
      },
      {
        key: "suwayomi_source_ids",
        label: "Sources",
        hint: "Run Test & load sources, then choose installed extensions here. Automatic selection means every safe extension matching Tankarr's enabled languages, including compatible sources installed later. A manual selection stores Suwayomi's stable numeric IDs; no source-name allowlist is needed.",
        kind: "suwayomi_sources",
      },
    ],
    providerTest: {
      name: "suwayomi",
      label: "Test & load sources",
      keys: [
        "suwayomi_enabled",
        "search_languages",
        "suwayomi_url",
        "suwayomi_username",
        "suwayomi_password",
        "suwayomi_source_ids",
        "suwayomi_mode",
        "suwayomi_managed_heap_mb",
        "suwayomi_extension_store",
      ],
    },
  },
  {
    title: "Internet Archive (direct download)",
    group: "indexers",
    description: "archive.org holds whole volumes as CBZ, CBR and PDF. Tankarr searches it by the work's title, once per pass and never chapter by chapter, downloads the file itself (no torrent client) and passes it through the same identity, language and page-quality gates as any release. Ranked below every indexer: the fallback that closes \"Not obtainable\" verdicts on out-of-print works.",
    fields: [
      {
        key: "internet_archive_enabled",
        label: "Enabled",
        hint: "Include archive.org in Series release search and the Wanted book search.",
        kind: "boolean",
      },
    ],
    internetArchiveTest: { keys: ["internet_archive_enabled"] },
  },
  {
    title: "Prowlarr indexers",
    group: "indexers",
    description: "Prowlarr is used by Interactive Search on series and Wanted, and by the automatic Wanted recovery, which grabs only unambiguous matches and asks on System > To confirm otherwise. The exact release is resolved through Prowlarr, verified by info hash, and sent to qBittorrent or SABnzbd.",
    fields: [
      {
        key: "prowlarr_enabled",
        label: "Enabled",
        hint: "Enables Prowlarr for interactive release searches and for the automatic Wanted recovery.",
        kind: "boolean",
      },
      {
        key: "prowlarr_url",
        label: "Server URL",
        hint: "Internal service root, for example http://prowlarr:9696.",
        kind: "text",
      },
      {
        key: "prowlarr_api_key",
        label: "API key",
        hint: "Copy it from Prowlarr > Settings > General > Security. It is stored in /config/metadata.env with mode 0600 and is never returned unmasked.",
        kind: "password",
      },
      {
        key: "prowlarr_indexer_ids",
        label: "Indexers",
        hint: "Automatic selection uses every enabled Prowlarr indexer that advertises book, literature, manga, or comic categories.",
        kind: "prowlarr_indexers",
      },
      {
        key: "prowlarr_categories",
        label: "Categories",
        hint: "Only these Prowlarr categories will be eligible for Tankarr searches. At least one category must remain selected.",
        kind: "prowlarr_categories",
      },
    ],
    prowlarrTest: {
      keys: [
        "prowlarr_enabled",
        "prowlarr_url",
        "prowlarr_api_key",
        "prowlarr_indexer_ids",
        "prowlarr_categories",
      ],
    },
  },
  {
    title: "Metadata catalogues",
    group: "metadata",
    description: "MangaBaka is the only manga catalogue Tankarr queries: it identifies the work and already aggregates MangaUpdates, AniList, MAL and Kitsu. The calendar comes from the mapped official platform's own release dates.",
    fields: [
      {
        key: "metadata_enabled",
        label: "MangaBaka · Always on",
        hint: "The whole metadata spine: identity, counts, description, official links and the MangaUpdates/AniList ids and ratings it aggregates. Use the test to check it is reachable.",
        testSource: "mangabaka",
        kind: "boolean",
        readOnly: true,
      },
      {
        key: "metadata_refresh_interval_hours",
        advanced: true,
        label: "Refresh interval (hours)",
        hint: "New series are discovered promptly; existing source records are queried again only after this interval. 168 hours means weekly.",
        kind: "number",
      },
    ],
  },
  {
    title: "Reader",
    group: "reader",
    description: "The built-in reader opens Tankarr's CBZ files directly and stores progress locally, with no second library or metadata scan. External readers remain optional.",
    fields: [
      {
        key: "reader_kind",
        label: "Reader",
        hint: "Tankarr is ready immediately and keeps reading progress here. External readers maintain their own library and may require synchronization.",
        kind: "select",
        options: [
          ["tankarr", "Tankarr (built-in)"],
          ["auto", "Automatic"],
          ["komga", "Komga"],
          ["kavita", "Kavita"],
          ["stump", "Stump"],
          ["url", "URL template"],
          ["none", "No shortcut"],
        ],
      },
      {
        key: "reader_display_mode",
        label: "Default reading mode",
        hint: "Automatic uses series metadata and page shape. Manga turns pages from right to left; webtoons scroll vertically.",
        kind: "select",
        options: [
          ["auto", "Automatic"],
          ["manga", "Manga (right to left)"],
          ["webtoon", "Webtoon (vertical)"],
        ],
      },
      {
        key: "reader_url",
        label: "Reader URL",
        hint: "Browser-facing root of the reader, for example http://nas.local:5000 (Kavita) or http://nas.local:10801 (Stump).",
        kind: "text",
        visibleWhen: { key: "reader_kind", equals: "kavita" },
      },
      {
        key: "reader_url",
        label: "Komga URL",
        hint: "Browser-facing root of Komga, for example http://nas.local:25600. With an API key Tankarr also keeps Komga aligned (targeted scans, titles, covers) — automatically, nothing else to configure.",
        kind: "text",
        visibleWhen: { key: "reader_kind", equals: "komga" },
      },
      {
        key: "reader_api_key",
        label: "Komga API key",
        hint: "From Komga → Account → API keys. Stored in /config/metadata.env with mode 0600.",
        kind: "password",
        visibleWhen: { key: "reader_kind", equals: "komga" },
      },
      {
        key: "reader_url",
        label: "Stump URL",
        hint: "Browser-facing root of Stump, for example http://nas.local:10801.",
        kind: "text",
        visibleWhen: { key: "reader_kind", equals: "stump" },
      },
      {
        key: "reader_username",
        label: "Stump username",
        hint: "A Stump account; Tankarr reads series and book ids to build Open and Read links.",
        kind: "text",
        visibleWhen: { key: "reader_kind", equals: "stump" },
      },
      {
        key: "reader_password",
        label: "Stump password",
        hint: "Stored in /config/metadata.env with mode 0600.",
        kind: "password",
        visibleWhen: { key: "reader_kind", equals: "stump" },
      },
      {
        key: "reader_internal_url",
        label: "Reader URL from inside Tankarr",
        hint: "Only if Tankarr cannot reach the browser URL from its container (typical with Docker): the service name, e.g. http://stump:10801 or http://kavita:5000.",
        kind: "text",
        advanced: true,
      },
      {
        key: "reader_library_path",
        label: "Library path inside Stump",
        hint: "How Stump sees Tankarr's library folder (the container mount), e.g. /data/comics. Series and books are resolved by exact folder and file paths, never by title.",
        kind: "text",
        advanced: true,
        visibleWhen: { key: "reader_kind", equals: "stump" },
      },
      {
        key: "reader_api_key",
        label: "Kavita API key",
        hint: "From Kavita → Settings → Account → API key. Stored in /config/metadata.env with mode 0600.",
        kind: "password",
        visibleWhen: { key: "reader_kind", equals: "kavita" },
      },
      {
        key: "reader_series_url_template",
        label: "Series URL template",
        hint: "Series shortcut only. For example https://reader.local/search?q={title} · placeholders: {title}, {title_raw}, {folder}, {id}.",
        kind: "text",
        visibleWhen: { key: "reader_kind", equals: "url" },
      },
    ],
  },
  {
    title: "Torrent client (qBittorrent)",
    group: "indexers",
    description: "Used only for torrent releases found through Prowlarr: qBittorrent downloads them, Tankarr imports the files into the library once validated and leaves the torrent seeding under qBittorrent's own rules (it is removed only when you discard it from Activity). Chapters from Suwayomi never go through qBittorrent.",
    fields: [
      {
        key: "torrent_auto_import",
        label: "Automatic import",
        hint: "Import completed comic archives only after numbering and OCR language checks pass.",
        kind: "boolean",
      },
      {
        key: "torrent_completed_action",
        label: "After import",
        hint: "Keep seeding leaves the torrent to qBittorrent's ratio/time rules. Remove after import deletes torrent and files as soon as the books are in the library. Remove when seeding is done waits until qBittorrent pauses the torrent at its limits, then removes it. Torrents in Tankarr's category that no job references are removed after the grace period below.",
        kind: "select",
        options: [
          ["seed", "Keep seeding"],
          ["remove_after_import", "Remove after import"],
          ["remove_when_seeded", "Remove when seeding is done"],
        ],
      },
      {
        key: "torrent_orphan_grace_hours",
        advanced: true,
        label: "Orphan grace period (hours)",
        hint: "A torrent Tankarr added whose download was deleted afterwards is removed with its files after this many hours. Torrents Tankarr did not add are never removed.",
        kind: "number",
      },
      {
        key: "qbittorrent_url",
        label: "qBittorrent URL",
        hint: "Internal service URL, for example http://qbittorrent:8080.",
        kind: "text",
      },
      {
        key: "qbittorrent_public_url",
        advanced: true,
        label: "qBittorrent link (browser)",
        hint: "The address a browser on your network can open, for example http://nas.local:8080. Powers the shortcut on each torrent in Activity; leave blank to hide it.",
        kind: "text",
      },
      { key: "qbittorrent_username", label: "Username", kind: "text" },
      { key: "qbittorrent_password", label: "Password", kind: "password" },
      {
        key: "qbittorrent_category",
        advanced: true,
        label: "Category",
        hint: "Tankarr owns every torrent in this qBittorrent category.",
        kind: "text",
      },
    ],
  },
  {
    title: "Usenet client (SABnzbd)",
    group: "indexers",
    description: "NZB releases found through Prowlarr (official digital volumes are common on Usenet): SABnzbd downloads them under Tankarr's category, Tankarr imports the books and removes the entry unless \"Keep seeding\" is selected above. The default paths follow the common /data/downloads layout.",
    fields: [
      {
        key: "sabnzbd_url",
        label: "SABnzbd URL",
        hint: "Internal service URL, for example http://sabnzbd:8080.",
        kind: "text",
      },
      {
        key: "sabnzbd_public_url",
        advanced: true,
        label: "SABnzbd link (browser)",
        hint: "The address a browser on your network can open, for example http://nas.local:8080. Powers the shortcut on each Usenet download in Activity; leave blank to hide it.",
        kind: "text",
      },
      { key: "sabnzbd_api_key", label: "API key", hint: "SABnzbd → Config → General → API Key.", kind: "password" },
      {
        key: "sabnzbd_category",
        advanced: true,
        label: "Category",
        hint: "Tankarr owns every download in this SABnzbd category.",
        kind: "text",
      },
      {
        key: "sabnzbd_complete_path",
        advanced: true,
        label: "Completed folder (as SABnzbd sees it)",
        hint: "SABnzbd's complete_dir, as SABnzbd sees it. Mount the same folder read-only into Tankarr and set TANKARR_USENET_DOWNLOAD_DIR to that mount.",
        kind: "text",
      },
    ],
  },
  {
    title: "Notifications (ntfy)",
    group: "notifications",
    description: "Notify after a verified import, a failed download, or a new decision that needs your review. Imports and decisions use normal priority; failures use high priority. Routine queue activity and searches stay quiet. The test sends immediately using the values shown, even before you save them.",
    fields: [
      {
        key: "ntfy_url",
        label: "ntfy URL",
        hint: "Server root, for example https://ntfy.sh or your own ntfy server. On iOS, this must exactly match the ntfy app's Default Server.",
        kind: "text",
      },
      {
        key: "ntfy_topic",
        label: "Topic",
        hint: "Subscribe to this exact topic on the configured server. URL and topic together enable ntfy.",
        kind: "text",
      },
      {
        key: "ntfy_on_chapter_imported",
        label: "On chapter imported",
        hint: "Notify after the validated CBZ has been written to the library successfully.",
        kind: "boolean",
      },
      {
        key: "ntfy_on_download_failed",
        label: "On download failed",
        hint: "Notify with high priority when a Tankarr download job reaches Failed.",
        kind: "boolean",
      },
      {
        key: "ntfy_on_decision_needed",
        label: "On decision needed",
        hint: "Notify once for each new match review and each Wanted item every channel has given up on (\"Not obtainable\"). Tankarr never deletes or ignores anything on its own.",
        kind: "boolean",
      },
    ],
  },
];

export default function SettingsPage() {
  const { notify, refreshHealth } = useApp();
  const [settings, setSettings] = useState<SettingsPayload | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [values, setValues] = useState<Record<string, string>>({});
  const [activeTab, setActiveTab] = useState<SettingsTab>(() => {
    const fromHash = new URLSearchParams(window.location.hash.split("?")[1]).get("tab") as SettingsTab | null;
    return fromHash && TABS.some((tab) => tab.id === fromHash) ? fromHash : "general";
  });
  const [showAdvanced, setShowAdvanced] = useState<boolean>(() => window.localStorage.getItem("tankarr.settings.advanced") === "1");
  const toggleAdvanced = () =>
    setShowAdvanced((current) => {
      window.localStorage.setItem("tankarr.settings.advanced", current ? "0" : "1");
      return !current;
    });
  const [saving, setSaving] = useState(false);
  const [leavingAfterSave, setLeavingAfterSave] = useState(false);
  const [qbitTest, setQbitTest] = useState<string | null>(null);
  const [metadataTests, setMetadataTests] = useState<Record<string, string>>({});
  const [providerTests, setProviderTests] = useState<Record<string, string>>({});
  const [suwayomiSources, setSuwayomiSources] = useState<SuwayomiSource[]>([]);
  const [prowlarrTest, setProwlarrTest] = useState<string | null>(null);
  const [internetArchiveTest, setInternetArchiveTest] = useState<string | null>(null);
  const [komgaTest, setKomgaTest] = useState<string | null>(null);
  const [readerTest, setReaderTest] = useState<string | null>(null);
  const [readerDiscovery, setReaderDiscovery] = useState<string | null>(null);
  const [ntfyTest, setNtfyTest] = useState<string | null>(null);
  const [sabTest, setSabTest] = useState<string | null>(null);
  const [prowlarrIndexers, setProwlarrIndexers] = useState<ProwlarrIndexer[]>([]);
  const [prowlarrCategories, setProwlarrCategories] = useState<ProwlarrCategory[]>([]);

  const load = async (submittedValues?: Record<string, string>) => {
    try {
      const payload = await api.getSettings();
      setSettings(payload);
      setLoadError(null);
      const serverValues = Object.fromEntries(
          Object.entries(payload).map(([key, view]) => [
            key,
            view.value === null || view.value === undefined ? "" : String(view.value),
          ]),
        );
      setValues((current) => {
        if (!submittedValues) return serverValues;
        // A save acknowledges only the draft sent with that request. Keep
        // edits made while it was pending, including edits on another tab.
        return { ...serverValues, ...Object.fromEntries(Object.entries(current)
          .filter(([key, value]) => value !== submittedValues[key])) };
      });
    } catch (caught) {
      // Settings load only on mount, so a restart or a slow reply used to
      // leave the page on a spinner for ever.
      setLoadError(String(caught));
      notify("error", String(caught));
    }
  };

  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    const syncTab = () => {
      const next = new URLSearchParams(window.location.hash.split("?")[1]).get("tab");
      if (next && TABS.some((tab) => tab.id === next)) setActiveTab(next as SettingsTab);
    };
    window.addEventListener("hashchange", syncTab);
    return () => window.removeEventListener("hashchange", syncTab);
  }, []);

  const changedEntries = Object.entries(values).filter(([key, value]) => {
    const current = settings?.[key]?.value;
    return value !== (current === null || current === undefined ? "" : String(current));
  });
  const dirtyKeys = changedEntries.map(([key]) => key);
  const changes = Object.fromEntries(changedEntries);
  useUnsavedChangesGuard(dirtyKeys.length > 0 && !leavingAfterSave);

  if (!settings)
    return loadError ? (
      <div className="panel">
        <h2>Settings unavailable</h2>
        <p className="muted">{loadError}</p>
        <button type="button" className="btn" onClick={() => void load()}>
          <Icon name="refresh" size={14} /> Retry
        </button>
      </div>
    ) : (
      <Spinner />
    );

  const pickValues = (keys: string[]) =>
    Object.fromEntries(keys.map((key) => [key, values[key] ?? ""]));

  const selectedSearchLanguages = (values.search_languages ?? "")
    .split(",")
    .map((value) => value.trim())
    .filter(Boolean);

  const languageLabels = new Map(LANGUAGES);
  const suwayomiSourceOptions: CatalogOption[] = suwayomiSources.map((source) => ({
    id: source.id,
    label: source.name,
    detail: `${languageLabels.get(source.language) ?? source.language.toUpperCase()} · ID ${source.id}`,
    defaultSelected: source.enabled,
    disabled: !source.allowed,
    warning: source.allowed
      ? undefined
      : `Blocked by the current ${source.content_warning} safety classification`,
  }));
  const prowlarrIndexerOptions: CatalogOption[] = prowlarrIndexers.map((indexer) => ({
    id: String(indexer.id),
    label: indexer.name,
    detail: `${indexer.protocol.toUpperCase()} · priority ${indexer.priority} · ID ${indexer.id}`,
    defaultSelected: indexer.enabled && indexer.compatible,
    disabled: !indexer.enabled || !indexer.compatible,
    warning: !indexer.enabled
      ? "Disabled in Prowlarr"
      : !indexer.compatible
        ? "No book/comic categories advertised"
        : undefined,
  }));
  const prowlarrCategoryOptions: CatalogOption[] = prowlarrCategories.map((category) => ({
    id: String(category.id),
    label: category.name,
    detail: `ID ${category.id} · ${category.indexer_ids.length} indexer${category.indexer_ids.length === 1 ? "" : "s"}`,
    defaultSelected: [7000, 7020, 7030].includes(category.id),
  }));

  const toggleSearchLanguage = (code: string, checked: boolean) => {
    setValues((current) => {
      const selected = (current.search_languages ?? "")
        .split(",")
        .map((value) => value.trim())
        .filter(Boolean);
      const next = checked
        ? [...selected, code].filter((value, index, all) => all.indexOf(value) === index)
        : selected.filter((value) => value !== code);
      if (next.length === 0) return current;
      return {
        ...current,
        search_languages: next.join(","),
        default_language: next.includes(current.default_language) ? current.default_language : next[0],
      };
    });
  };

  const save = async () => {
    const submittedValues = { ...values };
    setSaving(true);
    try {
      const result = await api.putSettings(changes);
      notify(
        "success",
        result.applied.length
          ? `Saved: ${result.applied.join(", ")}. Changes apply immediately.`
          : "Nothing changed.",
      );
      const authenticationChanged = result.applied.some((key) =>
        ["auth_method", "auth_username", "auth_password"].includes(key),
      );
      if (authenticationChanged) {
        setLeavingAfterSave(true);
        window.setTimeout(() => window.location.reload(), 700);
        return;
      }
      await Promise.all([load(submittedValues), refreshHealth()]);
    } catch (caught) {
      notify("error", String(caught));
    } finally {
      setSaving(false);
    }
  };

  const testSab = async () => {
    setSabTest("…");
    try {
      const result = await api.testSabnzbd(pickValues(["sabnzbd_url", "sabnzbd_api_key", "sabnzbd_category", "sabnzbd_complete_path"]));
      setSabTest(result.ok ? `OK — SABnzbd ${result.version ?? "connected"}` : `Failed: ${result.error}`);
    } catch (caught) {
      setSabTest(String(caught));
    }
  };

  const testQbit = async () => {
    setQbitTest("…");
    try {
      const result = await api.testQbittorrent(
        pickValues([
          "qbittorrent_url",
          "qbittorrent_username",
          "qbittorrent_password",
          "qbittorrent_category",
        ]),
      );
      setQbitTest(result.ok ? `OK — qBittorrent ${result.version ?? "connected"}` : `Failed: ${result.error}`);
    } catch (caught) {
      setQbitTest(String(caught));
    }
  };

  const testMetadata = async (source: string, key: string) => {
    setMetadataTests((current) => ({ ...current, [source]: "…" }));
    try {
      const result = await api.testMetadataSource(source, pickValues([key]));
      setMetadataTests((current) => ({
        ...current,
        [source]: result.ok
          ? `OK — ${result.source ?? source} reachable${typeof result.results === "number" ? ` (${result.results} result${result.results === 1 ? "" : "s"})` : ""}.`
          : `Failed: ${result.error}`,
      }));
    } catch (caught) {
      setMetadataTests((current) => ({ ...current, [source]: String(caught) }));
    }
  };

  const testProvider = async (
    provider: DownloadProvider,
    keys: string[],
  ) => {
    setProviderTests((current) => ({ ...current, [provider]: "…" }));
    if (provider === "suwayomi") setSuwayomiSources([]);
    try {
      const result = await api.testDownloadProvider(provider, pickValues(keys));
      let detail = result.label ?? provider;
      if (provider === "suwayomi" && result.sources !== undefined) {
        detail = `${result.sources} allowed ${result.language ?? ""} source${result.sources === 1 ? "" : "s"}`.trim();
        setSuwayomiSources(result.source_details ?? []);
      }
      setProviderTests((current) => ({
        ...current,
        [provider]: result.ok ? `OK — ${detail}` : `Failed: ${result.error}`,
      }));
    } catch (caught) {
      setProviderTests((current) => ({ ...current, [provider]: String(caught) }));
    }
  };

  const testInternetArchive = async (keys: string[]) => {
    setInternetArchiveTest("…");
    try {
      const result = await api.testInternetArchive(pickValues(keys));
      if (!result.ok) {
        setInternetArchiveTest(`Failed: ${result.error ?? "unknown error"} (${result.latency_ms} ms)`);
        return;
      }
      const sample = (result.sample ?? []).slice(0, 3).join(", ");
      setInternetArchiveTest(
        `OK — archive.org answered in ${result.latency_ms} ms; "${result.probe_title}" → ${result.items ?? 0} item${
          result.items === 1 ? "" : "s"
        }, ${result.releases ?? 0} book file${result.releases === 1 ? "" : "s"}${sample ? ` (${sample})` : ""}${
          result.enabled ? "" : " · currently disabled"
        }`,
      );
    } catch (caught) {
      setInternetArchiveTest(String(caught));
    }
  };

  const testProwlarr = async (keys: string[]) => {
    setProwlarrTest("…");
    setProwlarrIndexers([]);
    setProwlarrCategories([]);
    try {
      const result = await api.testProwlarr(pickValues(keys));
      if (!result.ok) {
        setProwlarrTest(`Failed: ${result.error ?? "unknown error"}`);
        return;
      }
      setProwlarrIndexers(result.indexers ?? []);
      setProwlarrCategories(result.categories ?? []);
      setProwlarrTest(
        `OK — Prowlarr ${result.version ?? "connected"}; ${result.compatible_indexers ?? 0} compatible of ${result.enabled_indexers ?? 0} enabled indexers`,
      );
    } catch (caught) {
      setProwlarrTest(String(caught));
    }
  };

  const testKomga = async (keys: string[]) => {
    setKomgaTest("…");
    try {
      const result = await api.testKomga(pickValues(keys));
      if (!result.ok) {
        setKomgaTest(`Failed: ${result.error ?? "unknown error"}`);
        return;
      }
      const library = result.library_name
        ? `${result.library_name}${result.library_id ? ` (${result.library_id})` : ""}`
        : result.library_id ?? "library connected";
      const counts = [
        result.series_count === undefined
          ? null
          : `${result.series_count} series`,
        result.book_count === undefined
          ? null
          : `${result.book_count} books`,
      ].filter(Boolean);
      const authentication = result.auth_method === "api_key"
        ? "API key"
        : result.auth_method === "basic"
          ? "username/password"
          : null;
      setKomgaTest(
        `OK — ${library}${authentication ? ` · ${authentication}` : ""}${counts.length ? ` · ${counts.join(" · ")}` : ""}`,
      );
    } catch (caught) {
      setKomgaTest(String(caught));
    }
  };

  const testNtfy = async () => {
    setNtfyTest("…");
    try {
      const result = await api.testNtfy(pickValues(["ntfy_url", "ntfy_topic"]));
      if (!result.ok) {
        const message = `Failed: ${result.error ?? "unknown error"}`;
        setNtfyTest(message);
        notify("error", message);
        return;
      }
      const message = `Sent — ntfy accepted the test for topic ${result.topic ?? values.ntfy_topic}.`;
      setNtfyTest(message);
      notify("success", message);
    } catch (caught) {
      const message = String(caught);
      setNtfyTest(message);
      notify("error", message);
    }
  };

  const testReader = async () => {
    setReaderTest("…");
    try {
      const result = await api.testReader(
        pickValues(["reader_kind", "reader_url", "reader_internal_url", "reader_api_key", "reader_username", "reader_password", "reader_library_path", "reader_series_url_template"]),
      );
      setReaderTest(result.ok ? `OK — ${result.detail}` : `Failed: ${result.error}`);
    } catch (caught) {
      setReaderTest(String(caught));
    }
  };

  const discoverReader = async () => {
    setReaderDiscovery("…");
    setReaderTest(null);
    try {
      const result = await api.discoverReader();
      const selected =
        result.matches.find((match) => match.kind === values.reader_kind) ??
        result.selected;
      if (!selected) {
        setReaderDiscovery(result.message);
        notify("info", result.message);
        return;
      }
      const discovered: Record<string, string> = {
        reader_kind: selected.kind,
        reader_url: browserRootForReader(selected),
        reader_internal_url: selected.internal_url,
      };
      if (selected.kind === "stump" && !values.reader_library_path?.trim()) {
        discovered.reader_library_path = "/data/comics";
      }
      const filled = Object.entries(discovered).filter(
        ([key, value]) => values[key] !== value,
      ).length;
      const needsSave = Object.entries(discovered).filter(([key, value]) => {
        const persisted = settings[key]?.value;
        const persistedValue =
          persisted === null || persisted === undefined ? "" : String(persisted);
        return persistedValue !== value;
      }).length;
      setValues((current) => ({ ...current, ...discovered }));
      const action = needsSave
        ? `${filled} field${filled === 1 ? "" : "s"} filled; review and save.`
        : filled
          ? `${filled} field${filled === 1 ? "" : "s"} restored; they match the saved settings, so no save is needed.`
          : "The form already matches; no save is needed.";
      const message = `${selected.detail}. ${action}`;
      setReaderDiscovery(message);
      notify("success", message);
    } catch (caught) {
      const message = String(caught);
      setReaderDiscovery(message);
      notify("error", message);
    }
  };

  const discard = () => {
    setValues(
      Object.fromEntries(
        Object.entries(settings).map(([key, view]) => [
          key,
          view.value === null || view.value === undefined ? "" : String(view.value),
        ]),
      ),
    );
  };

  return (
    <div className="page">
      <div className="toolbar">
        <h1 className="page-title">Settings</h1>
        <label className="setting-toggle settings-advanced-switch" title="Show rarely needed settings (intervals, ports, internal URLs). Remembered on this browser.">
          <input type="checkbox" checked={showAdvanced} onChange={toggleAdvanced} />
          <span>Show advanced</span>
        </label>
        <button
          type="button"
          className="btn btn-primary"
          disabled={saving || dirtyKeys.length === 0}
          onClick={() => void save()}
        >
          <Icon name="check" /> {saving ? "Saving…" : "Save Changes"}
        </button>
      </div>
      <p className="muted small">
        Values set here are stored in the database and override the container environment.
        Sensitive credentials are stored separately in <span className="mono">/config/metadata.env</span>
        {" "}with restricted permissions and are never returned unmasked. Clear a field to remove its override.
      </p>
      <div className="settings-layout">
        <label className="mobile-settings-section">Settings section
          <select className="input" aria-label="Settings section" value={activeTab} onChange={(event) => {
            const tab = event.target.value as SettingsTab;
            window.location.hash = `/settings?tab=${tab}`;
            setActiveTab(tab);
          }}>
            {TABS.map((tab) => <option key={tab.id} value={tab.id}>{tab.label}</option>)}
          </select>
        </label>
        <nav className="settings-nav" aria-label="Settings sections">
          {TABS.map((tab) => {
            const dirtyInTab = SECTIONS.filter((section) => section.group === tab.id).some((section) =>
              section.fields.some((field) => dirtyKeys.includes(field.key)),
            );
            return (
              <button
                key={tab.id}
                type="button"
                className={`settings-nav-item ${activeTab === tab.id ? "active" : ""}`}
                aria-current={activeTab === tab.id ? "page" : undefined}
                onClick={() => { window.location.hash = `/settings?tab=${tab.id}`; setActiveTab(tab.id); }}
              >
                <Icon name={tab.icon} size={16} /> <span>{tab.label}</span>
                {dirtyInTab ? <span className="settings-nav-dot" aria-label="Unsaved changes" /> : null}
              </button>
            );
          })}
        </nav>
        <div className="settings-content">
        {activeTab === "data" ? <Suspense fallback={<Spinner />}><BackupPanel /></Suspense> : null}
        {SECTIONS.filter((section) => section.group === activeTab).map((section) => (
          <section key={section.title} className="panel settings-section">
            <h2>{section.title}</h2>
            {section.description ? <p className="muted small setting-section-description">{section.description}</p> : null}
            {section.group === "translation" ? <p className="muted small" role="status">
              {values.translation_processor_url?.trim()
                ? "Processing: external service. Open Advanced settings to change it or clear its URL to use this computer."
                : "Processing: this computer. No processor URL or access token is required. Basic lettering uses white text boxes; complex pages may need an external processor in Advanced settings."}
            </p> : null}
            {section.fields.map((field) => {
              if (
                field.visibleWhen
                && (values[field.visibleWhen.key] ?? "") !== field.visibleWhen.equals
              ) return null;
              if (field.advanced && !showAdvanced) return null;
              const view = settings[field.key];
              if (!view) return null;
              const fieldId = `setting-${field.key}`;
              const hintId = field.hint ? `${fieldId}-hint` : undefined;
              return (
                <div key={field.key} className="form-row">
                  <label id={`${fieldId}-label`} htmlFor={fieldId}>
                    {field.label}{" "}

                  </label>
                  {field.kind === "language" ? (
                    <select
                      id={fieldId}
                      aria-describedby={hintId}
                      className="input"
                      value={values[field.key] ?? ""}
                      onChange={(event) =>
                        setValues((current) => ({ ...current, [field.key]: event.target.value }))
                      }
                    >
                      {LANGUAGES.filter(([code]) => selectedSearchLanguages.includes(code)).map(([code, label]) => (
                        <option key={code} value={code}>
                          {label}
                        </option>
                      ))}
                    </select>
                  ) : field.kind === "languages" ? (
                    <div className="language-checklist" role="group" aria-labelledby={`${fieldId}-label`} aria-describedby={hintId}>
                      {LANGUAGES.map(([code, label]) => {
                        const checked = selectedSearchLanguages.includes(code);
                        return (
                          <label key={code} className={`language-option ${checked ? "selected" : ""}`}>
                            <input
                              type="checkbox"
                              checked={checked}
                              onChange={(event) => toggleSearchLanguage(code, event.target.checked)}
                            />
                            <span>{label}</span>
                            <code>{code}</code>
                          </label>
                        );
                      })}
                    </div>
                  ) : field.kind === "suwayomi_sources" ? (
                    <CatalogSelector
                      id={fieldId}
                      labelId={`${fieldId}-label`}
                      hintId={hintId}
                      value={values[field.key] ?? ""}
                      options={suwayomiSourceOptions}
                      emptyMeansAll
                      emptyMessage="Run Test & load sources below to discover installed Suwayomi extensions."
                      onChange={(value) =>
                        setValues((current) => ({ ...current, [field.key]: value }))
                      }
                    />
                  ) : field.kind === "prowlarr_indexers" ? (
                    <CatalogSelector
                      id={fieldId}
                      labelId={`${fieldId}-label`}
                      hintId={hintId}
                      value={values[field.key] ?? ""}
                      options={prowlarrIndexerOptions}
                      emptyMeansAll
                      emptyMessage="Run Test & load indexers below to discover Prowlarr indexers."
                      onChange={(value) =>
                        setValues((current) => ({ ...current, [field.key]: value }))
                      }
                    />
                  ) : field.kind === "prowlarr_categories" ? (
                    <CatalogSelector
                      id={fieldId}
                      labelId={`${fieldId}-label`}
                      hintId={hintId}
                      value={values[field.key] ?? ""}
                      options={prowlarrCategoryOptions}
                      emptyMeansAll={false}
                      emptyMessage="Run Test & load indexers below to discover compatible categories."
                      selectLabel="Select recommended"
                      onChange={(value) =>
                        setValues((current) => ({ ...current, [field.key]: value }))
                      }
                    />
                  ) : field.kind === "select" ? (
                    <select
                      id={fieldId}
                      aria-describedby={hintId}
                      className="input"
                      value={values[field.key] ?? ""}
                      onChange={(event) =>
                        setValues((current) => ({ ...current, [field.key]: event.target.value }))
                      }
                    >
                      {(field.options ?? []).map(([value, label]) => (
                        <option key={value} value={value}>
                          {label}
                        </option>
                      ))}
                    </select>
                  ) : field.kind === "boolean" ? (
                    <label className="setting-toggle">
                      <input
                        id={fieldId}
                        aria-labelledby={`${fieldId}-label`}
                        aria-describedby={hintId}
                        type="checkbox"
                        disabled={field.readOnly}
                        checked={field.readOnly ? true : (values[field.key] ?? "false") === "true"}
                        onChange={(event) =>
                          setValues((current) => ({
                            ...current,
                            [field.key]: String(event.target.checked),
                          }))
                        }
                      />
                      <span>{(values[field.key] ?? "false") === "true" ? "Enabled" : "Disabled"}</span>
                    </label>
                  ) : (
                    <input
                      id={fieldId}
                      aria-describedby={hintId}
                      className="input"
                      type={field.kind === "password" ? "password" : field.kind}
                      value={field.displayDivisor && values[field.key] !== "" && values[field.key] !== undefined ? Number(values[field.key]) / field.displayDivisor : values[field.key] ?? ""}
                      autoComplete={field.kind === "password" && field.key.startsWith("translation_") ? "new-password" : "off"}
                      data-lpignore={field.key.startsWith("translation_") ? "true" : undefined}
                      data-1p-ignore={field.key.startsWith("translation_") ? "true" : undefined}
                      onChange={(event) =>
                        setValues((current) => ({ ...current, [field.key]: field.displayDivisor && event.target.value !== "" ? String(Number(event.target.value) * field.displayDivisor) : event.target.value }))
                      }
                    />
                  )}
                  {field.hint ? <p id={hintId} className="muted small">{field.hint}</p> : null}
                  {field.helpUrl || field.testSource ? (
                    <div className="setting-field-actions">
                      {field.helpUrl ? (
                        <a
                          className="btn btn-small"
                          href={field.helpUrl}
                          target="_blank"
                          rel="noreferrer"
                        >
                          <Icon name="external" size={14} /> {field.helpLabel ?? "Instructions"}
                        </a>
                      ) : null}
                      {field.testSource ? (
                        <button
                          type="button"
                          className="btn btn-small"
                          disabled={metadataTests[field.testSource] === "…"}
                          onClick={() => void testMetadata(field.testSource!, field.key)}
                        >
                          <Icon name="check" size={14} /> {field.kind === "boolean" ? "Test connection" : "Test current value"}
                        </button>
                      ) : null}
                      {field.testSource && metadataTests[field.testSource] ? (
                        <p className="muted small setting-test-result">
                          {metadataTests[field.testSource]}
                        </p>
                      ) : null}
                    </div>
                  ) : null}
                </div>
              );
            })}
            {section.providerTest ? (
              <div className="form-row setting-test-row">
                <button
                  type="button"
                  className="btn btn-small"
                  disabled={providerTests[section.providerTest.name] === "…"}
                  onClick={() => void testProvider(
                    section.providerTest!.name,
                    section.providerTest!.keys,
                  )}
                >
                  <Icon name="check" size={14} /> {section.providerTest.label}
                </button>
                {providerTests[section.providerTest.name] ? (
                  <p className="muted small setting-test-result">
                    {providerTests[section.providerTest.name]}
                  </p>
                ) : null}
              </div>
            ) : null}
            {section.title === "Reader" ? (
              <div className="form-row setting-test-row reader-actions">
                <button
                  type="button"
                  className="btn btn-small reader-discovery-button"
                  disabled={readerDiscovery === "…" || readerTest === "…"}
                  onClick={() => void discoverReader()}
                >
                  <Icon name="search" size={14} /> Discover reader
                </button>
                <button
                  type="button"
                  className="btn btn-small"
                  disabled={readerTest === "…" || readerDiscovery === "…"}
                  onClick={() => void testReader()}
                >
                  <Icon name="check" size={14} /> Test reader connection
                </button>
                {readerDiscovery || readerTest ? (
                  <p className="muted small setting-test-result" aria-live="polite">
                    {readerDiscovery === "…"
                      ? "Searching configured and local Docker routes…"
                      : readerDiscovery ?? readerTest}
                  </p>
                ) : null}
              </div>
            ) : null}
            {section.title === "Usenet client (SABnzbd)" ? (
              <div className="form-row setting-test-row">
                <button type="button" className="btn btn-small" disabled={sabTest === "…"} onClick={() => void testSab()}>
                  <Icon name="check" size={14} /> Test connection
                </button>
                {sabTest ? <p className="muted small setting-test-result">{sabTest}</p> : null}
              </div>
            ) : null}
            {section.title === "Torrent client (qBittorrent)" ? (
              <div className="form-row setting-test-row">
                <button
                  type="button"
                  className="btn btn-small"
                  disabled={qbitTest === "…"}
                  onClick={() => void testQbit()}
                >
                  <Icon name="check" size={14} /> Test connection
                </button>
                {qbitTest ? <p className="muted small setting-test-result">{qbitTest}</p> : null}
              </div>
            ) : null}
            {section.title === "Suwayomi" ? (
              <SuwayomiManager
                mode={values.suwayomi_mode ?? "managed"}
                publicUrl={
                  values.suwayomi_public_url?.trim() ||
                  ((values.suwayomi_mode ?? "managed") === "external"
                    ? values.suwayomi_url ?? ""
                    : `${window.location.protocol}//${window.location.hostname}:4567`)
                }
                languages={(values.search_languages ?? "en")
                  .split(",")
                  .map((code) => code.trim().toLowerCase())
                  .filter(Boolean)}
              />
            ) : null}
            {section.internetArchiveTest ? (
              <div className="form-row setting-test-row">
                <button
                  type="button"
                  className="btn btn-small"
                  disabled={internetArchiveTest === "…"}
                  onClick={() => void testInternetArchive(section.internetArchiveTest!.keys)}
                >
                  <Icon name="check" size={14} /> Test archive.org
                </button>
                {internetArchiveTest ? (
                  <p className="muted small setting-test-result">{internetArchiveTest}</p>
                ) : null}
              </div>
            ) : null}
            {section.prowlarrTest ? (
              <div className="form-row setting-test-row">
                <button
                  type="button"
                  className="btn btn-small"
                  disabled={prowlarrTest === "…"}
                  onClick={() => void testProwlarr(section.prowlarrTest!.keys)}
                >
                  <Icon name="check" size={14} /> Test & load indexers
                </button>
                {prowlarrTest ? (
                  <p className="muted small setting-test-result">{prowlarrTest}</p>
                ) : null}
              </div>
            ) : null}
            {section.komgaTest ? (
              <div className="form-row setting-test-row">
                <button
                  type="button"
                  className="btn btn-small"
                  disabled={komgaTest === "…"}
                  onClick={() => void testKomga(section.komgaTest!.keys)}
                >
                  <Icon name="check" size={14} /> Test connection
                </button>
                {values.komga_url?.trim() ? (
                  <a className="btn btn-small" href={values.komga_url.trim()} target="_blank" rel="noreferrer">
                    <Icon name="external" size={14} /> Open Komga
                  </a>
                ) : null}
                {komgaTest ? (
                  <p className="muted small setting-test-result">{komgaTest}</p>
                ) : null}
              </div>
            ) : null}
            {section.title === "Notifications (ntfy)" ? (
              <div className="form-row setting-test-row">
                <button
                  type="button"
                  className="btn btn-small"
                  disabled={ntfyTest === "…"}
                  onClick={() => void testNtfy()}
                >
                  <Icon name="check" size={14} /> Send test notification
                </button>
                {ntfyTest ? (
                  <p className="muted small setting-test-result">{ntfyTest}</p>
                ) : null}
              </div>
            ) : null}
          </section>
        ))}
        </div>
      </div>
      {dirtyKeys.length ? (
        <div className="settings-save-bar" role="status">
          <span>
            <strong>{dirtyKeys.length}</strong> unsaved change{dirtyKeys.length === 1 ? "" : "s"}
          </span>
          <div className="toolbar-group">
            <button type="button" className="btn" disabled={saving} onClick={discard}>
              Discard
            </button>
            <button type="button" className="btn btn-primary" disabled={saving} onClick={() => void save()}>
              <Icon name="check" /> {saving ? "Saving…" : "Apply Changes"}
            </button>
          </div>
        </div>
      ) : null}
    </div>
  );
}
