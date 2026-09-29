import { lazy, memo, Suspense, useCallback, useDeferredValue, useEffect, useId, useMemo, useRef, useState } from "react";
import { api } from "./api";
import { useSetupGate } from "./useSetupGate";
import { useLibrary } from "./useLibrary";
import { RouteErrorBoundary } from "./components/RouteErrorBoundary";
import {
  AppContext,
  AppActionsContext,
  Cover,
  Icon,
  IconName,
  Logo,
  Modal,
  Spinner,
  Toast,
  languageName,
  navigate,
  seriesCoverUrl,
  useHashRoute,
} from "./components";
import { seriesCounts } from "./seriesStatus";
import type { Route } from "./components";
import type { AuthStatus, Health, Manga } from "./types";
import { msg, t } from "./i18n";

// Keep the authentication shell and navigation tiny. Most visits only need
// one page; loading every settings panel, modal and table before first paint
// made the initial Library request compete with hundreds of kilobytes of JS.
const ActivityPage = lazy(() => import("./pages/ActivityPage"));
const AddPage = lazy(() => import("./pages/AddPage"));
const AuthorPage = lazy(() => import("./pages/AuthorPage"));
const CalendarPage = lazy(() => import("./pages/CalendarPage"));
const BookmarksPage = lazy(() => import("./pages/BookmarksPage"));
const ImportPage = lazy(() => import("./pages/ImportPage"));
const SetupPage = lazy(() => import("./pages/SetupPage"));
const SettingsPage = lazy(() => import("./pages/SettingsPage"));
const HistoryPage = lazy(() => import("./pages/HistoryPage"));
const LibraryPage = lazy(() => import("./pages/LibraryPage"));
const ReaderPage = lazy(() => import("./pages/ReaderPage"));
const SeriesPage = lazy(() => import("./pages/SeriesPage"));
const SystemPage = lazy(() => import("./pages/SystemPage"));
const WantedPage = lazy(() => import("./pages/WantedPage"));

function SpinnerPage() {
  return (
    <div className="page">
      <Spinner />
    </div>
  );
}

function normalizeLibrarySearch(value: string) {
  return value
    .normalize("NFKD")
    .replace(/\p{Diacritic}/gu, "")
    .toLocaleLowerCase();
}

function mangaAuthors(manga: Manga) {
  return manga.metadata?.authors?.length ? manga.metadata.authors : manga.authors;
}

const searchCollator = new Intl.Collator(undefined, { numeric: true, sensitivity: "base" });

/** Sonarr-style quick find: search only series already managed by Tankarr. */
const LibrarySearch = memo(function LibrarySearch() {
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const { data: library, loading, error: loadError, refresh } = useLibrary(open);
  const boxRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const listId = useId();

  useEffect(() => {
    const listener = (event: MouseEvent) => {
      if (boxRef.current && !boxRef.current.contains(event.target as Node)) setOpen(false);
    };
    window.addEventListener("mousedown", listener);
    return () => window.removeEventListener("mousedown", listener);
  }, []);

  const term = useDeferredValue(normalizeLibrarySearch(query.trim()));
  const searchIndex = useMemo(() => (library ?? []).map((manga) => ({
    manga,
    title: normalizeLibrarySearch(manga.title),
    text: normalizeLibrarySearch([
      manga.title,
      ...mangaAuthors(manga),
      ...(manga.metadata?.alternate_titles ?? []),
      manga.source_name ?? "",
      manga.provider,
      manga.preferred_language,
    ].join(" ")),
  })).sort((left, right) => searchCollator.compare(left.manga.title, right.manga.title)), [library]);
  const matches = useMemo(() => {
    if (!term) return [];
    const ranked: Manga[][] = [[], [], []];
    for (const item of searchIndex) {
      if (!item.text.includes(term)) continue;
      const rank = item.title === term ? 0 : item.title.startsWith(term) ? 1 : 2;
      if (ranked[rank].length < 8) ranked[rank].push(item.manga);
    }
    return ranked.flat().slice(0, 8);
  }, [searchIndex, term]);
  const optionCount = matches.length;
  const activeIndex = Math.min(active, Math.max(0, optionCount - 1));

  const close = () => {
    setOpen(false);
    setQuery("");
    setActive(0);
    inputRef.current?.blur();
  };

  const choose = (index: number) => {
    if (index < matches.length) {
      navigate(`/series/${encodeURIComponent(matches[index].id)}`);
    }
    close();
  };

  return (
    <div className="topbar-search" ref={boxRef}>
      <Icon name="search" size={16} />
      <input
        ref={inputRef}
        placeholder={t("Search your library…")}
        aria-label={t("Search series in your library")}
        role="combobox"
        aria-autocomplete="list"
        aria-expanded={open && Boolean(term)}
        aria-controls={listId}
        aria-activedescendant={open && optionCount ? `${listId}-${activeIndex}` : undefined}
        value={query}
        onFocus={() => {
          setOpen(true);
        }}
        onChange={(event) => {
          setQuery(event.target.value);
          setOpen(true);
          setActive(0);
        }}
        onKeyDown={(event) => {
          if (event.key === "Escape") close();
          if (!optionCount) return;
          if (event.key === "ArrowDown") {
            event.preventDefault();
            setActive((value) => (value + 1) % optionCount);
          } else if (event.key === "ArrowUp") {
            event.preventDefault();
            setActive((value) => (value - 1 + optionCount) % optionCount);
          } else if (event.key === "Enter") {
            event.preventDefault();
            choose(activeIndex);
          }
        }}
      />
      {open && term ? (
        <div className="search-dropdown" id={listId} role="listbox">
          {matches.map((manga, index) => (
            <button
              key={manga.id}
              id={`${listId}-${index}`}
              type="button"
              role="option"
              aria-selected={index === activeIndex}
              className={`search-option ${index === activeIndex ? "active" : ""}`}
              onMouseEnter={() => setActive(index)}
              onClick={() => choose(index)}
            >
              <Cover
                url={seriesCoverUrl(manga)}
                title={manga.title}
                className="search-cover"
              />
              <span className="search-copy">
                <span className="search-title">{manga.title}</span>
                <span className="muted small">
                  {mangaAuthors(manga).join(", ") || manga.source_name || manga.provider}
                </span>
              </span>
              <span className="muted small">
                {languageName(manga.preferred_language)} · {seriesCounts(manga).downloaded_count}/
                {seriesCounts(manga).total_count}
              </span>
            </button>
          ))}
          {loading && library === null ? (
            <div className="search-option muted">{t("Loading library…")}</div>
          ) : loadError ? (
            <div className="search-option muted" role="alert">
              <span>{t("Library search is temporarily unavailable.")}</span>
              <button type="button" className="btn" onClick={() => void refresh().catch(() => undefined)}>
                {t("Retry library search")}
              </button>
            </div>
          ) : matches.length === 0 ? (
            <div className="search-option muted">{t("No series found in your library.")}</div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
});

type NavItem = {
  path: string;
  page: string;
  label: string;
  icon?: IconName;
  subitem?: boolean;
  groupStart?: boolean;
  activePages?: string[];
};

const NAV_ITEMS: NavItem[] = [
  { path: "#/", page: "library", label: msg("Comics"), icon: "comics", activePages: ["library", "series", "author"] },
  { path: "#/add", page: "add", label: msg("Add New"), icon: "add", subitem: true },
  { path: "#/import", page: "import", label: msg("Library Import"), icon: "upload", subitem: true },
  { path: "#/calendar", page: "calendar", label: msg("Calendar"), icon: "calendar", groupStart: true },
  { path: "#/bookmarks", page: "bookmarks", label: msg("Bookmarks"), icon: "bookmark" },
  {
    path: "#/activity",
    page: "activity",
    label: msg("Activity"),
    icon: "history",
    activePages: ["activity", "history"],
  },
  { path: "#/wanted", page: "wanted", label: msg("Wanted"), icon: "wanted" },
  { path: "#/settings", page: "settings", label: msg("Settings"), icon: "gears", groupStart: true },
  { path: "#/system", page: "system", label: msg("System"), icon: "system" },
];

const PageRouter = memo(function PageRouter({ route, finishSetup }: { route: Route; finishSetup: (skipped?: boolean) => void }) {
  switch (route.page) {
    case "library":
      return <LibraryPage />;
    case "author":
      return <AuthorPage key={route.id} id={route.id} />;
    case "add":
      return <AddPage initialQuery={route.query} />;
    case "series":
      return <SeriesPage key={route.id} id={route.id} />;
    case "reader":
      return <ReaderPage key={route.id} id={route.id} initialPage={route.initialPage} />;
    case "calendar":
      return <CalendarPage />;
    case "bookmarks":
      return <BookmarksPage />;
    case "import":
      return <ImportPage />;
    case "settings":
      return <SettingsPage />;
    case "activity":
      return <ActivityPage />;
    case "wanted":
      return <WantedPage />;
    case "history":
      return <HistoryPage />;
    case "system":
      return <SystemPage />;
    case "setup":
      return <SetupPage onFinish={finishSetup} />;
  }
});

export default function App({
  authentication,
  onLogout,
}: {
  authentication: AuthStatus;
  onLogout: () => Promise<void>;
}) {
  const route = useHashRoute();
  const setup = useSetupGate();
  useEffect(() => {
    if (setup.required && route.page !== "setup" && route.page !== "settings") navigate("/setup");
  }, [setup.required, route.page]);
  const [health, setHealth] = useState<Health | null>(null);
  const [toasts, setToasts] = useState<Toast[]>([]);
  const [mobileNavOpen, setMobileNavOpen] = useState(false);
  const toastId = useRef(0);

  useEffect(() => {
    setMobileNavOpen(false);
  }, [route.page]);

  useEffect(() => {
    const desktop = window.matchMedia("(min-width: 901px)");
    const changed = () => { if (desktop.matches) setMobileNavOpen(false); };
    desktop.addEventListener("change", changed);
    return () => desktop.removeEventListener("change", changed);
  }, []);

  const notify = useCallback((kind: Toast["kind"], text: string) => {
    const id = ++toastId.current;
    setToasts((current) => [...current, { id, kind, text }]);
    window.setTimeout(
      () => setToasts((current) => current.filter((toast) => toast.id !== id)),
      kind === "error" ? 8000 : 4500,
    );
  }, []);

  const refreshHealth = useCallback(async () => {
    try {
      setHealth(await api.health());
    } catch {
      setHealth(null);
    }
  }, []);

  const [queueActive, setQueueActive] = useState<number | null>(null);
  const refreshJobs = useCallback(async () => {
    try {
      const summary = await api.jobsSummary();
      setQueueActive(summary.active);
    } catch {
      // Leave the previous queue visible when a poll fails.
    }
  }, []);

  useEffect(() => {
    let stopped = false;
    let healthPolling = false;
    let jobsPolling = false;
    let healthTimer: number | undefined;
    let jobsTimer: number | undefined;

    const pollHealth = async () => {
      if (stopped || healthPolling) return;
      healthPolling = true;
      await refreshHealth();
      healthPolling = false;
      if (!stopped) {
        healthTimer = window.setTimeout(
          pollHealth,
          document.hidden ? 60000 : 15000,
        );
      }
    };
    const pollJobs = async () => {
      if (stopped || jobsPolling) return;
      jobsPolling = true;
      await refreshJobs();
      jobsPolling = false;
      if (!stopped) {
        jobsTimer = window.setTimeout(
          pollJobs,
          document.hidden ? 30000 : 6000,
        );
      }
    };
    const visibilityChanged = () => {
      if (document.hidden || stopped) return;
      if (healthTimer !== undefined) window.clearTimeout(healthTimer);
      if (jobsTimer !== undefined) window.clearTimeout(jobsTimer);
      healthTimer = window.setTimeout(pollHealth, 0);
      jobsTimer = window.setTimeout(pollJobs, 0);
    };

    // The open page owns first paint. These badges are useful but should not
    // occupy the API executor while Library or Wanted is loading.
    healthTimer = window.setTimeout(pollHealth, 400);
    jobsTimer = window.setTimeout(pollJobs, 800);
    document.addEventListener("visibilitychange", visibilityChanged);
    return () => {
      stopped = true;
      if (healthTimer !== undefined) window.clearTimeout(healthTimer);
      if (jobsTimer !== undefined) window.clearTimeout(jobsTimer);
      document.removeEventListener("visibilitychange", visibilityChanged);
    };
  }, [refreshHealth, refreshJobs]);

  const state = useMemo(
    () => ({ health, refreshJobs, refreshHealth, notify }),
    [health, refreshJobs, refreshHealth, notify],
  );
  const actions = useMemo(
    () => ({ refreshJobs, refreshHealth, notify }),
    [refreshJobs, refreshHealth, notify],
  );

  const activeCount = queueActive ?? 0;
  const blocked =
    health?.deletion_recovery?.recovery_blocked || health?.library_organization?.organization_blocked;
  const libraryDown = health !== null && !health.library.available;
  const systemIssueCount = health
    ? [
        Boolean(blocked),
        libraryDown,
        Boolean(health.monitor.last_cycle_error),
        Boolean(health.library_alignment?.error),
      ].filter(Boolean).length
    : 0;

  return (
    <AppContext.Provider value={state}>
      <div className={`app${route.page === "reader" ? " reader-mode" : ""}`}>
        <aside className="sidebar">
          <a className="sidebar-brand" href="#/">
            <Logo />
            <span className="brand-name">tankarr</span>
          </a>
          <nav className="sidebar-nav" aria-label={t("Main navigation")}>
            {NAV_ITEMS.map((item) => (
              <a
                key={item.page}
                className={[
                  "nav-item",
                  item.subitem ? "nav-subitem" : "",
                  item.groupStart ? "nav-group-start" : "",
                  item.page === "settings" || item.page === "system" ? "nav-secondary" : "",
                  (item.activePages ?? [item.page]).includes(route.page) ? "active" : "",
                ]
                  .filter(Boolean)
                  .join(" ")}
                href={item.path}
              >
                {item.icon ? <Icon name={item.icon} /> : null}
                <span>{t(item.label)}</span>
                {item.page === "activity" && activeCount > 0 ? (
                  <span className="nav-badge">{activeCount}</span>
                ) : null}
                {item.page === "system" && systemIssueCount > 0 ? (
                  <span className="nav-badge">{systemIssueCount}</span>
                ) : null}
              </a>
            ))}
          </nav>
          <div className="sidebar-footer">
            <div className="sidebar-runtime">
              <span className={`health-dot ${health ? "ok" : "down"}`} />
              <span>{health ? `v${health.version}` : t("offline")}</span>
              {authentication.username ? (
                <span className="sidebar-user">{authentication.username}</span>
              ) : null}
            </div>
            {authentication.configured && authentication.method === "forms" ? (
              <button
                type="button"
                className="sidebar-logout"
                title={t("Log out")}
                aria-label={t("Log out")}
                onClick={() => void onLogout()}
              >
                <Icon name="logout" size={15} />
              </button>
            ) : null}
          </div>
        </aside>

        <div className="main">
          <header className="topbar">
            <button type="button" className="btn btn-ghost btn-icon mobile-navigation-toggle" aria-label={t("Open navigation")} aria-haspopup="dialog" aria-expanded={mobileNavOpen} onClick={() => setMobileNavOpen(true)}>
              <Icon name="menu" />
            </button>
            <LibrarySearch />
            <a href="#/add" className="btn btn-ghost btn-icon mobile-navigation-toggle" aria-label={t("Add new series")}><Icon name="add" /></a>
          </header>
          {mobileNavOpen ? <Modal title={t("Navigation")} className="navigation-dialog" onClose={() => setMobileNavOpen(false)}>
            <nav aria-label={t("Mobile navigation")}>
              {NAV_ITEMS.map((item) => <a key={item.page} href={item.path}
                className={`nav-item${item.groupStart ? " nav-group-start" : ""}${(item.activePages ?? [item.page]).includes(route.page) ? " active" : ""}`}
                aria-current={(item.activePages ?? [item.page]).includes(route.page) ? "page" : undefined}
                onClick={() => setMobileNavOpen(false)}>
                {item.icon ? <Icon name={item.icon} /> : null}<span>{t(item.label)}</span>
                {item.page === "activity" && activeCount > 0 ? <span className="nav-badge">{activeCount}</span> : null}
                {item.page === "system" && systemIssueCount > 0 ? <span className="nav-badge">{systemIssueCount}</span> : null}
              </a>)}
            </nav>
            {authentication.configured && authentication.method === "forms" ? <button type="button" className="btn mobile-logout" onClick={() => { setMobileNavOpen(false); void onLogout(); }}><Icon name="logout" /> {t("Log out")}</button> : null}
          </Modal> : null}

          {setup.required && route.page === "settings" ? (
            <div className="banner banner-warn"><a href="#/setup">{t("Continue setup checks")}</a></div>
          ) : null}
          {blocked ? (
            <div className="banner banner-danger">
              <Icon name="alert" /> {t("Tankarr is paused: a deletion or organization step needs attention. Check the System page.")}
            </div>
          ) : null}
          {libraryDown ? (
            <div className="banner banner-danger">
              <Icon name="alert" /> {t("The library mount is unavailable")}
              {health && !health.library.available ? `: ${health.library.reason}` : ""}.
            </div>
          ) : null}

          <main className="content">
            <AppActionsContext.Provider value={actions}>
              <RouteErrorBoundary key={JSON.stringify(route)}>
                <Suspense fallback={<SpinnerPage />}>
                  <PageRouter route={route} finishSetup={setup.finish} />
                </Suspense>
              </RouteErrorBoundary>
            </AppActionsContext.Provider>
          </main>
        </div>

        <div className="toast-stack">
          {toasts.map((toast) => (
            <div key={toast.id} className={`toast toast-${toast.kind}`}>
              <Icon name={toast.kind === "success" ? "check" : toast.kind === "error" ? "alert" : "activity"} />
              <span>{toast.text}</span>
            </div>
          ))}
        </div>
      </div>
    </AppContext.Provider>
  );
}
