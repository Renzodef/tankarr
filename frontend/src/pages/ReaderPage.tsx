import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api";
import { Icon, Spinner, navigate } from "../components";
import type { ReaderBook } from "../types";

function pageUrl(id: string, index: number, version?: string): string {
  const path = `/api/reader/books/${encodeURIComponent(id)}/pages/${index}`;
  return version ? `${path}?v=${encodeURIComponent(version)}` : path;
}

function BackgroundPagePreload({ id, version, pageCount, page, ready }: {
  id: string; version?: string; pageCount: number; page: number; ready: boolean;
}) {
  const warmedPages = useRef<Set<number>>(new Set());
  const [visible, setVisible] = useState(() => document.visibilityState !== "hidden");

  useEffect(() => {
    const update = () => setVisible(document.visibilityState !== "hidden");
    document.addEventListener("visibilitychange", update);
    return () => document.removeEventListener("visibilitychange", update);
  }, []);

  useEffect(() => {
    if (!ready || !visible || !pageCount) return;
    const saveData = (navigator as Navigator & { connection?: { saveData?: boolean } }).connection?.saveData;
    if (saveData) return;
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      void (async () => {
        // Nearby pages use image preloads or the webtoon's eager images.
        // Warm the remaining pages in reading order without retaining their
        // decoded pixels or competing with the page currently being shown.
        for (let offset = 0; offset < pageCount; offset += 1) {
          const index = (page + 3 + offset) % pageCount;
          if (controller.signal.aborted) return;
          if (warmedPages.current.has(index)) continue;
          try {
            const response = await fetch(pageUrl(id, index, version), {
              signal: controller.signal,
              priority: "low",
            });
            if (!response.ok) return;
            await response.arrayBuffer();
            if (controller.signal.aborted) return;
            warmedPages.current.add(index);
          } catch {
            return;
          }
        }
      })();
    }, 1000);
    return () => { window.clearTimeout(timer); controller.abort(); };
  }, [id, version, page, pageCount, ready, visible]);

  return null;
}

export default function ReaderPage({ id, initialPage }: { id: string; initialPage: number | null }) {
  const [book, setBook] = useState<ReaderBook | null>(null);
  const [page, setPage] = useState(0);
  const [displayMode, setDisplayMode] = useState<"manga" | "webtoon">("manga");
  const [fitWidth, setFitWidth] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [savingBookmark, setSavingBookmark] = useState(false);
  const [compactControls, setCompactControls] = useState(() =>
    window.matchMedia?.("(max-width: 720px), (pointer: coarse)").matches ?? false);
  const [seekPage, setSeekPage] = useState<number | null>(null);
  const seeking = useRef(false);
  const pendingScrollPage = useRef<number | null>(null);
  const canvasRef = useRef<HTMLDivElement>(null);
  const preloadedPages = useRef<Map<number, HTMLImageElement>>(new Map());
  const [preloadReady, setPreloadReady] = useState(false);

  useEffect(() => {
    const query = window.matchMedia?.("(max-width: 720px), (pointer: coarse)");
    if (!query) return;
    const update = () => setCompactControls(query.matches);
    query.addEventListener("change", update);
    return () => query.removeEventListener("change", update);
  }, []);

  useEffect(() => {
    let active = true;
    preloadedPages.current.clear();
    setPreloadReady(false);
    setBook(null);
    setError(null);
    seeking.current = false;
    setSeekPage(null);
    void api.readerBook(id).then((next) => {
      if (!active) return;
      setBook(next);
      setDisplayMode(next.display_mode);
      const requested = initialPage ?? next.page_index;
      const bounded = Math.min(Math.max(requested, 0), next.page_count - 1);
      pendingScrollPage.current = bounded;
      setPage(bounded);
    }).catch((caught) => {
      if (active) setError(caught instanceof Error ? caught.message : String(caught));
    });
    return () => {
      active = false;
      preloadedPages.current.clear();
    };
  }, [id, initialPage]);

  const preloadNearbyPages = useCallback((loadedPage: number, pageCount: number, version?: string) => {
    const preloads = preloadedPages.current;
    for (const index of [loadedPage + 1, loadedPage + 2, loadedPage - 1]) {
      if (index < 0 || index >= pageCount || preloads.has(index)) continue;
      const image = new Image();
      image.fetchPriority = index === loadedPage + 1 ? "high" : "low";
      image.src = pageUrl(id, index, version);
      if (index === loadedPage + 1 && image.decode) void image.decode().catch(() => {});
      preloads.set(index, image);
    }
    for (const index of preloads.keys()) {
      if (index < loadedPage - 1 || index > loadedPage + 2) preloads.delete(index);
    }
  }, [id]);

  const goToPage = useCallback((target: number) => {
    if (!book) return;
    const bounded = Math.min(Math.max(target, 0), book.page_count - 1);
    if (displayMode === "manga" && bounded !== page) setPreloadReady(false);
    setPage(bounded);
    if (displayMode === "webtoon") {
      pendingScrollPage.current = bounded;
      canvasRef.current?.querySelector(`[data-reader-page="${bounded}"]`)?.scrollIntoView({ block: "start" });
    }
  }, [book, displayMode, page]);

  const move = useCallback((offset: number) => goToPage(page + offset), [goToPage, page]);

  useEffect(() => {
    if (displayMode !== "webtoon" || !book || !canvasRef.current) return;
    const root = canvasRef.current;
    let frame = 0;
    const updateVisiblePage = () => {
      frame = 0;
      if (pendingScrollPage.current !== null) return;
      const line = root.getBoundingClientRect().top + Math.min(root.clientHeight * 0.25, 160);
      const images = Array.from(root.querySelectorAll<HTMLElement>("[data-reader-page]"));
      const visible = images.find((node) => node.getBoundingClientRect().bottom > line) ?? images.at(-1);
      if (visible) setPage(Number(visible.dataset.readerPage));
    };
    const onScroll = () => {
      if (!frame) frame = window.requestAnimationFrame(updateVisiblePage);
    };
    const onInteract = () => { pendingScrollPage.current = null; onScroll(); };
    root.addEventListener("scroll", onScroll);
    root.addEventListener("wheel", onInteract, { passive: true });
    root.addEventListener("touchstart", onInteract, { passive: true });
    root.addEventListener("pointerdown", onInteract);
    // Lazy images have unknown heights. Keep the requested page aligned while
    // they load, until the reader explicitly starts scrolling.
    pendingScrollPage.current = page;
    const initial = window.setTimeout(() => {
      root.querySelector(`[data-reader-page="${page}"]`)?.scrollIntoView({ block: "start" });
    }, 0);
    return () => {
      window.clearTimeout(initial);
      window.cancelAnimationFrame(frame);
      root.removeEventListener("scroll", onScroll);
      root.removeEventListener("wheel", onInteract);
      root.removeEventListener("touchstart", onInteract);
      root.removeEventListener("pointerdown", onInteract);
    };
  // Reconnect only when the book or mode changes; scroll events own page updates.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [book?.release_id, displayMode]);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement;
      if (target.closest("input, select, textarea, button, a, [contenteditable=true]")) return;
      const nextKey = displayMode === "manga"
        ? book?.reading_direction === "ltr" ? "ArrowRight" : "ArrowLeft"
        : "ArrowDown";
      const previousKey = displayMode === "manga"
        ? book?.reading_direction === "ltr" ? "ArrowLeft" : "ArrowRight"
        : "ArrowUp";
      if (event.key === nextKey || event.key === "PageDown" || event.key === " ") {
        event.preventDefault();
        move(1);
      } else if (event.key === previousKey || event.key === "PageUp") {
        event.preventDefault();
        move(-1);
      } else if (event.key === "Home") {
        event.preventDefault();
        goToPage(0);
      } else if (event.key === "End" && book) {
        event.preventDefault();
        goToPage(book.page_count - 1);
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [book, displayMode, goToPage, move]);

  const toggleBookmark = async () => {
    if (!book || savingBookmark) return;
    setSavingBookmark(true);
    setError(null);
    try {
      const isCurrentBookmark = book.bookmarked && book.bookmark_page_index === page;
      if (isCurrentBookmark) {
        await api.clearReaderBookmark(book.manga_id);
        setBook({ ...book, bookmarked: false, bookmark_page_index: null });
      } else {
        await api.saveReaderBookmark(book.manga_id, id, page);
        setBook({ ...book, bookmarked: true, bookmark_page_index: page });
      }
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setSavingBookmark(false);
    }
  };

  if (error && !book) {
    return (
      <div className="reader-error">
        <h1>Unable to open this book</h1>
        <p>{error}</p>
        <button className="btn" type="button" onClick={() => window.history.back()}>Go back</button>
      </div>
    );
  }
  if (!book) return <div className="reader-loading"><Spinner /></div>;

  const currentBookmark = book.bookmarked && book.bookmark_page_index === page;
  const sliderPage = seekPage ?? page;
  const verticalSlider = displayMode === "webtoon" && !compactControls;
  const readingDirection = book.reading_direction ?? "rtl";
  const previousLabel = displayMode === "webtoon"
    ? "↑ Previous"
    : readingDirection === "ltr" ? "← Previous" : "Previous →";
  const nextLabel = displayMode === "webtoon"
    ? "Next ↓"
    : readingDirection === "ltr" ? "Next →" : "← Next";
  return (
    <section className={`native-reader ${displayMode}`} aria-label={`${book.series_title}, ${book.book_title}`}>
      <BackgroundPagePreload key={`${id}-${book.page_version ?? ""}`} id={id} version={book.page_version} pageCount={book.page_count} page={page} ready={preloadReady} />
      <header className="reader-toolbar">
        <a href={`#/series/${encodeURIComponent(book.manga_id)}`} className="reader-back" title="Back to series">
          <span aria-hidden="true">←</span>
          <span className="reader-title"><strong>{book.series_title}</strong><small>{book.book_title}</small></span>
        </a>
        <div className="reader-actions">
          <button className="reader-tool" type="button" onClick={() => setFitWidth((value) => !value)} aria-pressed={fitWidth}>
            {fitWidth ? "Fit page" : "Fit width"}
          </button>
          <button className="reader-tool" type="button" onClick={() => {
            setPreloadReady(false);
            setDisplayMode((value) => value === "manga" ? "webtoon" : "manga");
          }}>
            {displayMode === "manga" ? `Pages · ${readingDirection.toUpperCase()}` : "Webtoon · Vertical"}
          </button>
          <button className={`reader-tool${currentBookmark ? " active" : ""}`} type="button" onClick={() => void toggleBookmark()} disabled={savingBookmark} aria-pressed={currentBookmark}>
            <Icon name="bookmark" size={16} /> {savingBookmark ? "Saving…" : currentBookmark ? "Remove bookmark" : "Save bookmark"}
          </button>
          <span className="reader-bookmark-status" role="status">
            {book.bookmarked && book.bookmark_page_index !== null
              ? `Bookmark saved · ${book.book_title} · page ${book.bookmark_page_index + 1}`
              : "No bookmark in this book"}
          </span>
          <span className="reader-counter">{sliderPage + 1} / {book.page_count}</span>
        </div>
      </header>

      <div ref={canvasRef} className={`reader-canvas ${displayMode} ${readingDirection}${fitWidth ? " fit-width" : ""}`}>
        {displayMode === "manga" ? (
          <>
            <button className="reader-hit reader-hit-next" type="button" aria-label="Next page" disabled={page + 1 === book.page_count} onClick={() => move(1)} />
            <img
              key={`${id}-${book.page_version ?? ""}-${page}`}
              src={pageUrl(id, page, book.page_version)}
              alt={`Page ${page + 1} of ${book.page_count}`}
              fetchPriority="high"
              decoding="async"
              onLoad={() => { preloadNearbyPages(page, book.page_count, book.page_version); setPreloadReady(true); }}
              draggable={false}
            />
            <button className="reader-hit reader-hit-previous" type="button" aria-label="Previous page" disabled={page === 0} onClick={() => move(-1)} />
          </>
        ) : Array.from({ length: book.page_count }, (_, index) => (
          <img
            key={`${id}-${book.page_version ?? ""}-${index}`}
            data-reader-page={index}
            onLoad={() => {
              if (Math.abs(index - page) < 2) setPreloadReady(true);
              if (pendingScrollPage.current !== null) {
                canvasRef.current?.querySelector(`[data-reader-page="${pendingScrollPage.current}"]`)?.scrollIntoView({ block: "start" });
              }
            }}
            src={pageUrl(id, index, book.page_version)}
            alt={`Page ${index + 1} of ${book.page_count}`}
            loading={Math.abs(index - page) < 2 ? "eager" : "lazy"}
            fetchPriority={Math.abs(index - page) < 2 ? "high" : "low"}
            decoding="async"
            draggable={false}
          />
        ))}
      </div>

      <footer className="reader-footer">
        <button className="btn" type="button" disabled={page === 0} onClick={() => move(-1)}>{previousLabel}</button>
        <input
          aria-label="Current page"
          aria-orientation={verticalSlider ? "vertical" : "horizontal"}
          aria-valuetext={`Page ${sliderPage + 1} of ${book.page_count}`}
          dir={displayMode === "manga" ? readingDirection : "ltr"}
          style={{ writingMode: verticalSlider ? "vertical-lr" : "horizontal-tb" }}
          type="range"
          min={1}
          max={book.page_count}
          value={sliderPage + 1}
          onPointerDown={(event) => {
            if (!event.isPrimary || event.button !== 0) return;
            seeking.current = true;
            setSeekPage(page);
            event.currentTarget.setPointerCapture(event.pointerId);
          }}
          onChange={(event) => {
            const target = Number(event.target.value) - 1;
            if (seeking.current) setSeekPage(target);
            else goToPage(target);
          }}
          onPointerUp={(event) => {
            if (!seeking.current) return;
            seeking.current = false;
            goToPage(Number(event.currentTarget.value) - 1);
            setSeekPage(null);
          }}
          onLostPointerCapture={() => { seeking.current = false; setSeekPage(null); }}
          onPointerCancel={() => { seeking.current = false; setSeekPage(null); }}
        />
        {page + 1 === book.page_count && book.next_release_id ? (
          <button className="btn btn-primary" type="button" onClick={() => navigate(`/reader/${encodeURIComponent(book.next_release_id!)}`)}>{displayMode === "webtoon" ? "Next book ↓" : readingDirection === "ltr" ? "Next book →" : "← Next book"}</button>
        ) : (
          <button className="btn" type="button" disabled={page + 1 === book.page_count} onClick={() => move(1)}>{nextLabel}</button>
        )}
      </footer>
      {error ? <div className="reader-toast" role="alert">{error}</div> : null}
    </section>
  );
}
