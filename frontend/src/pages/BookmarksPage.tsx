import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "../api";
import { Cover, EmptyState, Icon, Spinner, formatDate, seriesPath } from "../components";
import { LoadError } from "../components/LoadError";
import type { ReaderBookmark } from "../types";
import { t, tn } from "../i18n";

type BookmarkSort = "updated" | "title";
type SortDirection = "asc" | "desc";

export default function BookmarksPage() {
  const [bookmarks, setBookmarks] = useState<ReaderBookmark[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [removing, setRemoving] = useState<string | null>(null);
  const [sort, setSort] = useState<BookmarkSort>("updated");
  const [sortDirection, setSortDirection] = useState<SortDirection>("desc");

  const sortedBookmarks = useMemo(() => [...(bookmarks ?? [])].sort((left, right) => {
    const comparison = sort === "updated"
      ? Date.parse(left.updated_at) - Date.parse(right.updated_at)
      : left.manga_title.localeCompare(right.manga_title, undefined, { sensitivity: "base", numeric: true });
    return (sortDirection === "asc" ? comparison : -comparison) ||
      left.manga_id.localeCompare(right.manga_id, undefined, { numeric: true });
  }), [bookmarks, sort, sortDirection]);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setBookmarks(await api.readerBookmarks());
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  const remove = async (mangaId: string) => {
    setRemoving(mangaId);
    setError(null);
    try {
      await api.clearReaderBookmark(mangaId);
      setBookmarks((current) => current?.filter((item) => item.manga_id !== mangaId) ?? null);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setRemoving(null);
    }
  };

  return (
    <div className="page bookmarks-page">
      <div className="toolbar">
        <div className="page-heading">
          <h1 className="page-title">{t("Bookmarks")}</h1>
          <span className="muted small">{t("Saved reading positions across your library")}</span>
        </div>
        <div className="toolbar-group">
          {bookmarks ? <span className="muted small">{tn(bookmarks.length, "{count} saved", "{count} saved")}</span> : null}
          <button type="button" className="btn" onClick={() => void load()} disabled={loading}>
            <Icon name="refresh" size={15} /> {t("Refresh")}
          </button>
        </div>
      </div>
      {error ? <LoadError message={error} retryLabel={t("Retry bookmarks")} retry={() => void load()} loading={loading} hasData={bookmarks !== null} /> : null}
      {bookmarks?.length ? (
        <div className="list-controls" aria-label={t("Sort bookmarks")}>
          <div className="list-control-fields">
            <select
              className="input"
              value={sort}
              aria-label={t("Sort bookmarks by")}
              onChange={(event) => {
                const nextSort = event.target.value as BookmarkSort;
                setSort(nextSort);
                setSortDirection(nextSort === "updated" ? "desc" : "asc");
              }}
            >
              <option value="updated">{t("Sort: Recently saved")}</option>
              <option value="title">{t("Sort: Series title")}</option>
            </select>
            <button
              type="button"
              className="btn sort-direction"
              onClick={() => setSortDirection((current) => current === "asc" ? "desc" : "asc")}
              aria-label={sortDirection === "asc" ? t("Sort descending") : t("Sort ascending")}
              title={sortDirection === "asc" ? t("Currently ascending; click to reverse") : t("Currently descending; click to reverse")}
            >
              <Icon name={sortDirection === "asc" ? "sortAscending" : "sortDescending"} />
              <span>{sortDirection === "asc" ? t("Ascending") : t("Descending")}</span>
            </button>
          </div>
        </div>
      ) : null}
      {bookmarks === null ? (loading ? <Spinner /> : null) : bookmarks.length === 0 ? (
        <EmptyState icon="bookmark" title={t("No bookmarks saved")} hint={t("Open a book and select Save bookmark to keep your place.")} />
      ) : (
        <div className="bookmark-grid">
          {sortedBookmarks.map((item) => (
            <article className="bookmark-card panel" key={item.manga_id}>
              <a href={seriesPath(item.manga_id)} className="bookmark-cover-link" aria-label={t("View {title}", { title: item.manga_title })}>
                <Cover url={item.manga_cover_url} title={item.manga_title} className="bookmark-cover" />
              </a>
              <div className="bookmark-details">
                <a className="bookmark-series-title" href={seriesPath(item.manga_id)}>{item.manga_title}</a>
                <span className="bookmark-position"><Icon name="bookmark" size={15} /> {item.label} {t("· page")} {item.page_index + 1}</span>
                <span className="muted small">{t("Saved")} {formatDate(item.updated_at)}</span>
                <div className="bookmark-actions">
                  <a className="btn btn-primary" href={item.url}><Icon name="library" size={15} /> {t("Continue reading")}</a>
                  <button type="button" className="btn btn-ghost" onClick={() => void remove(item.manga_id)} disabled={removing === item.manga_id} aria-label={t("Remove bookmark for {title}", { title: item.manga_title })}>
                    <Icon name="trash" size={15} /> {t("Remove")}
                  </button>
                </div>
              </div>
            </article>
          ))}
        </div>
      )}
    </div>
  );
}
