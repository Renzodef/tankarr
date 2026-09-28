import { ReactNode, createContext, useContext, useEffect, useId, useRef, useState } from "react";
import { URL_BASE, serverUrl } from "./serverUrl";
import type { Health, Manga, MonitorMode } from "./types";

/* ---------------------------------- icons --------------------------------- */

const ICON_PATHS: Record<string, ReactNode> = {
  comics: <polygon points="6 3 20 12 6 21 6 3" />,
  library: (
    <>
      <path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20" />
      <path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z" />
    </>
  ),
  add: (
    <>
      <line x1="12" y1="5" x2="12" y2="19" />
      <line x1="5" y1="12" x2="19" y2="12" />
    </>
  ),
  menu: <path d="M3 6h18M3 12h18M3 18h18" />,
  more: (
    <>
      <circle cx="5" cy="12" r="1" />
      <circle cx="12" cy="12" r="1" />
      <circle cx="19" cy="12" r="1" />
    </>
  ),
  image: (
    <>
      <rect x="3" y="3" width="18" height="18" rx="2" />
      <circle cx="8.5" cy="8.5" r="1.5" />
      <polyline points="21 15 16 10 5 21" />
    </>
  ),
  activity: <polyline points="22 12 18 12 15 21 9 3 6 12 2 12" />,
  history: (
    <>
      <circle cx="12" cy="12" r="10" />
      <polyline points="12 6 12 12 16 14" />
    </>
  ),
  wanted: (
    <>
      <path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z" />
      <line x1="12" y1="9" x2="12" y2="13" />
      <line x1="12" y1="17" x2="12.01" y2="17" />
    </>
  ),
  system: (
    <>
      <rect x="2" y="3" width="20" height="14" rx="2" />
      <line x1="8" y1="21" x2="16" y2="21" />
      <line x1="12" y1="17" x2="12" y2="21" />
    </>
  ),
  search: (
    <>
      <circle cx="11" cy="11" r="8" />
      <line x1="21" y1="21" x2="16.65" y2="16.65" />
    </>
  ),
  user: (
    <>
      <path d="M20 21a8 8 0 0 0-16 0" />
      <circle cx="12" cy="7" r="4" />
    </>
  ),
  refresh: (
    <>
      <polyline points="23 4 23 10 17 10" />
      <path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10" />
    </>
  ),
  retry: (
    <>
      <polyline points="1 4 1 10 7 10" />
      <path d="M3.51 15a9 9 0 1 0 2.13-9.36L1 10" />
    </>
  ),
  download: (
    <>
      <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
      <polyline points="7 10 12 15 17 10" />
      <line x1="12" y1="15" x2="12" y2="3" />
    </>
  ),
  trash: (
    <>
      <polyline points="3 6 5 6 21 6" />
      <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" />
    </>
  ),
  edit: (
    <>
      <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7" />
      <path d="M18.5 2.5a2.12 2.12 0 0 1 3 3L12 15l-4 1 1-4z" />
    </>
  ),
  check: <polyline points="20 6 9 17 4 12" />,
  close: (
    <>
      <line x1="18" y1="6" x2="6" y2="18" />
      <line x1="6" y1="6" x2="18" y2="18" />
    </>
  ),
  chevronLeft: <polyline points="15 18 9 12 15 6" />,
  chevronDown: <polyline points="6 9 12 15 18 9" />,
  sortAscending: (
    <>
      <line x1="4" y1="6" x2="11" y2="6" />
      <line x1="4" y1="12" x2="9" y2="12" />
      <line x1="4" y1="18" x2="7" y2="18" />
      <line x1="17" y1="5" x2="17" y2="19" />
      <polyline points="13 9 17 5 21 9" />
    </>
  ),
  sortDescending: (
    <>
      <line x1="4" y1="6" x2="11" y2="6" />
      <line x1="4" y1="12" x2="9" y2="12" />
      <line x1="4" y1="18" x2="7" y2="18" />
      <line x1="17" y1="5" x2="17" y2="19" />
      <polyline points="13 15 17 19 21 15" />
    </>
  ),
  chevronRight: <polyline points="9 18 15 12 9 6" />,
  chevronsLeft: (
    <>
      <polyline points="11 17 6 12 11 7" />
      <polyline points="18 17 13 12 18 7" />
    </>
  ),
  chevronsRight: (
    <>
      <polyline points="13 17 18 12 13 7" />
      <polyline points="6 17 11 12 6 7" />
    </>
  ),
  external: (
    <>
      <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6" />
      <polyline points="15 3 21 3 21 9" />
      <line x1="10" y1="14" x2="21" y2="3" />
    </>
  ),
  bookmark: <path d="M19 21l-7-5-7 5V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2z" />,
  alert: (
    <>
      <circle cx="12" cy="12" r="10" />
      <line x1="12" y1="8" x2="12" y2="12" />
      <line x1="12" y1="16" x2="12.01" y2="16" />
    </>
  ),
  calendar: (
    <>
      <rect x="3" y="4" width="18" height="18" rx="2" />
      <line x1="16" y1="2" x2="16" y2="6" />
      <line x1="8" y1="2" x2="8" y2="6" />
      <line x1="3" y1="10" x2="21" y2="10" />
    </>
  ),
  upload: (
    <>
      <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
      <polyline points="17 8 12 3 7 8" />
      <line x1="12" y1="3" x2="12" y2="15" />
    </>
  ),
  settings: (
    <>
      <line x1="4" y1="21" x2="4" y2="14" />
      <line x1="4" y1="10" x2="4" y2="3" />
      <line x1="12" y1="21" x2="12" y2="12" />
      <line x1="12" y1="8" x2="12" y2="3" />
      <line x1="20" y1="21" x2="20" y2="16" />
      <line x1="20" y1="12" x2="20" y2="3" />
      <line x1="1" y1="14" x2="7" y2="14" />
      <line x1="9" y1="8" x2="15" y2="8" />
      <line x1="17" y1="16" x2="23" y2="16" />
    </>
  ),
  gears: (
    <>
      <circle cx="12" cy="12" r="3" />
      <path d="M19.4 15a1.7 1.7 0 0 0 .34 1.88l.06.06-2.83 2.83-.06-.06a1.7 1.7 0 0 0-1.88-.34 1.7 1.7 0 0 0-1.03 1.56V21h-4v-.08A1.7 1.7 0 0 0 8.94 19.4a1.7 1.7 0 0 0-1.88.34l-.06.06-2.83-2.83.06-.06A1.7 1.7 0 0 0 4.57 15 1.7 1.7 0 0 0 3 14H3v-4h.08A1.7 1.7 0 0 0 4.6 8.94a1.7 1.7 0 0 0-.34-1.88L4.2 7l2.83-2.83.06.06A1.7 1.7 0 0 0 9 4.57 1.7 1.7 0 0 0 10 3.08V3h4v.08A1.7 1.7 0 0 0 15.06 4.6a1.7 1.7 0 0 0 1.88-.34L17 4.2 19.83 7l-.06.06a1.7 1.7 0 0 0-.34 1.88A1.7 1.7 0 0 0 20.92 10H21v4h-.08A1.7 1.7 0 0 0 19.4 15z" />
    </>
  ),
  logout: (
    <>
      <path d="M10 17l5-5-5-5" />
      <path d="M15 12H3" />
      <path d="M21 19V5a2 2 0 0 0-2-2h-6" />
    </>
  ),
};

export type IconName = keyof typeof ICON_PATHS;

export function Icon({ name, size = 18 }: { name: IconName; size?: number }) {
  return (
    <svg
      className="icon"
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {ICON_PATHS[name]}
    </svg>
  );
}

export function Pagination({
  page,
  pageSize,
  total,
  onPageChange,
  itemLabel = "records",
  ariaLabel = "Pagination",
}: {
  page: number;
  pageSize: number;
  total: number;
  onPageChange: (page: number) => void;
  itemLabel?: string;
  ariaLabel?: string;
}) {
  const root = useRef<HTMLElement>(null);
  if (total <= 0) return null;

  const pages = Math.max(1, Math.ceil(total / pageSize));
  const current = Math.min(Math.max(page, 1), pages);
  const first = (current - 1) * pageSize + 1;
  const last = Math.min(current * pageSize, total);
  const goTo = (next: number) => {
    const destination = Math.min(Math.max(next, 1), pages);
    if (destination === current) return;
    onPageChange(destination);
    window.requestAnimationFrame(() => {
      root.current?.closest<HTMLElement>(".content")?.scrollTo({ top: 0, behavior: "smooth" });
    });
  };

  return (
    <nav ref={root} className="pagination" aria-label={ariaLabel}>
      <div className="pagination-controls">
        <button
          type="button"
          className="pagination-button"
          disabled={current === 1}
          title="First page"
          aria-label="First page"
          onClick={() => goTo(1)}
        >
          <Icon name="chevronsLeft" size={17} />
        </button>
        <button
          type="button"
          className="pagination-button"
          disabled={current === 1}
          title="Previous page"
          aria-label="Previous page"
          onClick={() => goTo(current - 1)}
        >
          <Icon name="chevronLeft" size={17} />
        </button>
        <span className="pagination-page" aria-live="polite">
          <strong>{current}</strong>
          <span>/</span>
          {pages}
        </span>
        <button
          type="button"
          className="pagination-button"
          disabled={current === pages}
          title="Next page"
          aria-label="Next page"
          onClick={() => goTo(current + 1)}
        >
          <Icon name="chevronRight" size={17} />
        </button>
        <button
          type="button"
          className="pagination-button"
          disabled={current === pages}
          title="Last page"
          aria-label="Last page"
          onClick={() => goTo(pages)}
        >
          <Icon name="chevronsRight" size={17} />
        </button>
      </div>
      <span className="pagination-total">
        {first}–{last} of {total} {itemLabel}
      </span>
    </nav>
  );
}

/** Stylized tankobon spines used as the app mark. */
export function Logo({ size = 26 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" aria-hidden="true">
      <rect x="2.5" y="4" width="4.6" height="18" rx="1" fill="var(--accent)" />
      <rect x="8.7" y="2" width="4.6" height="20" rx="1" fill="var(--accent)" opacity="0.82" />
      <rect
        x="17.2"
        y="3.2"
        width="4.6"
        height="19"
        rx="1"
        fill="var(--accent)"
        opacity="0.64"
        transform="rotate(8 19.5 12.7)"
      />
    </svg>
  );
}

/* --------------------------------- helpers -------------------------------- */

export const LANGUAGES: readonly (readonly [string, string])[] = [
  ["en", "English"],
  ["it", "Italiano"],
  ["ar", "Arabic"],
  ["bn", "Bengali"],
  ["bg", "Bulgarian"],
  ["my", "Burmese"],
  ["ca", "Catalan"],
  ["zh", "Chinese (Simplified)"],
  ["zh-hk", "Chinese (Traditional)"],
  ["zh-ro", "Chinese (Romanized)"],
  ["cs", "Czech"],
  ["da", "Danish"],
  ["nl", "Dutch"],
  ["fil", "Filipino"],
  ["fi", "Finnish"],
  ["fr", "French"],
  ["de", "German"],
  ["el", "Greek"],
  ["he", "Hebrew"],
  ["hi", "Hindi"],
  ["hu", "Hungarian"],
  ["id", "Indonesian"],
  ["ja", "Japanese"],
  ["ja-ro", "Japanese (Romanized)"],
  ["ko", "Korean"],
  ["ko-ro", "Korean (Romanized)"],
  ["lt", "Lithuanian"],
  ["ms", "Malay"],
  ["mn", "Mongolian"],
  ["no", "Norwegian"],
  ["fa", "Persian"],
  ["pl", "Polish"],
  ["pt-br", "Portuguese (Brazil)"],
  ["pt", "Portuguese (Portugal)"],
  ["ro", "Romanian"],
  ["ru", "Russian"],
  ["sr", "Serbo-Croatian"],
  ["es", "Spanish (Spain)"],
  ["es-la", "Spanish (Latin America)"],
  ["sv", "Swedish"],
  ["th", "Thai"],
  ["tr", "Turkish"],
  ["uk", "Ukrainian"],
  ["vi", "Vietnamese"],
] as const;

export const MONITOR_OPTIONS: {
  value: MonitorMode;
  title: string;
  short: string;
  description: string;
}[] = [
  {
    value: "all",
    title: "All chapters",
    short: "All",
    description: "Download every available chapter, then keep watching for new releases.",
  },
  {
    value: "future",
    title: "Future chapters",
    short: "Future",
    description: "Use today's releases as a baseline and download only chapters published later.",
  },
  {
    value: "existing",
    title: "Existing chapters",
    short: "Existing",
    description: "Download the chapters available now without monitoring future releases.",
  },
  {
    value: "none",
    title: "None",
    short: "Manual",
    description: "Track the series and its chapter index without automatic downloads.",
  },
];

export function monitorLabel(mode: MonitorMode | undefined) {
  return MONITOR_OPTIONS.find((option) => option.value === mode)?.short ?? "Manual";
}

export function languageName(code: string) {
  return LANGUAGES.find(([value]) => value === code)?.[1] ?? code.toUpperCase();
}

export function providerChainLabel(provider: string, sourceName?: string | null) {
  const normalized = provider.trim().toLocaleLowerCase();
  const gateway = normalized === "suwayomi" ? "Suwayomi" : humanize(provider);
  const source = sourceName?.trim();
  if (normalized === "suwayomi" && source && source.toLocaleLowerCase() !== "suwayomi") {
    return `${gateway} → ${source}`;
  }
  return source || gateway || "Unknown";
}

export function formatDate(value: string | null | undefined) {
  if (!value) return "Never";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(date);
}

export function formatBytes(value: number | null | undefined) {
  if (value === null || value === undefined) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let size = value;
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024;
    unit += 1;
  }
  return `${size.toFixed(size >= 100 || unit === 0 ? 0 : 1)} ${units[unit]}`;
}

export function humanize(value: string | null | undefined) {
  if (!value) return "Unknown";
  return value.replaceAll("_", " ").replace(/\b\w/g, (character) => character.toUpperCase());
}

export function chapterLabel(volume: string | null, chapter: string | null) {
  // One canonical label everywhere, whatever the source called it:
  // "Chapter N" for chapters, "Volume N" for whole books.
  if (chapter) return `Chapter ${chapter}`;
  if (volume) return `Volume ${volume}`;
  return "Book";
}

const chapterNumberCollator = new Intl.Collator(undefined, { numeric: true });

export function compareChapterNumbers(a: string | null, b: string | null): number {
  const left = Number(a);
  const right = Number(b);
  if (Number.isFinite(left) && Number.isFinite(right)) return left - right;
  return chapterNumberCollator.compare(String(a ?? ""), String(b ?? ""));
}

/* ------------------------------- hash routing ------------------------------ */

export type Route =
  | { page: "library" }
  | { page: "author"; id: string }
  | { page: "add"; query: string }
  | { page: "series"; id: string }
  | { page: "reader"; id: string; initialPage: number | null }
  | { page: "bookmarks" }
  | { page: "calendar" }
  | { page: "activity" }
  | { page: "wanted" }
  | { page: "history" }
  | { page: "import" }
  | { page: "settings" }
  | { page: "system" }
  | { page: "setup" };

export function legacyToolsRedirect(hash: string): string | null {
  const [path, search] = hash.replace(/^#\/?/, "").split("?");
  const parameters = new URLSearchParams(search ?? "");
  if (path !== "settings" || parameters.get("tab") !== "tools") return null;
  const mangaId = parameters.get("manga_id");
  return mangaId ? `#/series/${encodeURIComponent(mangaId)}` : "#/settings?tab=data";
}

function currentRoute(): Route {
  const redirect = legacyToolsRedirect(window.location.hash);
  if (redirect) window.history.replaceState(null, "", redirect);
  return parseRoute(redirect ?? window.location.hash);
}

export function parseRoute(hash: string): Route {
  const raw = (legacyToolsRedirect(hash) ?? hash).replace(/^#\/?/, "");
  const [path, search] = raw.split("?");
  const parameters = new URLSearchParams(search ?? "");
  const segments = path.split("/").filter(Boolean);
  if (segments[0] === "add") {
    return { page: "add", query: parameters.get("q") ?? "" };
  }
  if (segments[0] === "series" && segments[1]) {
    return { page: "series", id: decodeURIComponent(segments.slice(1).join("/")) };
  }
  if (segments[0] === "reader" && segments[1]) {
    const requestedPage = Number(parameters.get("page"));
    return {
      page: "reader",
      id: decodeURIComponent(segments.slice(1).join("/")),
      initialPage: Number.isInteger(requestedPage) && requestedPage > 0 ? requestedPage - 1 : null,
    };
  }
  if (segments[0] === "authors" && segments[1]) {
    return { page: "author", id: decodeURIComponent(segments.slice(1).join("/")) };
  }
  if (segments[0] === "calendar") return { page: "calendar" };
  if (segments[0] === "bookmarks") return { page: "bookmarks" };
  if (segments[0] === "activity") return { page: "activity" };
  if (segments[0] === "import") return { page: "import" };
  if (segments[0] === "settings") return { page: "settings" };
  if (segments[0] === "wanted") return { page: "wanted" };
  if (segments[0] === "history") return { page: "history" };
  if (segments[0] === "system") return { page: "system" };
  if (segments[0] === "setup") return { page: "setup" };
  return { page: "library" };
}

export function useHashRoute(): Route {
  const [route, setRoute] = useState<Route>(currentRoute);
  useEffect(() => {
    const listener = () => {
      // Ask mounted editors synchronously before changing the route. A native
      // hashchange listener alone can run after React has unmounted the form.
      // Resolve legacy links first so the editor sees their real destination.
      const nextRoute = currentRoute();
      const allowed = window.dispatchEvent(new Event("tankarr:before-navigation", { cancelable: true }));
      if (allowed) setRoute(nextRoute);
    };
    window.addEventListener("hashchange", listener);
    return () => window.removeEventListener("hashchange", listener);
  }, []);
  return route;
}

export function navigate(path: string) {
  window.location.hash = path;
}

export function seriesPath(id: string) {
  return `#/series/${encodeURIComponent(id)}`;
}

export function authorPath(authorId: string) {
  return `#/authors/${encodeURIComponent(authorId)}`;
}

/* ------------------------------- app context ------------------------------- */

export type Toast = { id: number; kind: "success" | "error" | "info"; text: string };

export type AppState = {
  health: Health | null;
  refreshJobs: () => Promise<void>;
  refreshHealth: () => Promise<void>;
  notify: (kind: Toast["kind"], text: string) => void;
};

export const AppContext = createContext<AppState | null>(null);
export const AppActionsContext = createContext<Omit<AppState, "health"> | null>(null);

export function useAppActions(): Omit<AppState, "health"> {
  const value = useContext(AppActionsContext);
  if (!value) throw new Error("AppActionsContext is not mounted");
  return value;
}

export function useApp(): AppState {
  const value = useContext(AppContext);
  if (!value) throw new Error("AppContext is not mounted");
  return value;
}

/* ------------------------------ building blocks ---------------------------- */

export function ProgressBar({ value, kind = "accent" }: { value: number; kind?: "accent" | "success" }) {
  const percent = Math.max(0, Math.min(100, Math.round(value * 100)));
  return (
    <div className="progress" role="progressbar" aria-valuenow={percent}>
      <div className={`progress-fill progress-${kind}`} style={{ width: `${percent}%` }} />
    </div>
  );
}

export function StatusPill({
  kind,
  children,
  title,
}: {
  kind: string;
  children: ReactNode;
  title?: string;
}) {
  return (
    <span className={`pill pill-${kind}`} title={title}>
      {children}
    </span>
  );
}

export function jobStatusPill(status: string): { kind: string; label: string } {
  switch (status) {
    case "completed":
      return { kind: "success", label: "Imported" };
    case "failed":
      return { kind: "danger", label: "Failed" };
    case "queued":
      return { kind: "muted", label: "Queued" };
    case "downloading":
      return { kind: "info", label: "Downloading" };
    case "packaging":
      return { kind: "info", label: "Packaging" };
    case "importing":
      return { kind: "info", label: "Importing" };
    default:
      return { kind: "info", label: humanize(status) };
  }
}

function coverThumbnailWidth(className?: string): number | null {
  if (!className) return null;
  if (className.includes("poster-image") || className.includes("series-poster")) return 320;
  if (className.includes("result-cover")) return 192;
  if (
    className.includes("table-cover") ||
    className.includes("search-cover") ||
    className.includes("volume-cover") ||
    className.includes("queue-series-cover")
  ) {
    return 64;
  }
  return null;
}

export function artworkThumbnailUrl(url: string | null, width: number): string | null {
  if (!url) return null;
  try {
    const resolved = serverUrl(url);
    const parsed = new URL(resolved, window.location.origin);
    if (parsed.origin !== window.location.origin) return url;
    // Tankarr's own artwork routes, seen without the URL base they live under.
    const pathname = URL_BASE && parsed.pathname.startsWith(`${URL_BASE}/`) ? parsed.pathname.slice(URL_BASE.length) : parsed.pathname;
    const isSeries = /^\/api\/metadata\/artwork\/[^/]+\/series$/.test(pathname);
    const isVolume = /^\/api\/metadata\/artwork\/[^/]+\/volumes\/[^/]+$/.test(pathname);
    if (!isSeries && !isVolume) return resolved;
    return `${URL_BASE}${pathname}/thumbnail/${width}${parsed.search}`;
  } catch {
    return url;
  }
}

type CoverProps = { url: string | null; title: string; className?: string; priority?: boolean };

export function Cover({ url, title, className, priority = false }: CoverProps) {
  const thumbnailWidth = coverThumbnailWidth(className);
  const source = thumbnailWidth ? artworkThumbnailUrl(url, thumbnailWidth) : url ? serverUrl(url) : null;
  // Each source owns its image and error state. An old request's error cannot
  // hide a replacement image, and React handles cancellation on unmount.
  return (
    <CoverImage
      key={source ?? ""}
      source={source}
      title={title}
      className={className}
      thumbnailWidth={thumbnailWidth}
      priority={priority}
    />
  );
}

function CoverImage({ source, title, className, thumbnailWidth, priority }: {
  source: string | null;
  title: string;
  className?: string;
  thumbnailWidth: number | null;
  priority: boolean;
}) {
  const [failed, setFailed] = useState(false);
  if (!source || failed) {
    return (
      <div className={`cover-fallback ${className ?? ""}`}>
        <Icon name="library" size={28} />
        <span>{title}</span>
      </div>
    );
  }
  return (
    <img
      className={className}
      src={source}
      alt={title}
      loading={priority ? "eager" : "lazy"}
      fetchPriority={priority ? "high" : undefined}
      decoding="async"
      width={thumbnailWidth ?? undefined}
      height={thumbnailWidth ? Math.round(thumbnailWidth * 1.5) : undefined}
      onError={() => setFailed(true)}
    />
  );
}

export function seriesCoverUrl(manga: Manga): string | null {
  return manga.artwork_url ?? manga.metadata?.cover_url ?? manga.cover_url;
}

export function Spinner() {
  return <div className="spinner" aria-label="Loading" />;
}

export function EmptyState({ icon, title, hint }: { icon: IconName; title: string; hint?: string }) {
  return (
    <div className="empty-state">
      <Icon name={icon} size={42} />
      <h3>{title}</h3>
      {hint ? <p>{hint}</p> : null}
    </div>
  );
}

const openDialogs: HTMLElement[] = [];
let modalBodyOverflow = "";

export function Modal({
  title,
  onClose,
  children,
  footer,
  wide,
  className = "",
}: {
  title: string;
  onClose: () => void;
  children: ReactNode;
  footer?: ReactNode;
  wide?: boolean;
  className?: string;
}) {
  const dialogRef = useRef<HTMLDivElement>(null);
  const titleId = useId();
  const [viewport, setViewport] = useState<{ height: number; top: number } | null>(null);
  useEffect(() => {
    const visible = window.visualViewport;
    if (!visible) return;
    const update = () => setViewport({ height: visible.height, top: visible.offsetTop });
    update();
    visible.addEventListener("resize", update);
    visible.addEventListener("scroll", update);
    return () => {
      visible.removeEventListener("resize", update);
      visible.removeEventListener("scroll", update);
    };
  }, []);
  const closeRef = useRef(onClose);
  closeRef.current = onClose;
  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    const previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    if (openDialogs.length === 0) {
      modalBodyOverflow = document.body.style.overflow;
      document.body.style.overflow = "hidden";
    }
    openDialogs.push(dialog);
    const focusable = () => [...dialog.querySelectorAll<HTMLElement>(
      'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
    )].filter((element) => element.getClientRects().length > 0 && !element.closest("[inert]"));
    const focusFirst = () => (focusable()[0] ?? dialog).focus();
    focusFirst();
    const listener = (event: KeyboardEvent) => {
      if (openDialogs.at(-1) !== dialog) return;
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopImmediatePropagation();
        closeRef.current();
      } else if (event.key === "Tab") {
        const elements = focusable();
        const index = elements.indexOf(document.activeElement as HTMLElement);
        if (!elements.length || (event.shiftKey ? index <= 0 : index === elements.length - 1 || index < 0)) {
          event.preventDefault();
          (event.shiftKey ? elements.at(-1) ?? dialog : elements[0] ?? dialog).focus();
        }
      }
    };
    const containFocus = (event: FocusEvent) => {
      if (openDialogs.at(-1) === dialog && !dialog.contains(event.target as Node)) focusFirst();
    };
    window.addEventListener("keydown", listener, true);
    document.addEventListener("focusin", containFocus);
    return () => {
      window.removeEventListener("keydown", listener, true);
      document.removeEventListener("focusin", containFocus);
      const index = openDialogs.indexOf(dialog);
      if (index >= 0) openDialogs.splice(index, 1);
      if (!openDialogs.length) document.body.style.overflow = modalBodyOverflow;
      if (previousFocus?.isConnected) previousFocus.focus();
    };
  }, []);
  return (
    <div className="modal-backdrop" style={viewport ? { height: viewport.height, top: viewport.top, bottom: "auto" } : undefined} onMouseDown={(event) => event.target === event.currentTarget && onClose()}>
      <div ref={dialogRef} className={`modal ${wide ? "modal-wide" : ""} ${className}`} role="dialog" aria-modal="true" aria-labelledby={titleId} tabIndex={-1}>
        <div className="modal-header">
          <h2 id={titleId}>{title}</h2>
          <button type="button" className="btn btn-ghost btn-icon" onClick={onClose} aria-label="Close">
            <Icon name="close" />
          </button>
        </div>
        <div className="modal-body">{children}</div>
        {footer ? <div className="modal-footer">{footer}</div> : null}
      </div>
    </div>
  );
}
