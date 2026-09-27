import { useEffect, useMemo, useState } from "react";
import { api } from "../api";
import type {
  Chapter,
  ChapterReleaseSearchResponse,
  TorrentRelease,
} from "../types";
import {
  Icon,
  Modal,
  Spinner,
  StatusPill,
  chapterLabel,
  formatDate,
  humanize,
  useApp,
} from "../components";

type SeriesTarget = {
  id: string;
  title: string;
  preferred_language: string;
};

export type ChapterSearchTarget = {
  key: string;
  chapter: string | null;
  volume: string | null;
};

function comparableNumber(value: string | null): string {
  if (!value) return "";
  const numeric = Number(value);
  return Number.isFinite(numeric) ? String(numeric) : value.trim().toLocaleLowerCase();
}

function rangeContains(hint: string | null, wanted: string | null): boolean {
  if (!hint || !wanted) return false;
  const target = Number(wanted);
  const match = hint.match(/^\s*(\d+(?:\.\d+)?)\s*[-–]\s*(\d+(?:\.\d+)?)\s*$/);
  if (Number.isFinite(target) && match) {
    return target >= Number(match[1]) && target <= Number(match[2]);
  }
  return comparableNumber(hint) === comparableNumber(wanted);
}

function torrentMatch(
  release: TorrentRelease,
  target: ChapterSearchTarget,
): "chapter" | "volume" | null {
  if (target.chapter && rangeContains(release.chapter, target.chapter)) return "chapter";
  if (target.volume && rangeContains(release.volume, target.volume)) return "volume";
  return null;
}

export function ChapterReleaseSearchModal({
  manga,
  target,
  onClose,
  onQueued,
}: {
  manga: SeriesTarget;
  target: ChapterSearchTarget;
  onClose: () => void;
  onQueued: () => Promise<void>;
}) {
  const { notify } = useApp();
  const [result, setResult] = useState<ChapterReleaseSearchResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [queuedIds, setQueuedIds] = useState<Set<string>>(new Set());

  useEffect(() => {
    let active = true;
    setLoading(true);
    setError(null);
    void api.searchChapterReleases(manga.id, target.chapter, target.volume)
      .then((response) => {
        if (active) setResult(response);
      })
      .catch((caught) => {
        if (active) setError(String(caught));
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [manga.id, target.chapter, target.volume]);

  const directReleases = useMemo(
    () =>
      [...(result?.direct_releases ?? [])].sort(
        (left, right) =>
          Number(right.downloaded) - Number(left.downloaded) ||
          (right.version ?? 1) - (left.version ?? 1) ||
          (right.pages ?? 0) - (left.pages ?? 0),
      ),
    [result],
  );
  const torrentReleases = useMemo(
    () =>
      [...(result?.torrent.results ?? [])].sort(
        (left, right) =>
          Number(Boolean(torrentMatch(right, target))) -
            Number(Boolean(torrentMatch(left, target))) ||
          right.match_score - left.match_score ||
          right.seeders - left.seeders,
      ),
    [result, target],
  );

  const slotDownloaded = directReleases.some((release) => release.downloaded);

  const queueDirect = async (release: Chapter, replace = false) => {
    if (
      replace &&
      !window.confirm(
        `Replace the file already in the library for ${chapterLabel(release.volume, release.chapter)} with this release from ${release.source_name || release.provider}? The current file is kept until the replacement is verified and imported.`,
      )
    ) {
      return;
    }
    setBusy(true);
    try {
      await api.download(release.id, replace);
      setQueuedIds((current) => new Set(current).add(release.id));
      notify(
        "success",
        `${chapterLabel(release.volume, release.chapter)} queued from ${release.provider}.`,
      );
      await onQueued();
    } catch (caught) {
      notify("error", String(caught));
    } finally {
      setBusy(false);
    }
  };

  const grabTorrent = async (release: TorrentRelease) => {
    setBusy(true);
    try {
      await api.grabTorrent(manga.id, release.provider, release.id);
      setQueuedIds((current) =>
        new Set(current).add(`${release.provider}:${release.id}`),
      );
      notify("success", `${release.title} added to qBittorrent.`);
      await onQueued();
    } catch (caught) {
      notify("error", String(caught));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      wide
      title={`Interactive Search · ${manga.title} · ${chapterLabel(target.volume, target.chapter)}`}
      onClose={onClose}
      footer={<button type="button" className="btn" onClick={onClose}>Close</button>}
    >
      <div className="search-scope manual-search-scope">
        <StatusPill kind="provider">
          Exact {target.volume && !target.chapter ? "volume" : "chapter"}
        </StatusPill>
        <span className="muted small">Direct providers and Prowlarr indexers</span>
      </div>
      {loading ? <Spinner /> : null}
      {error ? <div className="banner banner-warn"><Icon name="alert" /> {error}</div> : null}
      {!loading && result ? (
        <div className="manual-release-sections">
          <section>
            <h3>Direct providers</h3>
            <div className="source-search-summary">
              {result.direct_sources.map((source, index) => (
                <span
                  className="small"
                  key={`${source.provider}:${source.provider_manga_id ?? index}:${source.state}`}
                  title={source.error ?? source.reason}
                >
                  <StatusPill
                    kind={
                      source.state === "matched"
                        ? "success"
                        : source.state === "error"
                          ? "warn"
                          : "muted"
                    }
                  >
                    {source.label} · {humanize(source.state)}
                  </StatusPill>
                </span>
              ))}
            </div>
            {directReleases.length ? (
              <div className="manual-release-list">
                {directReleases.map((release) => (
                  <article className="manual-release-row" key={release.id}>
                    <div className="manual-release-identity">
                      <StatusPill kind="provider">{release.source_name || humanize(release.provider)}</StatusPill>
                    </div>
                    <div className="manual-release-details muted small">
                      {release.source_chapter &&
                      comparableNumber(release.source_chapter) !==
                        comparableNumber(release.chapter) ? (
                        <span>
                          Canonical {release.chapter} · source item {release.source_chapter}
                        </span>
                      ) : null}
                      {release.selection ? <div className="muted small" aria-label="Automatic selection">
                        {release.selection.selected ? "Automatic choice: " : "Not selected: "}{release.selection.reasons.join(" · ")}
                        {release.selection.next_retry_at ? ` · Retry after ${new Date(release.selection.next_retry_at * 1000).toLocaleTimeString()}` : ""}
                      </div> : null}
                      <span>v{release.version ?? 1}</span>
                      <span>{release.publish_at ? formatDate(release.publish_at) : "Unknown date"}</span>
                    </div>
                    <div className="manual-release-actions">
                        {release.source_url ? (
                          <a className="btn btn-ghost btn-small" href={release.source_url} target="_blank" rel="noreferrer">
                            <Icon name="external" size={13} /> Source
                          </a>
                        ) : null}
                        <button
                          type="button"
                          className={`btn btn-small ${slotDownloaded && !release.downloaded ? "" : "btn-primary"}`}
                          disabled={busy || release.downloaded || queuedIds.has(release.id) || Boolean(release.queue_status)}
                          onClick={() => void queueDirect(release, slotDownloaded && !release.downloaded)}
                          title={
                            slotDownloaded && !release.downloaded
                              ? "Delete the current file for this chapter and download this release instead"
                              : undefined
                          }
                        >
                          <Icon name={slotDownloaded && !release.downloaded ? "refresh" : "download"} size={13} />
                          {release.downloaded
                            ? "In library"
                            : release.queue_status || queuedIds.has(release.id)
                              ? "Queued"
                              : slotDownloaded
                                ? "Replace file"
                                : "Grab"}
                        </button>
                    </div>
                  </article>
                ))}
              </div>
            ) : <p className="muted small">No verified direct release exposes this chapter yet.</p>}
          </section>
          <section>
            <h3>Torrent indexers</h3>
            {result.torrent.errors.map((item) => (
              <div className="banner banner-warn" key={`${item.provider}:${item.error}`}>
                <Icon name="alert" /> {item.provider}: {item.error}
              </div>
            ))}
            {manga.preferred_language !== "en" ? (
              <p className="muted small">Torrent import currently requires English for OCR verification.</p>
            ) : torrentReleases.length ? (
              <div className="manual-release-list">
                {torrentReleases.slice(0, 25).map((release) => {
                  const match = torrentMatch(release, target);
                  const key = `${release.provider}:${release.id}`;
                  return (
                    <article className="manual-release-row" key={key}>
                      <div className="manual-release-identity manual-release-identity-stacked">
                        <StatusPill kind="provider">{release.provider_label}</StatusPill>
                        {release.source_url ? (
                          <a className="table-link manual-release-title" href={release.source_url} target="_blank" rel="noreferrer">
                            {release.title}
                          </a>
                        ) : <div className="manual-release-title">{release.title}</div>}
                        <div className="muted small">{release.size} · {release.seeders} seeds</div>
                      </div>
                      <div className="manual-release-details">
                        <StatusPill kind={match ? "success" : "muted"}>
                          {match === "chapter" ? "Chapter match" : match === "volume" ? "Volume match" : "Series result"}
                        </StatusPill>
                        <span className="muted small">{release.match_score}% match</span>
                      </div>
                      <div className="manual-release-actions">
                            <button
                              type="button"
                              className="btn btn-primary btn-small"
                              disabled={busy || Boolean(release.download) || queuedIds.has(key)}
                              onClick={() => void grabTorrent(release)}
                            >
                              <Icon name="download" size={13} />
                              {release.download || queuedIds.has(key) ? "Queued" : "Grab"}
                            </button>
                      </div>
                    </article>
                  );
                })}
              </div>
            ) : <p className="muted small">No torrent result found for “{result.torrent.query}”.</p>}
          </section>
        </div>
      ) : null}
    </Modal>
  );
}
