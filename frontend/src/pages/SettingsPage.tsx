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
import { LOCALES, browserLocale, msg, saveLocale, storedLocale, t, tn, type LocaleChoice } from "../i18n";

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

/** The interface language is a browser preference, not a server setting: each
 * browser keeps its own choice and the page reloads so every module sees one
 * catalogue for its whole life. */
function InterfaceLanguageSection() {
  const [choice, setChoice] = useState<LocaleChoice>(() => storedLocale());
  const browserLanguage = LOCALES.find((item) => item.code === browserLocale())?.name ?? "English";
  const change = (next: LocaleChoice) => {
    setChoice(next);
    saveLocale(next);
    window.location.reload();
  };
  return (
    <section className="panel settings-section">
      <h2>{t("Interface language")}</h2>
      <p className="muted small setting-section-description">
        {t("Stored in this browser only: every browser and device keeps its own choice. Messages from the server, log lines and file names stay in English.")}
      </p>
      <div className="form-row">
        <label htmlFor="setting-interface-language">{t("Language")}</label>
        <select
          id="setting-interface-language"
          className="input"
          value={choice}
          onChange={(event) => change(event.target.value as LocaleChoice)}
        >
          <option value="auto">{t("Automatic (browser language: {language})", { language: browserLanguage })}</option>
          {LOCALES.map((item) => (
            <option key={item.code} value={item.code}>
              {item.name}
            </option>
          ))}
        </select>
      </div>
    </section>
  );
}

const TABS: { id: SettingsTab; label: string; icon: "settings" | "search" | "download" | "external" | "library" | "alert" | "gears" }[] = [
  { id: "general", label: msg("General"), icon: "settings" },
  { id: "sources", label: msg("Sources"), icon: "search" },
  { id: "translation", label: msg("Translation"), icon: "library" },
  { id: "indexers", label: msg("Indexers & torrents"), icon: "download" },
  { id: "reader", label: msg("Reader"), icon: "external" },
  { id: "metadata", label: msg("Metadata"), icon: "library" },
  { id: "notifications", label: msg("Notifications"), icon: "alert" },
  { id: "security", label: msg("Security"), icon: "gears" },
  { id: "data", label: msg("Data"), icon: "gears" },
];

type SectionDef = {
  title: string;
  group: SettingsTab;
  description?: string;
  /** A notification channel with its own "Send test notification" button. */
  notificationChannel?: string;
  fields: FieldDef[];
  providerTest?: {
    name: DownloadProvider;
    label: string;
    keys: string[];
  };
  prowlarrTest?: { keys: string[] };
  internetArchiveTest?: { keys: string[] };
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
  selectLabel = t("Select available"),
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
          {automatic ? t("Automatic selection") : t("{count} selected", { count: selected.size })}
        </span>
        <div className="toolbar-group">
          {emptyMeansAll ? (
            <button type="button" className="btn btn-small" onClick={() => onChange("")}>
              {t("Use all automatically")}
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
    title: msg("Translation fallback"),
    group: "translation",
    description: msg("Translate missing books or chapters from another language. Tankarr tries the series language first. Enable fallback here and separately in each series, then enter your AI provider settings. By default, basic OCR and lettering run on this computer, one job at a time. Docker includes the required tools; other installations need Tesseract and DejaVu fonts. The AI provider may charge for translation. Existing files remain available when fallback is switched off."),
    fields: [
      { key: "translation_enabled", label: msg("Enable translation fallback"), kind: "boolean" },
      { key: "translation_source_languages", label: msg("Source languages, in order"), kind: "text", hint: msg("Use language codes separated by commas, for example original,ja,en,fr. Original uses the work’s original language when known. The requested language is skipped.") },
      { key: "translation_ai_url", label: msg("AI provider API base URL"), kind: "url", hint: msg("A provider with an OpenAI-compatible chat API, including its API path when required.") },
      { key: "translation_ai_model", label: msg("AI model"), kind: "text", hint: msg("Model identifier supplied by your AI provider.") },
      { key: "translation_ai_api_key", label: msg("AI provider API key"), kind: "password", hint: msg("Used to translate recognized dialogue with your provider. If an external processor is configured, it also receives this key. Stored with Tankarr’s other integration secrets.") },
      { key: "translation_processor_url", label: msg("External processor URL (optional)"), kind: "url", advanced: true, hint: msg("Leave empty to process on this computer. For advanced OCR and lettering, enter the HTTP(S) address of a compatible external processor. This overrides local processing.") },
      { key: "translation_processor_token", label: msg("External processor access token (optional)"), kind: "password", advanced: true, hint: msg("Only needed if your external processor requires authentication. This is not your AI provider key and is unused for local processing.") },
    ],
  },
  {
    title: msg("Retention"),
    group: "data",
    fields: [
      { key: "backup_retention_count", label: msg("Backups to keep"), kind: "number", hint: msg("Number of automatic recovery bundles to retain (default 7).") },
      { key: "recycle_bin_retention_days", label: msg("Recycle bin retention (days)"), kind: "number", hint: msg("Retired files are retained for this many days before nightly cleanup (default 7).") },
    ],
  },
  {
    title: msg("Security"),
    group: "security",
    fields: [
      {
        key: "auth_method",
        label: msg("Authentication method"),
        hint: msg("Forms shows the Tankarr login page. Basic uses the browser's credential prompt and remains available to API clients. External trusts a reverse proxy (Authelia, Authentik, Caddy forward_auth) that signs users in: requests are accepted from the trusted proxies only."),
        kind: "select",
        options: [
          ["forms", msg("Forms (Login Page)")],
          ["basic", msg("Basic (Browser Prompt)")],
          ["external", msg("External (Reverse Proxy)")],
        ],
      },
      {
        key: "auth_trusted_proxies",
        label: msg("Trusted proxies"),
        hint: msg("Comma-separated addresses or networks of your reverse proxy, for example 172.18.0.2 or 10.0.0.0/8. Required by External; also tells Tankarr which forwarded client address to believe."),
        kind: "text",
      },
      {
        key: "auth_required_for_local",
        label: msg("Require a login on the local network"),
        hint: msg("Off: requests from private and loopback addresses skip the login, like \"Disabled for local addresses\" in the other *arr applications. Behind a reverse proxy, set Trusted proxies first; forwarding headers from an untrusted address never count as local."),
        kind: "boolean",
      },
      {
        key: "auth_username",
        label: msg("Username"),
        hint: msg("Changing the username signs out existing browser sessions."),
        kind: "text",
      },
      {
        key: "auth_password",
        label: msg("Password"),
        hint: msg("At least 8 characters. Leave the masked value unchanged to keep the current password."),
        kind: "password",
      },
    ],
  },
  {
    title: msg("General"),
    group: "general",
    fields: [
      {
        key: "search_languages",
        label: msg("Search languages"),
        hint: msg("Choose which translation languages appear when adding a series. English is enabled by default."),
        kind: "languages",
      },
      { key: "default_language", label: msg("Default language"), kind: "language" },
      {
        key: "monitor_enabled",
        label: msg("Release monitoring"),
        hint: msg("Poll monitored remote series for newly published chapters."),
        kind: "boolean",
      },
      {
        key: "log_level",
        advanced: true,
        label: msg("Log level"),
        hint: msg("Applies at once to the console and the log file (System → Logs). Debug is verbose: use it while investigating a problem, then go back to info."),
        kind: "select",
        options: [
          ["debug", msg("Debug")],
          ["info", msg("Info")],
          ["warning", msg("Warning")],
          ["error", msg("Error")],
        ],
      },
      {
        key: "monitor_interval_seconds",
        advanced: true,
        label: msg("Monitor interval (seconds)"),
        hint: msg("How often monitored series are checked for new releases (min 60)."),
        kind: "number",
      },
      {
        key: "wanted_search_enabled",
        label: msg("Scheduled Wanted recovery"),
        hint: msg("Periodically retry monitored releases that are still missing from the library."),
        kind: "boolean",
      },
      {
        key: "wanted_search_interval_seconds",
        advanced: true,
        label: msg("Wanted recovery interval (seconds)"),
        hint: msg("Separate from release monitoring. Default 21600 means every 6 hours (min 900)."),
        kind: "number",
      },
      {
        key: "download_concurrency",
        advanced: true,
        label: msg("Maximum page concurrency"),
        hint: msg("Adaptive ceiling (1–12). Tankarr uses latency, source errors, memory pressure and temperature to choose the effective value."),
        kind: "number",
      },
      {
        key: "download_pipeline_max",
        advanced: true,
        label: msg("Maximum simultaneous chapters"),
        hint: msg("0 is automatic. Tankarr increases or reduces parallel series from live CPU, working-set memory and source/network pressure; a positive value only sets a ceiling."),
        kind: "number",
      },
      {
        key: "import_max_expanded_bytes",
        advanced: true,
        label: msg("Expanded archive size limit (MiB)"),
        hint: msg("Maximum uncompressed size accepted for an import. 1024 MiB = 1 GiB; the default 16384 MiB is 16 GiB. Existing books are not changed."),
        kind: "number",
        displayDivisor: 1024 * 1024,
      },
      {
        key: "import_max_pages",
        advanced: true,
        label: msg("Maximum pages per import"),
        hint: msg("Reject oversized imports before accepting more than this number of pages. Default: 20000 pages."),
        kind: "number",
      },
      {
        key: "import_subprocess_memory_mb",
        advanced: true,
        label: msg("Import subprocess memory limit (MiB)"),
        hint: msg("Memory ceiling for an import subprocess, not a new concurrency target. Default: 1024 MiB (1 GiB)."),
        kind: "number",
      },
      {
        key: "import_disk_reserve_bytes",
        advanced: true,
        label: msg("Import free-space reserve (MiB)"),
        hint: msg("Keep this much disk space in reserve when estimating expansion and packaging. Default: 512 MiB. Imports may be refused when space is insufficient."),
        kind: "number",
        displayDivisor: 1024 * 1024,
      },
    ],
  },
  {
    title: msg("Download sources"),
    group: "sources",
    description: msg("Works are added from the MangaBaka catalogue; chapters come from the Suwayomi sources and Prowlarr indexers. Choose a source preference profile; automatic selection follows it for every matching chapter. Optional source lists let you refine the order."),
    fields: [
      {
        key: "release_acquisition_policy",
        label: msg("New release policy"),
        hint: msg("This chooses the file only after every candidate has been mapped to the same canonical chapter. It never changes numbering or calendar dates."),
        kind: "select",
        options: [
          ["prefer_official", msg("Prefer official when available")],
          ["first_available", msg("First available")],
          ["official_only", msg("Official only")],
        ],
      },
      {
        key: "duplicate_cleanup_enabled",
        label: msg("Remove duplicate chapter files automatically"),
        hint: msg("When the volume↔chapter map proves a chapter file's content is inside a book already on disk, the chapter file is quarantined and removed on the monitor's cycle (up to 10 series per cycle). Off: duplicates are only reported on the series page."),
        kind: "boolean",
      },
      {
        key: "release_preference_profile",
        label: msg("Source preference profile"),
        hint: msg("Balanced keeps the normal new-release/backlog ordering. Official prioritizes publishers; curated prioritizes curated scans. Language and numbering rules always apply. Explicit source lists take precedence."),
        kind: "select",
        options: [["balanced", msg("Balanced")], ["official", msg("Official sources")], ["curated", msg("Curated scans")]],
      },
      {
        key: "source_priority_fresh", label: msg("Preferred sources for new chapters"),
        hint: msg("Optional ordered source names, separated by commas, for example suwayomi:mangaplus,suwayomi:weebcentral. Empty uses the profile."), kind: "text",
      },
      {
        key: "source_priority_backfill", label: msg("Preferred sources for backlog and upgrades"),
        hint: msg("Optional ordered source names. Unlisted sources remain available as fallbacks."), kind: "text",
      },
      {
        key: "source_upgrade_enabled", label: msg("Upgrade to preferred sources automatically"),
        hint: msg("Replace an owned chapter only with a strictly preferred source in the same language and numbering. The old file stays until the replacement is verified. Equal preferences, age and size never trigger replacement. When enabled, this profile also governs official upgrades."), kind: "boolean",
      },
      {
        key: "official_upgrade_enabled",
        label: msg("Upgrade to official releases"),
        hint: msg("Independent from the acquisition policy: replace a non-official file when the same canonical chapter later appears on the publisher platform (up to 20 per series per cycle)."),
        kind: "boolean",
      },
      {
        key: "preferred_unit",
        label: msg("Preferred unit"),
        hint: msg("Tankarr follows whatever the sources can deliver in full: chapters or whole books. When both can complete a finished work, this preference decides; a running work follows chapters unless only books exist."),
        kind: "select",
        options: [
          ["volumes", msg("Whole volumes (books)")],
          ["chapters", msg("Chapters")],
        ],
      },
      {
        key: "special_chapters_outside_books",
        label: msg("Show specials no book contains"),
        hint: msg("A half chapter, an omake, or a second cut of a story a source published on its own: content that was never bound into a volume. Off by default, so a shelf of books carries no rows for it. Either way it is never counted as missing."),
        kind: "boolean",
      },
    ],
  },
  {
    title: msg("Suwayomi"),
    group: "sources",
    description: msg("Suwayomi is Tankarr's source engine: it runs Mihon/Tachiyomi-compatible extensions from the repository you configure, so Tankarr never re-implements a manga site. In Managed mode Tankarr installs and supervises the official server inside its own container; Tankarr keeps owning monitoring, downloads, CBZ creation, and library import."),
    fields: [
      {
        key: "suwayomi_enabled",
        label: msg("Enabled"),
        hint: msg("Disabling stops Suwayomi searches and downloads immediately. Existing Suwayomi series cannot refresh or download until it is enabled again."),
        kind: "boolean",
      },
      {
        key: "suwayomi_mode",
        label: msg("Mode"),
        hint: msg("Managed: Tankarr downloads, verifies and runs the official Suwayomi-Server JAR inside its own container. External: connect to a Suwayomi server you run yourself."),
        kind: "select",
        options: [
          ["managed", msg("Managed by Tankarr (recommended)")],
          ["external", msg("External server")],
        ],
      },
      {
        key: "suwayomi_managed_heap_mb",
        advanced: true,
        label: msg("Java heap (MiB)"),
        hint: msg("Maximum heap for the managed server (128–1024). With the full language catalogue installed, 512 MiB is a safer floor; raise it if the log shows OutOfMemoryError."),
        kind: "number",
        visibleWhen: { key: "suwayomi_mode", equals: "managed" },
      },
      {
        key: "suwayomi_extension_store",
        label: msg("Extension repository"),
        hint: msg("Index URL of a Mihon/Tachiyomi-compatible extension repository, usually ending in index.min.json. Tankarr ships no repository and recommends none: enter the one you trust. Applied when the managed server restarts."),
        kind: "url",
        visibleWhen: { key: "suwayomi_mode", equals: "managed" },
      },
      {
        key: "suwayomi_url",
        label: msg("Server URL"),
        hint: msg("Internal service root, for example http://suwayomi:4567. Do not append /api/graphql."),
        kind: "text",
        visibleWhen: { key: "suwayomi_mode", equals: "external" },
      },
      {
        key: "suwayomi_username",
        label: msg("Username"),
        hint: msg("The login already configured in Suwayomi. Changing this does not create or modify a Suwayomi account."),
        kind: "text",
        visibleWhen: { key: "suwayomi_mode", equals: "external" },
      },
      {
        key: "suwayomi_password",
        label: msg("Password"),
        hint: msg("Stored in /config/metadata.env with mode 0600 and returned only as a masked value."),
        kind: "password",
        visibleWhen: { key: "suwayomi_mode", equals: "external" },
      },
      {
        key: "suwayomi_auto_update",
        label: msg("Automatic server updates"),
        hint: msg("Tankarr installs the Suwayomi-Server release it was tested with, verified against a digest kept in its own source. The daily check still reports newer releases; switch this on to install them automatically, or use Update in the panel below when you decide."),
        kind: "boolean",
        visibleWhen: { key: "suwayomi_mode", equals: "managed" },
      },
      {
        key: "suwayomi_auto_install_official",
        label: msg("Auto-install official extensions"),
        hint: msg("When a work's catalogue record names a free official platform (MANGA Plus, WEBTOON, Tapas, Comikey, Manga UP!), Tankarr installs its extension in the managed runtime and maps it as a download source."),
        kind: "boolean",
        visibleWhen: { key: "suwayomi_mode", equals: "managed" },
      },
      {
        key: "suwayomi_source_ids",
        label: msg("Sources"),
        hint: msg("Run Test & load sources, then choose installed extensions here. Automatic selection means every safe extension matching Tankarr's enabled languages, including compatible sources installed later. A manual selection stores Suwayomi's stable numeric IDs; no source-name allowlist is needed."),
        kind: "suwayomi_sources",
      },
    ],
    providerTest: {
      name: "suwayomi",
      label: msg("Test & load sources"),
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
    title: msg("Internet Archive (direct download)"),
    group: "indexers",
    description: msg("archive.org holds whole volumes as CBZ, CBR and PDF. Tankarr searches it by the work's title, once per pass and never chapter by chapter, downloads the file itself (no torrent client) and passes it through the same identity, language and page-quality gates as any release. Ranked below every indexer: the fallback that closes \"Not obtainable\" verdicts on out-of-print works."),
    fields: [
      {
        key: "internet_archive_enabled",
        label: msg("Enabled"),
        hint: msg("Include archive.org in Series release search and the Wanted book search."),
        kind: "boolean",
      },
    ],
    internetArchiveTest: { keys: ["internet_archive_enabled"] },
  },
  {
    title: msg("Prowlarr indexers"),
    group: "indexers",
    description: msg("Prowlarr is used by Interactive Search on series and Wanted, and by the automatic Wanted recovery, which grabs only unambiguous matches and asks on System > To confirm otherwise. The exact release is resolved through Prowlarr, verified by info hash, and sent to qBittorrent or SABnzbd."),
    fields: [
      {
        key: "prowlarr_enabled",
        label: msg("Enabled"),
        hint: msg("Enables Prowlarr for interactive release searches and for the automatic Wanted recovery."),
        kind: "boolean",
      },
      {
        key: "prowlarr_url",
        label: msg("Server URL"),
        hint: msg("Internal service root, for example http://prowlarr:9696."),
        kind: "text",
      },
      {
        key: "prowlarr_api_key",
        label: msg("API key"),
        hint: msg("Copy it from Prowlarr > Settings > General > Security. It is stored in /config/metadata.env with mode 0600 and is never returned unmasked."),
        kind: "password",
      },
      {
        key: "prowlarr_indexer_ids",
        label: msg("Indexers"),
        hint: msg("Automatic selection uses every enabled Prowlarr indexer that advertises book, literature, manga, or comic categories."),
        kind: "prowlarr_indexers",
      },
      {
        key: "prowlarr_categories",
        label: msg("Categories"),
        hint: msg("Only these Prowlarr categories will be eligible for Tankarr searches. At least one category must remain selected."),
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
    title: msg("Metadata catalogues"),
    group: "metadata",
    description: msg("MangaBaka is the only manga catalogue Tankarr queries: it identifies the work and already aggregates MangaUpdates, AniList, MAL and Kitsu. The calendar comes from the mapped official platform's own release dates."),
    fields: [
      {
        key: "metadata_enabled",
        label: msg("MangaBaka · Always on"),
        hint: msg("The whole metadata spine: identity, counts, description, official links and the MangaUpdates/AniList ids and ratings it aggregates. Use the test to check it is reachable."),
        testSource: "mangabaka",
        kind: "boolean",
        readOnly: true,
      },
      {
        key: "metadata_refresh_interval_hours",
        advanced: true,
        label: msg("Refresh interval (hours)"),
        hint: msg("New series are discovered promptly; existing source records are queried again only after this interval. 168 hours means weekly."),
        kind: "number",
      },
    ],
  },
  {
    title: msg("Reader"),
    group: "reader",
    description: msg("The built-in reader opens Tankarr's CBZ files directly and stores progress locally, with no second library or metadata scan. External readers remain optional."),
    fields: [
      {
        key: "reader_kind",
        label: msg("Reader"),
        hint: msg("Tankarr is ready immediately and keeps reading progress here. External readers maintain their own library and may require synchronization."),
        kind: "select",
        options: [
          ["tankarr", msg("Tankarr (built-in)")],
          ["auto", msg("Automatic")],
          ["komga", msg("Komga")],
          ["kavita", msg("Kavita")],
          ["stump", msg("Stump")],
          ["url", msg("URL template")],
          ["none", msg("No shortcut")],
        ],
      },
      {
        key: "reader_display_mode",
        label: msg("Default reading mode"),
        hint: msg("Automatic uses series metadata and page shape. Manga turns pages from right to left; webtoons scroll vertically."),
        kind: "select",
        options: [
          ["auto", msg("Automatic")],
          ["manga", msg("Manga (right to left)")],
          ["webtoon", msg("Webtoon (vertical)")],
        ],
      },
      {
        key: "reader_url",
        label: msg("Reader URL"),
        hint: msg("Browser-facing root of the reader, for example http://nas.local:5000 (Kavita) or http://nas.local:10801 (Stump)."),
        kind: "text",
        visibleWhen: { key: "reader_kind", equals: "kavita" },
      },
      {
        key: "reader_url",
        label: msg("Komga URL"),
        hint: msg("Browser-facing root of Komga, for example http://nas.local:25600. With an API key Tankarr also keeps Komga aligned (targeted scans, titles, covers) — automatically, nothing else to configure."),
        kind: "text",
        visibleWhen: { key: "reader_kind", equals: "komga" },
      },
      {
        key: "reader_api_key",
        label: msg("Komga API key"),
        hint: msg("From Komga → Account → API keys. Stored in /config/metadata.env with mode 0600."),
        kind: "password",
        visibleWhen: { key: "reader_kind", equals: "komga" },
      },
      {
        key: "reader_url",
        label: msg("Stump URL"),
        hint: msg("Browser-facing root of Stump, for example http://nas.local:10801."),
        kind: "text",
        visibleWhen: { key: "reader_kind", equals: "stump" },
      },
      {
        key: "reader_username",
        label: msg("Stump username"),
        hint: msg("A Stump account; Tankarr reads series and book ids to build Open and Read links."),
        kind: "text",
        visibleWhen: { key: "reader_kind", equals: "stump" },
      },
      {
        key: "reader_password",
        label: msg("Stump password"),
        hint: msg("Stored in /config/metadata.env with mode 0600."),
        kind: "password",
        visibleWhen: { key: "reader_kind", equals: "stump" },
      },
      {
        key: "reader_internal_url",
        label: msg("Reader URL from inside Tankarr"),
        hint: msg("Only if Tankarr cannot reach the browser URL from its container (typical with Docker): the service name, e.g. http://stump:10801 or http://kavita:5000."),
        kind: "text",
        advanced: true,
      },
      {
        key: "reader_library_path",
        label: msg("Library path inside Stump"),
        hint: msg("How Stump sees Tankarr's library folder (the container mount), e.g. /data/comics. Series and books are resolved by exact folder and file paths, never by title."),
        kind: "text",
        advanced: true,
        visibleWhen: { key: "reader_kind", equals: "stump" },
      },
      {
        key: "reader_api_key",
        label: msg("Kavita API key"),
        hint: msg("From Kavita → Settings → Account → API key. Stored in /config/metadata.env with mode 0600."),
        kind: "password",
        visibleWhen: { key: "reader_kind", equals: "kavita" },
      },
      {
        key: "reader_series_url_template",
        label: msg("Series URL template"),
        hint: msg("Series shortcut only. For example https://reader.local/search?q={title} · placeholders: {title}, {title_raw}, {folder}, {id}."),
        kind: "text",
        visibleWhen: { key: "reader_kind", equals: "url" },
      },
    ],
  },
  {
    title: msg("Torrent client (qBittorrent)"),
    group: "indexers",
    description: msg("Used only for torrent releases found through Prowlarr: qBittorrent downloads them, Tankarr imports the files into the library once validated and leaves the torrent seeding under qBittorrent's own rules (it is removed only when you discard it from Activity). Chapters from Suwayomi never go through qBittorrent."),
    fields: [
      {
        key: "torrent_auto_import",
        label: msg("Automatic import"),
        hint: msg("Import completed comic archives only after numbering and OCR language checks pass."),
        kind: "boolean",
      },
      {
        key: "torrent_completed_action",
        label: msg("After import"),
        hint: msg("Keep seeding leaves the torrent to qBittorrent's ratio/time rules. Remove after import deletes torrent and files as soon as the books are in the library. Remove when seeding is done waits until qBittorrent pauses the torrent at its limits, then removes it. Torrents in Tankarr's category that no job references are removed after the grace period below."),
        kind: "select",
        options: [
          ["seed", msg("Keep seeding")],
          ["remove_after_import", msg("Remove after import")],
          ["remove_when_seeded", msg("Remove when seeding is done")],
        ],
      },
      {
        key: "torrent_orphan_grace_hours",
        advanced: true,
        label: msg("Orphan grace period (hours)"),
        hint: msg("A torrent Tankarr added whose download was deleted afterwards is removed with its files after this many hours. Torrents Tankarr did not add are never removed."),
        kind: "number",
      },
      {
        key: "qbittorrent_url",
        label: msg("qBittorrent URL"),
        hint: msg("Internal service URL, for example http://qbittorrent:8080."),
        kind: "text",
      },
      {
        key: "qbittorrent_public_url",
        advanced: true,
        label: msg("qBittorrent link (browser)"),
        hint: msg("The address a browser on your network can open, for example http://nas.local:8080. Powers the shortcut on each torrent in Activity; leave blank to hide it."),
        kind: "text",
      },
      { key: "qbittorrent_username", label: msg("Username"), kind: "text" },
      { key: "qbittorrent_password", label: msg("Password"), kind: "password" },
      {
        key: "qbittorrent_category",
        advanced: true,
        label: msg("Category"),
        hint: msg("Tankarr owns every torrent in this qBittorrent category."),
        kind: "text",
      },
    ],
  },
  {
    title: msg("Usenet client (SABnzbd)"),
    group: "indexers",
    description: msg("NZB releases found through Prowlarr (official digital volumes are common on Usenet): SABnzbd downloads them under Tankarr's category, Tankarr imports the books and removes the entry unless \"Keep seeding\" is selected above. The default paths follow the common /data/downloads layout."),
    fields: [
      {
        key: "sabnzbd_url",
        label: msg("SABnzbd URL"),
        hint: msg("Internal service URL, for example http://sabnzbd:8080."),
        kind: "text",
      },
      {
        key: "sabnzbd_public_url",
        advanced: true,
        label: msg("SABnzbd link (browser)"),
        hint: msg("The address a browser on your network can open, for example http://nas.local:8080. Powers the shortcut on each Usenet download in Activity; leave blank to hide it."),
        kind: "text",
      },
      { key: "sabnzbd_api_key", label: msg("API key"), hint: msg("SABnzbd → Config → General → API Key."), kind: "password" },
      {
        key: "sabnzbd_category",
        advanced: true,
        label: msg("Category"),
        hint: msg("Tankarr owns every download in this SABnzbd category."),
        kind: "text",
      },
      {
        key: "sabnzbd_complete_path",
        advanced: true,
        label: msg("Completed folder (as SABnzbd sees it)"),
        hint: msg("SABnzbd's complete_dir, as SABnzbd sees it. Mount the same folder read-only into Tankarr and set TANKARR_USENET_DOWNLOAD_DIR to that mount."),
        kind: "text",
      },
    ],
  },
  {
    title: msg("Notifications (events)"),
    group: "notifications",
    description: msg("Notify after a verified import, a failed download, or a new decision that needs your review. Imports and decisions use normal priority; failures use high priority. Routine queue activity and searches stay quiet. These switches apply to every channel below; each channel's test sends immediately using the values shown, even before you save them."),
    fields: [
      {
        key: "ntfy_on_chapter_imported",
        label: msg("On chapter imported"),
        hint: msg("Notify after the validated CBZ has been written to the library successfully."),
        kind: "boolean",
      },
      {
        key: "ntfy_on_download_failed",
        label: msg("On download failed"),
        hint: msg("Notify with high priority when a Tankarr download job reaches Failed."),
        kind: "boolean",
      },
      {
        key: "ntfy_on_decision_needed",
        label: msg("On decision needed"),
        hint: msg("Notify once for each new match review and each Wanted item every channel has given up on (\"Not obtainable\"). Tankarr never deletes or ignores anything on its own."),
        kind: "boolean",
      },
    ],
  },
  {
    title: msg("Notifications (ntfy)"),
    group: "notifications",
    notificationChannel: "ntfy",
    description: msg("Push notifications through a public or self-hosted ntfy server."),
    fields: [
      {
        key: "ntfy_url",
        label: msg("ntfy URL"),
        hint: msg("Server root, for example https://ntfy.sh or your own ntfy server. On iOS, this must exactly match the ntfy app's Default Server."),
        kind: "text",
      },
      {
        key: "ntfy_topic",
        label: msg("Topic"),
        hint: msg("Subscribe to this exact topic on the configured server. URL and topic together enable ntfy."),
        kind: "text",
      },
    ],
  },
  {
    title: msg("Notifications (webhook)"),
    group: "notifications",
    notificationChannel: "webhook",
    description: msg("One JSON document per event (event, title, message, priority, tags, data), by POST, for Home Assistant, n8n or a script of your own."),
    fields: [
      {
        key: "webhook_url",
        label: msg("Webhook URL"),
        hint: msg("Address that receives the POST requests. Empty disables the webhook."),
        kind: "text",
      },
      {
        key: "webhook_token",
        label: msg("Bearer token"),
        hint: msg("Optional. Sent as Authorization: Bearer <token> with every request."),
        kind: "password",
      },
    ],
  },
  {
    title: msg("Notifications (Discord)"),
    group: "notifications",
    notificationChannel: "discord",
    description: msg("Events arrive as embeds in a Discord channel, red for failures."),
    fields: [
      {
        key: "discord_webhook_url",
        label: msg("Discord webhook URL"),
        hint: msg("Channel settings → Integrations → Webhooks → New webhook. The URL holds the webhook's secret, so it is stored like a password."),
        kind: "password",
      },
    ],
  },
  {
    title: msg("Notifications (Telegram)"),
    group: "notifications",
    notificationChannel: "telegram",
    description: msg("A Telegram bot posts the events to a chat, group or channel it is a member of."),
    fields: [
      {
        key: "telegram_bot_token",
        label: msg("Bot token"),
        hint: msg("From @BotFather. Token and chat ID together enable Telegram."),
        kind: "password",
      },
      {
        key: "telegram_chat_id",
        label: msg("Chat ID"),
        hint: msg("The chat the bot posts to: a number, negative for groups."),
        kind: "text",
      },
    ],
  },
  {
    title: msg("Notifications (Apprise)"),
    group: "notifications",
    notificationChannel: "apprise",
    description: msg("An Apprise API server forwards the events to most other services: e-mail, Matrix, Pushover, Gotify, Slack and many more."),
    fields: [
      {
        key: "apprise_url",
        label: msg("Apprise server URL"),
        hint: msg("Root address of the apprise-api server, for example http://apprise:8000."),
        kind: "text",
      },
      {
        key: "apprise_key",
        label: msg("Configuration key"),
        hint: msg("Key of a configuration stored on the server; Tankarr posts to /notify/<key>. Leave empty to send the URLs below instead."),
        kind: "text",
      },
      {
        key: "apprise_urls",
        label: msg("Notification URLs"),
        hint: msg("Comma-separated Apprise URLs (mailto://…, pover://…) sent with each request when no configuration key is set. Stored like a password."),
        kind: "password",
      },
    ],
  },
];

const NOTIFICATION_CHANNEL_KEYS: Record<string, string[]> = {
  ntfy: ["ntfy_url", "ntfy_topic"],
  webhook: ["webhook_url", "webhook_token"],
  discord: ["discord_webhook_url"],
  telegram: ["telegram_bot_token", "telegram_chat_id"],
  apprise: ["apprise_url", "apprise_key", "apprise_urls"],
};

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
  const [readerTest, setReaderTest] = useState<string | null>(null);
  const [readerDiscovery, setReaderDiscovery] = useState<string | null>(null);
  const [notificationTests, setNotificationTests] = useState<Record<string, string | null>>({});
  const [sabTest, setSabTest] = useState<string | null>(null);
  const [prowlarrIndexers, setProwlarrIndexers] = useState<ProwlarrIndexer[]>([]);
  const [prowlarrCategories, setProwlarrCategories] = useState<ProwlarrCategory[]>([]);
  const [apiKey, setApiKey] = useState<string | null>(null);
  const [apiKeyVisible, setApiKeyVisible] = useState(false);
  const [apiKeyBusy, setApiKeyBusy] = useState(false);
  useEffect(() => {
    let cancelled = false;
    api
      .apiKey()
      .then((result) => {
        if (!cancelled) setApiKey(result.api_key);
      })
      .catch(() => {
        if (!cancelled) setApiKey(null);
      });
    return () => {
      cancelled = true;
    };
  }, []);

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
        <h2>{t("Settings unavailable")}</h2>
        <p className="muted">{loadError}</p>
        <button type="button" className="btn" onClick={() => void load()}>
          <Icon name="refresh" size={14} /> {t("Retry")}
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
    detail: t("{language} · ID {id}", { language: languageLabels.get(source.language) ?? source.language.toUpperCase(), id: source.id }),
    defaultSelected: source.enabled,
    disabled: !source.allowed,
    warning: source.allowed
      ? undefined
      : t("Blocked by the current {classification} safety classification", { classification: source.content_warning }),
  }));
  const prowlarrIndexerOptions: CatalogOption[] = prowlarrIndexers.map((indexer) => ({
    id: String(indexer.id),
    label: indexer.name,
    detail: t("{protocol} · priority {priority} · ID {id}", { protocol: indexer.protocol.toUpperCase(), priority: indexer.priority, id: indexer.id }),
    defaultSelected: indexer.enabled && indexer.compatible,
    disabled: !indexer.enabled || !indexer.compatible,
    warning: !indexer.enabled
      ? t("Disabled in Prowlarr")
      : !indexer.compatible
        ? t("No book/comic categories advertised")
        : undefined,
  }));
  const prowlarrCategoryOptions: CatalogOption[] = prowlarrCategories.map((category) => ({
    id: String(category.id),
    label: category.name,
    detail: t("ID {id} · {indexers}", { id: category.id, indexers: tn(category.indexer_ids.length, "{count} indexer", "{count} indexers") }),
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
          ? t("Saved: {fields}. Changes apply immediately.", { fields: result.applied.join(", ") })
          : t("Nothing changed."),
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
      setSabTest(result.ok ? t("OK — SABnzbd {version}", { version: result.version ?? t("connected") }) : t("Failed: {error}", { error: result.error }));
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
      setQbitTest(result.ok ? t("OK — qBittorrent {version}", { version: result.version ?? t("connected") }) : t("Failed: {error}", { error: result.error }));
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
          ? t("OK — {source} reachable{results}.", { source: result.source ?? source, results: typeof result.results === "number" ? ` (${tn(result.results, "{count} result", "{count} results")})` : "" })
          : t("Failed: {error}", { error: result.error }),
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
        detail = tn(result.sources, "{count} allowed {language} source", "{count} allowed {language} sources", { language: result.language ?? "" }).replace(/\s+/g, " ").trim();
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
        setInternetArchiveTest(t("Failed: {error} ({latency} ms)", { error: result.error ?? t("unknown error"), latency: result.latency_ms }));
        return;
      }
      const sample = (result.sample ?? []).slice(0, 3).join(", ");
      setInternetArchiveTest(
        t("OK — archive.org answered in {latency} ms; “{probe}” → {items}, {files}{sample}{disabled}", {
          latency: result.latency_ms,
          probe: result.probe_title,
          items: tn(result.items ?? 0, "{count} item", "{count} items"),
          files: tn(result.releases ?? 0, "{count} book file", "{count} book files"),
          sample: sample ? ` (${sample})` : "",
          disabled: result.enabled ? "" : " · " + t("currently disabled"),
        }),
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
        setProwlarrTest(t("Failed: {error}", { error: result.error ?? t("unknown error") }));
        return;
      }
      setProwlarrIndexers(result.indexers ?? []);
      setProwlarrCategories(result.categories ?? []);
      setProwlarrTest(
        t("OK — Prowlarr {version}; {compatible} compatible of {enabled} enabled indexers", { version: result.version ?? t("connected"), compatible: result.compatible_indexers ?? 0, enabled: result.enabled_indexers ?? 0 }),
      );
    } catch (caught) {
      setProwlarrTest(String(caught));
    }
  };


  const copyApiKey = async () => {
    if (!apiKey) return;
    try {
      await navigator.clipboard.writeText(apiKey);
      notify("success", t("API key copied to the clipboard."));
    } catch {
      notify("error", t("Copying failed: show the key and copy it by hand."));
    }
  };

  const regenerateApiKey = async () => {
    if (
      !window.confirm(
        t("Regenerate the API key? Every application using the current key stops working until it gets the new one."),
      )
    ) {
      return;
    }
    setApiKeyBusy(true);
    try {
      const result = await api.regenerateApiKey();
      setApiKey(result.api_key);
      setApiKeyVisible(true);
      notify("success", t("A new API key is in use; the old one no longer works."));
    } catch (caught) {
      notify("error", String(caught));
    } finally {
      setApiKeyBusy(false);
    }
  };

  const testNotification = async (channel: string) => {
    const setResult = (message: string | null) =>
      setNotificationTests((current) => ({ ...current, [channel]: message }));
    setResult("…");
    try {
      const result = await api.testNotificationChannel(channel, pickValues(NOTIFICATION_CHANNEL_KEYS[channel] ?? []));
      if (!result.ok) {
        const message = t("Failed: {error}", { error: result.error ?? t("unknown error") });
        setResult(message);
        notify("error", message);
        return;
      }
      const message =
        channel === "ntfy"
          ? t("Sent — ntfy accepted the test for topic {topic}.", { topic: result.topic ?? values.ntfy_topic })
          : t("Sent — the test notification was accepted (HTTP {status}).", { status: result.status_code ?? 200 });
      setResult(message);
      notify("success", message);
    } catch (caught) {
      const message = String(caught);
      setResult(message);
      notify("error", message);
    }
  };

  const testReader = async () => {
    setReaderTest("…");
    try {
      const result = await api.testReader(
        pickValues(["reader_kind", "reader_url", "reader_internal_url", "reader_api_key", "reader_username", "reader_password", "reader_library_path", "reader_series_url_template"]),
      );
      setReaderTest(result.ok ? t("OK — {detail}", { detail: result.detail }) : t("Failed: {error}", { error: result.error }));
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
        ? tn(filled, "{count} field filled; review and save.", "{count} fields filled; review and save.")
        : filled
          ? tn(filled, "{count} field restored; it matches the saved settings, so no save is needed.", "{count} fields restored; they match the saved settings, so no save is needed.")
          : t("The form already matches; no save is needed.");
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
        <h1 className="page-title">{t("Settings")}</h1>
        <label className="setting-toggle settings-advanced-switch" title={t("Show rarely needed settings (intervals, ports, internal URLs). Remembered on this browser.")}>
          <input type="checkbox" checked={showAdvanced} onChange={toggleAdvanced} />
          <span>{t("Show advanced")}</span>
        </label>
        <button
          type="button"
          className="btn btn-primary"
          disabled={saving || dirtyKeys.length === 0}
          onClick={() => void save()}
        >
          <Icon name="check" /> {saving ? t("Saving…") : t("Save Changes")}
        </button>
      </div>
      <p className="muted small">
        {t("Values set here are stored in the database and override the container environment. Sensitive credentials are stored separately in")} <span className="mono">/config/metadata.env</span>
        {" "}{t("with restricted permissions and are never returned unmasked. Clear a field to remove its override.")}
      </p>
      <div className="settings-layout">
        <label className="mobile-settings-section">{t("Settings section")}
          <select className="input" aria-label={t("Settings section")} value={activeTab} onChange={(event) => {
            const tab = event.target.value as SettingsTab;
            window.location.hash = `/settings?tab=${tab}`;
            setActiveTab(tab);
          }}>
            {TABS.map((tab) => <option key={tab.id} value={tab.id}>{t(tab.label)}</option>)}
          </select>
        </label>
        <nav className="settings-nav" aria-label={t("Settings sections")}>
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
                <Icon name={tab.icon} size={16} /> <span>{t(tab.label)}</span>
                {dirtyInTab ? <span className="settings-nav-dot" aria-label={t("Unsaved changes")} /> : null}
              </button>
            );
          })}
        </nav>
        <div className="settings-content">
        {activeTab === "general" ? <InterfaceLanguageSection /> : null}
        {activeTab === "data" ? <Suspense fallback={<Spinner />}><BackupPanel /></Suspense> : null}
        {SECTIONS.filter((section) => section.group === activeTab).map((section) => (
          <section key={section.title} className="panel settings-section">
            <h2>{t(section.title)}</h2>
            {section.description ? <p className="muted small setting-section-description">{t(section.description)}</p> : null}
            {section.group === "translation" ? <p className="muted small" role="status">
              {values.translation_processor_url?.trim()
                ? t("Processing: external service. Open Advanced settings to change it or clear its URL to use this computer.")
                : t("Processing: this computer. No processor URL or access token is required. Basic lettering uses white text boxes; complex pages may need an external processor in Advanced settings.")}
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
                    {t(field.label)}{" "}

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
                          {t(label)}
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
                            <span>{t(label)}</span>
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
                      emptyMessage={t("Run Test & load sources below to discover installed Suwayomi extensions.")}
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
                      emptyMessage={t("Run Test & load indexers below to discover Prowlarr indexers.")}
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
                      emptyMessage={t("Run Test & load indexers below to discover compatible categories.")}
                      selectLabel={t("Select recommended")}
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
                          {t(label)}
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
                      <span>{(values[field.key] ?? "false") === "true" ? t("Enabled") : t("Disabled")}</span>
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
                  {field.hint ? <p id={hintId} className="muted small">{t(field.hint)}</p> : null}
                  {field.helpUrl || field.testSource ? (
                    <div className="setting-field-actions">
                      {field.helpUrl ? (
                        <a
                          className="btn btn-small"
                          href={field.helpUrl}
                          target="_blank"
                          rel="noreferrer"
                        >
                          <Icon name="external" size={14} /> {t(field.helpLabel ?? msg("Instructions"))}
                        </a>
                      ) : null}
                      {field.testSource ? (
                        <button
                          type="button"
                          className="btn btn-small"
                          disabled={metadataTests[field.testSource] === "…"}
                          onClick={() => void testMetadata(field.testSource!, field.key)}
                        >
                          <Icon name="check" size={14} /> {field.kind === "boolean" ? t("Test connection") : t("Test current value")}
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
                  <Icon name="check" size={14} /> {t(section.providerTest.label)}
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
                  <Icon name="search" size={14} /> {t("Discover reader")}
                </button>
                <button
                  type="button"
                  className="btn btn-small"
                  disabled={readerTest === "…" || readerDiscovery === "…"}
                  onClick={() => void testReader()}
                >
                  <Icon name="check" size={14} /> {t("Test reader connection")}
                </button>
                {readerDiscovery || readerTest ? (
                  <p className="muted small setting-test-result" aria-live="polite">
                    {readerDiscovery === "…"
                      ? t("Searching configured and local Docker routes…")
                      : readerDiscovery ?? readerTest}
                  </p>
                ) : null}
              </div>
            ) : null}
            {section.title === "Usenet client (SABnzbd)" ? (
              <div className="form-row setting-test-row">
                <button type="button" className="btn btn-small" disabled={sabTest === "…"} onClick={() => void testSab()}>
                  <Icon name="check" size={14} /> {t("Test connection")}
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
                  <Icon name="check" size={14} /> {t("Test connection")}
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
                  <Icon name="check" size={14} /> {t("Test archive.org")}
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
                  <Icon name="check" size={14} /> {t("Test & load indexers")}
                </button>
                {prowlarrTest ? (
                  <p className="muted small setting-test-result">{prowlarrTest}</p>
                ) : null}
              </div>
            ) : null}
            {section.title === "Security" ? (
              <div className="api-key-block">
                <strong>{t("API key")}</strong>
                <p className="muted small">
                  {t("Lets other applications (dashboards, scripts, mobile clients) use the API without your login: they send it in the")}{" "}
                  <code>X-Api-Key</code> {t("header. It grants the same access as the login, so treat it like the password and regenerate it if it leaks.")}
                </p>
                <div className="form-row setting-test-row">
                  <code className="api-key-value" aria-label={t("API key")}>
                    {apiKey === null ? "…" : apiKeyVisible ? apiKey : "•".repeat(32)}
                  </code>
                  <button type="button" className="btn btn-small" disabled={!apiKey} onClick={() => setApiKeyVisible((visible) => !visible)}>
                    {apiKeyVisible ? t("Hide") : t("Show")}
                  </button>
                  <button type="button" className="btn btn-small" disabled={!apiKey} onClick={() => void copyApiKey()}>
                    {t("Copy")}
                  </button>
                  <button type="button" className="btn btn-small" disabled={apiKeyBusy} onClick={() => void regenerateApiKey()}>
                    <Icon name="refresh" size={14} /> {t("Regenerate")}
                  </button>
                </div>
              </div>
            ) : null}
            {section.notificationChannel ? (
              <div className="form-row setting-test-row">
                <button
                  type="button"
                  className="btn btn-small"
                  disabled={notificationTests[section.notificationChannel] === "…"}
                  onClick={() => void testNotification(section.notificationChannel!)}
                >
                  <Icon name="check" size={14} /> {t("Send test notification")}
                </button>
                {notificationTests[section.notificationChannel] ? (
                  <p className="muted small setting-test-result">{notificationTests[section.notificationChannel]}</p>
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
            <strong>{dirtyKeys.length}</strong> {dirtyKeys.length === 1 ? t("unsaved change") : t("unsaved changes")}
          </span>
          <div className="toolbar-group">
            <button type="button" className="btn" disabled={saving} onClick={discard}>
              {t("Discard")}
            </button>
            <button type="button" className="btn btn-primary" disabled={saving} onClick={() => void save()}>
              <Icon name="check" /> {saving ? t("Saving…") : t("Apply Changes")}
            </button>
          </div>
        </div>
      ) : null}
    </div>
  );
}
